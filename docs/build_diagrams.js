// Builds docs/architecture.html: the system design with hand-drawn inline SVG figures.
//
//   node docs/build_diagrams.js <page-out.html> docs/architecture.html
//
// The first file gets the page fragment (head + body, for an HTML viewer that adds its own
// document skeleton); the second gets the full standalone document kept in the repository.
//
// Figures are drawn from the code on main. Parts that the design describes but this checkout
// does not contain yet are found on disk (see BUILT) and drawn dashed with a "not built yet"
// tag, so rebuilding after those parts land updates the figures without editing them.

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const exists = p => fs.existsSync(path.join(ROOT, p));
const hasModule = dir => {
  try {
    return fs.readdirSync(path.join(ROOT, dir)).some(f => f.endsWith('.py') && f !== '__init__.py');
  } catch {
    return false;
  }
};

const has = (p, needle) => {
  try {
    return fs.readFileSync(path.join(ROOT, p), 'utf8').includes(needle);
  } catch {
    return false;
  }
};
const migration = n => {
  try {
    return fs.readdirSync(path.join(ROOT, 'migrations')).some(f => f.startsWith(`${String(n).padStart(4, '0')}_`));
  } catch {
    return false;
  }
};

// What is in this checkout. Everything else in the figures is built, except the parts drawn
// dashed on purpose: the integration layer (mocked) and the production deployment (design).
// The wave 3 cards' files exist from the start as stubs, so their parts are found by the
// names each card must define (docs/decisions.md, "Wave 3 file ownership").
const BUILT = {
  api: hasModule('src/freight_agent/api'),
  bootstrap: exists('src/freight_agent/bootstrap.py'),
  postgres: hasModule('src/freight_agent/adapters/persistence/postgres'),
  web: exists('web/package.json'),
  compose: exists('docker-compose.yml'),
  suite: exists('src/freight_agent/evalkit/suite.py'), // k trials, pass^k, `agent eval run`
  brokers: migration(3), // W3-BRK: brokers and broker_versions
  loop: has('src/freight_agent/application/agent.py', 'def stage_from_transcript'), // W3-AGT
  onboarding: has('src/freight_agent/application/sop_ingestion.py', 'def publish_draft'), // W3-KNW
  search: has('src/freight_agent/adapters/knowledge/postgres.py', 'def knowledge_index'), // W3-KNW
  documents: has('src/freight_agent/application/documents.py', 'class DocumentPipeline'), // W3-DOC
  judge: has('src/freight_agent/evalkit/judge.py', 'class JudgeVerdict'), // W3-EVK
};
const NOT_BUILT = 'NOT BUILT YET';
const pending = key => !BUILT[key];
const planCls = (cls, key) => (pending(key) ? `${cls} plan` : cls);
const planTag = key => (pending(key) ? NOT_BUILT : undefined);

/* ---------------- drawing helpers ---------------- */

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const T = (x, y, s, cls = 's', anchor) =>
  `<text class="${cls}" x="${x}" y="${y}"${anchor ? ` text-anchor="${anchor}"` : ''}>${esc(s)}</text>`;
const R = (x, y, w, h, cls, rx = 4) =>
  `<rect class="${cls}" x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}"/>`;

// A box with a title and lines. A line is a string or {t, c} (c = text class). A line that
// starts with two spaces is indented. o.tc: title class; o.tag: small label top right;
// o.foot: mono line at the bottom; o.step: line height.
function box(x, y, w, h, cls, title, lines = [], o = {}) {
  const step = o.step || 16;
  let s = R(x, y, w, h, cls);
  if (title) s += T(x + 12, y + 20, title, o.tc || 't');
  lines.forEach((l, i) => {
    const c = typeof l === 'object' ? l.c : o.lc || 's';
    let t = typeof l === 'object' ? l.t : l;
    let dx = 12;
    if (t.startsWith('  ')) {
      dx = 22;
      t = t.trimStart();
    }
    if (t) s += T(x + dx, y + (title ? 38 : 20) + i * step, t, c);
  });
  if (o.tag) s += T(x + w - 10, y + (o.tagY || 16), o.tag, o.tagCls || 'tag', 'end');
  if (o.foot) s += T(x + 12, y + h - 10, o.foot, 'm');
  return s;
}

// Arrows: k = 'a' (neutral), 'c' (accent), 'd' (dashed neutral).
const KIND = { a: 'flow', c: 'flow c', d: 'flow d' };
const MARK = { a: 'a', c: 'c', d: 'a' };
const L = (n, k, x1, y1, x2, y2, o = {}) =>
  `<line class="${KIND[k]}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#${MARK[k]}${n})"${
    o.both ? ` marker-start="url(#${MARK[k]}${n})"` : ''
  }/>`;
const P = (n, k, d, o = {}) =>
  `${o.halo ? `<path class="halo" d="${d}"/>` : ''}<path class="${KIND[k]}" d="${d}" marker-end="url(#${MARK[k]}${n})"${
    o.both ? ` marker-start="url(#${MARK[k]}${n})"` : ''
  }/>`;
const AL = (x, y, s, anchor = 'middle') => T(x, y, s, 'al', anchor);
const NUM = (x, y, d) => `<circle class="numc" cx="${x}" cy="${y}" r="8"/>` + T(x, y + 3.5, d, 'num', 'middle');

const defs = n => `<defs>
<marker id="a${n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker>
<marker id="c${n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:var(--accent)"/></marker>
</defs>`;

function fig(n, W, H, aria, body, caption) {
  const minW = Math.round(W * 0.8);
  return `<figure id="fig${n}">
<div class="scroll"><svg viewBox="0 0 ${W} ${H}" style="min-width:${minW}px" role="img" aria-label="${esc(aria)}">${defs(n)}${body}</svg></div>
<figcaption><span class="fignum">Figure ${n}.</span> ${caption}</figcaption>
</figure>`;
}

/* ---------------- Figure 1: event flow end to end ---------------- */
function figFlow() {
  const n = 1;
  let s = '';
  s += T(16, 30, 'People and systems', 'h') + T(180, 30, 'Integration layer', 'h');
  s += T(390, 30, 'Ingress · code, no model', 'h') + T(674, 30, 'Queue', 'h');
  s += T(890, 30, 'Worker', 'h') + T(1100, 30, 'Delivery', 'h');

  const people = [
    ['Driver', 'SMS', 'both'],
    ['Dispatcher', 'email, main thread', 'both'],
    ['Broker staff', 'email cc · Slack', 'both'],
    ['Location feed', 'pings + delay_risk', 'pings'],
    ['TMS', 'notes', 'notes'],
    ['Hero team', 'tasks, Slack', 'tasks'],
  ];
  people.forEach(([t, sub, dir], i) => {
    const y = 50 + i * 60;
    const cy = y + 25;
    s += box(16, y, 116, 50, 'node', t, [sub]);
    if (dir === 'notes' || dir === 'tasks') s += L(n, 'a', 180, cy, 132, cy);
    else s += L(n, 'a', 132, cy, 180, cy, { both: dir === 'both' });
    s += AL(156, cy - 6, dir === 'both' ? 'in · out' : dir);
  });

  s += box(180, 50, 156, 350, 'node plan', 'Integration layer', [
    'existing services of',
    'the integrations team',
    '',
    'stable message_id',
    'contact_id → phone',
    '  or email address',
    'at-least-once delivery',
    'attachments by ref',
  ], { foot: 'mocked in this repo' });

  // ingress
  s += L(n, 'a', 336, 110, 390, 110) + AL(363, 103, 'message');
  s += L(n, 'a', 336, 300, 390, 300) + AL(363, 293, 'ping');
  s += box(390, 50, 204, 146, 'node', 'ingest_message', [
    'dedupe: event_id = message_id',
    'already in the log → duplicate,',
    '  nothing queued',
    'else append to the event log',
    '  and enqueue, one unit of work',
    'each attachment: its bytes in',
    '  the blob store, a pending doc',
  ], { tc: 'tc' });
  s += box(390, 210, 204, 190, 'node', 'ingest_ping', [
    'RiskTracker with hysteresis:',
    '  a higher band: 1 ping confirms',
    '  a lower band: 2 pings confirm',
    'each confirmed band → save',
    '  load.delay_risk',
    'into or out of HIGH → append',
    '  risk_changed and enqueue',
    'medium alone wakes nothing',
    'a repeated ping_id is ignored',
  ], { tc: 'tc' });
  s += L(n, 'a', 594, 110, 674, 110) + AL(634, 103, 'event');
  s += L(n, 'a', 594, 300, 674, 300) + AL(634, 293, 'risk_changed');

  // the queue: the element this figure hinges on
  s += box(674, 50, 160, 330, 'key', 'Per-load FIFO', [
    'one queue, in arrival',
    '  order, for every load',
    '',
    'claim_next: the oldest',
    '  event whose load has',
    '  nothing in flight',
    '',
    'so one run per load at',
    '  a time, in order',
    '',
    'a failed run releases',
    '  the event; 3 failures',
    '  → dead letter',
  ]);

  // the document pipeline: read in the tick, before the queue runs
  s += box(390, 420, 204, 146, planCls('node', 'documents'), 'process_pending', [
    'each pending document:',
    '  blob → pages (pypdfium2)',
    '  one extract call: type, fields',
    '  checks against the load',
    'clean POD closes its obligation',
    '→ one document_processed event',
  ], { tc: 'tc', tag: planTag('documents'), tagY: 138 });
  s += NUM(606, 438, '2');
  s += P(n, 'a', 'M594 470 H644 V340 H674');
  s += `<text class="al" x="657" y="405" text-anchor="middle" transform="rotate(-90 657 405)">doc:{id}</text>`;

  // worker
  s += L(n, 'a', 834, 220, 890, 220) + AL(862, 213, 'claims') + NUM(862, 236, '3');
  s += box(890, 150, 160, 140, 'node', 'process_next', [
    '1 claim + read context',
    '2 think: the run graph',
    '3 commit in one',
    '  unit of work',
    { t: 'Figure 2', c: 'm' },
  ], { tc: 'tc' });
  s += L(n, 'a', 1050, 220, 1100, 220) + AL(1075, 213, 'writes');
  s += box(1100, 150, 126, 140, 'store', 'Outbox', [
    'one row per action',
    'idempotency key',
    '  = event_id:index',
    'written with the',
    '  run’s state',
  ]);

  // memory and the timer loop
  s += R(674, 440, 376, 110, 'zone') + T(776, 458, 'Per-load memory', 'h');
  s += box(686, 470, 150, 66, 'store', 'Timers', ['pending until due_at']);
  s += box(852, 470, 186, 66, 'store', 'Event log, ledger', ['load, agent_action rows']);
  s += L(n, 'a', 754, 470, 754, 380) + T(762, 404, 'fires at due_at', 'al') + T(762, 418, '→ timer_due event', 'al');
  s += NUM(738, 410, '1');
  s += L(n, 'a', 930, 290, 930, 440, { both: true }) + T(938, 360, 'reads at claim', 'al') + T(938, 374, 'writes at commit', 'al');

  // delivery back through the integration layer
  s += P(n, 'a', 'M1163 290 V590 H258 V400');
  s += AL(700, 583, 'dispatch_outbox: gateway ports send by contact_id with the idempotency key; a DeliveryError is retried, failed after 5');
  s += NUM(1179, 440, '4');

  s += T(16, 622, 'One worker tick:', 's') + NUM(122, 618, '1') + T(134, 622, 'fire due timers', 's');
  s += NUM(242, 618, '2') + T(254, 622, 'read pending documents', 's');
  s += NUM(406, 618, '3') + T(418, 622, 'run the queue until nothing is claimable', 's');
  s += NUM(650, 618, '4') + T(662, 622, 'dispatch the outbox', 's');

  return fig(n, 1240, 640,
    'Inputs pass through the integration layer and code-only ingress into one per-load FIFO queue; attachments are read by the document pipeline at the start of a tick and come back as document_processed events; the worker claims one event per load, commits memory and outbox together, and delivers from the outbox; timers fire back into the queue',
    s,
    'Every input becomes an event on one per-load FIFO queue, and follow-up timers come back through the same queue as <code>timer_due</code> events. Pings never reach a model: a risk tracker per load applies hysteresis, and only a confirmed move into or out of high is logged and queued; a message is deduplicated by its <code>message_id</code> before it is queued. An attachment is stored as a blob and a pending document; each tick reads pending documents before it runs the queue, so their <code>document_processed</code> events queue behind the message that carried them (Figure 8). The worker claims the oldest event whose load has nothing in flight, so runs of one load never overlap, and messages leave from the outbox only after the run has committed.');
}

/* ---------------- Figure 2: one agent run ---------------- */
function figRun() {
  const n = 2;
  let s = '';
  const bracket = (x1, x2, label) =>
    `<path class="brk" d="M${x1} 50 V42 H${x2} V50"/>` + T((x1 + x2) / 2, 32, label, 'h', 'middle');
  s += bracket(16, 156, '1 · Claim');
  s += bracket(196, 1004, '2 · Think · no unit of work open · run deadline 240 s');
  s += bracket(1044, 1184, '3 · Commit');
  s += R(196, 58, 808, 400, 'zone');
  s += T(600, 444, 'Nothing in this phase holds a claim, a row lock or a transaction while a model thinks.', 's', 'middle');

  s += box(16, 70, 140, 176, 'node', 'Unit of work 1', [
    'queue.claim_next()',
    'load_run_context:',
    '  the load, its broker',
    '  (canary: by load id),',
    '  ledger, timers,',
    '  last 30 events up',
    '  to this one,',
    '  outbound (60 min)',
    'commit the claim',
  ]);

  const X = [208, 373, 538, 703, 868];
  const W = 124;
  s += box(X[0], 70, W, 176, 'node', 'prefilter', ['code checks only', 'run already exists', 'timer is stale', 'message is empty', 'sender unknown'], { tc: 'tc' });
  s += box(X[1], 70, W, 176, 'node', 'triage', [
    { t: 'model: messages', c: 'acc' },
    { t: 'with text only', c: 'acc' },
    'risk, timer, doc:',
    '  intent from code',
    'the broker’s',
    '  message intents',
    'must_act intents',
    '  always go on',
  ], { tc: 'tc' });
  s += box(X[2], 70, W, 176, 'node', 'knowledge', ['by_applies:', '  intent, stage,', '  broker, types', '+ procedures in', '  progress', 'catalog the rest', 'no model'], { tc: 'tc' });
  s += box(X[3], 70, W, 176, planCls('key', 'loop'), 'agent', [
    { t: 'model: one call,', c: 'acc' },
    { t: 'up to 8 rounds', c: 'acc' },
    'read: SOP search,',
    '  history, ledger,',
    '  load, documents',
    'act: each action',
    '  checked by rules',
    'finish → outcome',
  ], { tc: 'tc', tag: planTag('loop'), tagY: 170 });
  s += box(X[4], 70, W, 176, 'node', 'validate', ['the rules again,', '  staged + answer', '  actions merged', 'drops a violating', '  action, never', '  edits it', 'HIGH severity', '  → escalate'], { tc: 'tc' });

  s += L(n, 'a', 156, 158, X[0], 158) + AL(182, 151, 'context');
  ['passes', 'intent', 'SOP', 'plan'].forEach((l, i) => {
    s += L(n, 'a', X[i] + W, 158, X[i + 1], 158) + AL(X[i] + W + 20.5, 151, l);
  });
  s += L(n, 'a', X[4] + W, 158, 1044, 158) + AL(1018, 151, 'verdict');

  // exits
  const exits = [
    ['ends', 'skipped', ['already_processed', 'stale_timer', 'empty_message', { t: 'or escalated:', c: 't2' }, 'unknown_sender', '  → Hero task']],
    ['no act', 'noop', ['act = false and', '  intent not', '  must_act', 'e.g. “ok” with', '  nothing open', 'one model call']],
    null,
    ['budget, gap', 'escalated', ['no procedure and', '  nothing accepted,', '  > max_rejections,', '  a model failed', '  or 240 s passed', '→ Hero task']],
    ['outcome', 'status', ['acted: ≥ 1', '  approved action', 'noop: none left', 'escalated on a', '  HIGH violation', '  (+ Hero task)']],
  ];
  exits.forEach((exit, i) => {
    if (!exit) {
      s += box(X[i], 280, W, 126, 'ghost', 'no exit', ['a gap goes on to', '  the agent, which', '  searches the', '  broker’s SOP', '  first']);
      return;
    }
    const [lab, title, lines] = exit;
    const cx = X[i] + W / 2;
    s += L(n, 'a', cx, 246, cx, 280) + T(cx + 6, 267, lab, 'al');
    s += box(X[i], 280, W, 126, 'node', title, lines);
  });

  s += box(1044, 70, 140, 196, 'node', 'Unit of work 2', [
    'run already stored?',
    '  → skipped',
    'Executor.apply:',
    '  ledger, timers,',
    '  outbox items,',
    '  agent_action',
    '  events',
    'save the RunRecord',
    'queue.complete',
    'commit',
  ]);
  s += L(n, 'a', 1114, 266, 1114, 300) + T(1120, 287, 'then', 'al');
  s += box(1044, 300, 140, 140, 'node', 'After the commit', [
    'the tracer exports',
    '  the run (fail-open)',
    'dispatch_outbox',
    '  delivers later',
    'any other error:',
    '  event released;',
    '  dead letter after 3',
  ]);

  return fig(n, 1200, 470,
    'One run in three phases: a unit of work that claims the event and reads its context and broker version, the LangGraph nodes prefilter, triage, knowledge, agent and validate with no transaction open, and a second unit of work that executes and records; only triage (for messages) and the agent call a model',
    s,
    'A run claims its event and reads the load’s memory and its broker’s config version in one unit of work, runs the graph with no transaction open, and writes everything it decided in a second unit of work. Only two nodes can call a model: triage, for an inbound message with text (risk changes, timers and processed documents get their intent from code), and the agent, once per run: one call whose tool loop reads what it needs and stages actions, each checked by the rules at the call; the plan is recomputed from the loop’s transcript, so a replayed run stages the same actions. Every exit is recorded as a run: the prefilter’s skips and the unknown-sender escalation cost no model call, a triage no-op costs one, and a missing procedure, too many refused actions, a model failure or the 240 s deadline becomes one Hero task instead of a retry.');
}

/* ---------------- Figure 3: layers ---------------- */
function figLayers() {
  const n = 3;
  let s = '';
  s += box(40, 30, 290, 100, planCls('node', 'api'), 'api', [
    'FastAPI: ingest, simulation, brokers,',
    'SOPs, documents, Hero console,',
    'inspector, evals, SSE',
  ], { tc: 'tc', tag: planTag('api') });
  s += box(415, 30, 290, 100, planCls('key', 'bootstrap'), 'bootstrap', [
    'the composition root:',
    'settings → adapters → RuntimeDeps',
    '  → AgentRuntime',
  ], { tc: 'tc', tag: planTag('bootstrap') || 'COMPOSITION ROOT' });
  s += box(790, 30, 290, 100, 'node', 'evalkit', [
    'spec · runner · graders · judge · draft',
    'builds the same AgentRuntime',
    'with test adapters',
  ], { tc: 'tc' });

  s += box(40, 200, 480, 130, 'node', 'application', [
    'runtime.AgentRuntime: ingest_message, ingest_ping, tick,',
    '  process_next, dispatch_outbox, snapshot',
    'graph/: prefilter → triage → knowledge → agent → validate',
    'context · gate · agent · agent_tools · prompts · executor',
    'documents · sop_ingestion · escalation · metrics',
  ], { tc: 'tc', tag: 'LangGraph allowed' });
  s += box(600, 200, 480, 130, 'node', 'adapters', [
    'llm/: openrouter (LangChain) · resilient · cassette · scripted',
    `persistence/: memory store${pending('postgres') ? ' · postgres (not built yet)' : ' · postgres'}`,
    'knowledge/: memory and pgvector index · embedders',
    'sop/: files, lint, importer · documents/: blobs, pdfium',
    'observability/: langsmith, null · channels/fakes · clock',
  ], { tc: 'tc', tag: 'LangChain only in llm/' });
  s += box(200, 390, 720, 66, 'node', 'ports  (typing.Protocol)', [
    'UnitOfWork + repositories (brokers, SOP store, documents) · EventQueue · SopIndex · Embedder',
    'BlobStore · PageReader · DocumentProcessor · Gateways · LanguageModel · Clock · IdGenerator · Tracer',
  ], { tc: 'tc' });
  s += box(40, 500, 1040, 60, 'node', 'domain  (pure: pydantic models and rules, no I/O)', [
    'events · load · broker · obligations · timers · ping_filter · sop · documents · actions · validation · rules · intents · runs · outbox',
  ], { tc: 'tc' });

  // imports from the outer ring
  s += L(n, 'a', 185, 130, 185, 200) + T(191, 170, 'AgentRuntime calls', 'al');
  s += L(n, 'a', 330, 80, 415, 80) + AL(372, 73, 'runtime');
  s += L(n, 'c', 460, 130, 460, 200) + AL(454, 152, 'RuntimeDeps', 'end');
  s += L(n, 'c', 640, 130, 640, 200) + T(646, 152, 'constructs', 'al');
  s += L(n, 'a', 935, 130, 935, 200) + T(941, 152, 'test adapters', 'al');
  s += P(n, 'a', 'M820 130 V165 H510 V200', { halo: true }) + AL(575, 159, 'same AgentRuntime');

  // the forbidden edge
  s += `<line class="forbid" x1="524" y1="265" x2="596" y2="265"/>`;
  s += T(560, 270, '✕', 'x', 'middle') + AL(560, 252, 'no import') + AL(560, 288, 'either way');

  // inner imports
  s += L(n, 'a', 280, 330, 280, 390) + T(286, 364, 'calls Protocols', 'al');
  s += L(n, 'a', 840, 330, 840, 390) + T(846, 364, 'implements', 'al');
  s += L(n, 'a', 100, 330, 100, 500) + T(106, 440, 'models, rules', 'al');
  s += L(n, 'a', 1020, 330, 1020, 500) + AL(1014, 440, 'models', 'end');
  s += L(n, 'a', 560, 456, 560, 500) + T(566, 482, 'types', 'al');

  return fig(n, 1120, 580,
    'Layers and allowed imports: api, bootstrap and evalkit on the outside; application and adapters never import each other; both depend on ports, and everything depends on the pure domain',
    s,
    'Arrows are the imports the import-linter contracts in <code>pyproject.toml</code> allow; dependencies only point inward. The domain imports nothing of ours, ports import only the domain, and application and adapters both depend on ports and never on each other, so the composition root is the one place where real adapters are built and handed to the <code>AgentRuntime</code>; the eval runner does the same with test adapters. <code>uv run lint-imports</code> checks the contracts in CI, LangChain stays inside <code>adapters/llm</code>, and nothing in the core imports <code>evalkit</code>.');
}

/* ---------------- Figure 4: memory ---------------- */
function figMemory() {
  const n = 4;
  let s = '';
  s += T(16, 28, 'Writes (phase 3)', 'h') + T(290, 28, 'Stores, per load', 'h') + T(720, 28, 'Reads (phase 1)', 'h') + T(940, 28, 'What the model sees', 'h');

  s += box(16, 110, 220, 302, 'node', 'Executor, in the commit', [
    'send_sms, requests: <kind>:',
    '  open the obligation or',
    '  record an attempt',
    'open_obligation, or a stage',
    '  that requires a document:',
    '  open it, attempts 0',
    'schedule_timer:',
    '  supersede pending timers,',
    '  attach a new active timer',
    'defer_obligation:',
    '  deferred_until = message',
    '  time + minutes; move the',
    '  active timer past it',
    'close_obligation:',
    '  cancel pending timers',
    'each outbox item: append',
    '  an agent_action event',
  ]);

  s += box(290, 40, 360, 60, 'store', 'Load metadata, broker, risk tracker', [
    'stops, contacts, instructions (PO), stage; the catalog',
  ], { tag: 'REFERENCE' });
  s += box(290, 120, 360, 160, 'store', 'Event log', [
    { t: '07:00 risk_changed     low → high', c: 'mf' },
    { t: '07:01 agent_action out sms, eta_request', c: 'mf' },
    { t: '07:12 inbound_message  sms “What’s the PO…”', c: 'mf' },
    { t: '07:13 agent_action out sms, reply, PO 55821', c: 'mf' },
    { t: '07:31 timer_due        timer:timer_1', c: 'mf' },
    'agent_action: act:{event_id}:{index}; never queued;',
    '  document_processed: doc:{id}, read with tools',
  ], { tag: 'EPISODIC' });
  s += box(290, 300, 360, 120, 'key', 'Obligations ledger', [
    { t: 'obl_1  eta_request  open', c: 'mf' },
    { t: 'attempts 1    last_action_at 07:01', c: 'mf' },
    { t: 'deferred_until none  deferrals_used 0', c: 'mf' },
    { t: 'active_timer_id timer_1', c: 'mf' },
    'facts only: no pointer to a step of the SOP',
  ], { tag: 'PROSPECTIVE', tagCls: 'tag k' });
  s += box(290, 440, 360, 100, 'store', 'Timers', [
    { t: 'timer_1  due 07:31  pending', c: 'mf' },
    'pending → fired, superseded or cancelled',
    'not the active timer of an open obligation:',
    '  stale, and the prefilter skips its run',
  ]);

  s += L(n, 'a', 236, 200, 290, 200) + AL(263, 193, 'appends');
  s += L(n, 'a', 236, 340, 290, 340) + AL(263, 333, 'saves');
  s += P(n, 'a', 'M200 412 V490 H290') + T(206, 440, 'creates,', 'al') + T(206, 454, 'supersedes,', 'al') + T(206, 468, 'cancels', 'al');

  s += box(720, 40, 150, 500, 'node', 'load_run_context', ['once per run, at the', '  claim, in one read'], { tc: 'tc' });
  const reads = [
    [118, ['the load: metadata,', '  stage, risk tracker']],
    [184, ['last 30 events up to', '  and incl. this one;', '  later ones wait for', '  their own run']],
    [262, ['agent_action SMS,', '  email, Slack of the', '  last 60 min → the', '  rate limits']],
    [352, ['every obligation,', '  open and closed']],
    [420, ['a run for this event', '  already? → skipped']],
    [484, ['pending timers and', '  this event’s timer']],
  ];
  reads.forEach(([y, ls]) => {
    ls.forEach((l, i) => {
      const ind = l.startsWith('  ');
      s += T(ind ? 742 : 732, y + i * 16, l.trim(), 's');
    });
  });
  s += L(n, 'a', 650, 70, 720, 70) + AL(685, 63, 'load');
  s += L(n, 'a', 650, 200, 720, 200) + AL(685, 193, 'events');
  s += L(n, 'c', 650, 360, 720, 360) + AL(685, 353, 'obligations');
  s += L(n, 'a', 650, 490, 720, 490) + AL(685, 483, 'timers');
  s += L(n, 'a', 870, 290, 940, 290) + AL(905, 283, 'RunContext');

  const blocks = [
    ['## Load', 'load metadata'],
    ['## Contacts', 'the only ids it may address'],
    ['## SOP sections for this event', 'by_applies(intent, stage)'],
    ['## Procedure in progress', 'follow-up of each open obligation', true],
    ['## Other SOP sections', 'catalog; search_sop, read_sop_section'],
    ['## Ledger', 'attempts, deferral, active timer', true],
    ['## Pending timers', 'due times'],
    ['## Transcript', 'events; agent_action = “out”'],
    ['## Now', 'the clock'],
    ['## Event', 'this event (timer: “firing now”)'],
    ['## Triage', 'intent and reason'],
  ];
  const lines = [];
  blocks.forEach(([b, src, hot]) => {
    lines.push({ t: b, c: hot ? 'accm' : 'mf' });
    lines.push({ t: `  ← ${src}`, c: 's' });
  });
  s += box(940, 40, 244, 500, 'node', 'Agent prompt', [
    { t: 'application/prompts.py; the read', c: 'm' },
    { t: 'tools fetch more on demand', c: 'm' },
    ...lines,
  ], { step: 19.5 });

  return fig(n, 1200, 560,
    'Memory: the executor writes agent_action events, obligations and timers; load_run_context reads the load, its broker, the last events, the ledger and the timers once per run and renders them into the agent prompt; the agent reads more with tools',
    s,
    'The agent keeps nothing between runs: each run reads the load, its broker’s catalog, the event log, the obligations ledger and the timers once, at the claim, and renders them into the prompt; while it thinks it can read further back (<code>get_history</code>), the ledger, the load, a processed document and the SOP with read tools. Its own actions come back as <code>agent_action</code> events, shown in the transcript as “out” lines, and the ledger holds only facts (attempts, deferral, active timer) for every obligation kind, never a step of the SOP. At 07:13 the ledger row is still open with one attempt, so the prompt shows it next to the procedure its timer will run, marked as context only.');
}

/* ---------------- Figure 5: the deferral race (G1) ---------------- */
function figRace() {
  const n = 5;
  let s = '';
  const lanes = [
    [150, 'Driver', 'drv_20931 · SMS'],
    [330, 'Per-load queue', 'FIFO, load 481207'],
    [510, 'fire_due_timers', 'first step of a tick'],
    [710, 'Run A', 'the 07:30 message'],
    [910, 'Run B', 'event timer:timer_1'],
    [1110, 'Ledger + timers', 'obl_1, timer_1'],
  ];
  lanes.forEach(([x, t, sub]) => {
    s += `<line class="life" x1="${x}" y1="60" x2="${x}" y2="606"/>`;
    s += box(x - 75, 14, 150, 46, 'node', t, [sub]);
  });

  // time gutter
  s += T(16, 104, 'before', 'h') + T(16, 154, '07:30', 'h') + T(16, 204, '07:31', 'h');
  s += `<path class="brk" d="M30 214 V600"/>` + T(38, 600, 'one tick', 'al');

  // state before
  s += box(1035, 76, 150, 50, 'store', null, [
    { t: 'open · attempts 1', c: 'mf' },
    { t: 'active: timer_1 07:31', c: 'mf' },
  ]);

  // 07:30
  s += L(n, 'a', 150, 150, 330, 150) + AL(240, 142, '“I’ll tell you in 5 minutes”') + AL(240, 166, 'appended + queued');

  // 07:31 tick
  s += NUM(100, 200, '1');
  s += L(n, 'a', 510, 200, 1110, 200) + AL(810, 192, 'timer_1 is due: marked fired (no longer pending)');
  s += L(n, 'a', 510, 236, 330, 236) + AL(420, 228, 'timer_due queued') + AL(420, 252, 'behind the message');

  s += NUM(100, 290, '2');
  s += L(n, 'a', 330, 290, 704, 290) + AL(517, 282, 'claims the oldest event: the message');
  s += R(704, 290, 12, 112, 'act', 2);
  s += box(724, 300, 160, 54, 'node', null, ['triage → eta_deferred', 'agent → defer 5 min'], { lc: 's' });
  s += T(730, 368, '2 model calls', 'al');
  s += AL(913, 376, 'defer: until 07:30 + 5 min = 07:35');
  s += AL(913, 390, 'timer_2 due 07:35 + 2 min grace = 07:37');
  s += L(n, 'a', 716, 400, 1110, 400);
  s += T(722, 420, 'acted, committed', 'al');
  s += box(1026, 408, 166, 70, 'store', null, [
    { t: 'deferred_until 07:35', c: 'mf' },
    { t: 'active: timer_2', c: 'mf' },
    { t: 'timer_2 07:37 pending', c: 'mf' },
  ]);

  s += NUM(100, 450, '3');
  s += L(n, 'a', 330, 450, 904, 450) + AL(617, 442, 'claims timer:timer_1, next in FIFO, after Run A committed');
  s += R(904, 450, 12, 120, 'act', 2);
  s += L(n, 'd', 1110, 500, 916, 500) + AL(1013, 492, 'context: active timer_2');
  s += box(926, 516, 176, 72, 'key', 'skipped: stale_timer', [
    'is_stale(timer_1) → true',
    'no model call, no SMS',
  ]);

  return fig(n, 1200, 620,
    'Sequence of scenario G1: the 07:30 deferral and the 07:31 timer meet in one tick; the message run defers the obligation and replaces timer_1 by timer_2 at 07:37, then timer_1’s run is skipped as stale with no model call',
    s,
    'Scenario G1 (<code>evals/scenarios/core/g1_deferral_stale_timer.yaml</code>): the driver’s 07:30 deferral and the 07:31 follow-up meet in one tick. The tick fires <code>timer_1</code> first, which only marks it fired and queues its event behind the message; the message run then defers the obligation from the message time and replaces <code>timer_1</code> with <code>timer_2</code> at 07:37. Run B is claimed only after Run A commits, so its context already names <code>timer_2</code> as the active timer, and the prefilter skips it without a model call: no reminder goes out at 07:31.');
}

/* ---------------- Figure 6: SOP resolution ---------------- */
function figSop() {
  const n = 6;
  let s = '';
  s += T(16, 28, 'Layers of a published version', 'h') + T(340, 28, 'Merge per section id', 'h') + T(840, 28, 'Select for one event', 'h');

  // the seed files, published as versions
  s += R(16, 40, 296, 340, 'store');
  const tree = [
    ['sops/standard/', 'standard'],
    ['  acknowledgements.md', ''],
    ['  communication_rules.md', 'no intents'],
    ['  delay_risk_cleared.md', ''],
    ['  delay_risk_high.md', ''],
    ['  documents.md', ''],
    ['  driver_departed.md', ''],
    ['  driver_questions.md', ''],
    ['  escalation.md', 'no intents'],
    ['  eta_deferred.md', ''],
    ['  eta_received.md', ''],
    ['  status_updates.md', ''],
    ['sops/clients/northline/', 'client'],
    ['  delay_risk_high.md', 'replace'],
    ['sops/clients/keystone/', 'client'],
    ['  delay_risk_high.md', 'replace'],
    ['  types/reefer/', 'load type'],
    ['    driver_departed.md', 'extend'],
  ];
  tree.forEach(([f, note], i) => {
    const y = 62 + i * 17;
    const indent = f.length - f.trimStart().length;
    s += T(28 + indent * 4, y, f.trim(), indent ? 'm' : 'mf');
    if (note) s += T(300, y, note, ['replace', 'extend'].includes(note) ? 'acc' : 's', 'end');
  });
  s += T(28, 372, 'the seed: agent sop import publishes these', 's');

  s += L(n, 'a', 312, 60, 428, 60) + AL(370, 53, 'per broker');

  // merge matrix
  const rows = [['standard', 90], ['client', 172], ['client +', 254], ['effective', 336]];
  rows.forEach(([r, y]) => { s += T(340, y + 34, r, 'h'); });
  s += T(340, 254 + 48, 'load type', 'h');
  const cw = 160;
  const ca = 432;
  const cb = 608;
  s += T(ca + cw / 2, 62, 'delay_risk_high', 'mf', 'middle') + T(ca + cw / 2, 78, 'load of northline', 's', 'middle');
  s += T(cb + cw / 2, 62, 'driver_departed', 'mf', 'middle') + T(cb + cw / 2, 78, 'keystone reefer load', 's', 'middle');
  s += box(ca, 90, cw, 70, 'node', 'standard text', ['2 SMS, then a Hero', '  calls the driver']);
  s += box(ca, 172, cw, 70, 'key', 'replace', ['Northline text and', '  metadata win'], { tc: 'acc' });
  s += R(ca, 254, cw, 70, 'ghost') + T(ca + cw / 2, 294, '(nothing)', 'm', 'middle');
  s += box(ca, 336, cw, 70, 'node', 'Northline only', ['2 SMS, email the', '  dispatcher, Hero task']);
  s += box(cb, 90, cw, 70, 'node', 'standard text', ['update the stage,', '  write a TMS note']);
  s += R(cb, 172, cw, 70, 'ghost') + T(cb + cw / 2, 212, '(inherits)', 'm', 'middle');
  s += box(cb, 254, cw, 70, 'key', 'extend', ['+ ask the trailer', '  temperature and seal'], { tc: 'acc' });
  s += box(cb, 336, cw, 70, 'node', 'standard + reefer', ['body appended; title,', '  intents, stages kept']);
  [ca, cb].forEach(x => {
    s += L(n, 'a', x + cw / 2, 160, x + cw / 2, 172);
    s += L(n, 'a', x + cw / 2, 242, x + cw / 2, 254);
    s += L(n, 'a', x + cw / 2, 324, x + cw / 2, 336);
  });
  s += T(340, 436, 'Only the standard layer and the load’s own broker’s layers are visible:', 's');
  s += T(340, 452, 'another broker’s override can never reach this load, by rules or by search.', 's');

  // selection
  s += box(840, 40, 344, 52, 'node', null, [
    { t: 'timer_due on load 481207 (northline)', c: 't' },
    'intent eta_follow_up_due · stage to_pickup',
  ]);
  s += L(n, 'a', 1012, 92, 1012, 120) + T(1018, 110, 'intent, stage', 'al');
  s += P(n, 'a', 'M768 371 H804 V140 H840');
  s += `<text class="al" x="798" y="256" text-anchor="middle" transform="rotate(-90 798 256)">all merged sections</text>`;
  s += box(840, 120, 344, 72, 'node', 'by_applies(intent, stage)', [
    'applies when its intents include the intent (or are',
    'empty) and its stages include the stage (or are empty)',
  ], { tc: 'tc' });
  s += L(n, 'a', 1012, 192, 1012, 226) + T(1018, 213, 'split', 'al');
  s += R(840, 226, 344, 194, 'zone') + T(852, 244, 'into the agent prompt', 'h');
  s += box(852, 254, 320, 48, 'node', 'For this event · full text', ['communication_rules, escalation, delay_risk_high']);
  s += box(852, 308, 320, 48, 'node', 'Procedure in progress · context only', ['the section an open obligation’s timer will run']);
  s += box(852, 362, 320, 48, 'node', 'Catalog · title and description', ['the rest; full text through read_sop_section(id)']);

  s += P(n, 'a', 'M840 175 H822 V483 H840') + AL(816, 476, 'not enough', 'end');
  s += box(840, 446, 344, 74, planCls('key', 'search'), 'The agent searches: search_sop', [
    'pgvector cosine + full text, fused by rank, over the',
    '  broker’s visible sections; read_sop_section opens one',
    'nothing fits: finish(no_procedure) → Hero task',
  ], { tag: planTag('search') });

  return fig(n, 1200, 540,
    'SOP resolution: the layers of a broker’s published version (seeded from the standard, client and load-type folders) are merged per section id with replace or extend, then selected by intent and stage into full text, context and a catalog; when they are not enough the agent searches the broker’s sections, and with nothing found the run goes to a Hero',
    s,
    'Each section is keyed by its <code>id</code>; the <code>sops/</code> folder is the seed, one section per Markdown file, and a broker’s uploaded SOP adds its own layer (Figure 7). For one load only the standard layer and that broker’s layers are visible, merged per id from the least to the most specific: <code>replace</code> swaps the text and the metadata, <code>extend</code> appends text and keeps the metadata. The merged sections that match the event’s intent and the load’s stage go into the prompt in full, the others as a catalog. When they do not cover the event the agent searches (hybrid retrieval in Postgres, word overlap in memory) and reads what it finds; if nothing fits it finishes with <code>no_procedure</code> and the run escalates instead of improvising.');
}

/* ---------------- Figure 7: onboarding a broker without code ---------------- */
function figOnboard() {
  const n = 7;
  let s = '';
  s += T(16, 28, 'Ingestion · application/sop_ingestion.py', 'h') + T(592, 28, 'Review and publish · SOP Studio or the API', 'h');

  const X = [16, 208, 400, 592, 784, 976];
  const W = 176;
  const onboarding = cls => planCls(cls, 'onboarding');
  s += box(X[0], 50, W, 150, 'node', 'Upload', [
    { t: 'POST …/{id}/sops', c: 'm' },
    'the broker’s PDF, in',
    '  its own layout',
    'bytes → BlobStore;',
    '  a SopDocument',
  ]);
  s += box(X[1], 50, W, 150, 'node', 'Pages', [
    { t: 'PageReader', c: 'm' },
    'the text of each page',
    'a thin page (a scan)',
    '  goes as a PNG',
  ]);
  s += box(X[2], 50, W, 150, onboarding('key'), 'Compile', [
    { t: 'compile role, one call', c: 'acc' },
    'sections: title, a one-',
    '  line description, the',
    '  body as written',
    'proposed: applies,',
    '  intents, document',
    '  requirements',
  ], { tag: planTag('onboarding') });
  s += box(X[3], 50, W, 150, 'node', 'Review', [
    { t: 'GET / PUT …/draft', c: 'm' },
    'the editor accepts,',
    '  rejects or edits',
    '  each proposal',
    'the draft shows what',
    '  a publish refuses',
  ]);
  s += box(X[4], 50, W, 150, onboarding('key'), 'Publish', [
    { t: 'POST …/publish', c: 'm' },
    'lint: every applies',
    '  valid, every intent',
    '  has a section',
    'embed each section',
    'hash → SopVersion,',
    '  immutable',
  ]);
  s += box(X[5], 50, W, 150, 'store', 'Broker, version n + 1', [
    'sop_version = hash',
    'config_version + 1',
    'accepted intents,',
    '  requirements,',
    '  obligations',
    'rollout: percent,',
    '  previous_version',
  ]);
  for (let i = 0; i < 5; i++) s += L(n, 'a', X[i] + W, 125, X[i + 1], 125);

  // the stores
  s += box(16, 250, 744, 96, 'store', 'SopStore · on the unit of work', [
    'uploads (SopDocument), the broker’s one draft, the published versions (immutable, keyed by content hash)',
    'Postgres: sop_documents, sop_drafts, sop_versions, sop_sections (embedding public.vector(1536), tsvector)',
  ]);
  s += box(784, 250, 368, 96, planCls('store', 'brokers'), 'BrokerRepository', [
    'the current broker and every version,',
    '  whole (jsonb): brokers, broker_versions',
    'a broker never saved: the standard catalog',
  ], { tag: planTag('brokers') });
  [[104, 'file'], [488, 'draft'], [680, 'draft']].forEach(([x, l]) => {
    s += L(n, 'a', x, 200, x, 250) + T(x + 6, 230, l, 'al');
  });
  s += P(n, 'a', 'M840 200 V226 H740 V250') + AL(790, 220, 'version');
  s += L(n, 'a', 1064, 200, 1064, 250) + T(1070, 230, 'saves', 'al');

  // what the runs read
  s += box(16, 400, 420, 110, planCls('node', 'search'), 'SopIndex · the think phase', [
    'by_applies: the selection by rules (Figure 6)',
    'search: pgvector cosine and full-text rank,',
    '  fused by reciprocal rank · read · version',
    'only the sections this broker sees',
  ], { tc: 'tc', tag: planTag('search') });
  s += box(452, 400, 320, 110, planCls('key', 'brokers'), 'choose_version', [
    '(current, previous, load_id): the sha256',
    '  of load_id in the first percent buckets',
    '  → current, else previous',
    'sticky per load · percent 0 = rollback',
  ], { tc: 'tc' });
  s += box(788, 400, 364, 110, 'node', 'A run of this broker’s load', [
    'follows the chosen catalog and the sections',
    '  of its version',
    'records broker_id, broker_config_version',
    '  and sop_version; the inspector shows them',
  ]);
  s += L(n, 'a', 226, 346, 226, 400) + T(232, 376, 'published sections, embedded', 'al');
  s += P(n, 'a', 'M900 346 V373 H612 V400') + AL(760, 367, 'current + previous version');
  s += L(n, 'a', 772, 455, 788, 455);
  s += P(n, 'a', 'M226 510 V532 H970 V510') + AL(598, 526, 'the sections it reads while it thinks');

  return fig(n, 1200, 548,
    'Onboarding a broker: the broker’s own PDF is uploaded, read page by page and compiled into a draft with proposed sections, intents and document requirements; the editor reviews it; publishing lints, embeds and hashes it into an immutable version and saves the broker as a new config version with a rollout; each load runs on the version chosen by a hash of its id',
    s,
    'A broker is onboarded without code. Its procedure is uploaded as it is, a PDF in its own layout; its pages are read (a scanned page goes to the model as an image) and the <code>compile</code> role turns it into a draft: sections whose body is the broker’s own text, with the intents and stages they apply to, plus proposed new intents and document requirements. A person reviews the draft. Publishing refuses a section that names an unknown intent or stage and an intent with no section, embeds the sections, stores an immutable version keyed by its hash, and saves the broker as a new config version with a rollout: <code>choose_version</code> sends a fixed share of the broker’s loads to it, by a hash of the load id, and <code>percent: 0</code> sends every load back.');
}

/* ---------------- Figure 8: documents ---------------- */
function figDocs() {
  const n = 8;
  let s = '';
  s += T(16, 28, 'Message', 'h') + T(200, 28, 'Ingest · one unit of work', 'h') + T(460, 28, 'Tick, before the queue', 'h');
  s += T(800, 28, 'Status', 'h') + T(1000, 28, 'The agent', 'h');

  s += box(16, 50, 150, 70, 'node', 'Driver', ['a POD as a PDF', '  or a phone photo']);
  s += L(n, 'a', 166, 85, 200, 85);
  s += box(200, 50, 220, 146, 'node', 'ingest_message', [
    'the message event,',
    '  appended and queued',
    'one pending Document',
    '  per attachment',
    'bytes → BlobStore',
    '  (a sha256 reference)',
  ], { tc: 'tc' });
  s += L(n, 'a', 420, 100, 460, 100);

  s += box(460, 50, 300, 262, planCls('key', 'documents'), 'DocumentPipeline', [
    '1  blob → PageReader (pypdfium2): text per',
    '   page; a thin page → a PNG',
    '2  one extract call: the type and its fields,',
    '   on the broker’s DocumentTypeDef schemas,',
    '   page images included',
    '3  checks in code (FieldDef.check):',
    '   po_number, mc_number, stop_name,',
    '   date_in_window, seal_number, signature',
    '4  one unit of work: save the document; a',
    '   clean one closes its requirement’s',
    '   obligation (satisfied); append and',
    '   enqueue doc:{document_id}, once',
  ], { tc: 'tc', tag: planTag('documents'), tagY: 254 });

  const statuses = [
    [50, 64, 'processed', ['every check passed']],
    [124, 82, 'needs_review', ['a check failed, a field', '  missing, low confidence']],
    [216, 64, 'failed', ['unreadable or a model', '  error']],
  ];
  statuses.forEach(([y, h, title, lines]) => {
    s += box(800, y, 170, h, title === 'processed' ? 'store' : 'node', title, lines, { tc: 'tc' });
    s += L(n, 'a', 760, y + 26, 800, y + 26);
    s += L(n, 'a', 970, y + 26, 1000, y + 26);
  });

  s += box(1000, 50, 184, 262, 'node', 'The agent’s run', [
    { t: 'document_processed', c: 'mf' },
    'intent from code',
    'read_document: the',
    '  type, values, checks',
    'clean POD: update_stage',
    '  delivered, TMS note',
    'needs_review: a Hero',
    '  task with the values',
    '',
    'the message’s own run',
    '  never acts on the',
    '  document, so it is',
    '  handled once',
  ]);

  s += box(16, 340, 560, 76, 'node', 'Documents the load owes', [
    'DocumentRequirement {type_id: pod, at_stage: at_delivery, within_minutes: 120}: entering',
    '  the stage opens the pod obligation and its timer; pod_follow_up_due asks for the POD',
    'a clean POD closes it as satisfied; needs_review leaves it open',
  ]);
  s += box(600, 340, 584, 76, 'node', 'Fixtures and checks', [
    'evals/fixtures/docs: pod_clean, pod_exceptions (the wrong PO), pod_scanned (no text',
    '  layer: the vision path), bol; the expected values in *.expected.json',
    'the docs suite: p1 clean POD, p2 exceptions to review, p3 overdue POD',
  ]);

  return fig(n, 1200, 432,
    'Documents: an attachment is stored as a blob and a pending document in the message’s unit of work; at the start of the next tick the pipeline reads its pages, classifies and extracts it in one extract call, checks the values against the load in code and saves it as processed, needs_review or failed with one document_processed event; that run’s agent reads the document and acts',
    s,
    'An attachment is read before the agent sees it. The message’s unit of work stores the bytes (by their sha256) and a pending document; the next tick reads pending documents before it runs the queue. One <code>extract</code> call classifies the document and fills its type’s fields from the broker’s catalog, with page images when the text is too thin; code checks the values against the load. A clean document of a required type closes its obligation, and exactly one <code>document_processed</code> event is queued behind the message. Only that run acts on the document: it reads the values and their checks with <code>read_document</code> and follows the SOP.');
}

/* ---------------- Figure 9: eval harness ---------------- */
function figEvals() {
  const n = 9;
  let s = '';
  s += T(16, 28, 'Scenario', 'h') + T(300, 28, 'One trial', 'h') + T(820, 28, 'Grading', 'h');

  const yaml = [
    'load: fixtures/load_481207.json',
    'date: 2026-10-13',
    'start: "06:55"',
    'steps:',
    '- at: "07:12"',
    '  message: {channel: sms,',
    '    sender: drv_20931,',
    '    body: "What\'s the PO number?"}',
    '  expect:',
    '    actions: [{kind: sms,',
    '      to: drv_20931,',
    '      contains: ["55821"]}]',
    '    obligations: {eta_request:',
    '      {status: open, attempts: 1}}',
    '    timers: {pending: 1,',
    '      due_at: "07:31"}',
    '    llm_calls: 2',
    '  forbid: [{kind: sms,',
    '    purpose: eta_request}]',
    '  script:  # scripted mode only',
    '    gate: [{intent: question}]',
    '    agent: [{actions: [...]}]',
  ];
  s += R(16, 40, 250, 470, 'node');
  s += T(28, 60, 'Scenario YAML', 't') + T(28, 78, 'evals/scenarios/<suite>/*.yaml', 'm');
  yaml.forEach((l, i) => {
    const ind = l.length - l.trimStart().length;
    s += T(28 + ind * 6, 102 + i * 17.5, l.trim(), 'mf');
  });

  s += L(n, 'a', 266, 120, 300, 120) + AL(283, 113, 'load');

  // runner
  s += R(300, 40, 480, 330, 'zone');
  s += T(312, 60, 'ScenarioRunner', 'tc') + T(312, 78, 'fresh clock, storage and gateways per trial', 's');
  s += box(312, 90, 456, 84, 'node', 'Per step', [
    '1 advance the clock to the step’s time, ticking at each timer due',
    '2 inject its message (attachments from fixtures) or ping, wait 60 s, tick',
    '3 observe: outbox, runs with their trajectory, model calls, ledger, timers',
  ]);
  s += box(312, 190, 140, 54, 'node', 'VirtualClock', ['moved by the runner']);
  s += box(464, 190, 140, 54, 'node', 'MemoryStore', ['+ blobs, per trial']);
  s += box(616, 190, 152, 54, 'node', 'RecordingGateways', ['deliveries recorded']);
  [[382, 'Clock'], [534, 'UnitOfWork'], [692, 'Gateways']].forEach(([x, l]) => {
    s += L(n, 'a', x, 244, x, 280) + T(x + 6, 266, l, 'al');
  });
  s += box(312, 280, 456, 72, 'key', 'AgentRuntime', [
    'the product class, unchanged: register_load, ingest_message,',
    'ingest_ping, tick, next_timer_due_at, snapshot',
  ], { tc: 'tc' });

  // model modes
  s += T(300, 544, 'The only part that changes between modes', 'h');
  s += box(300, 412, 152, 112, 'node', 'scripted', ['ScriptedModel: the', '  step’s script, per', '  role; agent entries', '  may carry tool_calls;', '  unused entries fail']);
  s += box(464, 412, 152, 112, 'node', 'replay', ['CassetteModel: keyed', '  by model, role,', '  prompts, images,', '  tools, schema; the', '  fold re-stages it']);
  s += box(628, 412, 152, 112, 'node', 'live', ['OpenRouter through', '  LangChain; fallback', '  chain + circuit', '  breaker; config/', '  models.yaml']);
  s += P(n, 'a', 'M376 412 V384 H540 V352');
  s += L(n, 'a', 540, 412, 540, 352);
  s += P(n, 'a', 'M704 412 V384 H540 V352') + T(546, 376, 'LanguageModel', 'al');

  // grading
  s += L(n, 'a', 780, 200, 820, 200) + AL(800, 193, 'obs');
  s += box(820, 40, 200, 330, 'node', 'graders', [
    'grade_step, per step:',
    '  actions: kind, to,',
    '  contains, purpose,',
    '  requests, count',
    '  forbid · obligations',
    '  timers · run_status',
    '  intent · llm_calls',
    'trajectory: tools_used,',
    '  sections_read,',
    '  max_rejections',
    'judge: a rubric and',
    '  min_score, on the',
    '  uncounted model',
    'check_invariants: keys,',
    '  recipients, budget',
  ], { tc: 'tc' });
  s += L(n, 'a', 1020, 200, 1050, 200);
  s += box(1050, 40, 134, 150, 'node', 'Failures', [
    'expected vs what',
    '  happened, per',
    '  step; the judge',
    '  only for wording',
  ]);
  s += L(n, 'a', 1117, 190, 1117, 220) + T(1123, 210, 'k trials', 'al');
  s += box(1050, 220, 134, 150, planCls('node', 'suite'), 'pass^k', [
    'run k trials;',
    '  the scenario',
    '  passes only if',
    '  all k pass',
    { t: 'agent eval run', c: 'm' },
    { t: '  --mode --k', c: 'm' },
  ], { tag: planTag('suite'), tagY: 142 });

  s += box(820, 412, 364, 98, 'node', 'What triggers a suite run', [
    'a publish: the broker’s suite on its new version',
    '  (eval lint: an intent without a scenario fails)',
    'every pull request: core, agent, docs; scripted, replay',
    'a model or prompt change: a bake-off, live k = 3',
  ]);

  return fig(n, 1200, 556,
    'Eval harness: a scenario YAML drives the ScenarioRunner, which runs the product AgentRuntime on a virtual clock, memory store and recording gateways; only the language model changes between scripted, replay and live; fact, trajectory and judge graders turn observations into failures, and k trials give pass^k',
    s,
    `A scenario is a timeline of injected messages (with attachments), pings and timers, each step with the outbox items, ledger, timers, run statuses, model calls and the agent’s trajectory expected after it. The runner drives the product’s <code>AgentRuntime</code> on a virtual clock, a fresh in-memory store and recording gateways, and the graders compare each step’s observation with its expectations and check run-wide invariants; a judge model scores the wording against a rubric, outside the counted calls. Only the model changes between modes: answers scripted in the YAML (an agent entry may carry the loop’s tool calls), cassettes replayed by the exact prompt (the transcript fold re-stages the loop without running a tool), or live calls through OpenRouter, where each scenario runs k times and counts only if all k pass (pass^k)${pending('suite') ? '; the k-trial loop and its CLI are not built yet' : ''}.`);
}

/* ---------------- Figure 10: deployment ---------------- */
function figDeploy() {
  const n = 10;
  let s = '';
  // local
  s += R(16, 40, 430, 460, 'zone');
  s += T(28, 30, 'Local · docker compose', 'h');
  if (pending('compose')) s += T(446, 30, NOT_BUILT, 'tag', 'end');
  s += box(36, 60, 180, 64, planCls('node', 'web'), 'web', ['React + Vite panels'], { tc: 'tc', tag: planTag('web') });
  s += box(246, 60, 180, 64, 'node', 'OpenRouter', ['every role + embeddings,', '  config/models.yaml'], {});
  s += L(n, 'a', 126, 124, 126, 170, { both: true }) + T(132, 152, 'HTTP + SSE', 'al');
  s += L(n, 'a', 336, 170, 336, 124) + T(342, 152, 'model calls', 'al');
  s += box(36, 170, 180, 110, planCls('node', 'api'), 'api', ['FastAPI: ingest, the', '  simulation clock,', '  brokers, SOPs, Hero', '  console, inspector'], { tc: 'tc', tag: planTag('api') });
  s += box(246, 170, 180, 110, planCls('node', 'api'), 'worker', ['tick() in a loop:', '  fire_due_timers,', '  documents,', '  process_all,', '  dispatch_outbox'], { tc: 'tc', tag: planTag('api') });
  s += box(36, 316, 260, 80, planCls('store', 'postgres'), 'Postgres (pgvector)', [
    'a container, or Neon via DATABASE_URL',
    'event log, ledger, timers, outbox, runs,',
    '  queue, brokers, SOP versions, documents',
  ], { tag: planTag('postgres') });
  s += box(316, 316, 110, 80, 'node', 'LangSmith', ['one trace per', '  run, fail-open']);
  s += L(n, 'a', 126, 280, 126, 316) + T(132, 302, 'ingest', 'al');
  s += L(n, 'a', 270, 280, 270, 316, { both: true }) + T(276, 302, 'claim, commit', 'al');
  s += L(n, 'd', 371, 280, 371, 316) + T(377, 302, 'traces', 'al');
  s += box(36, 420, 390, 64, 'store', 'blobs volume · BLOBS_DIR', [
    'attachment and SOP file bytes, named by sha256',
  ]);

  // production
  s += R(476, 40, 708, 500, 'zone');
  s += T(488, 30, 'Production on AWS · design, not built', 'h');
  s += box(496, 70, 180, 70, 'node plan', 'Integration services', ['SMS, email, Slack, TMS,', '  location feed']);
  s += L(n, 'a', 676, 105, 736, 105) + AL(706, 98, 'events');
  s += box(736, 70, 180, 70, 'node', 'ECS Fargate · API', ['behind an ALB: ingest,', '  dedupe, ping filter']);
  s += L(n, 'a', 916, 105, 976, 105) + AL(946, 98, 'files');
  s += box(976, 70, 188, 70, 'node', 'S3', ['attachments, SOP files:', '  the BlobStore port']);
  s += P(n, 'a', 'M756 140 V165 H600 V190') + AL(678, 159, 'append, dedupe');
  s += L(n, 'a', 846, 140, 846, 190) + T(852, 168, 'event_id', 'al');
  s += box(496, 190, 180, 210, 'store', 'RDS / Aurora Postgres', [
    'event log, ledger,',
    '  timers, outbox, runs',
    'brokers, SOP versions,',
    '  pgvector sections,',
    '  documents',
    'the agent’s memory and',
    '  the inspector’s source',
  ]);
  s += box(736, 190, 180, 80, 'key', 'SQS FIFO', ['MessageGroupId = load_id', 'dedup id = event_id', 'one run per load in flight']);
  s += box(976, 190, 188, 80, 'node', 'EventBridge Scheduler', ['one-time schedule per', '  timer at its due_at,', '  into the same queue']);
  s += L(n, 'a', 976, 215, 916, 215) + AL(946, 208, 'timers');
  s += L(n, 'a', 826, 270, 826, 320) + T(832, 298, 'one per group', 'al');
  s += box(736, 320, 180, 80, 'node', 'ECS Fargate · workers', ['the same AgentRuntime;', '  documents in the tick;', '  scale on queue age']);
  s += L(n, 'a', 736, 360, 676, 360, { both: true }) + AL(706, 353, 'reads') + AL(706, 375, 'commits');
  s += L(n, 'a', 916, 360, 976, 360) + AL(946, 353, 'model');
  s += box(976, 320, 188, 80, 'node', 'Bedrock', ['every role, behind the', '  LanguageModel port']);
  s += P(n, 'a', 'M916 335 H946 V250 H976') + T(952, 296, 'schedules', 'al');
  s += P(n, 'a', 'M790 400 V428 H486 V105 H496') + AL(640, 422, 'outbox delivery, same idempotency keys');
  s += box(736, 450, 428, 76, 'node', null, [
    'Secrets Manager for the keys; CloudWatch alarms on queue age,',
    'cost per load, dead letters and stalled obligations; LangSmith',
    'traces. Temporal could replace SQS and the Scheduler (table below).',
  ]);

  return fig(n, 1200, 545,
    'Deployment: locally docker compose runs the API and web against Postgres with pgvector or Neon, with a blobs volume; in production on AWS, ECS Fargate runs the API and workers, SQS FIFO grouped by load_id is the queue, EventBridge Scheduler fires timers, Aurora holds memory and the SOP vectors, S3 holds attachments and SOP files and Bedrock serves the models',
    s,
    `Locally, docker compose runs the API, the worker loop and the web panels against one Postgres with pgvector, a container or Neon, and keeps uploaded files in a <code>blobs</code> volume${pending('compose') || pending('api') || pending('postgres') ? ' (the parts marked are not built yet; today the agent runs through the eval harness on the in-memory store)' : ''}. In production the same <code>AgentRuntime</code> runs on ECS Fargate: SQS FIFO replaces the queue table, with the message group set to the load id so a load never has two runs in flight, EventBridge Scheduler fires each timer into the same queue, and S3 takes the place of the blob folder behind the same port. A superseded schedule may still fire: the prefilter drops its run as stale, so deleting schedules is an optimization, not a correctness need.`);
}

/* ---------------- Page ---------------- */
const prompt0713 = `## Load
...
instructions (verbatim from the load metadata):
  Call 1 hr before arrival. PO 55821 required at gate.

## SOP sections for this event (follow them exactly)
### communication_rules: Communication rules
### driver_questions: Questions from the driver or other contacts
...
5. A question is not an ETA. If an ETA request is open,
   leave it and its follow-up as they are, and do not ask
   for the ETA again in your reply.
### escalation: Escalation to the Hero team

## Procedure in progress (context)
An open obligation in the ledger follows this procedure.
Its next step runs when its follow-up timer fires. Do not
run its steps for this event unless the sections above
tell you to.
### delay_risk_high: Delay risk changes to high (Northline)

## Ledger (the source of truth for obligations and attempts)
Open:
- eta_request: attempts 1; opened 07:01; last action 07:01
  (12 min ago); deferred until none, deferrals used 0;
  follow-up timer due 07:31 (in 18 min)

## Transcript (recent events, oldest first; "out" lines
   are what you did)
- 07:00 system: delay risk changed low -> high
- 07:01 out sms to drv_20931 (driver), purpose eta_request:
  "Hi, this is Northline Logistics. What time will you
  arrive at the Riverbend plant pickup in Stockton?"
- 07:12 in sms from drv_20931 (driver): "What's the PO
  number?"  <- this event

## Gate
intent: question`;

const prompt0731 = `## SOP sections for this event (follow them exactly)
### communication_rules: Communication rules
### delay_risk_high: Delay risk changes to high (Northline)
...
When the follow-up timer is due and there is still no ETA:
- After 1 ETA request: send the driver a second SMS that
  asks for the ETA. This is ETA request 2. Set a follow-up
  timer for 30 minutes.
- After 2 ETA requests: do not text the driver again. ...
### escalation: Escalation to the Hero team

## Ledger (the source of truth for obligations and attempts)
Open:
- eta_request: attempts 1; opened 07:01; last action 07:01
  (30 min ago); deferred until none, deferrals used 0;
  follow-up timer due 07:31 (now), firing now (this event)

## Pending timers (earliest first)
- none

## Transcript (recent events, oldest first; "out" lines
   are what you did)
- 07:00 system: delay risk changed low -> high
- 07:01 out sms to drv_20931 (driver), purpose eta_request:
  "Hi, this is Northline Logistics. What time ..."
- 07:12 in sms from drv_20931 (driver): "What's the PO
  number?"
- 07:13 out sms to drv_20931 (driver), purpose reply: "The
  PO number is 55821. You will need it at the gate."
- 07:31 system: follow-up timer due for eta_request
  <- this event

## Event (decide what to do about this)
type: timer_due
time: 07:31 (now)
channel: system
timer: follow-up for eta_request, due 07:31 (now)

## Gate
intent: eta_follow_up_due
reason: "eta_request follow-up timer is due"`;

const notBuiltList = Object.entries({
  api: 'the FastAPI service',
  bootstrap: 'the production composition root',
  postgres: 'the Postgres adapter',
  web: 'the web panels',
  compose: 'docker compose',
  suite: 'the k-trial eval loop and its CLI',
  brokers: 'the broker store (W3-BRK)',
  loop: 'the agent’s tool loop (W3-AGT)',
  onboarding: 'SOP compile and publish (W3-KNW)',
  search: 'the Postgres SOP index (W3-KNW)',
  documents: 'the document pipeline (W3-DOC)',
  judge: 'the eval judge (W3-EVK)',
})
  .filter(([k]) => pending(k))
  .map(([, v]) => v);

const legend = `<div class="legend">
  <span><svg viewBox="0 0 28 14" width="28" height="14" aria-hidden="true"><rect class="node" x="1" y="1" width="26" height="12" rx="3"/></svg> code in this repository</span>
  <span><svg viewBox="0 0 28 14" width="28" height="14" aria-hidden="true"><rect class="node plan" x="1" y="1" width="26" height="12" rx="3"/></svg> described, not built here: the integration layer (mocked), production on AWS${notBuiltList.length ? `, and parts not built yet (${notBuiltList.join(', ')})` : ''}</span>
  <span><svg viewBox="0 0 28 14" width="28" height="14" aria-hidden="true"><rect class="key" x="1" y="1" width="26" height="12" rx="3"/></svg> the element the figure’s point turns on</span>
</div>`;

const body = `
<div class="page">
<header>
  <h1>Load Agent Architecture</h1>
  <p class="lede">How the agent follows a broker’s SOP for one load, drawn from the code in this repository, and how a broker is onboarded without code. The worked example is load 481207 (Client A, broker id <code>northline</code>): the delay risk turns high at 07:00, the driver asks for the PO number at 07:12, and the agent escalates when its second ETA request goes unanswered. The second one is Acme Freight, whose own PDF procedure becomes a published SOP version with a new intent and a POD requirement.</p>
  ${legend}
  <nav class="toc" aria-label="Figures">
    <a href="#s-flow">1 Event flow</a><a href="#s-run">2 One run</a><a href="#s-layers">3 Layers</a><a href="#s-memory">4 Memory</a><a href="#s-race">5 Deferral race</a><a href="#s-sop">6 SOP resolution</a><a href="#s-onboard">7 Broker onboarding</a><a href="#s-docs">8 Documents</a><a href="#s-evals">9 Eval harness</a><a href="#s-deploy">10 Deployment</a>
  </nav>
</header>

<section id="s-flow">
  <h2>Event flow, end to end</h2>
  ${figFlow()}
</section>

<section id="s-run">
  <h2>One agent run</h2>
  ${figRun()}
  <p class="note">Validator rules (<code>domain/rules.py</code>), checked per action against the state the earlier approved actions leave, by the action tool at the call and again in <code>validate</code>: recipient is a contact of the load (high), channel allowed for that contact, SMS rate limit (3 per contact per 60 min), the obligation exists, deferral policy (one deferral, at most 15 min), the attempt cap of the obligation’s kind (2 ETA requests in the standard catalog), no duplicate action, no phone number or email address in the text (high), the stage only moves forward. The loop’s budget per broker (<code>BrokerLimits</code>): <code>max_steps</code> tool rounds (8; 0 is the single plan), <code>max_cost_usd</code> per run (0.05) and <code>max_rejections</code> (3, then a <code>BLOCKED_TITLE</code> escalation with the accepted actions).</p>
</section>

<section id="s-layers">
  <h2>Layers</h2>
  ${figLayers()}
</section>

<section id="s-memory">
  <h2>Memory</h2>
  ${figMemory()}
  <div class="pair">
    <div>
      <h3>07:13 · prompt excerpt</h3>
      <p class="note">The triage model read the message and returned <code>question</code>. The question’s section is in full; the ETA procedure appears only as context because the ledger still has the request open.</p>
      <pre>${esc(prompt0713)}</pre>
    </div>
    <div>
      <h3>07:31 · prompt excerpt</h3>
      <p class="note">No triage call: a timer’s intent comes from code. The prefilter already checked that <code>timer_1</code> is the active timer of an open obligation. With one attempt in the ledger the SOP’s second step applies; the agent stages a second SMS and a new 30-minute timer, which makes 2 attempts and a follow-up at 08:01.</p>
      <pre>${esc(prompt0731)}</pre>
    </div>
  </div>
  <p class="note">Excerpts of the real prompts, as <code>application/prompts.py</code> rendered them for scenario A before wave 3; the agent prompt keeps these blocks, rendered from the broker’s catalog, and adds the tools. Long lines are wrapped and section bodies cut. In the evals timers fire with no latency, so the 08:01 follow-up creates the dispatcher email and the Hero task at 08:01, where the assignment’s table says 08:02.</p>
</section>

<section id="s-race">
  <h2>The deferral race (scenario G1)</h2>
  ${figRace()}
</section>

<section id="s-sop">
  <h2>SOP resolution</h2>
  ${figSop()}
  <div class="proscons">
    <div><h3>Advantages</h3><p>The common path is deterministic: the same event and load always select the same sections, and the run record keeps their ids. The prompt holds a few sections instead of a broker’s whole manual, which keeps cost and latency down. A situation no tag names is searched before it goes to a Hero. A broker’s layer holds only what that broker changed, and no other broker’s sections are visible to its loads, by rules or by search.</p></div>
    <div><h3>Disadvantages</h3><p>Search ranking is not exact, so the evals assert which tools and sections a run used, not a score. Editors still tag each section with the broker’s intents and stages, which the publish lint checks, and triage must classify a message into the broker’s intents correctly. Embeddings need a live model at publish; offline runs use a deterministic fake.</p></div>
  </div>
</section>

<section id="s-onboard">
  <h2>Onboarding a broker without code</h2>
  ${figOnboard()}
</section>

<section id="s-docs">
  <h2>Documents</h2>
  ${figDocs()}
</section>

<section id="s-evals">
  <h2>Eval harness</h2>
  ${figEvals()}
</section>

<section id="s-deploy">
  <h2>Deployment</h2>
  ${figDeploy()}
  <div class="tablewrap">
    <table class="alt">
      <caption>The same design on Temporal</caption>
      <thead><tr><th>Our component</th><th>Temporal</th></tr></thead>
      <tbody>
        <tr><td>Per-load FIFO queue, one event in flight per load</td><td>One workflow per load (workflow id = <code>load_id</code>) that handles its events one at a time</td></tr>
        <tr><td>An inbound message or a confirmed risk change</td><td>A signal to the load’s workflow (signal-with-start for the first one)</td></tr>
        <tr><td>One run: claim, think, commit</td><td>One activity per event, with the 240 s run deadline as its start-to-close timeout</td></tr>
        <tr><td>Timers table and <code>fire_due_timers</code></td><td>Durable timers in the workflow; superseding one cancels it, and the stale-timer prefilter stays as a guard</td></tr>
        <tr><td>Outbox and <code>dispatch_outbox</code></td><td>A delivery activity per item, retried, with the same idempotency key</td></tr>
        <tr><td>Release and dead letter after 3 failed attempts</td><td>The activity retry policy’s maximum attempts, then the workflow creates a Hero task</td></tr>
        <tr><td>Event log and obligations ledger</td><td>Stay in Postgres: they are what the model reads and what the inspector shows; the workflow history is not the agent’s memory</td></tr>
      </tbody>
    </table>
  </div>
</section>

<section>
  <h2>Budgets</h2>
  <p class="note">Enforced in code: the 240 s run deadline and 60 s per model call (<code>RuntimeConfig</code>), and per broker the agent loop’s <code>max_steps</code> rounds, <code>max_cost_usd</code> per run and <code>max_rejections</code> (<code>BrokerLimits</code>). Measured: the queue age against the 5-minute SLA and the cost per load against $1, flagged at 80% (<code>application/metrics.py</code>). The per-step ceilings and the spend split are targets; the wave 3 bake-off measures the loop’s latency and cost (<code>evals/RESULTS.md</code>).</p>
  <div class="pair">
    <div class="tablewrap">
      <table>
        <caption>Latency per run</caption>
        <thead><tr><th>Step</th><th class="num">Ceiling</th></tr></thead>
        <tbody>
          <tr><td>Queue wait (queue age)</td><td class="num">15 s</td></tr>
          <tr><td>Claim and load context</td><td class="num">1 s</td></tr>
          <tr><td>Triage (messages only)</td><td class="num">5 s</td></tr>
          <tr><td>Knowledge: sections by rules</td><td class="num">&lt; 0.1 s</td></tr>
          <tr><td>Agent loop (≤ 8 rounds of 3 to 5 s, under the call timeout)</td><td class="num">45 s</td></tr>
          <tr><td>Validate and commit</td><td class="num">1 s</td></tr>
          <tr><td>Outbox delivery</td><td class="num">10 s</td></tr>
          <tr class="sum"><td>Sum of ceilings</td><td class="num">≈ 77 s</td></tr>
          <tr><td>Run deadline, then an urgent Hero task</td><td class="num">240 s</td></tr>
          <tr><td>Event to last action (the requirement)</td><td class="num">300 s</td></tr>
          <tr><td>Document read, per attachment, in the tick before the queue</td><td class="num">60 s</td></tr>
        </tbody>
      </table>
    </div>
    <div class="tablewrap">
      <table>
        <caption>LLM spend per load</caption>
        <thead><tr><th>Part</th><th class="num">Budget</th></tr></thead>
        <tbody>
          <tr><td>Triage: about 80 messages, small model</td><td class="num">$0.10</td></tr>
          <tr><td>Agent: 40 to 80 runs of a few rounds, the prefix cached; at most $0.05 a run</td><td class="num">$0.65</td></tr>
          <tr><td>Documents: 3 to 5 attachments, one extract call each</td><td class="num">$0.15</td></tr>
          <tr><td>Reserve for retries; embeddings only at publish</td><td class="num">$0.10</td></tr>
          <tr class="sum"><td>Total</td><td class="num">$1.00</td></tr>
        </tbody>
      </table>
      <p class="note">Most events cost little: skipped runs (duplicates, stale timers, empty messages) make no model call, risk changes, timers and processed documents skip triage, and a triage no-op ends after one small call. The plan in <code>docs/answers.md</code> 5.3 puts a typical load at about $0.07 on the current agent model; the bake-off replaces it with the measured cost.</p>
    </div>
  </div>
</section>

<section>
  <h2>Alternatives considered</h2>
  <div class="tablewrap">
    <table class="alt">
      <thead><tr><th>Option</th><th>Why not</th></tr></thead>
      <tbody>
        <tr><td>One long prompt: full SOP and full history on every event</td><td>A 100-page SOP is roughly 50 to 70k tokens. Sent on about 80 events per load it breaks the $1 budget, and unrelated procedures push the model to act when it should not.</td></tr>
        <tr><td>Compile SOPs into a workflow engine</td><td>Deterministic, but every SOP change would need an engineer, and plain-English SOPs are ambiguous. We keep a small state machine for obligations only (attempts, deferral, timers); the steps stay in English.</td></tr>
        <tr><td>One agent per stage or role</td><td>Each handoff adds a model call and drops context. The stage is already a selection key for SOP sections, so a separate agent adds latency and nothing else.</td></tr>
        <tr><td>Vector search as the only way to find a section</td><td>A missed section means silent wrong behavior that a test cannot assert. Search is the second step: the sections selected by intent and stage come first, and the agent searches only when they do not cover the event.</td></tr>
        <tr><td>Tools that execute actions</td><td>An action tool here only stages: it checks the action with the rules and answers accepted or refused, and the executor applies the whole plan in the commit, so a bad answer can be refused but never half-applied.</td></tr>
        <tr><td>One structured plan per event (before wave 3)</td><td>Kept per broker as <code>max_steps: 0</code>. As the default it cannot search, read a document on demand or correct itself after a refused action.</td></tr>
        <tr><td>SOPs written in our format by engineers</td><td>A broker’s own PDF is compiled into a draft and reviewed instead; the <code>sops/</code> files remain the seed and the format a person can edit by hand.</td></tr>
      </tbody>
    </table>
  </div>
</section>
</div>`;

const css = `
/* One column of figures on a cool paper ground; wide diagrams scroll inside their frame */
:root {
  --bg: #F1F4F6;
  --surface: #FFFFFF;
  --fg: #14212B;
  --fg2: #4A5B68;
  --line: #B6C2CB;
  --accent: #0A6C8C;
  --accent-soft: #DCEEF4;
  --store: #E8EEF2;
  --sans: 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --cond: 'IBM Plex Sans Condensed', 'IBM Plex Sans', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0D151B; --surface: #15212A; --fg: #E3EAEF; --fg2: #9DB0BD; --line: #3A4C5A;
    --accent: #4FB8D8; --accent-soft: #12323F; --store: #1B2A35;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0D151B; --surface: #15212A; --fg: #E3EAEF; --fg2: #9DB0BD; --line: #3A4C5A;
  --accent: #4FB8D8; --accent-soft: #12323F; --store: #1B2A35;
  color-scheme: dark;
}
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.6; }
.page {
  max-width: 1180px; margin: 0 auto; padding-block: 36px 72px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: flex; flex-direction: column; gap: 56px;
}
header { display: flex; flex-direction: column; gap: 14px; }
h1 { font-family: var(--cond); font-weight: 600; font-size: 38px; line-height: 1.1; margin: 0; text-wrap: balance; }
h2 { font-family: var(--cond); font-weight: 600; font-size: 24px; line-height: 1.2; margin: 0 0 16px; text-wrap: balance; }
h3 { font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--fg2); margin: 0 0 8px; }
p { margin: 0; }
code { font-family: var(--mono); font-size: .9em; }
a { color: var(--accent); }
.lede { max-width: 72ch; color: var(--fg2); font-size: 16px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px 22px; font-size: 13px; color: var(--fg2); }
.legend span { display: inline-flex; align-items: center; gap: 8px; }
.legend svg { flex: none; }
.toc { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13.5px; }
.toc a { text-decoration: none; }
.toc a:hover { text-decoration: underline; }
section { min-width: 0; }
figure { margin: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 4px; padding: 16px; }
.scroll { overflow-x: auto; }
.scroll svg { display: block; width: 100%; height: auto; color: var(--fg2); }
figcaption { margin-top: 12px; max-width: 84ch; font-size: 13.5px; color: var(--fg2); }
.fignum { font-weight: 600; color: var(--fg); }
.proscons, .pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; margin-top: 24px; }
.proscons > div, .pair > div { min-width: 0; }
.proscons p { max-width: 60ch; }
.pair .note { margin-bottom: 10px; }
pre { margin: 0; padding: 14px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 4px; font-family: var(--mono); font-size: 12px; line-height: 1.55; overflow-x: auto; color: var(--fg); }
.note { font-size: 13.5px; color: var(--fg2); max-width: 84ch; margin-top: 14px; }
.tablewrap { overflow-x: auto; min-width: 0; margin-top: 24px; }
.tablewrap .note { margin-top: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
caption { text-align: left; font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--fg2); padding-bottom: 8px; }
th { text-align: left; font-size: 12px; font-weight: 600; color: var(--fg2); padding: 6px 10px; border-bottom: 1px solid var(--line); }
td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--mono); font-size: 13px; }
tr.sum td { font-weight: 600; border-top: 1px solid var(--fg2); }
table.alt td:first-child { width: 34%; font-weight: 500; }
table.alt { min-width: 560px; }
.pair table { min-width: 300px; }

/* diagram marks */
svg text { fill: var(--fg); font-family: var(--sans); }
svg .t { font-size: 13px; font-weight: 600; }
svg .t2 { font-size: 11.5px; font-weight: 600; }
svg .tc { font-family: var(--mono); font-size: 12.5px; font-weight: 600; }
svg .s { font-size: 11.5px; fill: var(--fg2); }
svg .m { font-family: var(--mono); font-size: 10.5px; fill: var(--fg2); white-space: pre; }
svg .mf { font-family: var(--mono); font-size: 10.5px; fill: var(--fg); white-space: pre; }
svg .acc { font-size: 11.5px; font-weight: 600; fill: var(--accent); }
svg .accm { font-family: var(--mono); font-size: 10.5px; font-weight: 600; fill: var(--accent); }
svg .h { font-size: 11px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase; fill: var(--fg2); }
svg .al { font-size: 11px; fill: var(--fg2); }
svg .tag { font-family: var(--mono); font-size: 9.5px; fill: var(--fg2); letter-spacing: .04em; }
svg .tag.k { fill: var(--accent); }
svg .num { font-size: 10px; font-weight: 600; fill: var(--fg); }
svg .x { font-size: 14px; font-weight: 600; fill: var(--fg2); }
svg .node { fill: var(--surface); stroke: var(--line); stroke-width: 1.2; }
svg .store { fill: var(--store); stroke: var(--line); stroke-width: 1.2; }
svg .key { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 1.6; }
svg .plan { stroke: var(--fg2); stroke-dasharray: 5 4; }
svg .key.plan { stroke: var(--accent); }
svg .ghost { fill: none; stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 4; }
svg .zone { fill: none; stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 4; }
svg .life { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 4; }
svg .act { fill: var(--surface); stroke: var(--fg2); stroke-width: 1; }
svg .brk { fill: none; stroke: var(--fg2); stroke-width: 1; }
svg .numc { fill: var(--surface); stroke: var(--fg2); stroke-width: 1; }
svg .flow { stroke: var(--fg2); stroke-width: 1.4; fill: none; }
svg .flow.c { stroke: var(--accent); stroke-width: 1.6; }
svg .flow.d { stroke-dasharray: 5 4; }
svg .halo { stroke: var(--surface); stroke-width: 9; fill: none; }
svg .forbid { stroke: var(--fg2); stroke-width: 1.2; stroke-dasharray: 3 3; }
@media (max-width: 520px) {
  h1 { font-size: 30px; }
  .page { gap: 44px; }
}
`;

const head = `<title>Load Agent Architecture</title>
<meta name="description" content="System design of the freight load agent: event flow, one agent run and its tool loop, layers, memory, the deferral race, SOP resolution and search, broker onboarding, documents, the eval harness and deployment.">
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>${css}</style>`;

const [out, repo] = process.argv.slice(2);
if (!out || !repo) {
  console.error('usage: node docs/build_diagrams.js <page-out.html> <docs/architecture.html>');
  process.exit(2);
}
fs.writeFileSync(out, head + body);
fs.mkdirSync(path.dirname(repo), { recursive: true });
fs.writeFileSync(
  repo,
  `<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n${head}</head><body>${body}</body></html>\n`,
);
const missing = Object.keys(BUILT).filter(pending);
console.log(`ok${missing.length ? ` (drawn as not built yet: ${missing.join(', ')})` : ''}`);
