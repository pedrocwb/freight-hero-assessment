import { useCallback, useEffect, useRef, useState } from "react";

import { subscribe, type Topic } from "../lib/bus";

export interface Resource<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
}

/**
 * Data a panel reads from the API. `key` names what is fetched (null: nothing to fetch
 * yet); a new key drops the old data so one load's thread never shows under another's
 * name. The resource refetches when any of its topics is invalidated, and keeps showing
 * the last good data while it does, so live refreshes do not flash.
 */
export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  topics: readonly Topic[],
): Resource<T> {
  const [state, setState] = useState<{ data: T | undefined; error: string | null; loading: boolean }>(
    { data: undefined, error: null, loading: key !== null },
  );
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const sequence = useRef(0);

  const load = useCallback(async () => {
    if (key === null) return;
    const id = ++sequence.current;
    setState((s) => ({ ...s, loading: true }));
    try {
      const data = await fetcherRef.current();
      if (id === sequence.current) setState({ data, error: null, loading: false });
    } catch (error) {
      if (id === sequence.current) {
        const message = error instanceof Error ? error.message : String(error);
        setState((s) => ({ data: s.data, error: message, loading: false }));
      }
    }
  }, [key]);

  useEffect(() => {
    sequence.current += 1; // results of the previous key are discarded
    setState({ data: undefined, error: null, loading: key !== null });
    void load();
  }, [key, load]);

  const topicKey = topics.join(",");
  useEffect(() => {
    if (key === null || !topicKey) return undefined;
    return subscribe(topicKey.split(",") as Topic[], () => void load());
  }, [key, load, topicKey]);

  return { ...state, reload: load };
}
