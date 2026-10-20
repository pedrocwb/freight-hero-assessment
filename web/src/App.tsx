import clsx from "clsx";
import { useEffect, useState, type ReactNode } from "react";

import { api } from "./api";
import { ClockRail } from "./components/ClockRail";
import { Toasts } from "./components/ui";
import { useResource } from "./hooks/useResource";
import { BrokerSlack } from "./screens/BrokerSlack";
import { ControlRoom } from "./screens/ControlRoom";
import { DriverPhone } from "./screens/DriverPhone";
import { EmailThread } from "./screens/EmailThread";
import { HeroConsole } from "./screens/HeroConsole";
import { Inspector } from "./screens/Inspector";
import { SopViewer } from "./screens/SopViewer";
import { SimProvider, useSim } from "./state/sim";
import { ToastProvider } from "./state/toasts";

type ScreenId =
  | "control"
  | "driver"
  | "email"
  | "slack"
  | "hero"
  | "inspector"
  | "sops";

interface ScreenDef {
  id: ScreenId;
  label: string;
  group: string;
}

const SCREENS: ScreenDef[] = [
  { id: "control", label: "Control room", group: "Simulation" },
  { id: "driver", label: "Driver phone", group: "Play a role" },
  { id: "email", label: "Main email thread", group: "Play a role" },
  { id: "slack", label: "Broker Slack", group: "Play a role" },
  { id: "hero", label: "Hero console", group: "Play a role" },
  { id: "inspector", label: "Agent inspector", group: "Look inside" },
  { id: "sops", label: "SOP viewer", group: "Look inside" },
];

function screenFromHash(): ScreenId {
  const id = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return SCREENS.some((s) => s.id === id) ? (id as ScreenId) : "control";
}

function useScreen(): [ScreenId, (id: ScreenId) => void] {
  const [screen, setScreen] = useState<ScreenId>(screenFromHash);
  useEffect(() => {
    const onHash = () => setScreen(screenFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return [screen, (id) => (window.location.hash = `/${id}`)];
}

function StreamBadge() {
  const { stream } = useSim();
  const label =
    stream === "live"
      ? "Live"
      : stream === "polling"
        ? "Polling every 5 s"
        : "Connecting";
  const title =
    stream === "polling"
      ? "The event stream is down, so panels refresh every 5 seconds"
      : "Panels refresh as the agent works";
  return (
    <span className={clsx("stream", `stream--${stream}`)} title={title}>
      <span className="stream-dot" aria-hidden="true" />
      {label}
    </span>
  );
}

function LoadPicker() {
  const { loads, selectedLoadId, selectLoad } = useSim();
  return (
    <label className="load-picker">
      <span className="load-picker-label">Load</span>
      <select
        className="input"
        value={selectedLoadId ?? ""}
        onChange={(e) => selectLoad(e.target.value || null)}
        disabled={!loads.length}
      >
        {!loads.length ? <option value="">No loads yet</option> : null}
        {loads.map((l) => (
          <option key={l.load_id} value={l.load_id}>
            {l.load_id}, {l.broker_name}
          </option>
        ))}
      </select>
    </label>
  );
}

function Shell() {
  const sim = useSim();
  const [screen, go] = useScreen();
  const openTasks = useResource("tasks:open", () => api.heroTasks("open"), ["tasks"]);

  const urgentOpen = openTasks.data?.filter((t) => t.urgent).length ?? 0;
  const badges: Partial<Record<ScreenId, ReactNode>> = {
    hero: openTasks.data?.length ? (
      <span className={clsx("count", urgentOpen && "count--urgent")}>
        {openTasks.data.length}
        <span className="visually-hidden"> open, {urgentOpen} urgent</span>
      </span>
    ) : null,
    inspector: sim.snapshot.data?.runs.length ? <span className="count">{sim.snapshot.data.runs.length}</span> : null,
  };

  const groups = [...new Set(SCREENS.map((s) => s.group))];
  const current = SCREENS.find((s) => s.id === screen) ?? SCREENS[0]!;

  useEffect(() => {
    document.title = `${current.label}, load agent simulator`;
  }, [current.label]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Load agent simulator</span>
        </div>
        <LoadPicker />
        <div className="topbar-meta">
          <span className="mode" title="How the agent's model answers">
            Model: <strong>{sim.state?.llm_mode ?? "n/a"}</strong>
          </span>
          <StreamBadge />
        </div>
      </header>

      <aside className="rail">
        <ClockRail />
        <nav className="nav" aria-label="Screens">
          {groups.map((group) => (
            <div className="nav-group" key={group}>
              <p className="nav-group-label">{group}</p>
              <ul className="nav-list">
                {SCREENS.filter((s) => s.group === group).map((s) => (
                  <li key={s.id}>
                    <a
                      href={`#/${s.id}`}
                      className={clsx("nav-link", s.id === screen && "nav-link--current")}
                      aria-current={s.id === screen ? "page" : undefined}
                    >
                      <span>{s.label}</span>
                      {badges[s.id] ?? null}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
      </aside>

      <main className="main" id="main" tabIndex={-1}>
        {sim.stateError && !sim.state ? (
          <div className="banner banner--critical" role="alert">
            <strong>The API is not answering.</strong> {sim.stateError}. Start it on port 8000 (see
            web/README.md); this page retries every 5 seconds.
          </div>
        ) : null}
        {screen === "control" && <ControlRoom go={go} />}
        {screen === "driver" && <DriverPhone />}
        {screen === "email" && <EmailThread />}
        {screen === "slack" && <BrokerSlack />}
        {screen === "hero" && <HeroConsole />}
        {screen === "inspector" && <Inspector />}
        {screen === "sops" && <SopViewer />}
      </main>
      <Toasts />
    </div>
  );
}

export function App() {
  return (
    <ToastProvider>
      <SimProvider>
        <Shell />
      </SimProvider>
    </ToastProvider>
  );
}
