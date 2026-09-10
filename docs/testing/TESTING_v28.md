# TESTING — FundOS backend v28

Each section states the command, the expected outcome, and what a deviation
means. Sections 1–4 need no API key. Sections 5–7 need a live Gemini key.

---

## 1. Static checks

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

| Expected | Meaning of deviation |
|---|---|
| `System check identified no issues (1 silenced)` | Any issue: stop |
| `No changes detected` | A model change is missing its migration — the v27.3 defect recurring |

---

## 2. v28 suites

```bash
python manage.py test tests.api.test_v28_tier_policy_and_seed
python manage.py test tests.api.test_v28_domain_agnostic_e2e
```

Expected: **23 tests OK**, then **6 tests OK**.

What they cover:

* Every one of the 29 roles has an explicit tier, and each tier matches its
  purpose — advanced only for roles that must reach the internet, judgement
  only for roles that reason over extracted data.
* The coherence guard fires in all three directions: a search role on
  `simple` raises; a judgement role on `simple` and an extraction role on
  `advanced` warn.
* The seeder adopts an existing provider endpoint rather than manufacturing a
  duplicate, and re-seeding twice changes nothing.
* No searching profile carries a responseSchema, and installs seeded the old
  way are repaired.
* The same pipeline shape runs for construction, electronics and logistics
  companies, with identical call counts and modes.

---

## 3. Carried-forward suites

```bash
python manage.py test \
  tests.api.test_v27_5_measured_fixes \
  tests.api.test_v27_4_contract_and_economics \
  tests.api.test_v27_4_evidence_run \
  tests.api.test_v27_3_llm_fixes
```

Expected: **89 tests OK**.

---

## 4. Full regression

```bash
python manage.py test tests.api tests.engines tests.investors tests.isolation
```

Expected: **698 pass, 0 failures**, with two documented exceptions.

| Known | Detail |
|---|---|
| `test_v23_phase3_phase4`, `test_v24_workbook_config` | 6 failures, 3 errors, 54 skipped. Identical on the untouched v27.5 tree. They need a sector workbook fixture absent from the repo — load one with `manage.py import_assessment_workbook <file> --activate` and they pass. |
| `test_a_valid_admin_choice_is_never_overwritten` | Fails only when run alongside certain other modules; passes in isolation and within its own module. Pre-existing cross-module test pollution, unchanged by v28. |

To confirm the second is not yours:

```bash
python manage.py test tests.api.test_configuration_integrity      # expect OK
```

---

## 5. H4 — the open question

```bash
python tools/diagnose_gemini_search.py --model gemini-3.5-flash
```

Sixteen calls, ~3 minutes. Output is a grid of
`{schema} × {thinking} × {directive position} × {framing}` against searches
actually performed, read from `groundingMetadata.webSearchQueries`.

The 19 August result, for comparison — this is the baseline:

```
SCHEMA (H1) is decisive: with=0% searched, without=100% searched
THINKING (H2) has no effect.
POSITION (H3) has no effect.
```

H4 (framing) is new. Reading it:

| Result | Meaning | Action |
|---|---|---|
| **FRAMING decisive** | The "populate every field from the supplied material" system prompt suppresses search independently of the schema. This explains why `company_profile_deep_extract` sent no schema on 19 Aug and still never searched. | Rewrite the deep-extract system prompt to lead with retrieval, not extraction. Removing the schema alone will not fix that role. |
| **FRAMING no effect, SCHEMA still decisive** | v28's schema removal is sufficient. | Nothing further; proceed to §6 and confirm `searches > 0`. |
| **Nothing searches in any cell** | The constraint is upstream of all four variables. | Check `google_search` is enabled on the key's project, and that the model string supports Search grounding. |
| **Everything searches** | The provider is not the constraint. | The remaining fault is in FundOS: confirm `web_search` survives `capabilities_payload()` and that the breaker is not open. |

**Record the answer in `fundos/llm/tier_contract.py`** beside the existing
grid, with the date and model string. Two halves of this codebase asserted
opposite answers about schema and search for six releases because nobody
wrote the measurement down.

---

## 6. First live generation

With `ai_mocked = OFF`, generate a profile for a company with a large website.

```bash
grep 'run.economics' var/logs/silk_generation.log | tail -2
```

| Field | Pass | Failure means |
|---|---|---|
| `searches` | **> 0** | The release did not achieve its objective. Return to §5. |
| `searches_unknown` | `0` | The provider returned no grounding metadata — a different fault needing the opposite fix. |
| `llm_calls` | matches the count of `llm.call.*` lines | The accounting window is wrong. |
| `thinking_tokens` | > 0 on judgement, ~0 elsewhere | v28 narrowed thinking to judgement-class roles; anything else is a tier override. |
| `cost_inr` | non-zero | Price book not seeded — re-run `seed_llm_costs`. |
| `concerns` | `none` | Read it; it names what it found. |

**Success is `searches > 0`, not the absence of a warning.** Six releases of
healthy-looking logs turned on that distinction.

Then confirm the three fixes that were failing on 19 August:

```bash
grep -c 'financials→CKB sync failed' var/logs/silk_generation.log     # expect 0
grep 'llm.call.assessment_inputs' var/logs/silk_generation.log        # expect no MAX_TOKENS
grep -c 'ignoring duplicate endpoint' var/logs/silk_generation.log    # expect 0
```

Baseline for comparison, from the 19 August runs:

| | 19 Aug (v27.4) | v28 target |
|---|---|---|
| LLM calls | 11 | 11 |
| Cost | ₹21.14 / ₹21.02 | lower — thinking narrowed to judgement |
| Latency | 150s / 146s | lower, same reason |
| `assessment_inputs` | failed / truncated | completes |
| `searches` | 0 / 6 | > 0 on the deep extract |
| CKB sync error | present | absent |

---

## 7. Domain coverage

The pipeline carries no sector knowledge in code — sector data comes from the
imported workbook. Verify with a company outside the sectors you have loaded
(a construction or electronics firm, if your workbook is D2C-oriented):

```bash
grep 'sub-sector' var/logs/silk_generation.log | tail -5
```

Expected: `matched no benchmark group … Category F will score blank`.

**This is correct behaviour, not a defect.** The resolver deliberately
refuses to fuzzy-match a construction firm onto a D2C benchmark group,
because assigning the wrong peer group produces a confident number against
the wrong comparators. A blank category is excluded from the denominator and
its weight redistributes across the categories that did score, with the
shares surfaced as `applied_weight`.

So the rating is not wrong — it is computed over fewer categories, and says
so. To score category F for a new industry, load a workbook covering it
rather than adding rows one sector at a time:

```bash
python manage.py import_sector_benchmarks <workbook.xlsx>
```

---

## Summary sheet

| # | Check | Expected |
|---|---|---|
| 1 | `manage.py check` | no issues |
| 1 | `makemigrations --check` | no changes |
| 2 | v28 tier/seed suite | 23 OK |
| 2 | v28 domain e2e | 6 OK |
| 3 | v27.3–v27.5 suites | 89 OK |
| 4 | full regression | 698 pass, 2 documented exceptions |
| 5 | H4 grid | recorded in `tier_contract.py` |
| 6 | live run | `searches > 0`, `concerns=none` |
| 6 | CKB sync | 0 occurrences |
| 6 | assessment_inputs | no `MAX_TOKENS` |
| 7 | unmapped sector | blank + redistributed, not mis-grouped |
