# v18 — Configuration diagnostics, Gemini thinking control, production logging

Three defects, one theme: the system was failing in ways that reported
success. Everything below either makes a silent failure loud, or removes the
cause of one.

---

## 1. `diagnose_config` reported PASS on a system that could not generate

The previous command had eleven checks and four blind spots. Each blind spot
is now a check.

| Blind spot | Consequence | New check |
|---|---|---|
| One role binding verified out of nine | `llm_generate` raises `LLMUnavailable` for an unbound role — it does not degrade. Eight unbound roles produced a green report and an empty profile. | **Profile role bindings** — all nine, plus mocked flags, inactive endpoints and cross-provider fallbacks |
| The API key was never read | Endpoint rows were counted; the environment was not consulted, in either process | **API credentials** |
| Only the advanced tier was resolved | Simple and Judgement could point at another vendor's inactive profile and nothing said so | **Simple / Judgement tier** chains |
| Model retirement was invisible | A model with a published end date in the catalog notes passed until the day it stopped answering | **Model retirement** |

New checks beyond those four:

- **Worker environment** — round-trips a task to Celery and compares the
  worker's code version and visible credentials against the web process. The
  worker reads the environment once at start and a deployment does not
  restart it; every other check in this command describes the web process
  only. `--no-worker-probe` skips it.
- **Output token ceilings** — see §3.
- **Thinking budget** — see §2.
- **Database migrations** — an unapplied migration produces impossible errors.
- **Endpoint timeout** — a search call runs for one to three minutes; the
  model default is 60s.
- **Prompt tier overrides** — moving a role to `advanced` switches web search
  on for every one of its calls.
- **Global tier rows** — duplicates or gaps in the NULL-tenant defaults.
- **Prompt library** — 28 rows expected.
- **Search capability** — `max_uses` unset, and the Gemini
  `responseSchema` + `google_search` conflict.

Also changed:

- Output is grouped (Runtime / AI provider / Model chain / Profile pipeline /
  Other modules) and the remediation list is ordered by impact with the exact
  commands to run.
- A check's declared severity is its **ceiling**. A `WARN` from a CRITICAL
  check now reports as HIGH rather than CRITICAL, so the ordering means
  something.
- An active endpoint with no key that **no call routes to** is a WARN, not a
  FAIL. One that a tier or binding does reach is still a FAIL.
- `--json`, `--scope profile`, `--strict` and `--probe-timeout` added.
- Investor database downgraded from a bare `[HIGH] FAIL` to a warning that
  states the scope — it affects investor discovery, not Company Profile —
  and explains that the corpus is a supplied workbook.

`_model_chain()` is kept as an alias for the advanced-tier resolution so
existing runbooks and tests keep working.

New task `fundos.llm.tasks.config_probe` reports the worker's view. It
carries credential env var **names and presence, never values**, because the
payload is logged.

---

## 2. Gemini reasons by default, and the thoughts were billed to the answer

Observed on a live UAT run: the deep-extract call returned
`completion_tokens=0`, `response_chars=215` and no searches, while logging as
a **successful** call. Four of five section blocks then recorded "the model
returned nothing for this block", and `readiness_summary` failed with
`JSONDecodeError: Unterminated string`.

On Gemini 2.5 and later, thinking is ON unless a budget says otherwise.
Thought tokens are deducted from `maxOutputTokens` but are **not** included in
`candidatesTokenCount`. A call can therefore spend its entire output
allowance thinking, return a truncated fragment, and report zero completion
tokens.

Three things made this unmanageable:

1. `thinking_budget` was absent from Gemini's entry in
   `PROVIDER_CAPABILITIES`, so no config profile could state a budget and
   `_run_gemini` never sent `thinkingConfig`. **Fixed** — the capability is
   declared, and `capabilities_payload()` now emits `{"budget_tokens": 0}`
   for a Gemini profile whose thinking mode is `none`. Silence is not "off"
   on this provider; it has to be said.
2. `NO_THINKING_ROLES` **dropped** the thinking key for extraction roles. On
   Anthropic and OpenAI absence means off; on Gemini it means the provider
   default, which is on — so the cost control had the opposite of its
   intended effect on the highest-volume roles. **Fixed** — the budget is
   pinned to zero for Gemini instead of removed.
3. `finishReason` was discarded, so truncation was indistinguishable from a
   model that had little to say. **Fixed** — `finish_reason` and
   `thoughts_tokenCount` are captured, the trace records both, and a
   truncated response is a `WARN` with the note *"response was CUT OFF at the
   output ceiling … raise max output tokens on the role binding, or lower the
   thinking budget on the config profile"*.

Two defensive details: `maxOutputTokens` is raised above a positive thinking
budget so there is room for an answer as well as the thoughts, and a model
that rejects `thinkingBudget` (the frontier Pro models cannot switch thinking
off) is retried once without the field rather than failing the call.

**Nothing changes for Anthropic or OpenAI** — for those providers a missing
thinking key still means off, and there is a test asserting it.

---

## 3. The output ceiling silently halved the dossier

`_dispatch` computes `min(binding.max_output_tokens, ROLE_MAX_OUTPUT_TOKENS)`,
and `LLMRoleBinding.max_output_tokens` defaults to **2048**. The role table
only ever lowers that value, so:

| Role | Designed | Binding default | Effective |
|---|---|---|---|
| `company_profile_deep_extract` | 8192 | 2048 | **2048** |
| `company_profile_judgment` | 4096 | 2048 | **2048** |

The dossier role was running at a quarter of its intended output budget. A
response cut off at the ceiling is not an error — it is parsed, repaired if
possible, and logged as a successful call with a thin result.

No code default was changed, because an admin may have set that value
deliberately. Instead the new **Output token ceilings** check reports any
profile role whose binding is below its designed cap, as a FAIL for the
essential roles, with the shell command to raise them.

---

## 4. `silk_generation.log` was never created outside development

`prod.py` replaced the base `LOGGING` dict wholesale. That removed the
`generation_file` handler and the four `fundos.*` logger definitions that
write to it. Every trace line still reached stdout, so logging looked
healthy while the file named throughout the runbooks did not exist.

`prod.py` now **extends** the base configuration: it overrides the console
formatter and the root logger, and leaves the handler and logger definitions
intact. Console output is byte-identical to before.

---

## Tests

`tests/api/test_gemini_thinking.py` — 12 new tests covering the capability
payload for all three providers, the wire body (`thinkingConfig` presence,
ceiling headroom, the 400 retry), `finishReason` and thought-token capture,
the `NO_THINKING_ROLES` reduction, the production logging configuration, and
the output-ceiling arithmetic.

`tests/api/test_configuration_integrity.py` — 20 new tests covering every new
diagnostic check, the JSON output shape, the exit code, and the guarantee
that the worker probe never returns a credential value.

Verification run: `test_configuration_integrity` 50 passed,
`test_gemini_thinking` 12 passed, and the `test_llm_*` modules pass unchanged.

**Pre-existing, unrelated:** six tests in `test_llm_cost_optimisation` fail
when `GEMINI_API_KEY` is present in the shell running the suite — seeding
then auto-selects Gemini while those tests assert the shipped Anthropic
defaults. This reproduces on the unmodified v17 tree. Run the suite with the
key unset, or fix the tests to pin their own provider.
