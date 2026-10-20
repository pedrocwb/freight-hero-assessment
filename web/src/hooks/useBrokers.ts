import { useCallback, useEffect, useState } from "react";

import { api, STANDARD_BROKER_ID, type BrokerView } from "../api";
import { useResource, type Resource } from "./useResource";

const STORAGE_KEY = "freight-sim.broker";

function stored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function store(brokerId: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, brokerId);
  } catch {
    // Private windows may refuse storage; the choice then lasts for the visit only.
  }
}

/** Every broker the API knows (card BRK), refreshed after the page's own writes. */
export function useBrokerList(): Resource<BrokerView[]> {
  return useResource("brokers", () => api.listBrokers(), ["brokers"]);
}

/**
 * The broker the onboarding screens work on, shared between them and kept across visits.
 * Falls back to the first broker that is not the standard one, then to the standard one.
 */
export function useBrokerChoice(brokers: readonly BrokerView[] | undefined): [string | null, (brokerId: string) => void] {
  const [chosen, setChosen] = useState<string | null>(stored);

  useEffect(() => {
    if (!brokers?.length) return;
    if (chosen && brokers.some((b) => b.broker_id === chosen)) return;
    const next = brokers.find((b) => b.broker_id !== STANDARD_BROKER_ID) ?? brokers[0]!;
    setChosen(next.broker_id);
  }, [brokers, chosen]);

  const choose = useCallback((brokerId: string) => {
    setChosen(brokerId);
    store(brokerId);
  }, []);

  return [chosen, choose];
}
