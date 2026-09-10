# v27 — the search that never ran, and four failures that reported themselves as successes

Every change here was traced to a specific line, reproduced, and verified.
Six files changed. **No migrations.** No API contract change.

---

## 1. ROOT CAUSE — Gemini received the search tool with no instruction to use it

**Symptom:** 21 LLM calls on 13 Aug, `searches=""` on every one, HTTP 200
throughout, no error at any layer.

**Not the cause:** the adapter's own warning told operators to *"check that
structured output is off"*. That advice is stale. Google's current docs state
that Gemini 3 supports Structured Outputs combined with Grounding with Google
Search; and on the older models where the combination was prohibited it failed
*loudly* with a 400 INVALID_ARGUMENT, never silently. Turning structured output
off would have cost schema conformance and bought nothing.

**Actual cause — two lines that disagree.**

`tier.advanced.gemini` is seeded with `web_search=True` and **no `max_uses`**.
`LLMConfigProfile.capabilities_payload()` then does:

```python
ws = {}
if self.max_uses:            # None → key omitted
    ws["max_uses"] = self.max_uses
out["web_search"] = ws       # → {}   an EMPTY DICT
```

An empty dict is **present but falsy**. The adapter asks two different
questions about it:

```python
if capabilities.get("web_search"):   # {} is FALSY  → directive block SKIPPED
    ...build the search directive...

if "web_search" in capabilities:     # key EXISTS   → tool ATTACHED
    tools.append({"google_search": {}})
```

So Gemini was handed `google_search` and told nothing about it. On Gemini
`google_search` is a **model-decided** tool — sending it grants permission, it
does not cause a search. A model holding 24,000 characters of context, granted
permission and given no instruction, answers from context. Every time. With a
200 and valid JSON, so every layer reported healthy.

The `SEARCH_EXPECTED_ROLES` machinery and the "SEARCH FIRST — THIS IS NOT
OPTIONAL" directive were already written and correct. They were simply never
reached.

**Fixed at three layers, so no single mistake can reproduce it:**

* `capabilities_payload()` now always emits a cap (`DEFAULT_SEARCH_MAX_USES = 6`).
  Blank means "deployment default", never "say nothing".
* The adapter tests `"web_search" in capabilities` — the same question the tool
  assembly asks. Two tests that must agree now ask the same thing.
* `_search_directive()` no longer returns empty for an expect-search role with
  no cap.

Plus: the seeder gives vendor alternates the same `max_uses` and
`user_location` the Anthropic profile has always had — an alternate that
behaves differently from the profile it replaces is not an alternate — and a
**repair pass** fills a blank cap on existing profiles, because `get_or_create`
does nothing to a row that already exists.

The misleading warning text is replaced with the three things actually worth
checking, and an explicit instruction *not* to disable structured output.

---

## 2. The profile was published from the model's memory

**13 Aug, Deepak Nitrite (`GEN[12617e]`):** website lost to a TLS error, 2
characters retrieved, no search — and the deep extract returned **122 populated
fields**, from which **twelve sections were written**, including
`financial_summary`, `competitors` and `investment_thesis`.

`assessment_inputs` correctly refused (`error="ungrounded"`). The profile did
not. Refusing to score while publishing a fabricated dossier is backwards, and
the profile is the founder-facing document — those rows are now
indistinguishable in the database from a profile built on a real website.

`generate_deep_profile` now runs the **same** `grounding()` function the
scorecard uses, before writing anything. Ungrounded → no sections written, a
`deep_extract.grounding FAIL` trace event carrying the populated-field count,
and an ERROR naming the contradiction: *N fields returned, nothing retrieved*.

Using the same function is the point: two gates with the same job drift apart.

---

## 3. A failed run suppressed its own retry

**13 Aug:** `GEN[12617e]` failed (TLS + ungrounded, `failures=2`) and stamped
the sources fingerprint anyway. `GEN[8d329c]` retried 89 seconds later, hit the
skip gate and **did nothing**, reporting a clean skip. From outside that is
indistinguishable from "we retried and it failed the same way".

The gate's own message says *"unchanged since the last **successful** run"* —
the write never checked success.

`_stamp_sources_hash()` now refuses to record a fingerprint when there were
source failures, when a downstream step failed, or when **nothing was
retrieved** — two failed collections hash identically, so an empty source set
would otherwise satisfy a cache whose whole premise is "identical sources
produce identical output". The gate independently refuses to fire on an empty
or failed collection.

---

## 4. `financials→CKB sync failed` was never about the sync

```
An error occurred in the current transaction.
You can't execute queries until the end of the 'atomic' block.
```

That is a *symptom*: an earlier statement failed, was caught by a
"never fail the run" handler, and left the transaction aborted. Every
subsequent query then dies — and the sync got the blame for being merely the
next thing to touch the database. The savepoint already at the sync could not
help, because the connection was broken before it was entered.

Four blocks that swallow exceptions now run inside their own savepoint: the
competitor loop (which runs immediately before the financials write, and is the
likely origin), the financials write, the deep-extract section loop, and the
per-section generation loop. A failure now rolls back only its own block, and
the next step's error is its own error.

---

## 5. Correct outcomes reported as faults

Two fire routinely and both landed on *"No specific fault signature matched —
send this trace to the development team"*:

* `generation.skipped` — the cache working.
* `generation.lock` — a duplicate request correctly declining to interleave.
  This additionally logged at **ERROR with a full traceback**, and reset
  `profile.status` to `draft` — the status of the run still in progress.

Both now diagnose as `[NONE]` with an accurate explanation. The lock returns
`{"status": "already_running"}` and logs at INFO. Anyone grepping ERROR was
finding two false positives per double-click, which is how the real ones stop
being read.

---

## 6. `records.form_seed kept=0` — every metrics row discarded, every run

```
company_metrics  kept=0 dropped=5  no recognisable label in
                 ['metric_name', 'source', 'value']
```

The alias table listed `metricName` and not `metric_name`. The model emits
both across runs, so rows were being discarded over an underscore while a
near-identical alias sat in the table.

Adding one more spelling would have been chasing a moving target. Keys are now
compared on **normalised identity** (`_key_norm`: lowercase, strip
non-alphanumerics), so `metric_name` ≡ `metricName` ≡ `MetricName` without any
of them needing an entry. Only genuinely different *words* need aliasing now —
and `modelType`, `type`, `description`, `indicator` and `count` were added,
having appeared in live output.

A row carrying `fiscal_year`/`revenue`/`ebitda` that lands in a metrics form is
also now reported for what it is — *"a fiscal-year financial row, not a
company_metrics row"* — rather than as a missing label, which sent the reader
after a naming bug that was not there.

## 7. `deep-extract financials skipped: Keep this under 9 characters`

Traced to `financial_summary.fiscalYear`, which is `max_len=9`. The model
writes `"FY2024-25 (Consolidated)"`, `"Financial Year 2024-25"`,
`"March 2025"` — all longer. `validate_structured_form` raises on the **first**
bad row and the caller wrapped the whole batch in one `try/except`, so one
verbose label discarded **every year of a company's financials**. The log line
named neither the field, nor the row, nor the value.

Three fixes:

* `_fiscal_year_label()` normalises the label to fit — `"FY2024-25
  (Consolidated)"` → `"FY2024-25"`, `"March 2025"` → `"FY2025"`. The label is
  presentational; the year is the information, so it is normalised rather than
  rejected.
* Financial rows are validated **one at a time**, so a row that cannot be
  rescued costs that row and not the batch, and the rejects are traced.
* `_text()`'s message now names the field and shows the value:
  `fiscalYear: keep this under 9 characters (got 24: 'FY2024-25 (Consolidated)')`.

## 8. `searches=""` could not answer "did the fix work?"

`None` (provider gave no count) and `0` (provider counted none) both rendered
as empty. The counter already distinguishes them — *"a genuine zero is never
confused with unknown"* — but the trace line threw it away. Now emits
`searches="unknown"` or `searches=0`, plus `search_expected=true|false`.

This is the line you will read to confirm §1 worked.

---

## Files changed

| File | Change |
|---|---|
| `fundos/llm/models.py` | `DEFAULT_SEARCH_MAX_USES`; never emit an empty `web_search` |
| `fundos/llm/adapter.py` | presence-not-truthiness; directive without a cap; honest warning; `searches` unknown-vs-zero |
| `fundos/platformcfg/.../seed_platform_config.py` | vendor alternates get search sub-controls; repair pass for existing rows |
| `fundos/profile/services.py` | grounding gate; fingerprint guard; empty-source detection; four savepoints |
| `fundos/profile/records.py` | validator message names the field and the value |
| `fundos/profile/trace.py` | skip and lock diagnose as `[NONE]` |
| `fundos/profile/tasks.py` | lock refusal is INFO, not ERROR-with-traceback |

## Verification performed

* `compileall` clean across `fundos/`.
* Capability resolution replayed for `tier.advanced.gemini` — before: `web_search={}`,
  tool sent, directive skipped. After: `{'max_uses': 6}`, both sent.
* `_search_directive(None, expect_search=True)` returns the SEARCH FIRST text;
  non-search roles still return empty.
* `_stamp_sources_hash` across four scenarios (clean / TLS failure / downstream
  refusal / nothing retrieved) — only the clean run stamps.
* `_sources_are_empty` across three payload shapes.
* `_count_populated` including the depth guard.
* Diagnosis branch replayed for skip, lock, genuine-empty and healthy runs.
* The five rows the 12/13 Aug logs reported as dropped, replayed through the
  new matcher — all five now resolve, including the misrouted fiscal-year row
  being named accurately.
* Seven fiscal-year phrasings normalised and checked against the 9-char field.
* Regression pass over the paths that already worked: canonical rows, the
  bare-string salvage, Indian magnitudes (`Rs 7,761 crore`), unknown sections
  passing through untouched, non-dict rows still dropped.

## Still open — deliberately not fixed here

* **Revenue-stream percentage splits.** `revenue_model` still drops rows that
  name a stream but give no split — `"4 row(s) named a revenue stream but gave
  no percentage split"`. That is the form's own rule (the splits must sum to
  100), not a mapping failure, so it needs a decision rather than a patch:
  either require the split in the prompt, or let the form store un-split
  streams. Say which and I will implement it.
* **`cp_extract.*` config profiles.** Nine missing, every section falling back.
  Configuration, not code — see `CONFIGURATION_v27.md`.
* **The TLS trust store.** Server administration — see the same document.
* **Research adapters still returning fixtures.** Every run is
  `mode="website_only"` until live feeds are behind them.
