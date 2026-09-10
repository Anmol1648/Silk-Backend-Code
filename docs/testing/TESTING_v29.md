# TESTING — FundOS backend v29

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
| `No changes detected` | A model change is missing its migration — the v27.3 defect recurring. v29 adds `platformcfg/0004_platformflag`; it is committed, so this must still say "No changes". |

---

## 2. v29 suite

```bash
python manage.py test tests.api.test_v29_defect_closure
```

Expected: **31 tests OK**.

Grouped by the layer the fault lived in:

* **Tier policy coverage** — every one of the 33 dispatchable roles has an
  explicit tier; the four v28 missed are named individually; every advanced
  role is genuinely search-required; an unknown role now logs
  `TIER POLICY GAP` rather than silently resolving to `simple`.
* **Role sets name real roles** — every member of `SEARCH_REQUIRED_ROLES`,
  `SEARCH_BENEFICIAL_ROLES`, `NO_THINKING_ROLES`, `THINKING_JUSTIFIED_ROLES`
  and `ROLE_MAX_OUTPUT_TOKENS` exists in `LLM_ROLES`.
* **The repair path** — the repair dispatch carries its role, so it keeps the
  role's designed ceiling instead of falling to 2048.
* **Truncation salvage** — six cases including a comma inside a string, an
  escaped quote, and the invariant that salvage yields FEWER fields but never
  wrong ones.
* **`structured_output`** — `company_profile_deep_extract` is asserted to be
  *outside* the schema allowlist; every ARRAY property in a derived schema
  declares `items`.
* **Endpoint adoption** — the row a role binding already points at wins over
  the alphabetically-first code, and the two dedupe paths agree.
* **Peer grouping** — an out-of-sector company gets an empty Group A; a
  genuine near-miss is still promoted; promotion caps at three.
* **Seed idempotence** — seeding twice changes nothing.

---

## 3. v28 suite (corrected)

```bash
python manage.py test tests.api.test_v28_tier_policy_and_seed
python manage.py test tests.api.test_v28_domain_agnostic_e2e
```

Expected: **26 tests OK**, then **6 tests OK**.

The tier/seed suite grew from 23 to 26. Three changes:

| Test | Change |
|---|---|
| `test_every_role_has_an_explicit_tier` | Re-keyed from `DEFAULT_PROMPTS` (29) to `LLM_ROLES` (33). The v28 version compared two 29-entry dicts maintained in the same file, so it was tautologically empty and could never fail. |
| `test_a_pre_existing_schema_search_conflict_is_repaired` | Now asserts Gemini-only scoping. |
| `test_the_schema_alignment_does_not_touch_other_providers`, `test_an_administrator_may_re_enable_schema_after_the_alignment` | New. The second is the one that matters: it asserts a repair cannot re-run forever and make a setting unsettable. |

---

## 4. Carried-forward suites and full regression

```bash
python manage.py test \
  tests.api.test_v27_5_measured_fixes \
  tests.api.test_v27_4_contract_and_economics \
  tests.api.test_v27_4_evidence_run \
  tests.api.test_v27_3_llm_fixes \
  tests.api.test_configuration_integrity
```

Expected: **all pass** (146 including the v28 suites when run together).

```bash
python manage.py test tests.api
python manage.py test tests.engines tests.investors tests.isolation
```

**Measured on the shipped v29 tarball, from a clean extract:**

| Package | Result |
|---|---|
| `tests.api` | **810 tests — 5 failures, 3 errors, 55 skipped** |
| `tests.engines` + `tests.investors` + `tests.isolation` | **56 tests — OK** |

All 8 `tests.api` exceptions are in **one module**, `test_v23_phase3_phase4`:

```
ERROR  SectorCategoryWiringTests.test_a_percentile_can_never_reach_exceptional
ERROR  SectorCategoryWiringTests.test_shared_sector_metrics_are_seeded_for_both_categories
ERROR  SubSectorResolutionTests.test_benchmark_values_are_the_workbook_scores
FAIL   ParameterPopulationCoverageTests.test_both_sector_and_sub_sector_are_asked_separately
FAIL   SectorCategoryWiringTests.test_lookup_parameters_exist_for_the_benchmark_table
FAIL   SectorCategoryWiringTests.test_percentile_is_banded_before_it_is_scored
FAIL   SubSectorResolutionTests.test_benchmark_rows_are_emitted_for_both_levels
FAIL   SubSectorResolutionTests.test_sectors_e_and_f_actually_score
```

They need a sector workbook fixture that is not in the repo. Load one and
they pass:

```bash
python manage.py import_assessment_workbook <file> --activate
```

**Confirm they are not yours** by running the same module against the
untouched v28 tree — it gives the identical `5 failures, 3 errors`:

```bash
python manage.py test tests.api.test_v23_phase3_phase4     # 65 tests, 5F/3E
```

Two notes on what changed from the v28 documentation:

* `tests.api` is **810** tests, up from 809. The `PeerGroupA` acceptance test
  was split into two (see §7 and CHANGES_v29 §4.1).
* v28's TESTING doc also listed `test_v24_workbook_config` and
  `test_a_valid_admin_choice_is_never_overwritten` as expected failures.
  Neither failed in the v29 full run. The second passes both in isolation and
  in the full package; if it fails for you it is the pre-existing
  cross-module pollution v28 described, not a v29 regression:

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

The 19 August baseline:

```
SCHEMA (H1) is decisive: with=0% searched, without=100% searched
THINKING (H2) has no effect.
POSITION (H3) has no effect.
```

**Read H1 differently now.** It is a true finding about the Gemini API and an
irrelevant one for FundOS: `output_schema` defaulted to `{}` and nothing ever
wrote to it, so no `responseSchema` was ever sent in production. Your own log
is the proof — `company_profile_deep_extract` (0 searches) and
`company_profile_section` (2, 2, 2) ran on the same profile with identical
`caps_sent="thinking,web_search"`. Whatever separates them, it is not the
schema, because neither had one.

That leaves H4 as the live hypothesis:

| Result | Meaning | Action |
|---|---|---|
| **FRAMING decisive** | The "populate every field from the supplied material" system prompt suppresses search independently of everything else. This is the hypothesis the logs now point at. | Rewrite the deep-extract system prompt to lead with retrieval, not extraction. Nothing in v28 or v29 will fix that role until this is done. |
| **FRAMING no effect** | The constraint is none of the four measured variables. | Compare the two roles directly: same profile, same caps, opposite behaviour. The difference is in the prompt text or the response size, and both are now the only remaining candidates. |
| **Nothing searches in any cell** | Upstream of all four variables. | Check `google_search` is enabled on the key's project and that the model string supports Search grounding. |
| **Everything searches** | The provider is not the constraint. | Confirm `web_search` survives `capabilities_payload()` and that the breaker is not open. |

**Record the answer in `fundos/llm/tier_contract.py`** beside the existing
grid, with the date and model string. Two halves of this codebase asserted
opposite answers about schema and search for six releases because nobody
wrote the measurement down — and then a seventh release was built on a
measurement that was written down but never checked against what the code
actually sent.

---

## 6. First live generation

With `ai_mocked = OFF`, generate a profile for a company with a large website.

```bash
grep 'run.economics' var/logs/silk_generation.log | tail -2
```

| Field | Pass | Failure means |
|---|---|---|
| `llm_calls` | matches the count of `llm.call.*` lines | The accounting window is wrong |
| `failed_calls` | `0` | Read the error; §6b covers the two that failed on 19 Aug |
| `thinking_tokens` | > 0 on judgement, ~0 on extraction roles | A tier override survives somewhere |
| `cost_inr` | non-zero | Price book not seeded — re-run `seed_llm_costs` |
| `searches` | **> 0 on the deep extract** | **Expected to still fail.** See below. |

**On `searches`: do not treat this as a v29 pass condition.** v28's release
notes made `searches > 0` the headline objective and attributed the failure
to a `responseSchema` that was never sent. v29 corrects the diagnosis but
does not fix the behaviour — that needs H4 answered first. A run where
everything else is green and the deep extract still reports `searches=0` is
the *expected* v29 outcome, not a regression.

### 6b. The three failures that must now be gone

```bash
grep -c 'financials→CKB sync failed' var/logs/silk_generation.log   # expect 0
grep 'llm.call.assessment_inputs' var/logs/silk_generation.log      # expect no MAX_TOKENS, no FAIL
grep -c 'ignoring duplicate endpoint' var/logs/silk_generation.log  # expect 0
```

If `assessment_inputs` truncates anyway, you will now see this instead of a
hard failure:

```
LLM: assessment_inputs returned JSON that was cut off; recovered the
complete prefix locally without paying for a repair call.
```

That is the salvage path working — the result is honestly incomplete, and the
fix is to raise the ceiling further, not to ignore the line.

Baseline for comparison:

| | 19 Aug (v27.4) | v29 target |
|---|---|---|
| LLM calls | 11 | 11 |
| Cost | ₹21.14 / ₹21.02 | lower — thinking narrowed to judgement |
| Latency | 150s / 146s | lower, same reason |
| `assessment_inputs` | failed / truncated | completes, or salvages with a named warning |
| CKB sync error | present | absent |
| `searches` (deep extract) | 0 / 0 | **still 0 until H4 is answered** |

---

## 7. Domain coverage

The pipeline carries no sector knowledge in code. Verify with a company
outside the sectors you have loaded:

```bash
grep 'sub-sector' var/logs/silk_generation.log | tail -5
```

Expected: `matched no benchmark group … Category F will score blank`. This is
correct behaviour — a blank category is excluded from the denominator and its
weight redistributes across the categories that did score, with the shares
surfaced as `applied_weight`.

**Also check the peer universe, which v28 did not.**

```bash
grep 'PEERS: no candidate reached the promotion floor' var/logs/silk_generation.log
```

For a construction, electronics or logistics company against a SaaS-oriented
peer set, expect that line and an **empty Group A**. Before v29 the code
promoted the single top scorer into "Closest Comparables" regardless of
score, so an unrelated SaaS company was presented as the closest comparable
with a rationale line asserting it. The assessment path had always refused to
do this; the peer path did the opposite, and only one of the two had been
audited.

To populate Group A for a new industry, load peers covering it — the same
answer as for benchmarks:

```bash
python manage.py import_sector_benchmarks <workbook.xlsx>
```

---

## Summary sheet

| # | Check | Expected |
|---|---|---|
| 1 | `manage.py check` | no issues |
| 1 | `makemigrations --check` | no changes |
| 2 | v29 defect closure | 31 OK |
| 3 | v28 tier/seed | 26 OK |
| 3 | v28 domain e2e | 6 OK |
| 4 | v27.3–v27.5 + config integrity | all OK |
| 4 | `tests.api` full | 810 tests, 8 exceptions — all in `test_v23_phase3_phase4` |
| 4 | engines + investors + isolation | 56 OK |
| 5 | H4 grid | recorded in `tier_contract.py` |
| 6 | live run | `failed_calls=0`, CKB sync absent |
| 6 | `assessment_inputs` | completes or salvages — never `FAIL` |
| 6 | deep-extract `searches` | still 0 — expected, pending H4 |
| 7 | unmapped sector | blank + redistributed, not mis-grouped |
| 7 | out-of-sector peers | Group A empty, floor line logged |
