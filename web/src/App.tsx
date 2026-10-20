import clsx from "clsx";
import { useEffect, useState, type ReactNode } from "react";

import { langsmithProject, modelReady } from "./api";
import { ClockRail } from "./components/ClockRail";
import { Toasts } from "./components/ui";
import { href, parseHash, type Route, type ScreenId } from "./lib/route";
import { LoadInspector } from "./screens/LoadInspector";
import { Scenarios } from "./screens/Scenarios";
import { SopViewer } from "./screens/SopViewer";
import { SimProvider, useSim } from "./state/sim";
import { ToastProvider } from "./state/toasts";

const SCREENS: { id: ScreenId; label: string }[] = [
  { id: "scenarios", label: "Scenarios" },
  { id: "inspector", label: "Load inspector" },
  { id: "sops", label: "SOPs" },
];

function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

function StreamBadge() {
  const { stream } = useSim();
  const label = stream === "live" ? "Live" : stream === "polling" ? "Polling every 5 s" : "Connecting";
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

/** Whether the agent can run (live on OpenRouter), and where its traces go. */
function ModelBadge() {
  const { state } = useSim();
  if (!state) return null;
  const project = langsmithProject(state);
  return (
    <>
      {modelReady(state) ? (
        <span className="mode" title="Every run calls the models on OpenRouter and costs money">
          Model: <strong>live</strong>
        </span>
      ) : (
        <span className="mode mode--off" title="Set OPENROUTER_API_KEY and restart the API">
          Model: <strong>no API key</strong>
        </span>
      )}
      {project ? (
        <a href={project} target="_blank" rel="noreferrer">
          LangSmith project
        </a>
      ) : null}
    </>
  );
}

function LoadPicker({ screen }: { screen: ScreenId }) {
  const { loads, selectedLoadId, selectLoad } = useSim();
  return (
    <label className="load-picker">
      <span className="load-picker-label">Load</span>
      <select
        className="input"
        value={selectedLoadId ?? ""}
        onChange={(e) => {
          selectLoad(e.target.value || null);
          // A link's load and run no longer apply once another load is picked.
          if (screen === "inspector") window.location.hash = href("inspector");
        }}
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
  const { screen, params } = useRoute();
  const runs = sim.snapshot.data?.runs.length ?? 0;
  const badges: Partial<Record<ScreenId, ReactNode>> = {
    scenarios: sim.playing ? <span className="count count--live">playing</span> : null,
    inspector: runs ? <span className="count">{runs}</span> : null,
  };
  const current = SCREENS.find((s) => s.id === screen) ?? SCREENS[0]!;

  useEffect(() => {
    document.title = `${current.label}, load agent platform`;
  }, [current.label]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Load agent platform</span>
        </div>
        <LoadPicker screen={screen} />
        <div className="topbar-meta">
          <ModelBadge />
          <StreamBadge />
        </div>
      </header>

      <aside className="rail">
        <ClockRail />
        <nav className="nav" aria-label="Screens">
          <ul className="nav-list">
            {SCREENS.map((s) => (
              <li key={s.id}>
                <a
                  href={href(s.id)}
                  className={clsx("nav-link", s.id === screen && "nav-link--current")}
                  aria-current={s.id === screen ? "page" : undefined}
                >
                  <span>{s.label}</span>
                  {badges[s.id] ?? null}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </aside>

      <main className="main" id="main" tabIndex={-1}>
        {sim.stateError && !sim.state ? (
          <div className="banner banner--critical" role="alert">
            <strong>The API is not answering.</strong> {sim.stateError}. Start it on port 8000 (see
            web/README.md); this page retries every 5 seconds.
          </div>
        ) : null}
        {screen === "scenarios" && <Scenarios params={params} />}
        {screen === "inspector" && <LoadInspector params={params} />}
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
