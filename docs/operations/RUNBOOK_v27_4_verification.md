# RUNBOOK — v27.4 live verification

Everything in v27.4 was verified against 635 passing tests and a mocked
end-to-end run. **Two things cannot be verified without a live provider call**,
and both are on the critical path. This is the runbook for them.

> **Security note first.** If the `GOOGLE_AI_API_KEY` used for this was ever
> pasted into a chat, an issue tracker, a shared document or a commit, treat it
> as compromised: Google AI Studio → API keys → delete and regenerate. Use an
> environment variable, never a literal in a file.

---

## Step 0 — Preconditions

```bash
export GEMINI_API_KEY='…'          # the adapter reads this name
export GOOGLE_AI_API_KEY="$GEMINI_API_KEY"

python manage.py migrate
python manage.py seed_platform_config
python manage.py seed_llm_costs     # or every cost line reads 0.00
python manage.py diagnose_config
```

`diagnose_config` must show the advanced tier resolving with a **non-zero
thinking budget**. If it reports `thinking budget is 0 on a searching tier`,
stop — `google_search` is model-decided and a model with no deliberation budget
has no step in which to choose to call it. Nothing below will produce a search.

Use **`gemini-3.5-flash` or `gemini-3.6-flash`** only. Note that the two are
priced differently in the seeded book ($9.00 vs $7.50 per M output), so fix one
model before comparing any cost figures across runs.

---

## Step 1 — Settle H1: does `responseSchema` suppress `google_search`?

This is the highest-value unknown in the codebase. Two halves of the repo
asserted **opposite** answers for six releases — `diagnose_config` called the
combination rejected, the adapter's warning told operators it was supported —
and both were written from reasoning, neither from a measurement.

```bash
python tools/diagnose_gemini_search.py --model gemini-3.5-flash
```

Eight calls across `{schema on/off} × {thinking 0/2048} × {directive
before/after}`, reading `groundingMetadata.webSearchQueries` — the same signal
`adapter._count_searches()` uses, so the result maps directly onto production.

### Reading the result

| Outcome | What it means | Action |
|---|---|---|
| **No cell searches** | Neither variable is responsible; something more basic is wrong | Check the tool is on the wire at all — `caps_sent` on the trace line |
| **Every cell searches** | The v27.4 tier contract alone fixed it | Record it in `TIER_CONTRACT`; no further change |
| **Splits on `thinking`** | Confirms the model-decided hypothesis | Raise the advanced floor above 2,048 if the searching rate is still low |
| **Splits on `schema`** | H1 confirmed — the schema suppresses the tool | Add `structured_output: False` to the advanced contract and **split the deep extract into a search pass and a structuring pass** |
| **Splits on `position`** | The tail reminder is load-bearing | Keep it; consider moving the full directive |

Whatever the answer, **write it into `fundos/llm/tier_contract.py`** next to
`TIER_CONTRACT`, with the date and the model string. The point of running this
is not to fix this release — it is to stop the question being re-derived from
reasoning a seventh time.

---

## Step 2 — One live generation

```bash
# Admin → Application Configuration → AI mocked = OFF
python manage.py generate_profile <company-id> --force   # or trigger from the UI
```

Pick a company whose site is **over ~30,000 characters** — that is what
exercises the compression reporting. MediBuddy is the known case.

### What to grep for, in order

```bash
grep -E 'run.economics|sources.compression|fanout_trim|CONTRACT_VIOLATION' \
     var/logs/silk_generation.log | tail -40
```

**1. `run.economics` — the one line that answers most questions**

```
run.economics status="OK" mode="consolidated" llm_calls=11
  prompt_tokens=… completion_tokens=… thinking_tokens=…
  billed_output_tokens=… total_tokens=… cost_inr=…
  searches=6 searches_unknown=0 redundant_input_pct=44.4 concerns="none"
```

| Field | Pass condition | If it fails |
|---|---|---|
| `searches` | **> 0** | The release did not achieve its objective. Go back to Step 1 |
| `searches_unknown` | `0` | The provider returned no grounding metadata — a different fault from zero searches, needing the opposite fix |
| `llm_calls` | matches the count of `llm.call.*` lines | The window is wrong; file it |
| `thinking_tokens` | > 0 on advanced/judgment | The contract is not reaching the wire |
| `cost_inr` | non-zero | Price book not seeded — every spend number is zero |
| `concerns` | `none` | Read it; it names what it found |

**Success is `searches > 0`, not the absence of a warning.** That distinction
is what six releases of healthy-looking logs got wrong.

**2. `run.economics.role` — where the money goes**

Sort mentally by `tokens_in_per_token_out`. Anything above ~40:1 is re-reading
a corpus to produce a paragraph. On 17 Aug `company_profile_records` sat at
**58.8:1** across four calls. That is C-03, and this number is how you will
know when fixing it worked.

**3. `sources.compression` — how much of the site never reached a model**

```
sources.compression status="WARN" keys_truncated=4 chars_in=48210
  chars_kept=24000 chars_dropped=24210 pct_dropped=50.2
  worst="website.about:-9200 | website.team:-7100 | …"
```

This was silent before v27.4. A tail-slice removes the **end** of each page,
which on a scraped site is disproportionately About / Team / Investors — the
highest-value diligence content. If `pct_dropped` is material, raise
`MAX_WEBSITE_CHARS` in `profile/services.py` for the named keys and re-run.
Compare assessment coverage before and after; the 43.8% vs 75.0% gap between
MediBuddy and ClearDekho is the hypothesis this tests.

**4. `CONTRACT_VIOLATION` / `BREAKER_OPEN` — should not appear**

If either does, the run was **refused before tokens were spent**, and the trace
line names the remedy. This is working as designed, not a crash.

---

## Step 3 — Confirm the refusal path renders (X-01)

v27.4 added two paths that end in `_abandon_ungrounded_run`. No frontend tree
was supplied with either tarball, so this is unverified.

Trigger a run against a URL that 403s with all research sources disabled.
Confirm the client shows a **terminal, explained failure** — not a spinner and
not a blank profile. The v25.2 frontend was built against backend v26 and has
not been tested against a resting status on an abandoned run.

---

## Step 4 — Record the numbers

Put `run.economics` from the first clean live run into the issue register as
the baseline. Every optimisation after this — batching the fan-out, enabling
caching, right-sizing models per role — is measured against it, and until it
exists you are tuning against figures that were 39% low.

---

## Known-failing tests (do not chase)

`test_v23_phase3_phase4` and `test_v24_workbook_config`: 6 failures, 3 errors,
54 skipped. **Identical on the untouched v27.3 tarball** — verified by running
both trees. They need a sector workbook fixture that is not in the repo:

```bash
python manage.py import_assessment_workbook <file> --activate
```
