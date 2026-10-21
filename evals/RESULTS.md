# Eval results

Live measurements of the current design: the run graph of `docs/architecture.md`, the
gate on `openai/gpt-4.1-nano` (OpenAI first, then Azure), the agent on
`openai/gpt-oss-120b` (DeepInfra at bf16 preferred, low reasoning effort) with
`anthropic/claude-haiku-5.5` and then `google/gemini-2.5-flash-lite` as its fallbacks, a
rate-limited call retried twice on the same model before any fallback, `extract` on
`google/gemini-2.5-flash`, and the prompt templates at the Hub commits tagged `prod`
(gate `5b488b4298d0`, agent `c6aeff6531a1`, extract `29aa43379513`, judge
`9c0319c9ac97`). SOP tree hash `65f7acfd60c9`.

Every number here comes from a LangSmith experiment: its rows (one per scenario
repetition), the evaluator scores, and the run records in each row's outputs (tokens,
cost, latency, model and provider of every model call). The links open in the LangSmith
workspace the experiments were run in; they need access to that workspace. To reproduce a
figure on your own keys, run the command next to it (see `README.md`).

## Short answer

| Suite | Scenarios | pass^3 | Trials passed | Cost of the round | Agent call p50 / p95 |
|---|---|---|---|---|---|
| `core` | 22 | 21/22 | 65/66 | $0.098 | 7.7 / 12.5 s |
| `agent` | 1 | 1/1 | 3/3 | $0.003 | 5.6 / 6.2 s |
| `docs` | 3 | 3/3 | 9/9 | $0.026 | 8.6 / 11.4 s |

Measured 2026-10-21 (UTC), `agent eval run --suite S --k 3`, git `ab483a4` (core) and
`35e5f09` (docs: the same agent, the provider read from OpenRouter's routing metadata, and
the SOP wording for a failed document check described under "What failed, and why"). The `agent` row is from an earlier k = 3 round (`agent-k3-bf9234d2`, git
`45e38fc`, the same prompts), which no rate limit touched.

DeepInfra rate limited `gpt-oss-120b` again during this round (138 HTTP 429 answers). The
retries absorbed them: no run escalated, 91 of the 99 rate-limited agent rounds were
answered by `gpt-oss-120b` itself, and Claude Haiku 5.5 answered the other 8. Gemini 2.5
Flash Lite served no round. The one failing scenario failed on rounds the primary model
served alone (below). That earlier round, with no retry and Gemini as the only fallback,
passed core 20/22 and docs 0/3, all failures but one on rate-limited rows
(`core-k3-fa90f50e`, `docs-k3-08a4b06e`).

Cost per load, from these calls: about **$0.04 for a typical load and $0.07 for a heavy
one**, against the $1 target ("Cost per load").

## The k = 3 round

| Experiment | Link |
|---|---|
| `core-k3-0feb7477` | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/1323a82c-f83a-4404-8872-65a907007dd8/compare?selectedSessions=ba66f58a-895e-4c13-bb49-da05113903ca) |
| `agent-k3-bf9234d2` (the earlier round) | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/cd482a91-71d0-44aa-ac65-7d1c717406a2/compare?selectedSessions=4ffc8d76-608a-42f9-83e9-7ac194892393) |
| `docs-k3-539fb656` | [compare view](https://smith.langchain.com/o/6ebea7ef-3343-4561-943b-9653a7ed572f/datasets/26ccb0ec-f35e-48be-90b3-21945808ed9e/compare?selectedSessions=68da8c88-6b6c-42f1-859a-95ae3c04d316) |

| Measure | core | agent | docs |
|---|---|---|---|
| Runs (acted / noop / skipped / escalated) | 129 / 15 / 15 / 0 | 3 / 0 / 0 / 0 | 21 / 6 / 0 / 0 |
| Gate calls, mean cost, p50 / p95 | 66, $0.000099, 0.91 / 1.24 s | 3, $0.000098, 0.99 / 1.07 s | 9, $0.000100, 1.04 / 1.99 s |
| Agent runs | 135 | 3 | 27 |
| Agent input tokens per run, mean / median (all rounds) | 16,169 / 17,411 | 11,102 / 11,093 | 16,762 / 19,298 |
| Agent output tokens per run, mean | 223 | 205 | 232 |
| Rounds per agent run, mean / max | 2.68 / 4 | 2.00 / 2 | 2.44 / 3 |
| Refused action calls per agent run, mean | 0.00 | 0.00 | 0.00 |
| Cost per model-calling run, mean | $0.00068 | $0.00054 | $0.00072 |
| Agent rounds served by a fallback model | 8 of 362 (Haiku) | 0 of 6 | 1 of 66 (Haiku) |
| Extract calls | | | 6: 1,812 input and 113 output tokens, $0.0008, 1.3 s each |

The skipped runs are the stale timers and duplicates the prefilter stops without a model
call; the noops are the acknowledgements and the "do nothing" cases. A latency is the
answered call's: a round that was rate limited also waited its backoff, about 1 to 3 s.
The billed cost (OpenRouter's `usage.cost` in each trace) equals the harness's figure:
$0.0981 for core, $0.0262 for docs.

### Rate limits, retries and providers

Every 429 came from DeepInfra (`"provider_error_code": "engine_overloaded"`,
`"limit_source": "upstream_provider_shared_pool"` in OpenRouter's error body), with no
`Retry-After`. Each retry is logged (`openai/gpt-oss-120b (agent): provider error (HTTP 429
from DeepInfra) ...; retry 1 of 2 in 0.8 s`) and each failed attempt is an errored model
run in the trace.

| Suite | 429 answers | Rate-limited rounds | Answered at the 1st retry | At the 2nd retry | Served by Claude Haiku 5.5 |
|---|---|---|---|---|---|
| core | 122 | 86 | 58 | 20 | 8 |
| docs (`docs-k3-b2bf27d1`, before the SOP wording change) | 16 | 13 | 10 | 3 | 0 |

OpenRouter's provider fallback (`allow_fallbacks: true`) absorbed none of them, and could
not: its routing metadata reports `available=1, selected=DeepInfra` for every agent round.
The other bf16 endpoints of `gpt-oss-120b` (DekaLLM, AkashML, Crusoe) take only
`tool_choice: auto`, and an agent round forces a tool call, so with `require_parameters`
DeepInfra is the only endpoint left. The gate's routing reports `available=2` (OpenAI and
Azure); OpenAI served all 9 docs gate calls. The core experiment ran before the adapter
read the provider, so its calls record none; at bf16 they could only be DeepInfra's.

### What failed, and why

| Scenario (trials) | What happened |
|---|---|
| `d2_keystone_reefer_departure` (1 of 3) | The departure run, served by `gpt-oss-120b` in all four rounds, tagged the SMS asking for the trailer temperature and the seal number with `requests: eta_request`, which opened an ETA request the scenario forbids. The `requests` description says to leave it out for such a question; the other two trials did. The row's one Haiku round was the last round of the next step, which passed. |
| `p2_pod_exceptions_needs_review` (fixed) | In the round before (`docs-k3-b2bf27d1`) it failed 3 of 3: the Hero task for the POD that needs review said only that the PO did not match, without the PO on the POD (55812), which the scenario requires. The agent's prompt showed `po_numbers: MISMATCH; expected "55821", found "55812"`, and the Acme `proof_of_delivery` section asked for "the values found and what does not match". Both SOP sections now ask for the value on the document and the value on the load of each failed check, with an example on other numbers (`sops/standard/documents.md`, `sops/clients/acme/proof_of_delivery.md`); it then passed 3 of 3 (`docs-k3-539fb656`). |

The remaining failure involves no rate limit and no fallback model. The assignment's scenario
(`a_client_a_high_risk`) passed 3 of 3; in the earlier round, its failing trial was the
fallback model tagging the 08:01 dispatcher email with `requests: eta_request` after both
requests were used. The `attempt_cap` refusal now tells the model that no further message
may carry that request and that the escalation email goes without `requests`.

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

From the k = 3 round: a gate call costs $0.000099, an agent run $0.00068 on average
(every round included; the core suite's mean, which already holds the Haiku rounds), an
extraction $0.0008. The volumes per load are the assignment's (50 to 100 inbound
messages) and a guess for the rest:

| Per load | Typical | Heavy |
|---|---|---|
| Inbound messages, one gate call each | 75: $0.0074 | 100: $0.0099 |
| Agent runs: messages the gate passes (40% / 60%), plus risk changes, timers and processed documents | 30 + 12 = 42: $0.029 | 60 + 17 = 77: $0.052 |
| Documents read | 4: $0.0032 | 5: $0.0040 |
| Location pings | hundreds, no model call | hundreds, no model call |
| **Total** | **about $0.04** | **about $0.07** |
| With the agent on Claude Haiku 5.5 (3.6 times the agent cost) | about $0.11 | about $0.20 |

The cost of a run is what the harness computes from the tokens and `config/models.yaml`
prices; it matched OpenRouter's billed `usage.cost` for both experiments of this round.
DeepInfra reports no cache reads for gpt-oss, so these figures have no caching discount.

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

This round cost $0.124 (core $0.098, docs $0.026, two single-call checks of the routing
metadata $0.00002); the docs round it replaced cost $0.025. The earlier round cost $0.146 (core $0.111, agent $0.003, docs $0.032)
and its single-scenario rerun $0.011. The cost cap of each run (`--cost-cap-usd`) bounds
what one experiment can spend.
