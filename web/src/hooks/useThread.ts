import { useCallback, useEffect, useMemo, useState } from "react";

import { api, inboundMessage, type Contact, type ConversationChannel } from "../api";
import { ALL_TOPICS, invalidate } from "../lib/bus";
import { isConfirmed, mergeConversation, type PendingMessage, type ThreadEntry } from "../lib/conversation";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";
import { useResource } from "./useResource";

let localIds = 0;

/**
 * One channel of the selected load: the server's thread plus the messages the person just
 * wrote, and `send` to write one more as a contact of the load.
 */
export function useThread(channel: ConversationChannel) {
  const sim = useSim();
  const toasts = useToasts();
  const loadId = sim.selectedLoadId;
  const resource = useResource(
    loadId ? `conversation:${loadId}:${channel}` : null,
    () => api.conversation(loadId as string, channel),
    ["conversation"],
  );
  const [pending, setPending] = useState<(PendingMessage & { loadId: string })[]>([]);

  // Forget what the server now shows, so the list does not grow for the whole session.
  useEffect(() => {
    const server = resource.data;
    if (!server) return;
    setPending((list) => {
      const kept = list.filter((p) => p.state === "failed" || !isConfirmed(p, server));
      return kept.length === list.length ? list : kept;
    });
  }, [resource.data]);

  const entries: ThreadEntry[] = useMemo(
    () => mergeConversation(resource.data ?? [], pending.filter((p) => p.loadId === loadId)),
    [resource.data, pending, loadId],
  );

  const send = useCallback(
    async (sender: Contact, text: string): Promise<boolean> => {
      const body = text.trim();
      if (!loadId || !sim.state || !body) return false;
      const localId = `local-${++localIds}`;
      const at = sim.state.now;
      const update = (patch: Partial<PendingMessage>) =>
        setPending((list) => list.map((p) => (p.localId === localId ? { ...p, ...patch } : p)));
      setPending((list) => [...list, { localId, loadId, at, who: sender.id, role: sender.role, text: body, state: "sending" }]);
      try {
        const result = await api.sendMessage(
          inboundMessage({ loadId, channel, senderContactId: sender.id, body, at }),
          sim.latencySeconds,
        );
        if (!result.accepted) {
          update({ state: "failed", error: result.reason ?? "not accepted" });
          toasts.error(result.reason ?? "The message was not accepted.");
          return false;
        }
        update({ state: "sent" });
        if (result.duplicate) toasts.info("The API already had this message; it was not processed again.");
        return true;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        update({ state: "failed", error: message });
        toasts.error(message);
        return false;
      } finally {
        invalidate(...ALL_TOPICS);
      }
    },
    [loadId, channel, sim.state, sim.latencySeconds, toasts],
  );

  const discard = useCallback((key: string) => {
    setPending((list) => list.filter((p) => `p:${p.localId}` !== key));
  }, []);

  return { resource, entries, send, discard };
}
