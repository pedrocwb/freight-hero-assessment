import clsx from "clsx";
import { useState } from "react";

import { api, type HeroTask, type TaskFilter } from "../api";
import { Empty, InlineError, Panel, ScreenHead, Tag } from "../components/ui";
import { useResource } from "../hooks/useResource";
import { formatStamp } from "../lib/time";
import { useSim } from "../state/sim";
import { useToasts } from "../state/toasts";

const FILTERS: { id: TaskFilter; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "done", label: "Done" },
  { id: "all", label: "All" },
];

/** Urgent first, then the oldest first: the order a Hero should work the queue. */
export function sortTasks(tasks: readonly HeroTask[]): HeroTask[] {
  return [...tasks].sort(
    (a, b) =>
      Number(a.status === "done") - Number(b.status === "done") ||
      Number(b.urgent) - Number(a.urgent) ||
      Date.parse(a.created_at) - Date.parse(b.created_at),
  );
}

export function HeroConsole() {
  const [filter, setFilter] = useState<TaskFilter>("open");
  const tasks = useResource(`tasks:${filter}`, () => api.heroTasks(filter), ["tasks"]);
  const list = sortTasks(tasks.data ?? []);

  return (
    <>
      <ScreenHead
        title="Hero console"
        lede="Tasks the agent handed to the Hero team, across every load. Completing one records your note; it does not message the agent yet."
      />
      <Panel
        title="Tasks"
        flush
        actions={
          <div className="filter" role="group" aria-label="Show tasks">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                className={clsx("filter-btn", filter === f.id && "filter-btn--on")}
                aria-pressed={filter === f.id}
                onClick={() => setFilter(f.id)}
              >
                {f.label}
              </button>
            ))}
          </div>
        }
      >
        {tasks.error ? <InlineError message={tasks.error} onRetry={() => void tasks.reload()} /> : null}
        {tasks.data && list.length === 0 ? (
          <Empty title={filter === "done" ? "No task done yet" : "No open task"}>
            {filter === "done"
              ? "Tasks you complete move here with your note."
              : "The agent creates a task when it cannot finish a procedure alone, for example a call to the driver after two unanswered ETA texts."}
          </Empty>
        ) : null}
        <ul className="tasks">
          {list.map((task) => (
            <TaskRow key={task.task_id} task={task} />
          ))}
        </ul>
      </Panel>
    </>
  );
}

function TaskRow({ task }: { task: HeroTask }) {
  const sim = useSim();
  const toasts = useToasts();
  const [completing, setCompleting] = useState(false);
  const [note, setNote] = useState("");
  const tz = sim.tzFor(task.load_id);
  const noteId = `note-${task.task_id}`;

  async function complete() {
    const done = await sim.perform(() => api.completeHeroTask(task.task_id, note.trim()));
    if (done) {
      toasts.info(`Task done: ${task.title}`);
      setCompleting(false);
      setNote("");
    }
  }

  return (
    <li className={clsx("task", task.urgent && task.status === "open" && "task--urgent", task.status === "done" && "task--done")}>
      <div className="task-main">
        <div className="task-head">
          {task.urgent ? <Tag tone="urgent">Urgent</Tag> : null}
          <h3 className="task-title">{task.title}</h3>
        </div>
        <p className="task-meta">
          Load <span className="mono">{task.load_id}</span>, created {formatStamp(task.created_at, tz, sim.state?.now)}
          {task.status === "done" && task.done_at ? `, done ${formatStamp(task.done_at, tz, sim.state?.now)}` : null}
        </p>
        <p className="task-desc">{task.description}</p>
        {task.status === "done" && task.note ? (
          <p className="task-note">
            <span className="task-note-label">Note</span> {task.note}
          </p>
        ) : null}
        {completing ? (
          <form
            className="task-complete"
            onSubmit={(e) => {
              e.preventDefault();
              void complete();
            }}
          >
            <label className="field" htmlFor={noteId}>
              <span className="field-label">What did you do?</span>
            </label>
            <textarea
              id={noteId}
              className="input"
              rows={2}
              value={note}
              autoFocus
              placeholder="e.g. Called the driver, ETA 9:40 at the shipper."
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="row row--end">
              <button type="button" className="btn" onClick={() => setCompleting(false)}>
                Cancel
              </button>
              <button type="submit" className="btn btn--primary" disabled={!note.trim() || sim.busy}>
                Mark done
              </button>
            </div>
          </form>
        ) : null}
      </div>
      {task.status === "open" && !completing ? (
        <div className="task-actions">
          <button type="button" className="btn" onClick={() => setCompleting(true)}>
            Complete
          </button>
        </div>
      ) : null}
      {task.status === "done" ? <span className="chip chip--obl-satisfied task-status">Done</span> : null}
    </li>
  );
}
