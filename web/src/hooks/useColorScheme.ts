import { useEffect, useState } from "react";

function darkQuery(): MediaQueryList | null {
  return typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
}

/** The page's theme, which follows the system's (styles.css). */
export function useColorScheme(): "light" | "dark" {
  const [dark, setDark] = useState(() => darkQuery()?.matches ?? false);
  useEffect(() => {
    const media = darkQuery();
    if (!media) return undefined;
    const onChange = (event: MediaQueryListEvent) => setDark(event.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return dark ? "dark" : "light";
}
