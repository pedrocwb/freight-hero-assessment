import clsx from "clsx";
import { useEffect, useState } from "react";

import { modelReady } from "./api";
import { Toasts } from "./components/ui";
import { clockTime, zoneAbbreviation } from "./lib/time";
import { href, parseHash, type Route, type ScreenId } from "./lib/route";
import { Conversation } from "./screens/Conversation";
import { Scenarios } from "./screens/Scenarios";
import { SopViewer } from "./screens/SopViewer";
import { SimProvider, useSim } from "./state/sim";
import { ToastProvider } from "./state/toasts";

const SCREENS: { id: ScreenId; label: string }[] = [
  { id: "load", label: "Conversation" },
  { id: "scenarios", label: "Scenarios" },
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

/** The simulation's clock, in the load's local time; a dot when the live stream is down. */
function Clock() {
  const { state, timezone, stream } = useSim();
  const now = state?.now;
  return (
    <p className="clock" title={stream === "polling" ? "The live stream is down: the page refreshes every 5 s" : "Simulation time at the load's stop"}>
      <time dateTime={now}>{now ? clockTime(now, timezone) : "--:--"}</time>
      <span className="clock-zone">{now ? zoneAbbreviation(now, timezone) : ""}</span>
      {stream === "polling" ? <span className="clock-off" aria-label="Live updates are off" /> : null}
    </p>
  );
}

function Shell() {
  const sim = useSim();
  const { screen, params } = useRoute();
  const current = SCREENS.find((s) => s.id === screen) ?? SCREENS[0]!;

  useEffect(() => {
    document.title = `${current.label}, load agent`;
  }, [current.label]);

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <a className="brand" href={href("load")} aria-label="Load agent, conversation">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Load agent</span>
        </a>
        <nav className="nav" aria-label="Screens">
          {SCREENS.map((s) => (
            <a
              key={s.id}
              href={href(s.id)}
              className={clsx("nav-link", s.id === screen && "nav-link--current")}
              aria-current={s.id === screen ? "page" : undefined}
            >
              {s.label}
              {s.id === "scenarios" && sim.playing ? <span className="nav-dot" aria-label="playing" /> : null}
            </a>
          ))}
        </nav>
        <Clock />
      </header>

      <main className={clsx("main", screen === "load" && "main--chat")} id="main" tabIndex={-1}>
        {sim.stateError && !sim.state ? (
          <p className="banner banner--alert" role="alert">
            The API is not answering: {sim.stateError}. Start it with <code>uv run agent serve</code>; this page retries every 5 seconds.
          </p>
        ) : null}
        {sim.state && !modelReady(sim.state) ? (
          <p className="banner banner--warn" role="status">
            No model key: set <code>OPENROUTER_API_KEY</code> and restart the API, or every run escalates to a Hero.
          </p>
        ) : null}
        {screen === "load" && <Conversation params={params} />}
        {screen === "scenarios" && <Scenarios />}
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
