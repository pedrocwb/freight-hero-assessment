import { useEffect, useState } from "react";
import { parseHash } from "./lib/route";
import { BrokerWorkspace } from "./screens/BrokerWorkspace";

export function App() {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const changed = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", changed);
    document.title = "Load agent · Operações por broker";
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const broker =
    route.params.get("broker") ||
    (route.params.get("load")?.startsWith("aurora-") ? "aurora" : "northline");
  return <BrokerWorkspace key={broker} brokerId={broker} route={route} />;
}
