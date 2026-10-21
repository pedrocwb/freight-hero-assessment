import { useEffect, useRef, useState } from "react";

import { api, type LoadSummary, type OutboxItem, type RunRecord, type ScenarioRun, type SimState } from "../api";

export type StreamEvent =
  | { type: "run"; data: RunRecord }
  | { type: "outbox"; data: OutboxItem }
  | { type: "clock"; data: SimState }
  | { type: "load"; data: LoadSummary }
  | { type: "scenario"; data: ScenarioRun };

export type StreamStatus = "connecting" | "live" | "polling";

const EVENT_TYPES = ["run", "outbox", "clock", "load", "scenario"] as const;
const POLL_MS = 5000;
const OPEN_TIMEOUT_MS = 4000; // no stream by then: start polling while it keeps trying
const REOPEN_MS = 15000; // after the browser gives up on a stream, try a new one

/** Parse one SSE message: one JSON object per `data:` line. */
function parseStreamData(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/**
 * Live updates from `GET /api/stream`. While the stream is down (never opened, dropped,
 * or closed by the browser), `onPoll` runs every 5 s so the panels still refresh; once the
 * stream is back, polling stops.
 */
export function useEventStream(
  onEvent: (event: StreamEvent) => void,
  onPoll: () => void,
): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const handlers = useRef({ onEvent, onPoll });
  handlers.current = { onEvent, onPoll };

  useEffect(() => {
    let source: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let openTimer: ReturnType<typeof setTimeout> | null = null;
    let reopenTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const startPolling = () => {
      setStatus("polling");
      if (poll === null) poll = setInterval(() => handlers.current.onPoll(), POLL_MS);
    };
    const stopPolling = () => {
      if (poll !== null) clearInterval(poll);
      poll = null;
    };

    const open = () => {
      if (disposed) return;
      if (typeof EventSource === "undefined") {
        startPolling();
        return;
      }
      source = new EventSource(api.streamUrl);
      openTimer = setTimeout(() => {
        if (source?.readyState !== EventSource.OPEN) startPolling();
      }, OPEN_TIMEOUT_MS);

      source.onopen = () => {
        stopPolling();
        setStatus("live");
        handlers.current.onPoll(); // catch up on whatever happened while disconnected
      };
      source.onerror = () => {
        startPolling();
        if (source?.readyState === EventSource.CLOSED) {
          source.close();
          source = null;
          reopenTimer = setTimeout(open, REOPEN_MS);
        }
      };
      for (const type of EVENT_TYPES) {
        source.addEventListener(type, (message) => {
          const data = parseStreamData((message as MessageEvent<string>).data);
          if (data && typeof data === "object") {
            handlers.current.onEvent({ type, data } as StreamEvent);
          }
        });
      }
    };

    open();
    return () => {
      disposed = true;
      source?.close();
      stopPolling();
      if (openTimer) clearTimeout(openTimer);
      if (reopenTimer) clearTimeout(reopenTimer);
    };
  }, []);

  return status;
}
