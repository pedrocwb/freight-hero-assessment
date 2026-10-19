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

import { api, type Load, type LoadSnapshot, type LoadSummary, type SimState } from "../api";
import { useEventStream, type StreamEvent, type StreamStatus } from "../hooks/useEventStream";
import { useResource, type Resource } from "../hooks/useResource";
import { ALL_TOPICS, invalidate, subscribe } from "../lib/bus";
import { currentStop } from "../lib/load";
import { useToasts } from "./toasts";

export const DEFAULT_TIMEZONE = "America/Los_Angeles";

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
  tzFor: (loadId: string | null | undefined) => string;
  stream: StreamStatus;
  latencySeconds: number;
  setLatencySeconds: (seconds: number) => void;
  /** Run a write, show its error as a toast, then refresh every panel. */
  perform: <T>(work: () => Promise<T>) => Promise<T | undefined>;
  busy: boolean;
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

export function SimProvider({ children }: { children: ReactNode }) {
  const toasts = useToasts();
  const [state, setStateRaw] = useState<SimState | undefined>(undefined);
  const [stateError, setStateError] = useState<string | null>(null);
  const [selectedLoadId, setSelectedLoadId] = useState<string | null>(stored);
  const [latencySeconds, setLatencySeconds] = useState(60);
  const [pending, setPending] = useState(0);
  const zones = useRef(new Map<string, string>());

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
  // undone by a list fetched a moment before it existed.
  const selectedRef = useRef(selectedLoadId);
  selectedRef.current = selectedLoadId;
  useEffect(() => {
    if (!state) return;
    const ids = state.loads.map((l) => l.load_id);
    const selected = selectedRef.current;
    if (selected && ids.includes(selected)) return;
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

  const snapshot = useResource(
    selectedLoadId,
    () => api.getLoad(selectedLoadId as string),
    ["snapshot"],
  );
  const load = snapshot.data?.load.load_id === selectedLoadId ? snapshot.data?.load : undefined;

  if (load) {
    const stop = currentStop(load);
    if (stop) zones.current.set(load.load_id, stop.timezone);
  }
  const fallbackZone = state?.timezone || DEFAULT_TIMEZONE;
  const tzFor = useCallback(
    (loadId: string | null | undefined) => (loadId && zones.current.get(loadId)) || fallbackZone,
    [fallbackZone],
  );
  const timezone = (load && currentStop(load)?.timezone) || fallbackZone;

  const onEvent = useCallback(
    (event: StreamEvent) => {
      switch (event.type) {
        case "clock":
          setState(event.data);
          break;
        case "load":
          setStateRaw((s) => (s ? { ...s, loads: upsert(s.loads, event.data) } : s));
          invalidate("snapshot", "health");
          break;
        case "run":
          invalidate(...ALL_TOPICS);
          break;
        case "outbox":
          invalidate("conversation", "tms", "tasks", "snapshot", "health");
          break;
      }
    },
    [setState],
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
    tzFor,
    stream,
    latencySeconds,
    setLatencySeconds,
    perform,
    busy: pending > 0,
  };
  return <SimContext.Provider value={value}>{children}</SimContext.Provider>;
}

export function useSim(): SimContextValue {
  const value = useContext(SimContext);
  if (!value) throw new Error("useSim needs a SimProvider");
  return value;
}
