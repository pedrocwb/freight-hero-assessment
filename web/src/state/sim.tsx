import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  api,
  type Load,
  type LoadSnapshot,
  type LoadSummary,
  type ScenarioRun,
  type ScenarioStart,
  type SimState,
} from "../api";
import { useEventStream, type StreamEvent, type StreamStatus } from "../hooks/useEventStream";
import { useResource, type Resource } from "../hooks/useResource";
import { ALL_TOPICS, invalidate, subscribe } from "../lib/bus";
import { currentStop } from "../lib/load";
import { isPlaying, newerRun } from "../lib/scenarios";
import { useToasts } from "./toasts";

export const DEFAULT_TIMEZONE = "America/Los_Angeles";

/** While a scenario plays, its progress is read every second besides the stream's events. */
const SCENARIO_POLL_MS = 1000;

interface SimContextValue {
  state: SimState | undefined;
  stateError: string | null;
  setState: (state: SimState) => void;
  loads: LoadSummary[];
  selectedLoadId: string | null;
  selectLoad: (loadId: string | null) => void;
  /** Add a just-created load to the list and select it. */
  addLoad: (summary: LoadSummary) => void;
  snapshot: Resource<LoadSnapshot>;
  load: Load | undefined;
  /** The selected load's current stop timezone; the sim's zone when no load is selected. */
  timezone: string;
  stream: StreamStatus;
  latencySeconds: number;
  setLatencySeconds: (seconds: number) => void;
  /** Run a write, show its error as a toast, then refresh every panel. */
  perform: <T>(work: () => Promise<T>) => Promise<T | undefined>;
  busy: boolean;
  /** The scenario that plays now or played last; null before the first. */
  scenarioRun: ScenarioRun | null;
  /** A scenario plays: it owns the clock and the world until it ends. */
  playing: boolean;
  /** Start a scenario and select its load. */
  startScenario: (scenarioId: string) => Promise<ScenarioStart | undefined>;
}

const SimContext = createContext<SimContextValue | null>(null);

const STORAGE_KEY = "freight-sim.load";

function stored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function store(loadId: string | null): void {
  try {
    if (loadId) window.localStorage.setItem(STORAGE_KEY, loadId);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private windows may refuse storage; the selection then lasts for the session only.
  }
}

function upsert(loads: LoadSummary[], summary: LoadSummary): LoadSummary[] {
  const index = loads.findIndex((l) => l.load_id === summary.load_id);
  if (index < 0) return [...loads, summary];
  const copy = loads.slice();
  copy[index] = summary;
  return copy;
}

/** What changes as a scenario plays: its status and each step's. */
function progressKey(run: ScenarioRun | null): string {
  return run ? `${run.started_at}|${run.status}|${run.steps.map((s) => s.status).join(",")}` : "";
}

export function SimProvider({ children }: { children: ReactNode }) {
  const toasts = useToasts();
  const [state, setStateRaw] = useState<SimState | undefined>(undefined);
  const [stateError, setStateError] = useState<string | null>(null);
  const [selectedLoadId, setSelectedLoadId] = useState<string | null>(stored);
  const [latencySeconds, setLatencySeconds] = useState(60);
  const [pending, setPending] = useState(0);
  const [scenarioRun, setScenarioRun] = useState<ScenarioRun | null>(null);

  const setState = useCallback((next: SimState) => {
    setStateRaw(next);
    setStateError(null);
  }, []);

  const refreshState = useCallback(async () => {
    try {
      setState(await api.simState());
    } catch (error) {
      setStateError(error instanceof Error ? error.message : String(error));
    }
  }, [setState]);

  useEffect(() => {
    void refreshState();
    return subscribe(["state"], () => void refreshState());
  }, [refreshState]);

  const loads = useMemo(() => state?.loads ?? [], [state]);

  // Keep the selection on a load that exists: the first one after a reset or on first visit.
  // Checked when the list changes only, so a load selected right after its creation is not
  // undone by a list fetched a moment before it existed. A scenario's load is awaited: the
  // scenario resets the world and creates it while it plays.
  const selectedRef = useRef(selectedLoadId);
  selectedRef.current = selectedLoadId;
  const awaited = useRef<string | null>(null);
  useEffect(() => {
    if (!state) return;
    const ids = state.loads.map((l) => l.load_id);
    const selected = selectedRef.current;
    if (selected && ids.includes(selected)) {
      if (awaited.current === selected) awaited.current = null;
      return;
    }
    if (selected && awaited.current === selected) return;
    const next = ids[0] ?? null;
    setSelectedLoadId(next);
    store(next);
  }, [state]);

  const selectLoad = useCallback((loadId: string | null) => {
    setSelectedLoadId(loadId);
    store(loadId);
  }, []);

  const addLoad = useCallback(
    (summary: LoadSummary) => {
      setStateRaw((s) => (s ? { ...s, loads: upsert(s.loads, summary) } : s));
      selectLoad(summary.load_id);
    },
    [selectLoad],
  );

  // Fetched only once the load is in the list, so an awaited load is not a 404.
  const known = selectedLoadId !== null && loads.some((l) => l.load_id === selectedLoadId);
  const snapshot = useResource(known ? selectedLoadId : null, () => api.getLoad(selectedLoadId as string), ["snapshot"]);
  const load = snapshot.data?.load.load_id === selectedLoadId ? snapshot.data?.load : undefined;

  const timezone = (load && currentStop(load)?.timezone) || state?.timezone || DEFAULT_TIMEZONE;

  // The scenario run: from the stream, from a poll every second while it plays, and on demand.
  const runRef = useRef<ScenarioRun | null>(null);
  const acceptScenario = useCallback((incoming: ScenarioRun | null) => {
    const current = runRef.current;
    const next = newerRun(current, incoming);
    if (next === current) return;
    runRef.current = next;
    setScenarioRun(next);
    // A step moved: the world it plays in moved with it.
    if (progressKey(next) !== progressKey(current)) invalidate("state", "snapshot");
  }, []);
  const refreshScenario = useCallback(async () => {
    try {
      acceptScenario(await api.scenarioRun());
    } catch {
      // The Scenarios screen keeps the last report; the stream or the next poll catches up.
    }
  }, [acceptScenario]);
  useEffect(() => {
    void refreshScenario();
    return subscribe(["scenario"], () => void refreshScenario());
  }, [refreshScenario]);
  const playing = isPlaying(scenarioRun);
  useEffect(() => {
    if (!playing) return undefined;
    const timer = setInterval(() => void refreshScenario(), SCENARIO_POLL_MS);
    return () => clearInterval(timer);
  }, [playing, refreshScenario]);

  const onEvent = useCallback(
    (event: StreamEvent) => {
      switch (event.type) {
        case "clock":
          setState(event.data);
          break;
        case "load":
          setStateRaw((s) => (s ? { ...s, loads: upsert(s.loads, event.data) } : s));
          invalidate("snapshot");
          break;
        case "run":
        case "outbox":
          invalidate("snapshot");
          break;
        case "scenario":
          acceptScenario(event.data);
          break;
      }
    },
    [setState, acceptScenario],
  );
  const onPoll = useCallback(() => invalidate(...ALL_TOPICS), []);
  const stream = useEventStream(onEvent, onPoll);

  const perform = useCallback(
    async <T,>(work: () => Promise<T>): Promise<T | undefined> => {
      setPending((n) => n + 1);
      try {
        return await work();
      } catch (error) {
        toasts.error(error instanceof Error ? error.message : String(error));
        return undefined;
      } finally {
        setPending((n) => n - 1);
        invalidate(...ALL_TOPICS);
      }
    },
    [toasts],
  );

  const startScenario = useCallback(
    async (scenarioId: string) => {
      const started = await perform(() => api.runScenario(scenarioId));
      if (started) {
        awaited.current = started.load_id;
        selectLoad(started.load_id);
      }
      return started;
    },
    [perform, selectLoad],
  );

  const value: SimContextValue = {
    state,
    stateError,
    setState,
    loads,
    selectedLoadId,
    selectLoad,
    addLoad,
    snapshot,
    load,
    timezone,
    stream,
    latencySeconds,
    setLatencySeconds,
    perform,
    busy: pending > 0,
    scenarioRun,
    playing,
    startScenario,
  };
  return <SimContext.Provider value={value}>{children}</SimContext.Provider>;
}

export function useSim(): SimContextValue {
  const value = useContext(SimContext);
  if (!value) throw new Error("useSim needs a SimProvider");
  return value;
}
