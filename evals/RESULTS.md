# Eval results

Live measurements of the current design: the run graph of `docs/architecture.md`, the
gate on `openai/gpt-4.1-nano`, the agent on `openai/gpt-oss-120b` (DeepInfra, bf16, low
reasoning effort) with `google/gemini-2.5-flash-lite` as its fallback, `extract` on
`google/gemini-2.5-flash`, and the prompt templates at the Hub commits tagged `prod`
(gate `5b488b4298d0`, agent `c6aeff6531a1`, extract `29aa43379513`, judge
`9c0319c9ac97`). SOP tree hash `65f7acfd60c9`.

Every number here comes from a LangSmith experiment: its rows (one per scenario
repetition), the evaluator scores, and the run records in each row's outputs (tokens,
cost and latency of every model call). The links open in the LangSmith workspace the
experiments were run in; they need access to that workspace. To reproduce a figure on
your own keys, run the command next to it (see `README.md`).

## Short answer

| Suite | Scenarios | pass^3 | Trials passed | Cost of the round | Agent call p50 / p95 |
|---|---|---|---|---|---|
| `core` | 22 | 20/22 | 64/66 | $0.111 | 7.0 / 14.6 s |
| `agent` | 1 | 1/1 | 3/3 | $0.003 | 5.6 / 6.2 s |
| `docs` | 3 | 0/3 | 3/9 | $0.032 | 10.2 / 17.2 s |

Measured 2026-10-21 (UTC), `agent eval run --suite S --k 3`, git `860577d` (core) and
`45e38fc` (agent, docs; the same code). The round ran into provider rate limits (HTTP 429
on `gpt-oss-120b`, and in the docs suite on its fallback too). Every core row that the
primary model served alone passed (43 of 43); both core failures, and 5 of the 6 docs
failures, came from rows where the rate limit moved agent rounds to the fallback model or,
with the fallback limited too, escalated the run to a Hero. The same prompts at k = 1,
with no rate limit, passed the core suite 22/22. Details below.

Cost per load, from these calls: about **$0.04 for a typical load and $0.07 for a heavy
one**, against the $1 target ("Cost per load").

## The k = 3 round

| Experiment | Link |
|---|---|
| `core-k3-fa90f50e` | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/1323a82c-f83a-4404-8872-65a907007dd8/compare?selectedSessions=b477e742-d442-490a-b9e9-0a1e364e0988) |
| `agent-k3-bf9234d2` | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/cd482a91-71d0-44aa-ac65-7d1c717406a2/compare?selectedSessions=4ffc8d76-608a-42f9-83e9-7ac194892393) |
| `docs-k3-08a4b06e` | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/26ccb0ec-f35e-48be-90b3-21945808ed9e/compare?selectedSessions=64bde2e4-717a-40ce-9c1a-6dac19147a34) |

| Measure | core | agent | docs |
|---|---|---|---|
| Runs (acted / noop / skipped / escalated) | 129 / 15 / 15 / 0 | 3 / 0 / 0 / 0 | 15 / 5 / 0 / 6 |
| Gate calls, mean cost, p50 / p95 | 66, $0.000099, 0.88 / 1.27 s | 3, $0.000098, 0.99 / 1.07 s | 9, $0.000100, 0.92 / 6.90 s |
| Agent runs | 135 | 3 | 23 |
| Agent input tokens per run, mean / median (all rounds) | 15,454 / 17,411 | 11,102 / 11,093 | 16,166 / 16,390 |
| Agent output tokens per run, mean | 449 | 205 | 660 |
| Rounds per agent run, mean / max | 2.53 / 8 | 2.00 / 2 | 2.30 / 5 |
| Refused action calls per agent run, mean | 0.07 | 0.00 | 0.09 |
| Cost per model-calling run, mean | $0.00077 | $0.00054 | $0.00107 |
| Agent rounds served by the fallback | 54 of 342 | 0 of 6 | 16 of 53 |
| Extract calls | | | 6: 1,810 input and 115 output tokens, $0.0008, 1.3 s each |

The skipped runs are the stale timers and duplicates the prefilter stops without a model
call; the noops are the acknowledgements and the "do nothing" cases.

### What failed, and why

The adapter sends each model call once and moves to the next model of the role on any
error (`adapters/llm/resilient.py`; retries belong to the chain, not to one model). A
429 on the primary therefore hands that round, and possibly the next ones of the same
loop, to `gemini-2.5-flash-lite`, the role's fallback. That model is weaker on the loop:

| Scenario (trial) | What happened |
|---|---|
| `a_client_a_high_risk` (1 of 3) | The 07:01, 07:31 and 08:01 runs were served by the fallback. At 08:01 it staged the dispatcher email with `requests: eta_request`; `attempt_cap` refused it (both requests were used), and the model created the Hero task and closed the request without staging the email again. The scenario expects the email. |
| `g2_deferral_expires` (1 of 3) | The 07:37 follow-up run alternated between the two models for 8 rounds (the `max_steps` budget) and staged both the second ETA request and the escalation (email and two Hero tasks) in one run. The rules refused the duplicates, not the early escalation. |
| `p1`, `p3`, `p2` (5 trials) | The agent call failed on both models with 429. Each run escalated with "Agent failed on an event: handle it by hand", as designed, so the scenario's expected actions never happened. |
| `p2_pod_exceptions_needs_review` (1 trial, primary only) | The Hero task for the POD that needs review did not quote the PO found on the POD (55812), which the scenario requires. The same check failed in 1 of 3 trials of `docs-k1-234da102`. |

What this shows: the guardrails held in every failure (no message to a stranger, no
request over the cap, every model outage reached a Hero), but a mixed-model loop is less
reliable than the primary alone. Two changes follow from it, not made here: retry a 429
on the same model with a short backoff before falling back, and give the agent role a
fallback that passes the suite (Claude Haiku 5.5 did, below). The `p2` wording is a prompt
or SOP fix: the Acme `proof_of_delivery` section can ask the Hero task to quote each
failed check's expected and found values.

### The same prompts at k = 1

| Experiment | Suite | pass^1 | Cost | Agent call p50 / p95 | Fallback rounds |
|---|---|---|---|---|---|
| [`core-k1-c345964f`](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/d736f122-fcbb-457a-b25e-e83f110c96d1) | core | 22/22 | $0.031 | 6.8 / 10.2 s | 0 of 122 |
| [`agent-k1-c32638d2`](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/827dda88-6942-425d-904a-7a9e5b2a31f7) | agent | 1/1 | $0.001 | 11.3 s | 0 |
| [`docs-k1-234da102`](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/b8a96b8c-ad5a-49dd-865d-0f51f0caecc0) | docs | 2/3 | $0.008 | 13.4 / 25.9 s | 0 |

`core-k1-c345964f` ran the prompt commits above (git `65a841b`). The agent and docs
rows ran git `c0bb69a`; their metadata records prompt content hashes, not Hub commits, so
they are not tied to the commits above.

## Cost per load

From the k = 3 round: a gate call costs $0.000099, an agent run $0.00077 on average
(every round included; the core suite's mean, which already holds the fallback's
rounds), an extraction $0.0008. The volumes per load are the assignment's (50 to 100
inbound messages) and a guess for the rest:

| Per load | Typical | Heavy |
|---|---|---|
| Inbound messages, one gate call each | 75: $0.0074 | 100: $0.0099 |
| Agent runs: messages the gate passes (40% / 60%), plus risk changes, timers and processed documents | 30 + 12 = 42: $0.032 | 60 + 17 = 77: $0.059 |
| Documents read | 4: $0.0032 | 5: $0.0040 |
| Location pings | hundreds, no model call | hundreds, no model call |
| **Total** | **about $0.04** | **about $0.07** |
| With the agent on Claude Haiku 5.5 (3.6 times the agent cost) | about $0.13 | about $0.23 |

The cost of a run is what the harness computes from the tokens and `config/models.yaml`
prices; it matched OpenRouter's billed `usage.cost` when checked. DeepInfra reports no
cache reads for gpt-oss, so these figures have no caching discount.

## How the agent model was chosen

A bake-off: each candidate ran the three suites live on the same prompts, at k = 1 to
screen, then k = 3 on the best two. The thresholds were fixed before the first run:

1. The assignment's scenarios (group A) and the "do nothing" scenarios (group B) at
   pass^3 = 100%.
2. No required action dropped by the gate: no `must_act` message ends as `noop`.
3. At most $0.0015 per model-calling run on the core suite, so a long load of 150 runs
   stays under about $0.25.
4. At most 0.5 refused action calls per agent run, over every suite.
5. The SOP-gap scenario (`t2_gap_hero_without_promise`) passes in every trial.

The cheapest set that meets all five wins; latency breaks a tie. Only the agent role
differed between the sets; the gate was GPT-4.1 Nano in every run and dropped no required
action.

| Agent model | Core pass^3 | A | B | Agent (t2) | Docs | Refusals per run | Cost per run | Agent p50 / p95 | Thresholds |
|---|---|---|---|---|---|---|---|---|---|
| **openai/gpt-oss-120b** | 21/22 | 2/2 | 5/5 | 3/3 | 2/3 | 0.00 core, 0.04 docs | **$0.00066** | 5.1 / 9.6 s | **all five** |
| anthropic/claude-haiku-5.5 | 22/22 | 2/2 | 5/5 | 3/3 | 2/3 | 0.01 core, 0.00 docs | $0.00241 | 4.1 / 5.9 s | fails 3 (cost) |
| google/gemini-2.5-flash (k = 1) | 21/22 | 1/2 | 5/5 | | | | highest of the three | | fails 1 |

Experiments: [gpt-oss core k = 3](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/7f51c6ff-9c24-4cd0-b693-23758e30e2e2),
[Haiku core k = 3](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/f97cb582-698f-4752-90f3-0a785fa04751),
[Gemini core k = 1](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/projects/p/3a531045-d19c-4845-a902-523a2433c347).
The core suite cost $0.095 at k = 3 on gpt-oss and $0.347 on Haiku.

**Choice:** the agent runs `openai/gpt-oss-120b`, the only model that met the five
thresholds. Claude Haiku 5.5 is the first upgrade: 22/22, faster, about 3.6 times the
cost per run, still far under $1 per load. One difference to know: OpenRouter has no
Anthropic endpoint that takes `tool_choice: required`, so the adapter sends `auto` on
Anthropic agent rounds (`adapters/llm/openrouter.py`), and the graph asks once more for
the answer when the model replies in prose.

Four cheaper models (GPT-4.1 Nano, Gemini 2.5 Flash Lite, Mistral Small 3.2, Qwen3 30B
A3B) were screened at k = 1 against the same thresholds with a one-call planner in place
of the tool loop; none passed group A.

## Spend

The k = 3 round above cost $0.146 (core $0.111, agent $0.003, docs $0.032). The cost
cap of each run (`--cost-cap-usd`) bounds what one experiment can spend.
