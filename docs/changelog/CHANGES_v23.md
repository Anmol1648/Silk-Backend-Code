# v23 — Phase 3 (the profile→assessment bridge) and Phase 4 (step 2 completion)

**583 pre-existing tests still pass, unchanged. 52 new tests. 635 total.**

---

# First: a defect found while wiring Phase 3

## 30% of every deal rating could never score

Category **F (Sub-Sector, 20%)** had **zero parameters**. Six of category
**E (Sector, 10%)**'s seven sub-items had none either. Their weight was
carried in the config, redistributed silently by `weighted_rollup()` on every
assessment, and reported nowhere.

This is also why Phase 2 landed with nothing downstream: the benchmark table
was imported correctly and **no parameter consumed it**.

Three separate causes, all in config derivation:

1. **Shared ref codes.** The spec writes four sector metrics under
   `"E.1 / F.1"`, `"E.4 / F.4"`, `"E.5 / F.5"` — the same question asked of
   two populations. `".".join(ref.split(".")[:2])` turned `"E.1 / F.1"` into
   the parent `"E.1 / F"`, which matches no sub-item. `SEC_TAM`,
   `SEC_TAM_CAGR`, `SEC_PEER_AGE` and `SEC_PEER_RAISE_MTHS` all orphaned.

2. **`(ref)` suffixes.** `"C.6 (ref)"` became the parent `"C.6 (ref)"` while
   its scored sibling sat under `"C.6"`. Eight cross-check rows orphaned — and
   because `_reference_contradictions()` compares a ref against a scored row
   **in the same parent**, that check could never fire. It had never fired.

3. **No rows for the table-driven metrics.** Deal velocity, ticket size and
   active investors (E.2/E.3/E.6, F.2/F.3/F.6) are read from Sector Deal Data,
   not asked. No parameter declared them.

**Why nothing caught it.** Every existing check passed: weights summed to 100,
every scored parameter had a rubric, every rubric had four stages. A sub-item
with weight and nothing beneath it is invisible to all three.

**Fixed.** `clean_ref()` / `parent_of()` parse both shapes; the F-side of each
shared metric is seeded explicitly with mirrored cut-points; the six lookup
parameters are declared with the `lookup` scoring type (already in
`SCORING_TYPES`, never used) and handled in `band_parameter()` — the stored
value *is* the score, because a percentile computed at import over the whole
population must not be re-thresholded per assessment.

**Parameters: 76 → 86.** Two new integrity checks added, both of which fail
loudly on the old config:

```
check 2: every weighted sub-item has parameters ✓
check 3: every parameter rolls up to a real node ✓
```

## Coverage was its own denominator

`input_coverage()` was passed only the `ParameterValue` rows that existed, so
four answered parameters out of eighty reported **100%**. The figure was
meaningless, and worse, the 60% suppression gate that withholds a headline
score built on thin evidence **could never fire**.

Now measured against the model's parameter set; a parameter with no row is
what "unanswered" means. The medibuddy-shaped run above moved from a reported
100% to an honest **25.81%**.

---

# PHASE 3 — the profile finally reaches the scorecard

`fundos/assessment/` contained no reference to `CompanyProfile`. Extraction
read uploaded documents only, so a company researched exhaustively in step 1
scored blank in step 2 on parameters its own website answered.

## Step 1 side

`ProfileAssessmentInput` (migration `companyprofile.0012`) — one typed, sourced
value per parameter, keyed `(profile, input_key)`. A table rather than a
section because these are ~45 independent facts each needing its own
provenance and confidence, and Evidence & Workings requires a source per
figure.

`fundos/profile/assessment_extraction.py` emits them under **the workbook's
own `input_key` names**, so nothing needs translating downstream. New
`assessment_inputs` LLM role — declared in `LLM_ROLES` (migration `llm.0012`),
shipped prompt, schema, mock factory, ADVANCED tier. Runs at the end of both
the consolidated and per-section generation paths, isolated like a source
adapter: a failure marks itself and leaves the profile intact.

**What it does not extract, deliberately.** Category B comes from the uploaded
financial model — sourcing revenue or CM1 off a marketing site is the single
most damaging thing this module could do. Category G is internal to the
advisor. Both are asserted absent by test.

## Sub-sector resolution

Category F is 20% of the rating and the label picks one of 33 benchmark
groups. `resolve_sub_sector()` matches against the 104 raw labels Phase 2
loaded — exact, then group, then fuzzy above 88 — and returns
`(None, "unresolved")` rather than guessing. Wrong is worse than blank: blank
redistributes its weight, wrong quietly scores.

## Step 2 side

`fundos/assessment/profile_bridge.py`. Seeded **before** document extraction so
documents win where they overlap. Precedence is **source tier, not recency and
not confidence**:

```
1 the founder's own documents   2 company-owned   3 third party   4 inferred
```

A model can be 0.99 confident about a figure from a press article that the
company's audited model contradicts. Ranking by confidence lets the article
win. Founder confirmation outranks everything, including tier 1.

Document extraction now writes through the same `merge_value()`, so precedence
is decided in one place rather than by whichever writer ran last.

**Blank stays blank.** No zero-filling. §2.3.1 redistribution only works if
"we could not find it" is stored as absence — a zero is a claim, and an unmade
claim must not look like one.

---

# PHASE 4 — step 2 completion

**Stage gating.** `GET/POST …/assessment/stage`. Its own endpoint because
stage selects the cut-point column for every numeric parameter — the same
14-month runway is Fair at Series A and Good at Growth — so setting it
re-scores immediately and reports what moved. Generation now accepts a stage;
`resolve_initial_stage()` prefers an explicit choice, then the deal's own
stage, then defaults loudly. A confirmed stage clears `stage_was_defaulted`,
and the scorecard exposes `stageConfirmed`.

**Coverage shape.** `GET …/assessment/coverage`. 60% overall can mean every
category two-thirds answered or four complete and Financials empty — different
deals, different next actions. Gaps ranked by `unevidencedWeight`, the share of
the *rating* at stake, so a blank in Sub-Sector outranks one in Business
Quality.

**Review log.** `ReviewLog` shipped in migration 0001 and **nothing ever wrote
to it**. Now: every override writes a closed entry recording what it replaced,
and `…/assessment/review-log` raises, lists and resolves founder challenges.
Closing or rejecting requires `agentAction` — rejecting silently is the failure
this table exists to prevent, and the pattern of what gets rejected is how a
bad cut-point is found.

**Contradiction checks** now actually fire, as a consequence of the ref-code
fix above.

**Scorecard additions.** `evidenceMix` (scored rows by source) and
`openReviewCount`. A rating built mostly on researched public data is a
different claim from one built on the founder's own documents.

---

## Tests

`tests/api/test_v23_phase3_phase4.py` — 52 tests. The two headline defects are
asserted directly: no stranded sub-item weight, no orphaned parent, category F
non-empty, refs sharing a parent with what they cross-check, E and F scoring on
a real run, and coverage below 20% on two answered parameters out of eighty.
Plus the four precedence rules, sub-sector resolution including the deliberate
non-guess, and every Phase 4 endpoint over the real HTTP stack.

`makemigrations --check` reports no pending changes.

---

## Deploying

```bash
python manage.py migrate
python manage.py seed_assessment_config --config-version 2 --activate   # see below
python manage.py seed_platform_config --dry-run          # read this
python manage.py seed_platform_config --reset-prompts    # ships assessment_inputs
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
```

**`seed_assessment_config` must be re-run.** Config rows are versioned and never
edited in place, so the ten new parameters and the corrected parent codes
arrive as a new version. Seed without `--activate` first if you want to inspect
them; activating changes scores on the next run — which is the point, since
categories E and F begin scoring for the first time.

**`--reset-prompts` still matters**, and for the same reason as v21 and v22: a
`PromptTemplate` row overrides the shipped default entirely, so the new
`assessment_inputs` prompt will not reach the model without it. `diagnose_config`
lists which rows differ before you overwrite deliberate edits.

Then regenerate a company profile and a deal assessment, and check:

* `assessmentInputs.written` non-zero in the generation result
* `subSectorMethod` is `exact` or `group`, not `unresolved`
* categories **E and F carry a score** — the whole point of this release
* `coveragePct` is now a real number, and low on a thin deal

---

## Still open

Category **B (20%)** and **G (10%)** remain document- and advisor-sourced. B
fills from the uploaded financial model through the existing extraction path;
G has no input surface yet — the mandate context is advisor-entered and there
is currently no screen for it, so a deal with no uploaded model and no mandate
data will sit near 45% coverage with the headline suppressed. That suppression
is now correct behaviour rather than a bug, but the G input surface is the next
gap worth closing.
