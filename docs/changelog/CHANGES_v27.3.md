# FundOS backend v27.3 — the tier means something

**Ten files changed. No migrations. No API contract change.**

The 17 Aug log showed two clean runs — 45 steps, zero failures — that between
them performed **no web searches at all**. Both profiles were assembled from
one homepage and the model's recollection. Every layer reported healthy.

This release makes that state unreachable rather than merely detectable.

---

## The root cause, and why six releases missed it

`LLMConfigProfile.tier` was a LABEL. `web_search` was an independent boolean
defaulting to `False`. Nothing bound them, so `tier="advanced",
web_search=False` was a valid row that saved without complaint.

The invariant "advanced means it searches" was written down in three places:

| Where | Form |
|---|---|
| `models.py` `TIER_DEFINITIONS` | prose |
| `diagnose_config._search_capability` | a runtime check |
| `adapter.py` no-search warning | a message printed after the money was spent |

Three detectors. Zero constraints. And the second and third **contradicted
each other** on whether `responseSchema` may accompany `google_search` — one
told operators to clear the schema, the other told them not to. Both were
written from reasoning; neither from a measurement.

A second, independent mechanism made the same failure inevitable even with
the checkbox ticked: `capabilities_payload()` forced `budget_tokens: 0` on
any Gemini profile that had not stated a thinking mode. `google_search` is
**model-decided** — the model must deliberate to choose to call it. A search
tool handed to a model with no deliberation budget is permission granted to
something with no opportunity to take it.

The only two calls in the entire 17 Aug log with `thinking_tokens > 0` were
the judgment calls. They were also the only ones not raising the warning.

---

## What changed

### 1. The tier is a contract — `fundos/llm/tier_contract.py` (new)

The tier now states what a call MUST and MUST NOT be able to do. The profile
tunes what the contract leaves open.

|  | `simple` | `advanced` | `judgment` |
|---|---|---|---|
| Web search | forced OFF | **forced ON** | forced OFF |
| Thinking when unstated | 0 (stated explicitly) | **2,048 floor** | 4,096 |
| Profile may tune | everything else | budget, max_uses, domains | budget |

Applied at the end of `capabilities_payload()`, so the profile assembles
first and the contract has the last word. `clean()` refuses to save a profile
bound to a tier it cannot honour, and `apply_contract` raises rather than
silently returning a non-searching "advanced" call if validation was bypassed.

**Deliberately narrow.** The contract covers tools and the reasoning needed to
drive them — what `TIER_DEFINITIONS` actually defines a tier by. It does not
touch model choice, temperature, output ceilings or schemas. The same model
may back all three tiers, and today does: all 22 calls on 17 Aug ran on
`gemini-3.5-flash`. Tiers differ by what a call is allowed and required to do,
not by how strong the model is.

Thinking is a **floor**, never a ceiling. An administrator who states a budget
keeps it. The contract only intervenes where silence would produce a tier that
cannot do its job.

### 2. Search-required roles can no longer be silently downgraded

`resolve_tier_profile_code` used to fall back to `simple` on any resolution
failure, reasoning "never silently spend the advanced budget". Right instinct,
wrong direction for four roles: the cost of a silent downgrade is not a saved
search, it is a published dossier assembled from memory. `SEARCH_REQUIRED_ROLES`
now resolves to `advanced` or raises.

### 3. The search directive is no longer buried

On the implicit-cache path the context is prepended to the prompt, which on
17 Aug put the "SEARCH FIRST" directive **24,081 characters** ahead of the
task. The model read the directive, then a wall of material that looked
entirely sufficient, then a 611-character instruction.

A short reminder now goes in the volatile tail. The cached prefix stays
byte-stable, so this costs a few dozen tokens and no cache hits.

### 4. `searches="unknown"` is now interpretable

Every `llm.call` trace line carries `caps_sent` and `thinking_budget`. The old
`searches="unknown"` was compatible with *the tool was never attached* and
*the tool was attached and the model declined* — opposite fixes, indistinguishable
in a whole day of logs.

### 5. Thinking tokens are billed

`_log_call` never received `thinking_tokens`, so they reached neither
`total_tokens` nor `cost_inr`. The 17 Aug runs were under-reported by **39%**
(10,448 of 26,864 billed output tokens; ₹8.28 of ₹32.57), and `_enforce_budget`
reads that column — a tenant ceiling could be overshot by that margin before
tripping. Folded into the billed output figure; no migration. This mattered
less while thinking was off nearly everywhere, and v27.3 turns it on for two
tiers of three.

### 6. Context truncation is no longer silent — `MAX_CONTEXT_CHARS` 24,000 → 120,000

MediBuddy's sources reported 31,001 characters; the deep extract received
24,081. **22% of the only live source discarded** with no log line and no
marker in the text. Its assessment coverage came out at 43.8% against
ClearDekho's 75.0%, and ClearDekho happened to fit under the cap.

The ceiling now warns with exact figures and appends a marker telling the
model the material is incomplete.

### 7. Transaction aborts are contained where they start

The recurring `financials→CKB sync failed … can't execute queries until the
end of the 'atomic' block` was never about the sync. Three `try/except` blocks
around `FundingRound.objects.create` and two `Investor` writes had no
savepoint, so one failed INSERT aborted the transaction and the sync was
merely the next thing to touch the database. Each now has its own savepoint.

### 8. `llmCalls` is counted, not derived

It was `1 + len(residual sections)`, which omitted judgment, readiness and
assessment: **9 reported, 11 actual, 7 claimed in the v27.2 changelog**. Three
numbers for one quantity — and the v27.2 regression test asserts on it. The
adapter now keeps a thread-local tally, because it is the only component that
knows what it dispatched.

### 9. Unweighted revenue streams are kept

Both runs found four revenue streams, none with a percentage, and discarded
all four — the section rendered blank with nothing on screen to say why. Most
companies publish their streams and not the split, so the model was answering
correctly. Rows now seed as a draft and the section is marked needs-input. A
real split must still total 100; a *partial* split is rejected with a message
saying what to do.

### 10. Mocked runs are exempt from the grounding gate

The v27.2 gate fired on every mocked run and aborted it — mock output is
fabricated by construction. The gate exists to stop a founder being shown
invented facts; in mocked mode there is no founder. **Two shipped tests had
been failing on this since v27.2.** The exemption keys off the same
`ai_mocked` flag the adapter uses, so a run that reaches a real model is never
exempt.

### 11. Warning volume cut by ~40%

The `cp_extract.<section>` profiles are opt-in by design: when none exists the
tier drives the call, which is intended. Logging that at WARNING produced 18
warning lines per run for a system working as designed — which is how the
genuinely critical warnings in the same log came to be skimmed past for six
releases. Now INFO, and the trace step reads `OK` with a note.

---

## Test results

```
Ran 212 tests   (LLM + generation + grounding)      2 failures → 0
Ran 186 tests   (config, flows, exports, contract)  2 failures → 0
Ran 146 tests   (backfill, QA passes, benchmarks)   1 failure  → 0
Ran  18 tests   tests/api/test_v27_3_llm_fixes.py   NEW, all pass
```

Nine failures remain in `test_v23_phase3_phase4` and `test_v24_workbook_config`.
**All nine fail identically on the untouched v27.2 tarball** — they need a
sector workbook fixture that is not in the repo (54 tests skip for the same
reason). Untouched by this release.

Six existing tests were changed. Five declared `web_search=True` while leaving
`tier` at its `simple` default — under-specified rather than wrong, and now
explicit. The sixth,
`test_search_off_on_the_advanced_tier_fails`, asserted that clearing the
checkbox produced a diagnostic FAIL; that state is no longer representable,
so it now asserts the capability survives.

---

## What this release does NOT fix

**Whether `responseSchema` suppresses `google_search` on Gemini.** This could
not be settled without calling the live API. `tools/diagnose_gemini_search.py`
answers it in eight calls across `{schema on/off} × {thinking 0/2048} ×
{directive before/after}`; run it and the answer belongs in `TIER_CONTRACT`.
If a schema does suppress the tool, the advanced contract gains
`structured_output: False` and the deep extract splits into a search pass and
a structuring pass.

There is a real chance the contract alone restores search via the thinking
budget. **Run the diagnostic anyway** — otherwise you will not know which
variable was responsible, and that is the knowledge that stops this recurring.

**The fan-out (C-03).** Seven residual calls per run re-send a near-identical
~9,000-character context to produce a few hundred tokens: 43,748 of 85,545
input tokens (51%) are redundant. The root fix is extending the deep-extract
schema to cover `company_overview`, `cap_table` and `market_research`, which
means changing a prompt whose output could not be validated here. Left alone
deliberately — `llmCalls` now reports honestly, which is the instrument needed
to drive it.

**Explicit Gemini context caching (C-04).** The fan-out context is ~2,200
tokens, below Gemini's implicit-cache minimum, so the accounting columns stay
at zero. Needs explicit `cachedContent`, which is API work rather than a fix.

**`SectorMapping` coverage (D-02).** 'Eyewear' still matches nothing. Note the
register overstated the impact: an unresolved sub-sector is excluded from the
denominator and its weight redistributed, so the rating is not silently 20%
short. Add the row; consider auditing label coverage against the sub-sectors
the extract actually emits.

---

## Verifying the fix

```bash
python manage.py diagnose_config          # tier contract + search capability
python tools/diagnose_gemini_search.py    # needs GEMINI_API_KEY
```

Then one real generation. Success is `searches` greater than zero on
`llm.call.company_profile_deep_extract` — not the absence of a warning.
