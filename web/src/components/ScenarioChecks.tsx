import clsx from "clsx";

import type { ScenarioRun } from "../api";
import { tallyRun } from "../lib/scenarios";

const STATUS_LABEL: Record<ScenarioRun["status"], string> = {
  running: "Playing",
  passed: "Passed",
  failed: "Failed",
  error: "Stopped by an error",
};

/** A scenario run's checks, one line per step; a failed step opens to the checks that failed. */
export function ScenarioChecks({ run, title }: { run: ScenarioRun; title?: string }) {
  const tally = tallyRun(run);
  return (
    <section className={clsx("checks-card", `checks-card--${run.status}`)} aria-label={`Scenario ${run.scenario_id}`}>
      <p className="checks-head">
        <span className={clsx("status", `status--${run.status}`)}>{STATUS_LABEL[run.status]}</span>
        <span className="checks-title">{title ?? run.scenario_id}</span>
        <span className="checks-count">
          {tally.passed} of {run.steps.length} steps passed
        </span>
      </p>
      <ol className="checks-steps">
        {run.steps.map((step) => {
          const failed = step.checks.filter((c) => !c.passed);
          const mark = step.status === "passed" ? "✓" : step.status === "failed" ? "✕" : step.status === "running" ? "…" : "·";
          const line = (
            <>
              <span className={clsx("step-mark", `step-mark--${step.status}`)} aria-hidden="true">
                {mark}
              </span>
              <span className="step-time">{step.at}</span>
              <span className="step-note">{step.note}</span>
              <span className="visually-hidden">, {step.status}</span>
            </>
          );
          return (
            <li key={step.index}>
              {failed.length ? (
                <details>
                  <summary className="step-line">{line}</summary>
                  <ul className="step-failures">
                    {failed.map((c, i) => (
                      <li key={i}>
                        <code>{c.name}</code> {c.detail}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : (
                <p className="step-line">{line}</p>
              )}
            </li>
          );
        })}
      </ol>
      {run.error ? <p className="step-failures">{run.error}</p> : null}
    </section>
  );
}
