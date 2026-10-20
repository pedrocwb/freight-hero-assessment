import clsx from "clsx";

import { api, type DocumentStatus, type DocumentView } from "../api";
import { useResource } from "../hooks/useResource";
import { humanize } from "../lib/load";
import { formatStamp } from "../lib/time";
import { useSim } from "../state/sim";
import { Empty, InlineError, Panel, percent } from "./ui";

const STATUS: Record<DocumentStatus, { label: string; chip: string }> = {
  pending: { label: "Pending", chip: "chip--timer-pending" },
  processed: { label: "Processed", chip: "chip--pass" },
  needs_review: { label: "Needs review", chip: "chip--escalated" },
  failed: { label: "Failed", chip: "chip--fail" },
};

const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function show(value: unknown, timezone: string, now: string | undefined): string {
  if (value === null || value === undefined || value === "") return "none";
  if (typeof value === "string" && ISO_DATETIME.test(value) && !Number.isNaN(Date.parse(value))) return formatStamp(value, timezone, now);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * The selected load's documents as the pipeline left them (card DOC): the type it found,
 * the values it read and each check against the load. A run acts on a document only once
 * its `document_processed` event arrives.
 */
export function DocumentsPanel() {
  const { selectedLoadId, timezone, state } = useSim();
  const docs = useResource(
    selectedLoadId ? `documents:${selectedLoadId}` : null,
    () => api.loadDocuments(selectedLoadId as string),
    ["snapshot"],
  );
  const list = [...(docs.data ?? [])].sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at));

  return (
    <Panel title={`Documents${docs.data ? `, ${docs.data.length}` : ""}`} flush>
      {docs.error ? <InlineError message={docs.error} onRetry={() => void docs.reload()} /> : null}
      {docs.data && list.length === 0 ? (
        <Empty title="No document yet">
          Attach a file to a message in Free play. The next tick reads it: its type, its values
          and the checks against the load show here.
        </Empty>
      ) : null}
      <ul className="doc-list">
        {list.map((doc) => (
          <DocumentItem key={doc.document_id} doc={doc} timezone={timezone} now={state?.now} />
        ))}
      </ul>
    </Panel>
  );
}

function DocumentItem({ doc, timezone, now }: { doc: DocumentView; timezone: string; now: string | undefined }) {
  const status = STATUS[doc.status] ?? { label: humanize(doc.status), chip: "chip--neutral" };
  const failed = doc.checks.filter((c) => !c.ok).length;
  const extracted = Object.entries(doc.extracted ?? {});
  return (
    <li className={clsx("doc", doc.status === "needs_review" && "doc--review")}>
      <div className="ledger-head">
        <span className="ledger-kind">{doc.type_id ? humanize(doc.type_id) : "Unclassified"}</span>
        <span className={clsx("chip", status.chip)}>{status.label}</span>
      </div>
      <p className="doc-file">
        <span className="mono">{doc.filename}</span>
        <span className="muted">
          {" "}
          {doc.pages} page{doc.pages === 1 ? "" : "s"}
          {doc.confidence !== null ? `, ${percent(doc.confidence)} sure of the type` : ""}
        </span>
      </p>
      <p className="cell-sub">
        Received {formatStamp(doc.received_at, timezone, now)}
        {doc.processed_at ? `, read ${formatStamp(doc.processed_at, timezone, now)}` : ", not read yet"}
      </p>
      {doc.checks.length ? (
        <div className="table-wrap doc-checks">
        <table className="table table--compact">
          <caption className="visually-hidden">Checks of {doc.filename} against the load</caption>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Load says</th>
              <th scope="col">Document says</th>
              <th scope="col">
                <span className="visually-hidden">Result</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {doc.checks.map((c) => (
              <tr key={c.field} className={clsx(!c.ok && "row--fail")}>
                <td className="mono">{c.field}</td>
                <td>{c.expected ?? <span className="muted">none</span>}</td>
                <td>{c.found ?? <span className="muted">not found</span>}</td>
                <td>
                  <span className={clsx("chip", c.ok ? "chip--pass" : "chip--fail")}>{c.ok ? "OK" : "Mismatch"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
      {failed ? (
        <p className="violation">
          {failed} check{failed === 1 ? "" : "s"} failed: a Hero reviews the values before the load moves on.
        </p>
      ) : null}
      {extracted.length ? (
        <dl className="ledger-facts">
          {extracted.map(([name, value]) => (
            <div key={name}>
              <dt>{humanize(name)}</dt>
              <dd>{show(value, timezone, now)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <p className="run-id">
        <span className="mono">{doc.document_id}</span>
      </p>
    </li>
  );
}
