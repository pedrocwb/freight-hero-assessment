# Eval results: model bake-off and improvement rounds

Live runs of the core suite (`evals/scenarios/core`, 22 scenarios) on cheap OpenRouter
models, and the changes made between them. The plan's rule: start with cheap, light
models and improve the prompts, the schema and the tools on them before upgrading any
model. One change per round; every round is recorded here, the failed ones too.

**Outcome:** gate `openai/gpt-4.1-nano`, decide `openai/gpt-oss-120b` (DeepInfra, bf16,
low reasoning effort). The only cheap set that met every threshold; about $0.0002 per
agent run and about $0.02 to $0.03 of inference for a long load. Claude Haiku 5.5 also met
every threshold on decide, at about 5 times the cost per run.

These rounds measured the single-plan design (decide). Wave 3 replaced it with a tool loop
on the `agent` role; that role is measured again in "Wave 3: the agent loop" at the end.

## Selection thresholds (fixed before round 1)

A model set is chosen only if, on the full core suite at `--k 3`, live:

1. **A and B scenarios at pass^3 = 100%.** Every trial of every `a_*` and `b_*` scenario
   passes (the assignment's sequence and the "do nothing" cases).
2. **No required action dropped by the gate.** No trial in which a message that needs a
   decision ends as `noop` because the gate said `act: false` (an expected `acted` run that
   comes out `noop` with one model call).
3. **Cost well under $1 per load.** Average cost per model-calling agent run at most
   $0.0015, so a long load of 150 runs (100 messages, timers, risk changes) stays under
   about $0.25 of inference.

Among the sets that meet all three, the cheapest wins (measured cost, not list price);
latency breaks a tie. Other groups (C, D, E, G, capability) are reported and must not
regress between rounds, but they do not decide the selection.

Spend for this work was capped at USD 2.00 in total (live runs stop at USD 1.50). Every
round gives the run's cost and the running total of everything spent on live calls,
single-call probes included.

## How to read the tables

- Groups are the scenario letters: A (the assignment's sequence, 2 scenarios), B (do
  nothing, 5), C (ETA received, 5), D (other clients and SOP gaps, 3), E (adversarial and
  duplicates, 2), G (deferrals, 5). Each cell is scenarios passing every trial (pass^k).
  `reg` is the 20 regression scenarios, `cap` the 2 capability ones.
- Cost is what the harness computes from the tokens and the `pricing` of the model set.
  It matched OpenRouter's billed `usage.cost` on the probes.
- Latency is the decide call (all its tool rounds), p50 / p95.
- Gate: GPT-4.1 Nano (pinned to OpenAI) in every run. In the `haiku` set the gate's
  primary was Haiku, but every gate call fell back to Nano (see "Claude Haiku 5.5").
- "Gate drops" (threshold 2) was 0 in every run below.

## Root cause of the empty plans (round 0)

The orchestrator's smoke run failed at step 0: Gemini 2.5 Flash Lite returned
`{"actions": [], "rationale": "...send the driver an SMS... set a follow-up timer..."}`.
Probing the same request straight to OpenRouter (no LangChain) showed why: the `Plan`
answer tool carries the actions as `items: {anyOf: [10 action objects]}`, and through
OpenRouter's Google translation that union is dropped. The prompt token count gives it
away: 3,655 tokens with the union against 4,004 with the same actions as one flat object,
although the union schema is the longer one. The model sees an array of nothing, so it
can only answer `[]`. With a flat item (one object: `type` plus the fields of every
action) the same request returned the right two actions. GPT-4.1 Nano answered the union
correctly. Field order was not the cause: Gemini had already written the rationale first.

## Rounds

All on 2026-10-19. "Total" is the running spend after the row.

| # | Decide model (set) | Change before the run | Scope | Pass^k | A B C D E G | Cost | Total |
|---|---|---|---|---|---|---|---|
| 0 | gemini-2.5-flash-lite (default) | none: the orchestrator's smoke | A, k=1 | 0/1 | step 0 `actions: []` | $0.0009 | (before this card) |
| p | gemini, gpt-4.1-nano | probes of step A1 straight to OpenRouter | 3 calls | | see above | $0.0012 | $0.0012 |
| 1 | gemini (default) | **schema:** every discriminated union sent as one flat object, all models; the answer is narrowed back to its action's fields before validation | A, k=1 | 0/1 | steps 0-2 pass; at 08:01 it sets a third wait instead of escalating | $0.0018 | $0.0030 |
| 1b | gemini (default) | same | core, k=1 | 16/22 | 0/2 5/5 4/5 1/3 2/2 4/5 | $0.0134 | $0.0167 |
| 2 | gpt-4.1-nano (`nano`) | same | core, k=1 | 5/22 | 0/2 2/5 0/5 1/3 2/2 0/5 | $0.0094 | $0.0265 |
| | | **prompt (code review):** outside ids and attachment values JSON-quoted; a fired active timer shown as "follow-up due now" | | | | | |
| 3 | gpt-oss-120b (`gptoss`) | flat schema | core, k=1 | 20/22 | 0/2 5/5 5/5 3/3 2/2 5/5 | $0.0100 | $0.0365 |
| 4 | mistral-small-3.2 (`mistral`) | flat schema | core, k=1 | 5/22 | every decide call fell back to gpt-4.1-mini: Mistral's EU endpoint was filtered out | $0.0359 | $0.0724 |
| p | nano, mistral, qwen | probes: Nano fills every field of the flat item in one action ("SMS, then a timer" becomes one SMS); provider checks | 4 calls | | | $0.0015 | $0.0739 |
| | | **schema:** flat object for `google/` models only; every other model gets the anyOf union again. Mistral pinned to DeepInfra/Venice; the gpt-oss fallback changed to a model that takes `reasoning` | | | | | |
| 5 | nano (`nano`) | union | core, k=1 | 7/22 | 0/2 3/5 0/5 2/3 2/2 0/5 | $0.0090 | $0.0829 |
| 6 | gpt-oss-120b (`gptoss`) | union | core, k=1 | **22/22** | 2/2 5/5 5/5 3/3 2/2 5/5 | $0.0098 | $0.0927 |
| 7 | mistral-small-3.2 (`mistral`) | union | core, k=1 | 16/22 | 0/2 4/5 4/5 3/3 2/2 3/5 | $0.0238 | $0.1165 |
| 8 | qwen3-30b-a3b (`qwen`) | union | core, k=1 | 19/22 | 1/2 4/5 5/5 3/3 2/2 4/5 | $0.0757 | $0.1922 |
| 9 | qwen3-30b-a3b (`qwen`) | **tool:** `read_sop_section` offers only the sections not shown in full (Qwen re-read a shown section on nearly every run: 3 calls, ~15k tokens per decide) | core, k=1 | 4/22 | 0/2 2/5 0/5 1/3 1/2 0/5 | $0.0574 | $0.2496 |
| | | **reverted round 9:** refused reads left Qwen with empty plans | | | | | |
| 10 | gemini (default) | **schema:** `rationale` before `actions` in `Plan`, with descriptions tying the actions to it | core, k=1 | 18/22 | 1/2 5/5 5/5 1/3 2/2 4/5 | $0.0163 | $0.2659 |
| 10b | gpt-oss-120b (`gptoss`) | same | core, k=1 | 21/22 | 2/2 5/5 5/5 3/3 2/2 4/5 | $0.0099 | $0.2758 |
| p | claude-haiku-5.5 | probes: `tool_choice: "required"` has no Anthropic endpoint (404), `"auto"` works | 3 calls | | | $0.0015 | $0.2773 |
| 11 | gpt-oss-120b (`gptoss`) | none: **bake-off k=3** | core, k=3 | 21/22 | 2/2 5/5 5/5 3/3 2/2 4/5 (G4 0/3) | $0.0308 | $0.3081 |
| | | **adapter:** decide rounds use `tool_choice: "auto"` for `anthropic/` models | | | | | |
| 12 | claude-haiku-5.5 (`haiku`) | none | core, k=1 | 22/22 | 2/2 5/5 5/5 3/3 2/2 5/5 | $0.0455 | $0.3536 |
| 13 | gpt-oss-120b (`gptoss`) | **prompt:** "never defer on a timer event" (G4: on the 07:37 timer the model read "give me 10 more minutes" and proposed a second deferral, which the validator drops) | G4, k=3 | 1/1 | G4 3/3 | $0.0028 | $0.3564 |
| 14 | gpt-oss-120b (`gptoss`) | none: **bake-off k=3** | core, k=3 | 21/22 | 2/2 4/5 5/5 3/3 2/2 5/5 (B1 2/3: the gate said act true for "ok") | $0.0298 | |
| 15 | claude-haiku-5.5 (`haiku`) | none: **bake-off k=3** (ran next to 14) | core, k=3 | **22/22** | 2/2 5/5 5/5 3/3 2/2 5/5 | $0.1398 | $0.5260 |
| 16 | gpt-oss-120b (`gptoss`) | **gate prompt:** "an acknowledgement is always act false, whatever is open" | B1, B2, B-ack, k=10 | 3/3 | 30/30 trials | $0.0065 | $0.5325 |
| 17 | gpt-oss-120b (`gptoss`) | none: **final bake-off k=3** on the final prompts | core, k=3 | **22/22** | 2/2 5/5 5/5 3/3 2/2 5/5 | $0.0298 | $0.5623 |
| 17b | gpt-oss-120b (`gptoss`) | none: A and B again at k=3, started when round 17 was thought lost (it was not) | A, B, k=3 | 7/7 | 2/2 5/5 | $0.0088 | $0.5711 |

Classification of the failures that drove each change:

- Round 1 (schema): Gemini's empty plans, every one of them.
- Round 5 (schema): GPT models given the flat item filled every field in one item
  (probe), and gpt-oss put an email's text in `text` instead of `body`.
- Round 9 (tool): token cost. Reverted: worse for the model it was meant to help.
- Round 10 (schema): plans whose rationale named actions that the action list left out
  (Gemini, Nano, Mistral). It helped Gemini (16 to 18 of 22).
- Round 13 (prompt): G4, gpt-oss, 3 of 3 trials.
- Round 16 (gate threshold): B1, the gate, 1 of 3 trials. Nothing was sent, but decide
  ran for a bare "ok", which B1 forbids.

## Bake-off

k=1 screening of every candidate on the same prompts (rounds 5 to 8 and 10, before the
timer and gate rules), then k=3 on the best two (rounds 14 to 17).

| Decide model | Endpoint | k | Pass^k | A | B | C | D | E | G | Cost / scenario | Cost / run | Decide p50 / p95 | Thresholds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-4.1-nano | OpenAI | 1 | 7/22 | 0/2 | 3/5 | 0/5 | 2/3 | 2/2 | 0/5 | $0.0004 | $0.00021 | 1.1 / 1.4 s | no (A, B) |
| gemini-2.5-flash-lite | Google AI Studio | 1 | 18/22 | 1/2 | 5/5 | 5/5 | 1/3 | 2/2 | 4/5 | $0.0007 | $0.00034 | 1.1 / 1.6 s | no (A) |
| mistral-small-3.2-24b | DeepInfra / Venice, fp8 | 1 | 16/22 | 0/2 | 4/5 | 4/5 | 3/3 | 2/2 | 3/5 | $0.0011 | $0.00052 | 3.2 / 5.2 s | no (A, B) |
| qwen3-30b-a3b-2507 | Nebius, fp8 | 1 | 19/22 | 1/2 | 4/5 | 5/5 | 3/3 | 2/2 | 4/5 | $0.0034 | $0.00158 | 4.9 / 14.9 s | no (A, B, cost) |
| **gpt-oss-120b** | DeepInfra, bf16 | 3 | **22/22** | 2/2 | 5/5 | 5/5 | 3/3 | 2/2 | 5/5 | $0.0005 | $0.00021 | 4.7 / 8.2 s | **yes** |
| claude-haiku-5.5 | Anthropic | 3 | 22/22 | 2/2 | 5/5 | 5/5 | 3/3 | 2/2 | 5/5 | $0.0021 | $0.00097 | 2.7 / 4.6 s | yes |

"Cost / run" is per agent run that called a model (gate, decide or both). Per call: Nano
gate $0.00010, gpt-oss decide $0.00017, Haiku decide $0.00100 (about 7,400 input tokens
per call against 4,400 for the others, since Anthropic counts the same prompt and tools as
more tokens, and about 500 output tokens including its reasoning).

## Choice

Gate `openai/gpt-4.1-nano`, decide `openai/gpt-oss-120b` (`config/models.yaml`). Both
gpt-oss and Haiku meet the thresholds; gpt-oss is the cheaper by about 5 times per run, so
the rule picks it. Its decide call is slower (p50 about 5 s against 2.7 s), well inside
the 5-minute budget of a run.

Cost of a long load (100 inbound messages, 80 decide calls between messages, timers and
risk changes): gate 100 x $0.00010 + decide 80 x $0.00017, about **$0.024**. The same load
with Haiku on decide (gate still on Nano): 100 x $0.00010 + 80 x $0.00100, about
**$0.09**. Both are far under the $1 per load target.

### Claude Haiku 5.5

`anthropic/claude-haiku-5.5`, from the public catalog on 2026-10-19: USD 0.10 / 1M input,
0.50 output, 0.01 cache read, 1,000,000-token context, no `temperature`. Its set is
`config/models.haiku.yaml`.

- Decide: 22/22 at k=3, no failure in any group, before the timer and gate rules that
  gpt-oss needed. 3 of its 129 decide calls fell back to gpt-4.1-mini (a forced final call
  that no Anthropic endpoint accepts, below); the scenarios still passed.
- Gate: not measured. With `require_parameters` on, OpenRouter has no Anthropic endpoint
  for a forced `tool_choice` (a named tool or `"required"`, both 404), and the gate is one
  forced call, so every gate call fell back to Nano.
- Worth it for decide only? Not by this rule: on this suite gpt-oss reaches the same
  A and B results for a fifth of the price. Haiku is the better-behaved model (no fixes
  needed, half the latency), so it is the first candidate if gpt-oss regresses on new
  scenarios, and at about $0.09 per long load it fits the budget. Using it in production
  needs the forced-call issue fixed first (see the card report).

## Cassettes and replay

Recorded once, after every prompt and schema change, with the chosen models:
`uv run agent eval run --suite core --mode record --cost-cap-usd 0.20` passed 22/22 and
cost $0.0101 (running total $0.5812). The 48 files are in `evals/cassettes/`
(`openai-gpt-4.1-nano/` for the gate, `openai-gpt-oss-120b/` for decide); none holds a
key. `uv run agent eval run --suite core --mode replay` then passed 22/22 offline, with
no `.env` and no key in the environment.

A cassette key hashes the prompt, the tools and the schema, so any change to
`application/prompts.py`, the SOP text, the scenarios or `domain/actions.py` needs a new
recording (one run, about a cent).

**Total spend for this work: $0.5812** of the USD 2.00 cap (the orchestrator's $0.0009
smoke run not included).

## Wave 3: the agent loop

Measured on 2026-10-20 (branch `wave3`). The `decide` role became `agent`, one model call
that runs a tool loop inside the guardrails (read tools, one action tool per action type,
each action checked by the rules at the call; `docs/answers.md` 1.1). Three suites ran:
core (22 scenarios), agent (`t2_gap_hero_without_promise`; see the note on the guardrail
controls below) and docs (`p1_pod_clean_delivers`, `p2_pod_exceptions_needs_review`,
`p3_pod_overdue_follow_up`). Every run is a LangSmith experiment on the dataset of its
suite (`--langsmith`).

### Candidates

| Set | Agent model | Endpoint | Gate | Why it is a candidate |
|---|---|---|---|---|
| `gptoss` | `openai/gpt-oss-120b` | DeepInfra, bf16, low reasoning effort | GPT-4.1 Nano | The wave 2 winner on the single plan |
| `haiku` | `anthropic/claude-haiku-5.5` | Anthropic | GPT-4.1 Nano | Passed 22/22 at k=3 in wave 2 with no fix; the first upgrade |
| `gemini` | `google/gemini-2.5-flash` | Google | GPT-4.1 Nano | A stronger tool user than Flash Lite, at a price between the two |

Only the agent role differs between the sets (`config/models.NAME.yaml`); the gate, the
judge, `extract` and `compile` are the default set's. Prices were checked against
OpenRouter's catalog on 2026-10-20 before the runs.

### Selection thresholds (fixed before round 1)

A set is chosen only if, live, at `--k 3`:

1. **A and B scenarios at pass^3 = 100%** (as in wave 2).
2. **No required action dropped by the gate** (as in wave 2).
3. **Cost well under $1 per load:** average cost per model-calling run of the core suite at
   most $0.0015 (as in wave 2), so a long load of 150 runs stays under about $0.25.
4. **Few refused tool calls:** on average at most 0.5 refused action calls per agent run
   (`RunRecord.rejections`), over every suite.
5. **The loop handles a missing procedure:** first written as "`t1_gap_search_then_hero`
   calls `search_sop` in every trial". **Revised after round 1**, see below.

Among the sets that meet all five, the cheapest wins (measured cost); latency breaks a tie.

**What changed after round 1, and why.** The k=1 screening failed the whole agent suite
(n1, n2, t1) for all three models, and for the same reason: the models behaved well. `n1`
and `n2` script a gullible or confused agent (an email to an outside address, four
refused actions) to prove the rules refuse them at the call; a live model never made those
calls (each model handed the outside-email request to a Hero instead). `t1` required the
path search → read → "no procedure", but the `escalation` section names no intent, so it
is always in the prompt in full: no model needed to search, and each handed the case to a
Hero. So:

- `n1`, `n2` and `t1` are tagged `scripted_only`: guardrail controls that run in scripted
  mode (and in CI); live, record and replay skip them with a warning
  (`evalkit/suite.py`). Injection resistance of a live model stays measured by
  `e1_prompt_injection`.
- `t2_gap_hero_without_promise` judges the outcome of a gap in any mode: one Hero task
  with the driver's words, no SMS or email, and the judge's rubric (min 0.7). Threshold 5
  became "t2 passes in every trial".
- The screening also showed `requests` (the obligation an SMS asks for) being filled with
  its only enum value on an SMS that asked for a trailer temperature (d2, gpt-oss): its
  description now says when to leave it out. A prompt fix, applied before the k=3 round.

### Rounds

| # | Agent model (set) | Change before the run | Scope | Pass^k | A B C D E G | agent | docs | Cost | Total |
|---|---|---|---|---|---|---|---|---|---|
| 1 | gpt-oss-120b (`gptoss`) | none (prompts as merged) | 3 suites, k=1 | 21/22 | 2/2 5/5 5/5 2/3 2/2 5/5 | 0/3 | 3/3 | $0.0438 | $0.0438 |
| 1 | claude-haiku-5.5 (`haiku`) | same | 3 suites, k=1 | 22/22 | 2/2 5/5 5/5 3/3 2/2 5/5 | 0/3 | 2/3 | $0.1429 | $0.1867 |
| 1 | gemini-2.5-flash (`gemini`) | same | 3 suites, k=1 | 21/22 | 1/2 5/5 5/5 3/3 2/2 5/5 | 0/3 | 2/3 | $0.2156 | $0.4023 |
| 2 | gpt-oss-120b (`gptoss`) | `requests` described; `scripted_only`; t2 | 3 suites, k=3 | 21/22 | 2/2 5/5 5/5 2/3 2/2 5/5 | 1/1 | 2/3 | $0.1241 | $0.5264 |
| 2 | claude-haiku-5.5 (`haiku`) | same | 3 suites, k=3 | 22/22 | 2/2 5/5 5/5 3/3 2/2 5/5 | 1/1 | 2/3 | $0.4316 | $0.9580 |

Gemini was dropped after round 1: it missed a group A scenario (the dispatcher email at
08:01 in `a_question_during_last_wait`) and was the most expensive per run.

### Bake-off (round 2, k=3)

| Agent model | Core pass^3 | A | B | C | D | E | G | Agent (t2) | Docs | Mean refusals per run | Cost / run | Agent p50 / p95 | Thresholds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **gpt-oss-120b** | 21/22 | 2/2 | 5/5 | 5/5 | 2/3 | 2/2 | 5/5 | 3/3 | 2/3 | 0.00 core, 0.04 docs | **$0.00066** | 5.1 / 9.6 s | **all five** |
| claude-haiku-5.5 | 22/22 | 2/2 | 5/5 | 5/5 | 3/3 | 2/2 | 5/5 | 3/3 | 2/3 | 0.01 core, 0.00 docs | $0.00241 | 4.1 / 5.9 s | fails 3 (cost) |

"Cost / run" is per agent run that called a model (gate, agent or both), with every round
of the loop, from `RunRecord.usage`. Latency is the agent call, all its rounds.

Failures left, both intermittent:
- `d2_keystone_reefer_departure` (gpt-oss, 1 of 3 trials): an SMS asking for the trailer
  temperature and seal still opened an `eta_request` obligation once after the fix.
- `p3_pod_overdue_follow_up` (both models, 1 of 3 trials each): after the POD reminder
  SMS the agent did not schedule the next follow-up timer, so the Hero task 30 minutes
  later never came. Both models missing it once points at the Acme POD section or the
  scenario wording rather than a model; it is the first thing to improve.

### Choice

**Agent: `openai/gpt-oss-120b`** (DeepInfra, bf16), the only set that meets the five
thresholds, at about a third of Haiku's cost per run. `config/models.yaml` already names
it, so nothing changed there. Claude Haiku 5.5 stays the first upgrade: 22/22 and faster,
at 3.6 times the cost. Measured on the loop, a model-calling run costs about $0.0007, so a
typical load of about 45 such runs costs about $0.03, as estimated for the single plan
(`docs/answers.md` 5.3). The loop reads more: an agent call (all its rounds) averaged
16,636 input tokens in round 2 (median 17,978), against a measured median of 4,720 for the
single-plan decide call; the gate's call averaged 892. DeepInfra reported no cache reads
for gpt-oss, so the margin comes from its input price ($0.037 per million tokens), not
from caching.

Spend of this section: $0.958 (round 1 $0.402, round 2 $0.556) plus the smoke run of
load 481207 ($0.003) and the cassette recording below. The `extract` role stayed
`google/gemini-2.5-flash` (the docs suite's extractions passed in both rounds).
