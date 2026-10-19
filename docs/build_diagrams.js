const fs = require('fs');

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const T = (x, y, s, cls = 's', anchor) =>
  `<text class="${cls}" x="${x}" y="${y}"${anchor ? ` text-anchor="${anchor}"` : ''}>${esc(s)}</text>`;
const R = (x, y, w, h, cls) => `<rect class="${cls}" x="${x}" y="${y}" width="${w}" height="${h}" rx="3"/>`;

function box(x, y, w, h, cls, title, lines = [], o = {}) {
  const center = !!o.center;
  const tx = center ? x + w / 2 : x + 12;
  const an = center ? 'middle' : undefined;
  let s = R(x, y, w, h, cls);
  s += T(tx, y + 22, title, 't', an);
  lines.forEach((l, i) => {
    const c = typeof l === 'object' ? l.c : 's';
    const t = typeof l === 'object' ? l.t : l;
    s += T(tx, y + 42 + i * 16, t, c, an);
  });
  if (o.tag) s += T(x + w - 10, y + 16, o.tag, 'tag', 'end');
  if (o.foot) s += T(tx, y + h - 12, o.foot, 'm', an);
  return s;
}
// small step box inside a container: title + one sub line
function step(x, y, w, h, title, sub) {
  return R(x, y, w, h, 'node') + T(x + 10, y + 19, title, 't') + T(x + 10, y + 35, sub, 's');
}
const KIND = { a: 'flow', c: 'flow c', h: 'flow h' };
const L = (n, k, x1, y1, x2, y2, o = {}) =>
  `<line class="${KIND[k]}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#${k}${n})"${o.both ? ` marker-start="url(#${k}${n})"` : ''}/>`;
const P = (n, k, d) => `<path class="${KIND[k]}" d="${d}" fill="none" marker-end="url(#${k}${n})"/>`;
const AL = (x, y, s, anchor = 'middle') => T(x, y, s, 'al', anchor);

const defs = n => `<defs>
<marker id="a${n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:var(--fg2)"/></marker>
<marker id="c${n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:var(--accent)"/></marker>
<marker id="h${n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" style="fill:var(--amber)"/></marker>
</defs>`;

function fig(n, W, H, aria, body, caption) {
  return `<figure>
<div class="scroll"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(aria)}">${defs(n)}${body}</svg></div>
<figcaption>${caption}</figcaption>
</figure>`;
}

/* ---------------- Figure 1: system overview ---------------- */
function fig1() {
  const n = 1;
  let s = '';
  // column headers
  s += T(20, 116, 'Inputs', 'h') + T(260, 116, 'Ingest', 'h') + T(500, 116, 'Serialize', 'h') + T(1050, 116, 'Effects', 'h');
  // SOP store + authoring
  s += box(740, 20, 240, 70, 'store', 'SOP store', ['versioned Markdown, per client', 'compiled section index'], { tag: 'PROCEDURAL' });
  s += box(1050, 20, 170, 70, 'node', 'SOP authoring', ['lint → evals → publish']);
  s += L(n, 'a', 1050, 55, 980, 55) + AL(1015, 47, 'publish vN');
  s += L(n, 'c', 860, 90, 860, 130) + AL(870, 114, 'sections for this event', 'start');
  // sources
  s += box(20, 130, 170, 52, 'node', 'Driver', ['SMS']);
  s += box(20, 198, 170, 52, 'node', 'Dispatcher', ['email, main thread']);
  s += box(20, 266, 170, 52, 'node', 'Broker staff', ['email cc · Slack']);
  s += box(20, 334, 170, 52, 'node', 'Location pings', ['lat, lon, delay_risk']);
  s += box(20, 402, 170, 68, 'node', 'Attachments', ['BOL · POD · rate con', 'receipts: photo / PDF']);
  [156, 224, 292, 360].forEach((y, i) => {
    s += L(n, 'a', 190, y, 260, y) + AL(225, y - 7, i === 3 ? 'push' : 'webhook');
  });
  s += L(n, 'a', 190, 436, 260, 436) + AL(225, 429, 'files');
  // ingest
  s += box(260, 130, 170, 256, 'given', 'Integration layer', ['owned by integrations', 'Slack · email · SMS', 'TMS · location feed', '', 'contact_id → phone/email', 'mocked in this repo']);
  s += box(260, 402, 170, 68, 'core', 'Document pipeline', ['classify → extract', '→ validate']);
  // serialize
  s += L(n, 'a', 430, 165, 500, 165) + AL(465, 157, 'raw msg');
  s += box(500, 130, 170, 70, 'core', 'Event normalizer', ['typed event, dedupe', 'by message_id']);
  s += L(n, 'a', 585, 200, 585, 236) + AL(593, 222, 'event', 'start');
  s += box(500, 236, 170, 104, 'core', 'Per-load queue', ['FIFO per load_id', 'one run at a time', '~10 s debounce']);
  s += P(n, 'a', 'M430 436 H465 V288 H500') + AL(473, 380, 'doc event', 'start') + AL(473, 394, 'with fields', 'start');
  s += L(n, 'a', 540, 500, 540, 340) + AL(548, 440, 'timer.due', 'start');
  // agent
  s += L(n, 'c', 670, 288, 740, 288) + AL(705, 280, 'event');
  s += R(740, 130, 240, 326, 'core') + T(756, 154, 'Agent run (stateless worker)', 't');
  const steps = [
    ['1  Load context', 'metadata, ledger, recent log'],
    ['2  Gate', 'act or no-op? small model'],
    ['3  Resolve SOP', 'rules, no LLM'],
    ['4  Decide', 'LLM + tool calls'],
    ['5  Validate & commit', 'policy, idempotency, outbox'],
  ];
  steps.forEach((st, i) => { s += step(756, 172 + i * 56, 208, 44, st[0], st[1]); });
  // effects
  s += L(n, 'c', 980, 190, 1050, 190) + AL(1015, 182, 'outbox');
  s += box(1050, 130, 170, 120, 'node', 'External actions', ['via integration layer', 'SMS · email thread', 'broker Slack · TMS note']);
  s += L(n, 'h', 980, 335, 1050, 335) + AL(1015, 327, 'commit');
  s += box(1050, 280, 170, 110, 'node', 'Internal actions', ['Hero task (urgent flag)', 'stage / status update', 'internal Slack']);
  s += L(n, 'h', 1135, 390, 1135, 420) + AL(1143, 409, 'task', 'start');
  s += box(1050, 420, 170, 50, 'human', 'Hero team', ['results = new events']);
  // memory
  s += P(n, 'c', 'M770 456 V478 H640 V500') + AL(705, 470, 'set_timer');
  s += box(500, 500, 170, 100, 'store', 'Timer service', ['durable, per obligation', 'due_at → fires event', 'cancelled on close']);
  s += L(n, 'c', 860, 456, 860, 500, { both: true }) + AL(870, 482, 'read · append', 'start');
  s += R(740, 500, 240, 160, 'store') + T(756, 520, 'Per-load memory', 't');
  s += R(756, 534, 208, 50, 'node') + T(766, 553, 'Event log', 't') + T(766, 569, 'append-only, every in/out', 's') + T(954, 548, 'EPISODIC', 'tag', 'end');
  s += R(756, 594, 208, 50, 'node') + T(766, 613, 'State + ledger', 't') + T(766, 629, 'stage, risk, open obligations', 's') + T(954, 608, 'PROSPECTIVE', 'tag', 'end');
  s += L(n, 'a', 980, 580, 1050, 580) + AL(1015, 572, 'replay');
  s += box(1050, 500, 170, 160, 'node', 'Evals & monitoring', ['replay logged loads', 'shadow + canary runs', 'cost per load', 'no-op / escalate rate']);
  // legend
  const leg = [['core', 'our code: agent, pipelines'], ['given', 'existing service, mocked here'], ['store', 'state kept between runs'], ['human', 'people']];
  leg.forEach((l, i) => { s += R(20, 520 + i * 26, 20, 14, l[0]) + T(50, 531 + i * 26, l[1], 's'); });
  s += T(20, 636, 'Tags on the stores name the memory type.', 's');
  return fig(n, 1240, 680,
    'Inputs become typed events on a per-load queue; one stateless agent run reads memory and SOP sections, then writes actions, state and timers',
    s,
    'Every input becomes a typed event on a per-load queue, so one run handles one event and runs never overlap for a load. The agent keeps nothing in process: context is rebuilt from the event log, the ledger and the timer service on each run.');
}

/* ---------------- Figure 2: one run ---------------- */
function fig2() {
  const n = 2;
  let s = '';
  s += T(640, 20, 'run deadline 240 s · LLM call timeout 60 s · at most 2 tool rounds (targets)', 'al', 'middle');
  s += `<line class="dash" x1="20" y1="32" x2="1260" y2="32"/><line class="dash" x1="20" y1="26" x2="20" y2="38"/><line class="dash" x1="1260" y1="26" x2="1260" y2="38"/>`;
  const X = i => 20 + 216 * i;
  const defsB = [
    ['node', 'Load context', ['metadata, stage, risk', 'open obligations', 'last N events + summary'], 'reads state + log', null],
    ['core', 'Gate', ['cheap model + rules', 'Is the event actionable', 'given open obligations?'], 'reads ledger', 'LLM small'],
    ['node', 'Resolve SOP', ['key = event + stage', 'base ← client ← type', 'overrides; no LLM'], 'reads SOP index', null],
    ['core', 'Decide', ['LLM with tools', 'sees event, state,', 'ledger, SOP sections'], 'tool: read_document', 'LLM'],
    ['node', 'Validate', ['channel allowed?', 'contact valid, rate cap', 'idempotency key'], 'blocks, never edits', null],
    ['node', 'Execute & commit', ['outbox → integrations', 'append actions to log', 'update ledger, timers'], 'writes memory', null],
  ];
  defsB.forEach((d, i) => {
    s += box(X(i), 60, 160, 150, d[0], d[1], d[2], { tag: d[4] || undefined, foot: d[3] });
  });
  s = s.replace(/class="s"/g, 'class="s"');
  const labs = ['context', 'act', 'sections', 'plan', 'approved'];
  labs.forEach((l, i) => {
    s += L(n, 'a', X(i) + 160, 135, X(i + 1), 135) + AL(X(i) + 188, 127, l);
  });
  // no-op
  s += L(n, 'a', 316, 210, 316, 290) + AL(324, 254, 'no-op', 'start');
  s += box(216, 290, 200, 88, 'node', 'No-op', ['append “ignored” + reason', 'e.g. “ok” with no open question', 'timer for a closed obligation']);
  // escalate
  s += L(n, 'h', 748, 210, 748, 290) + AL(756, 254, 'low confidence', 'start');
  s += L(n, 'h', 964, 210, 964, 290) + AL(972, 254, 'policy block', 'start');
  s += box(668, 290, 400, 88, 'human', 'Escalate instead of acting', ['urgent Hero task with run context, or broker Slack per SOP', 'also on run deadline, LLM error, or no matching SOP section', 'the event is never dropped']);
  return fig(n, 1280, 400,
    'Six steps in one run: load context, gate, resolve SOP, decide, validate, execute; the gate can end the run with no action and failures escalate to a person',
    s,
    'The gate is where “ok” gets ignored: it sees the open obligations, finds nothing the message answers, and ends the run after one small-model call. Only runs that pass the gate pay for SOP retrieval and the full decision call.');
}

/* ---------------- Figure 3: SOP layers ---------------- */
function fig3() {
  const n = 3;
  let s = '';
  const cx = { sec: 20, std: 262, cli: 489, typ: 716, eff: 961 };
  s += T(cx.sec, 24, 'Section (by event)', 'h') + T(cx.std, 24, 'Standard', 'h') + T(cx.cli, 24, 'Client B', 'h') + T(cx.typ, 24, 'Client B · refrigerated', 'h') + T(cx.eff, 24, 'Effective for B reefer', 'h');
  s += L(n, 'c', 270, 48, 925, 48) + AL(597, 41, 'more specific layer wins');
  const rows = [
    {
      id: 'delay-risk-high', ev: 'event: delay_risk → high',
      std: ['Ask the driver for the ETA,', 'retry once, then escalate', 'to the Hero team'],
      cli: { tag: 'REPLACES', lines: ['1 SMS to the driver; no ETA', 'in 30 min → broker Slack', 'never contact the dispatcher'] },
      typ: null,
      eff: ['1 SMS, then Slack alert at 30 min', 'from: client B'],
    },
    {
      id: 'driver-leaves-pickup', ev: 'event: driver_departed',
      std: ['Confirm departure, ask for', 'the BOL photo, set the', 'delivery ETA timer'],
      cli: null,
      typ: { tag: 'EXTENDS', lines: ['Also ask for the trailer', 'temperature and the seal', 'number'] },
      eff: ['standard steps + temp and seal', 'from: standard + B reefer'],
    },
    {
      id: 'pod-received', ev: 'event: attachment is a POD',
      std: ['Validate POD fields, add a', 'TMS note, set the stage', 'to Delivered'],
      cli: null, typ: null,
      eff: ['standard, unchanged', 'from: standard'],
    },
  ];
  rows.forEach((r, i) => {
    const y = 64 + i * 108;
    s += T(cx.sec, y + 30, r.id, 'mono') + T(cx.sec, y + 48, r.ev, 's');
    s += box(cx.std, y, 215, 96, 'node', 'standard', r.std);
    [['cli', r.cli], ['typ', r.typ]].forEach(([k, v]) => {
      if (v) s += box(cx[k], y, 215, 96, 'core', 'override', v.lines, { tag: v.tag });
      else s += R(cx[k], y, 215, 96, 'ghost') + T(cx[k] + 107, y + 52, '— inherits', 'm', 'middle');
    });
    s += box(cx.eff, y, 259, 96, 'eff', 'resolved', [r.eff[0], { t: r.eff[1], c: 'm' }]);
  });
  const by = 400;
  s += box(20, by, 380, 70, 'node', '1  Build the key', ['event type + stage, from the event and load state']);
  s += box(430, by, 380, 70, 'node', '2  Collect sections per layer', ['standard, client, client + load type, by section id']);
  s += box(840, by, 380, 70, 'node', '3  Merge and inject', ['replace or extend; only these reach the prompt']);
  s += L(n, 'a', 400, by + 35, 430, by + 35) + L(n, 'a', 810, by + 35, 840, by + 35);
  return fig(n, 1240, 490,
    'A matrix of SOP sections against standard, client and client-plus-load-type layers, showing which layer wins for each section',
    s,
    'Resolution works per section id. A client file can replace a standard section or extend it, and a load-type file can do the same on top of the client. Sections nobody overrides fall through to the standard. Content in the cells is example text.');
}

/* ---------------- Figure 4: obligation state ---------------- */
function fig4() {
  const n = 4;
  let s = '';
  s += box(20, 100, 170, 90, 'node', 'No obligation', ['delay_risk: low', 'nothing to track']);
  s += box(290, 100, 170, 90, 'core', 'eta_request · OPEN', ['attempts: 1', 'asked 07:01', 'timer due 07:31']);
  s += box(560, 100, 170, 90, 'core', 'eta_request · OPEN', ['attempts: 2', 'asked 07:31', 'timer due 08:01']);
  s += box(830, 100, 170, 90, 'human', 'Exhausted', ['SOP: escalate', 'email dispatcher 08:02', 'Hero task 08:02']);
  s += box(1100, 100, 160, 90, 'node', 'Closed', ['SOP complete', 'timers cancelled']);
  // edges
  s += L(n, 'a', 190, 145, 290, 145) + AL(240, 128, '07:00 risk → high') + AL(240, 163, '07:01 send SMS');
  s += L(n, 'a', 460, 145, 560, 145) + AL(510, 122, '07:31 timer due') + AL(510, 135, 'no ETA → SMS #2') + AL(510, 163, 'set timer 08:01');
  s += L(n, 'h', 730, 145, 830, 145) + AL(780, 122, '08:01 timer due') + AL(780, 135, 'attempts = 2') + AL(780, 163, '08:02 email + task');
  s += L(n, 'a', 1000, 145, 1100, 145) + AL(1050, 128, 'actions') + AL(1050, 163, 'committed');
  // self loop at N1
  s += `<path class="flow c" d="M340 100 C340 52 410 52 410 100" fill="none" marker-end="url(#c${n})"/>`;
  s += AL(375, 26, '07:12 driver asks “PO number?”') + AL(375, 39, '07:13 reply with PO. Still OPEN, attempts 1');
  // satisfied
  s += box(380, 290, 300, 80, 'store', 'ETA received → SATISFIED', ['any channel: driver SMS, dispatcher email', 'cancel timer · TMS note · update ETA']);
  s += L(n, 'c', 375, 190, 450, 290) + AL(398, 244, 'ETA arrives', 'end');
  s += L(n, 'c', 645, 190, 610, 290) + AL(636, 244, 'ETA arrives', 'start');
  return fig(n, 1280, 400,
    'State machine for the eta_request obligation: open with one attempt, a self-loop for the driver question, open with two attempts, exhausted and escalated, with an exit to satisfied on any ETA',
    s,
    'The ETA request is a row in the ledger with an attempt count and a timer id. The driver’s PO question changes nothing in it, which is how the agent still knows at 07:13 that the ETA is owed. Any ETA, from any contact, closes the row and cancels the timer.');
}

/* ---------------- Figure 5: change control ---------------- */
function fig5() {
  const n = 5;
  let s = '';
  const X = i => 20 + 213 * i;
  const b = [
    ['node', 'Edit', ['Hero or CS writes', 'Markdown in the editor', 'draft only, no deploy']],
    ['node', 'Lint', ['unknown event or tool', 'conflicting overrides', 'vague steps flagged']],
    ['core', 'Scenario evals', ['standard suite + that', 'client’s suite, mocked', 'tools, assert actions']],
    ['core', 'Replay diff', ['last 30 days of real', 'events; list loads whose', 'actions would change']],
    ['node', 'Approve', ['customer team reads the', 'behavior diff, not the', 'prompt; signs off']],
    ['node', 'Canary → live', ['pinned SOP version vN', '5% of new loads first', 'rollback: repoint to vN-1']],
  ];
  b.forEach((d, i) => { s += box(X(i), 40, 165, 110, d[0], d[1], d[2]); });
  ['draft', 'pass', 'pass', 'diff', 'ok'].forEach((l, i) => {
    s += L(n, 'a', X(i) + 165, 95, X(i + 1), 95) + AL(X(i) + 189, 87, l);
  });
  s += box(20, 236, 330, 56, 'human', 'LLM or prompt change', ['same suites, plus side-by-side replay']);
  s += box(446, 236, 378, 56, 'core', 'Eval suites + replay corpus', ['standard · per client · logged loads']);
  s += box(900, 236, 350, 56, 'node', 'Production feedback', ['Hero overrides and bad no-ops become cases']);
  s += L(n, 'a', 350, 264, 446, 264) + AL(398, 256, 'triggers');
  s += L(n, 'a', 900, 264, 824, 264) + AL(862, 256, 'new cases');
  s += L(n, 'c', 528, 236, 528, 150) + AL(536, 200, 'run', 'start');
  s += L(n, 'c', 741, 236, 741, 150) + AL(749, 200, 'run', 'start');
  s += L(n, 'a', 1167, 150, 1167, 236) + AL(1175, 200, 'live traffic', 'start');
  return fig(n, 1280, 316,
    'Release pipeline for SOP edits: edit, lint, scenario evals, replay diff, approval, canary; model changes enter at the eval suites and production feedback adds cases',
    s,
    'An SOP edit cannot reach production without passing the same two gates a model change does: the scenario suites and a replay of recent real events. Approval is a review of changed behavior in plain terms. Rollback is a pointer change to the previous version.');
}

/* ---------------- Figure 6: memory types ---------------- */
function fig6() {
  const n = 6;
  let s = '';
  s += box(20, 40, 270, 76, 'store', 'Event log', ['what happened, in order', 'messages, pings, docs, actions'], { tag: 'EPISODIC' });
  s += box(20, 130, 270, 76, 'store', 'Obligation ledger', ['what is still owed', 'open requests, attempts, timer ids'], { tag: 'PROSPECTIVE' });
  s += box(20, 220, 270, 76, 'store', 'SOP store', ['how this client handles it', 'versioned Markdown'], { tag: 'PROCEDURAL' });
  s += box(20, 310, 270, 76, 'store', 'Load metadata', ['stops, contacts, PO, instructions', 'JSON from the TMS, at start'], { tag: 'REFERENCE' });
  // builder
  s += box(430, 120, 230, 180, 'core', 'Context builder', ['rules, no LLM', '', 'last N events + summary', 'open obligations', 'matching SOP sections', 'metadata fields'], { tag: 'WORKING' });
  s += L(n, 'a', 290, 78, 430, 160) + AL(300, 72, 'last N', 'start');
  s += L(n, 'a', 290, 168, 430, 190) + AL(300, 162, 'open items', 'start');
  s += L(n, 'a', 290, 258, 430, 240) + AL(300, 276, 'sections', 'start');
  s += L(n, 'a', 290, 348, 430, 270) + AL(300, 366, 'fields', 'start');
  s += box(430, 330, 230, 56, 'node', 'Rolling summary', ['derived from the log, rebuildable']);
  s += L(n, 'a', 545, 330, 545, 300) + AL(553, 320, 'older events', 'start');
  // agent
  s += L(n, 'c', 660, 210, 750, 210) + AL(705, 202, 'prompt');
  s += box(750, 140, 220, 140, 'core', 'Agent run', ['gate → resolve → decide', '', 'working memory exists', 'only for this run']);
  // writes
  s += L(n, 'c', 970, 210, 1030, 210) + AL(1000, 202, 'commit');
  s += box(1030, 120, 190, 180, 'node', 'Writes', ['append actions to log', 'update the ledger', 'set or cancel timers', '', { t: 'the next run reads these', c: 'm' }]);
  return fig(n, 1240, 410,
    'Four persistent stores feed a context builder that assembles working memory for one run; the run writes actions, ledger changes and timers back',
    s,
    'Three stores are persistent and each answers a different question: what happened (episodic), what is still owed (prospective), how to handle it (procedural). Working memory is assembled for one run and discarded. Nothing the agent learns is saved outside the log and the ledger.');
}

/* ---------------- Figure 7: multiple clients ---------------- */
function fig7() {
  const n = 7;
  let s = '';
  const lane = (y0, label, loadId, who, sopTxt, sopSub, memSub, destSub) => {
    let o = T(30, y0 - 8, label, 'h');
    o += R(20, y0, 1200, 172, 'ghost');
    o += box(40, y0 + 52, 160, 70, 'node', 'Load ' + loadId, [who, 'ping, SMS, timer']);
    o += box(240, y0 + 52, 170, 70, 'core', 'Queue partition', ['group = ' + loadId, 'FIFO, one run']);
    o += L(n, 'a', 200, y0 + 87, 240, y0 + 87) + AL(220, y0 + 79, 'event');
    o += L(n, 'c', 410, y0 + 87, 470, y0 + 87) + AL(440, y0 + 79, 'run');
    const ys = [y0 + 10, y0 + 64, y0 + 118];
    const rows = [[sopTxt, sopSub, 'store', 'reads'], ['Memory', memSub, 'store', 'state'], ['Destinations', destSub, 'node', 'sends']];
    rows.forEach((r, i) => {
      o += R(830, ys[i], 370, 44, r[2]) + T(840, ys[i] + 19, r[0], 't') + T(840, ys[i] + 35, r[1], 's');
      o += L(n, 'c', 770, ys[i] + 22, 830, ys[i] + 22) + AL(800, ys[i] + 14, r[3]);
    });
    return o;
  };
  s += lane(40, 'Client A · Northline', '481207', 'broker: northline', 'Client A SOP', 'client layer, pinned version', 'rows keyed by load_id 481207', 'Northline Slack, TMS, main email thread');
  s += lane(270, 'Client B', '77310', 'broker: client B', 'Client B SOP', 'client + reefer layers, pinned version', 'rows keyed by load_id 77310', 'Client B Slack, TMS, main email thread');
  s += R(470, 30, 300, 420, 'core') + T(486, 54, 'Shared worker pool (stateless)', 't');
  const items = [
    ['Resolve tenant', 'broker.id from the load metadata'],
    ['Standard SOP layer', 'shared, read-only, all clients'],
    ['Build context', 'only this load_id’s rows'],
    ['Model calls', 'per-client rate limit + cost meter'],
    ['Execute', 'destinations from the broker config'],
  ];
  items.forEach((it, i) => { s += step(486, 74 + i * 58, 268, 44, it[0], it[1]); });
  s += T(486, 392, 'Tenant comes from the load, never', 's') + T(486, 408, 'from message content. The model names', 's') + T(486, 424, 'a contact_id, not an address.', 's');
  return fig(n, 1240, 470,
    'Two clients share one stateless worker pool; each load has its own queue partition, memory rows, client SOP layer and destinations',
    s,
    'A run is bound to one load, and the load names its broker. Everything the run reads or sends is looked up from that broker. Only the worker code and the standard SOP layer are shared, so a change to the standard layer is replayed against every client before it ships.');
}

/* ---------------- Page ---------------- */
const ctx0713 = `event   sms.inbound  drv_20931  07:12
        "What's the PO number?"

ledger
  eta_request#1   status=open  attempts=1
    asked_at=07:01   timer=tm_0731
    sop=client_a/delay-risk-high  step 1 of 4

recent log
  07:00  ping  delay_risk low → high
  07:01  out   sms  "What time will you arrive
                     at the pickup?"
  07:12  in    sms  "What's the PO number?"

decision
  send sms "PO 55821"        (from load metadata)
  ledger unchanged           (a question, not an ETA)`;
const ctx0731 = `event   timer.due  tm_0731
        obligation=eta_request#1

pre-check (rules, no LLM)
  eta_request#1 status=open   → continue
  (if closed: no-op, cancel, stop)

ledger
  eta_request#1   status=open  attempts=1

recent log
  07:01  out  sms  "What time will you arrive..."
  07:12  in   sms  "What's the PO number?"
  07:13  out  sms  "PO 55821"

sop  client_a/delay-risk-high  step 2
  "send the SMS again"

decision
  send sms #2, set timer tm_0801, attempts=2`;

const body = `
<div class="page">
<header>
  <h1>Load Agent Architecture</h1>
  <p class="lede">How the agent follows a client’s SOP for one load, from the first location ping to the POD. Worked through for load #481207 (Client A): delay risk turns high at 07:00 and the agent escalates at 08:02.</p>
</header>

<section>
  <h2>System overview</h2>
  ${fig1()}
  <ul class="points">
    <li><strong>One event, one run.</strong> Each input is a typed event. The per-load queue allows one run at a time, so the agent never races itself on the same load.</li>
    <li><strong>No memory in the agent.</strong> State is the event log, the obligation ledger and the timer service. Each run rebuilds its context from them.</li>
    <li><strong>Procedures are data.</strong> Rules pick the SOP sections for an event. The model reads them and decides; it does not search for them.</li>
  </ul>
</section>

<section>
  <h2>Inside one run</h2>
  ${fig2()}
</section>

<section>
  <h2>How the SOP is chosen</h2>
  ${fig3()}
  <div class="proscons">
    <div><h3>Advantages</h3><p>Deterministic: the same event and state always pull the same sections, and the run log records which ones. Prompts stay small, which keeps cost and latency down. A client edit cannot leak into another client.</p></div>
    <div><h3>Disadvantages</h3><p>A situation with no tagged section finds nothing. The run then escalates to the Hero team and records an SOP gap for the authors. Authors must pick an event name for each section, which the editor constrains to a known list.</p></div>
  </div>
</section>

<section>
  <h2>Multiple clients</h2>
  ${fig7()}
</section>

<section>
  <h2>Memory types</h2>
  ${fig6()}
</section>

<section>
  <h2>Memory and timers</h2>
  ${fig4()}
  <div class="pair">
    <div>
      <h3>What the agent sees at 07:13</h3>
      <pre>${esc(ctx0713)}</pre>
    </div>
    <div>
      <h3>What the agent sees when the timer fires at 07:31</h3>
      <pre>${esc(ctx0731)}</pre>
    </div>
  </div>
</section>

<section>
  <h2>Changing SOPs and models safely</h2>
  ${fig5()}
</section>

<section>
  <h2>Budgets</h2>
  <p class="note">These are targets to enforce, not measurements. Replace them with measured values after the first week of traffic.</p>
  <div class="pair">
    <div class="tablewrap">
      <table>
        <caption>Latency ceiling per run</caption>
        <thead><tr><th>Step</th><th class="num">Ceiling</th></tr></thead>
        <tbody>
          <tr><td>Queue pickup + debounce</td><td class="num">15 s</td></tr>
          <tr><td>Load context</td><td class="num">1 s</td></tr>
          <tr><td>Gate</td><td class="num">5 s</td></tr>
          <tr><td>Resolve SOP</td><td class="num">&lt; 0.1 s</td></tr>
          <tr><td>Decide (2 tool rounds)</td><td class="num">45 s</td></tr>
          <tr><td>Validate + execute</td><td class="num">10 s</td></tr>
          <tr class="sum"><td>Sum of ceilings</td><td class="num">≈ 76 s</td></tr>
          <tr><td>Hard deadline, then escalate</td><td class="num">240 s</td></tr>
        </tbody>
      </table>
    </div>
    <div class="tablewrap">
      <table>
        <caption>LLM spend per load</caption>
        <thead><tr><th>Part</th><th class="num">Budget</th></tr></thead>
        <tbody>
          <tr><td>Gate, about 80 events, small model</td><td class="num">$0.10</td></tr>
          <tr><td>Decide, 15 to 20 runs, SOP in cached prefix</td><td class="num">$0.65</td></tr>
          <tr><td>Documents, 3 to 5 attachments</td><td class="num">$0.15</td></tr>
          <tr><td>Reserve for retries</td><td class="num">$0.10</td></tr>
          <tr class="sum"><td>Total</td><td class="num">$1.00</td></tr>
        </tbody>
      </table>
      <p class="note">A per-load meter reads the spend. At 80% the agent stops using the large model and escalates the remaining judgment calls.</p>
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
        <tr><td>Compile SOPs into a workflow engine</td><td>Deterministic, but editors would need an engineer for every change, and plain-English SOPs are ambiguous. We keep a small state machine for obligations only; the steps stay in English.</td></tr>
        <tr><td>One agent per stage or role</td><td>Each handoff adds a model call and drops context. Stage is already a retrieval key, so a separate agent adds latency and nothing else.</td></tr>
        <tr><td>Vector search over SOP text</td><td>A missed section means silent wrong behavior and cannot be asserted in a test. Kept as a fallback for events with no tagged section, never as the primary path.</td></tr>
      </tbody>
    </table>
  </div>
</section>
</div>`;

const css = `
/* Layout: one column of figures on a cool paper ground, wide diagrams scroll inside their frame */
:root {
  --bg: #F1F4F6;
  --surface: #FFFFFF;
  --fg: #14212B;
  --fg2: #4A5B68;
  --line: #B6C2CB;
  --accent: #0A6C8C;
  --accent-soft: #DCEEF4;
  --amber: #A65F00;
  --amber-soft: #FBEBD0;
  --store: #E5ECF0;
  --sans: 'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --cond: 'IBM Plex Sans Condensed', 'IBM Plex Sans', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0D151B; --surface: #15212A; --fg: #E3EAEF; --fg2: #9DB0BD; --line: #3A4C5A;
    --accent: #4FB8D8; --accent-soft: #12323F; --amber: #F0A74A; --amber-soft: #3A2A10; --store: #1B2A35;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0D151B; --surface: #15212A; --fg: #E3EAEF; --fg2: #9DB0BD; --line: #3A4C5A;
  --accent: #4FB8D8; --accent-soft: #12323F; --amber: #F0A74A; --amber-soft: #3A2A10; --store: #1B2A35;
  color-scheme: dark;
}
body { background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 15px; line-height: 1.6; }
.page {
  max-width: 1180px; margin: 0 auto; padding-block: 36px 72px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: flex; flex-direction: column; gap: 56px;
}
header { display: flex; flex-direction: column; gap: 10px; }
h1 { font-family: var(--cond); font-weight: 600; font-size: 38px; line-height: 1.1; margin: 0; text-wrap: balance; }
h2 { font-family: var(--cond); font-weight: 600; font-size: 24px; line-height: 1.2; margin: 0 0 16px; text-wrap: balance; }
h3 { font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--fg2); margin: 0 0 8px; }
p { margin: 0; }
.lede { max-width: 68ch; color: var(--fg2); font-size: 16px; }
section { min-width: 0; }
figure { margin: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 4px; padding: 16px; }
.scroll { overflow-x: auto; }
.scroll svg { display: block; width: 100%; min-width: 1000px; height: auto; color: var(--fg2); }
figcaption { margin-top: 12px; max-width: 80ch; font-size: 13.5px; color: var(--fg2); }
ul.points { list-style: none; margin: 20px 0 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; }
ul.points li { max-width: 46ch; }
.proscons, .pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 24px; margin-top: 24px; }
.proscons > div, .pair > div { min-width: 0; }
.proscons p { max-width: 60ch; }
pre { margin: 0; padding: 14px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 4px; font-family: var(--mono); font-size: 12px; line-height: 1.55; overflow-x: auto; color: var(--fg); }
.note { font-size: 13.5px; color: var(--fg2); max-width: 70ch; margin-bottom: 8px; }
.tablewrap { overflow-x: auto; min-width: 0; }
.tablewrap .note { margin-top: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
caption { text-align: left; font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--fg2); padding-bottom: 8px; }
th { text-align: left; font-size: 12px; font-weight: 600; color: var(--fg2); padding: 6px 10px; border-bottom: 1px solid var(--line); }
td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--mono); font-size: 13px; }
tr.sum td { font-weight: 600; border-top: 1px solid var(--fg2); }
table.alt td:first-child { width: 32%; font-weight: 500; }
table.alt { min-width: 640px; }
.pair table { min-width: 340px; }

/* diagram marks */
svg text { fill: var(--fg); font-family: var(--sans); }
svg .t { font-size: 13px; font-weight: 600; }
svg .s { font-size: 11.5px; fill: var(--fg2); }
svg .m { font-family: var(--mono); font-size: 10.5px; fill: var(--fg2); }
svg .mono { font-family: var(--mono); font-size: 12px; font-weight: 500; }
svg .h { font-size: 11px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase; fill: var(--fg2); }
svg .al { font-size: 11px; fill: var(--fg2); }
svg .tag { font-family: var(--mono); font-size: 9.5px; fill: var(--accent); letter-spacing: .04em; }
svg .node { fill: var(--surface); stroke: var(--line); stroke-width: 1.2; }
svg .given { fill: var(--surface); stroke: var(--fg2); stroke-width: 1.2; stroke-dasharray: 5 4; }
svg .ghost { fill: none; stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 4; }
svg .core { fill: var(--accent-soft); stroke: var(--accent); stroke-width: 1.4; }
svg .eff { fill: var(--surface); stroke: var(--accent); stroke-width: 2; }
svg .human { fill: var(--amber-soft); stroke: var(--amber); stroke-width: 1.4; }
svg .store { fill: var(--store); stroke: var(--line); stroke-width: 1.2; }
svg .flow { stroke: var(--fg2); stroke-width: 1.5; fill: none; }
svg .flow.c { stroke: var(--accent); }
svg .flow.h { stroke: var(--amber); }
svg .dash { stroke: var(--fg2); stroke-width: 1; stroke-dasharray: 4 4; }
svg .core ~ text.t { fill: var(--fg); }
@media (max-width: 520px) {
  h1 { font-size: 30px; }
  .page { gap: 44px; }
}
`;

const head = `<title>Load Agent Architecture</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>${css}</style>`;

const out = process.argv[2];
const repo = process.argv[3];
fs.writeFileSync(out, head + body);
fs.mkdirSync(require('path').dirname(repo), { recursive: true });
fs.writeFileSync(repo, `<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n${head}</head><body>${body}</body></html>\n`);
console.log('ok');
