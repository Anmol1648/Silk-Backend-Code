# v24 — the workbook IS the configuration, and Stage 1 must be grounded

**684 tests pass** (583 pre-existing, 65 v23, 36 new).

---

## The one cause behind every defect in v23

The scoring model existed **twice**: in the workbook, and again in Python —
once transcribed into `spec_config_data.py`, once assumed by the seeder. Two
copies of a truth are free to drift, and every v23 scoring defect was that
drift, not a misunderstanding of the domain:

| Defect | Workbook says | v23 did |
|---|---|---|
| Sub-sector keys | `SEC_TAM_SUB` (suffix) | `SUB_TAM` (prefix) — matched nothing |
| Percentile nodes | computed on the category sheet | six parameters invented, inflating a contract the sheet counts as 80 |
| Percentile scoring | band at 8/6/4, then score 9/7/5/3 | stored the percentile as the score |
| Thin samples | under 5 deals score **blank** | flagged `Thin` and scored anyway |
| E.1 / F.1 | the **worse** of TAM and CAGR | averaged the two |

The `_SUB` suffix is load-bearing: the sheet's own band formula opens with
`LET(key, SUBSTITUTE($C22,"_SUB",""), …)`. A key spelled the other way
resolves to no rubric and no parameter.

**Fixed at the cause.** Config is now READ FROM THE WORKBOOK. A revision is a
re-import, not a release, and there is no second copy left to drift.

---

## Workbook ingestion

`import_assessment_workbook <file> [--check] [--config-version N] [--activate]`

Reads seven sheets into versioned config:

| Sheet | Yields |
|---|---|
| Company Profile C22:C101 | the **80-row input contract** — ref, key, name, unit |
| Rubrics Table 1 | 49 metrics × 4 stages × 3 cut-points |
| Rubrics Table 2 | 3 range rubrics with tolerances |
| Qualitative Anchors | 24 × four written band definitions |
| A–G category sheets | the node tree: weights, parent/child, key per leaf |
| Deal Scorecard | band scores, percentile cut-offs, sample floor, rating ladder |
| Data Dictionary | per-parameter definition, where to find it, what to do if missing |

Verified against your file: **80 inputs, 49+3 rubrics, 24 anchors, 7 categories
summing to 100, 46 sub-items, 19 children, 60 scored leaves, 6 percentile
nodes, 74 dictionary rows, 13 constants.**

Three points of care:

* **Percentile nodes are detected from their Value FORMULA**, not their label
  — the label is prose, the column reference decides which number scores. A
  values-only read cannot see it, which is how the first attempt found zero of
  them on a blank template.
* **A re-import replaces the version rather than merging it.** Merging would
  leave a parameter deleted from the workbook alive in config — exactly how
  the previous copy drifted.
* **Structural problems refuse the import**: duplicate input key, category
  weights not summing to 100, or a key scored on a category sheet that the
  input sheet does not collect.

`ConfigConstant` is new (migration `assessment.0003`). The percentile cut-offs
and the sample floor are labelled "editable" in the sheet — they are tuning
dials, and they belong in config rather than in Python.

---

## Engine fidelity

* **Percentiles are banded before they are scored.** Fintech's ticket
  percentile of 10 now bands Excellent and scores 9; Ecommerce's 2.5 bands
  Poor and scores 3. Previously they scored 10 and 2.5 — and a raw 10 also
  reached a value reserved for human override.
* **The sample floor is honoured.** Web3 has four deals; the shipped workbook
  leaves its three score cells empty, and so do we. Excluded from the ranking,
  scored blank, weight redistributed.
* **E.1 / F.1 take the worse of TAM size and growth.** A TAM of 14.5 alone is
  Excellent; with growth at 18% the node scores Good, because the sheet's note
  says a large but stagnant market is not attractive. Averaging let size mask
  stagnation.
* **Band scores read from the scoring key** rather than a Python constant.

---

## Stage 1 — the part that actually blocked testing

The live runs collected **4 of 24** and **3 of 24** parameters, issued **zero
web searches**, and one populated **251 fields from 2 characters** of source.
That output reads as authoritative, cannot be cited or dated, and on a
scorecard is worse than a blank — a blank is visibly blank.

**The question set is now read from config**, so it carries all 61 researchable
parameters under the workbook's own key spellings, and each one arrives with
the Data Dictionary's guidance attached — instructions nobody would have
guessed, like *"treat an unmentioned seat as vacant, not as unknown"* and
*"mark Not Enough Information rather than inferring from age"*.

**Two guards make an ungrounded answer impossible to bank:**

1. **The grounding gate.** Below 400 characters of real source across website,
   documents and research, extraction refuses to run and records why.
2. **No URL, no value.** Every value and every band must carry a source URL or
   it is discarded and counted in `unsourced_rejected`.

Both are visible in the log, so a thin run now reports thinness instead of
producing a confident dossier.

**Still required in configuration** — the guards stop bad data, they do not
create good data:

* Web search ON and structured output OFF on the advanced tier (Gemini rejects
  the two together). This is the single highest-value fix; `searches=""` on
  every call is why yield was 17%.
* The nine research adapters return fixtures. Point them at live feeds or turn
  them off — a fixture reporting SKIP looks identical to a company with no
  public data.

---

## Deploying

```bash
python manage.py migrate
python manage.py import_assessment_workbook <workbook.xlsx> --check
python manage.py import_assessment_workbook <workbook.xlsx> \
    --config-version 2 --activate
python manage.py import_sector_benchmarks <workbook.xlsx> --dry-run
python manage.py import_sector_benchmarks <workbook.xlsx>
python manage.py seed_platform_config --reset-prompts
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
```

`import_assessment_workbook` replaces `seed_assessment_config`, which remains
only as a fallback for environments without the file. Activating changes
scores — run `--check` first and read the three integrity checks.

Then, before regenerating anything, fix web search on the advanced tier.
Without it Stage 1 will now correctly report that it has nothing to work with,
which is more honest than v23 but no more useful.

---

## The acceptance test still open

The workbook you supplied is a **blank template** (`Inputs Captured (Of 80) =
0`). Structure and cut-points are verified against it; arithmetic is not.

Send one **completed** assessment — the same workbook with the Value column
filled and scores calculated — and the suite will assert every band, applied
weight, category score and the overall rating match the sheet cell-for-cell.
That turns "we implemented the mechanism" into "we reproduce your workbook",
which is the only claim worth making.
