# v23 — Deployment, benchmark ingestion, and post-deployment runbook

| Part | Covers |
|---|---|
| **1** | Pre-flight — snapshot and backup |
| **2** | Deploying the code, migrations and configuration |
| **3** | **Ingesting the benchmark workbook** — in full |
| **4** | Post-deployment verification, end to end |
| **5** | Expected regressions, and how to tell them from faults |
| **6** | Rollback |
| **7** | What to send back, and what I read in it |

Two things in this release deliberately change existing numbers. Tell whoever
watches the dashboards before you start:

* **Activating the scoring config changes scores.** Categories E and F begin
  scoring for the first time; every deal's overall rating will move.
* **Coverage will drop sharply**, from a meaningless ~100% to the real figure.
  Deals under 60% will have their headline score suppressed.

Set this once and reuse it:

```bash
export WB=/path/to/Tyreplex_Deal_Assessment_v21_1.xlsx
```

---

# Part 1 — Pre-flight

```bash
pg_dump -Fc fundos > ~/fundos-pre-v23-$(date +%F-%H%M).dump

python manage.py shell -c "
from fundos.assessment.models import Assessment, ConfigParameter, SectorDealData
print('active params:', ConfigParameter.objects.filter(is_active=True).count())
print('benchmark rows:', SectorDealData.objects.count())
for a in Assessment.objects.exclude(status='superseded')[:10]:
    print(a.id, a.deal_stage, a.overall_score, a.input_coverage_pct)
"
```

Expect **76** active parameters. Keep this output — Part 5 compares against it.
There is no `--unactivate`, so the dump is your only route back.

---

# Part 2 — Deploy

## 2.1 Code

```bash
tar -xzf fundos-backend_silk_ai_v23.tar.gz -C /path/to/releases/
# repoint 'current' per your convention
cd /path/to/current/fundos-backend
pip install -r requirements.txt
```

## 2.2 Migrations

Both are additive — a new table and a widened choice list. Nothing is dropped.

```bash
python manage.py migrate
python manage.py makemigrations --check --dry-run     # expect: No changes detected
```

```
companyprofile.0012_assessment_inputs      # ProfileAssessmentInput
llm.0012_assessment_inputs_role            # adds 'assessment_inputs' to role choices
```

## 2.3 Prompts

```bash
python manage.py seed_platform_config --dry-run       # READ THIS
python manage.py seed_platform_config --reset-prompts
```

`--reset-prompts` is **required**, not optional. A `PromptTemplate` row
overrides the shipped default entirely, so the new `assessment_inputs` prompt
will not reach the model without it — the same failure that silently cost you
the v21 deep-extract rewrite.

If you have deliberately edited prompts in Admin, run `diagnose_config` first
(it names every row differing from the shipped text) and re-apply those edits
after.

Role bindings: 33 → **34**.

## 2.4 The scoring model

This is the step that moves scores. Seed inactive, inspect, then activate.

```bash
python manage.py seed_assessment_config --config-version 2
python manage.py seed_assessment_config --check
```

All three checks must pass. Two of them are new in v23:

```
check 1: category weights sum to 100 ✓
check 2: every weighted sub-item has parameters ✓
check 3: every parameter rolls up to a real node ✓
config integrity: all checks passed
```

**If check 2 or 3 fails, stop.** Activating would ship a scoring model with
weight that can never be scored — the exact defect this release fixes.

```bash
python manage.py seed_assessment_config --config-version 2 --activate

python manage.py shell -c "
from fundos.assessment.models import ConfigParameter as P
q = P.objects.filter(is_active=True)
print('total', q.count(), '| F', q.filter(category_code='F').count(),
      '| E', q.filter(category_code='E').count())
print(sorted(q.filter(scoring_type='lookup').values_list('input_key', flat=True)))
"
```

Must read **86 / 7 / 11**, and exactly:

```
['SEC_INVESTORS','SEC_TICKET','SEC_VELOCITY','SUB_INVESTORS','SUB_TICKET','SUB_VELOCITY']
```

Those six parameters are the only consumers of the benchmark table. **If they
are missing, Part 3 will import perfectly and score nothing** — which is
precisely the state v22 shipped in.

## 2.5 Restart and verify

```bash
sudo systemctl restart fundos-web fundos-worker
sudo systemctl status fundos-web fundos-worker --no-pager
python manage.py diagnose_config
curl -s https://<host>/api/v1/health | jq .
```

Read every WARN from `diagnose_config`, particularly prompt-drift rows.

---

# Part 3 — Ingesting the benchmark workbook

Categories E (10%) and F (20%) are scored against this table, so **30% of
every deal rating rests on this one ingest.** It is worth doing carefully once
rather than quickly three times.

## 3.1 What the importer is actually reading

The `Sector Deal Data` sheet is **three tables, not one**. This is the single
thing most likely to go wrong, and it fails silently rather than loudly:

| Block | Where | Holds | Used for |
|---|---|---|---|
| 1 | Left columns, first header | **18 sectors** | Category E scoring |
| 2 | Right columns of the *same* block | **104 raw sub-sector labels** → clubbed group | The label→group mapping only |
| 3 | Left columns, under a *second* header further down | **33 clubbed sub-sector groups** | Category F scoring |

Category F scores against **block 3**, not block 2 — a clubbed group
aggregates enough deals for a percentile to mean something, whereas a single
raw label usually does not.

Block 2 is **taller** than the sector block beside it (104 rows against 18).
Bounding it to block 1 keeps 21 mappings and drops 83, which is how a company
ends up unmatched against a benchmark group it should have hit.

Blocks are detected by finding every row carrying a deal-count column, and
each block ends at its **first blank name row**. Two traps live in your
workbook and are filtered explicitly:

* A `Total — Sector Table` row carries a deal count and would import as a
  sector.
* The sheet's own integrity checks sit *below* the tables with numbers in the
  same columns (`Deals in source extract (input)  2699`) and would import as
  scoreable rows.

## 3.2 Column detection

Headers are matched on a normalised form — lowercased, alphanumerics only — so
`# Deals`, `#Deals` and `Deals` all land on the same field, and a cosmetic
re-export does not break the import.

**The full list of accepted headers, the mandatory columns and the two
ordering rules are in 3.6 — read that before preparing a workbook.**

The dry run prints the detected mapping per block, so you can confirm every
column landed where you expect before anything is written.

## 3.3 How scores are decided

The workbook's own **Velocity / Ticket / Investors scores are preserved**
wherever the sheet carries them. An import reproduces the sheet rather than
quietly recomputing it, so the number an analyst can see is the number that
scores.

Percentile ranking fills gaps only, and:

* Ties share a rank.
* A missing value scores **nothing**, not zero — following the model's own
  "blank is not zero" rule.
* A single-row population gets no score, because a percentile against yourself
  is not information.
* Average ticket is derived where absent.
* Rows under five deals are flagged `Thin` in `sample_quality`.

Ranking happens **once, at import, over the whole population**. It is stored
rather than recomputed at read time, because a rank depends on every other row
— recomputing later after a partial refresh would silently re-score rows
nobody touched.

## 3.4 Re-import updates in place

There is no versioning on this table. A refresh is *meant* to move ratings:
newer market data should change what "a fast-moving sector" is, and an
assessment re-run afterwards ought to reflect it.

**So the dry run is the safeguard.** It names every row that would move, field
by field, with before and after — and lists re-pointed sub-sector mappings
separately, because a re-pointed label changes which benchmark group a company
is judged against in a category worth 20%.

## 3.5 Running it — CLI

**Always dry-run first.**

```bash
python manage.py import_sector_benchmarks "$WB" --dry-run
```

Optional flags:

| Flag | Purpose |
|---|---|
| `--sheet` | Sheet name if it is not `Sector Deal Data` |
| `--as-of YYYY-MM-DD` | The date this data represents. Defaults to today. |
| `--dry-run` | Report everything, write nothing |

Read the output in this order:

**a. Block detection.** You should see three blocks with their header row and
row count, then the per-field column mapping:

```
Block 1 (sector) — header row 3, 18 rows:
    name                     <- 'Sector'
    deal_count               <- '# Deals'
    ...
Raw sub-sector -> clubbed group (104 pairs):
    name                     <- 'Raw Sub-Sector'
    clubbed_group            <- 'Clubbed Group'
Block 3 (sub_sector) — header row 27, 33 rows:
    ...
```

If you see roughly **55 "sectors"**, block 3 is being read as block 1 — the
sheet layout has changed and you must not commit.

**b. Skipped rows.** The totals row and the integrity-check rows should appear
here with a reason. Anything else skipped is worth understanding before you
proceed.

**c. The diff.** Created / updated / unchanged counts, with the field-level
before-and-after on each updated row.

**d. Re-pointed mappings.** Listed separately as
`<raw label>: <old group> -> <new group>`. Read every line. These change which
population a company is scored against.

Then commit:

```bash
python manage.py import_sector_benchmarks "$WB" --as-of $(date +%F)
```

## 3.6 The required workbook format

A template is shipped alongside this document as
**`sector_deal_data_TEMPLATE.xlsx`**. It is not illustrative — it is parsed by
the importer as part of the test suite, so if you match its shape the ingest
will work.

### 3.6.1 The sheet

| | |
|---|---|
| **Sheet name** | `Sector Deal Data` — or pass `--sheet "<name>"` / set it in the admin form |
| **Format** | `.xlsx` (openpyxl). Formulas are fine; values are read, not formulas |
| **Other sheets** | Ignored entirely. The workbook may contain anything else |

If the sheet is missing, the importer raises and **names the sheets it did
find**, so a rename is a one-line fix rather than a guess.

### 3.6.2 Layout

```
      A            B         C          D            E         F        G       H     I      J              K
 1  Sector      # Deals  Raised $Mn  Avg Ticket  # Investors  Vel.Sc  Tkt.Sc  Inv.Sc │   Raw Sub-Sector  Clubbed Group
 2  Ecommerce      606     4977.55       8.21        410       10      2.5     10    │   B2B SaaS        SaaS
 3  Technology     488     3910.20       8.01        377        9.1    2.4      9.4  │   Enterprise SaaS SaaS
 ..            ← BLOCK 1: 18 sectors →                                               │   ← BLOCK 2 →
 20 Total — Sector Table   1003                                                      │   Payments        Payments
                                                                                     │   ..  104 rows, runs PAST block 1
 24 Clubbed Sub-Sector  # Deals  Raised $Mn  Avg Ticket  # Investors  Vel  Tkt  Inv
 25 SaaS                  120      880.40       7.34         96        7    6    8
 ..            ← BLOCK 3: 33 clubbed groups →
 58 Total — Sub-Sector Table  457
 60 Deals in source extract (input)   2699      ← integrity checks, ignored
```

**Block 1** starts at the first header row and holds the sectors.
**Block 2** occupies the columns to the *right* of block 1's last detected
column, in the same header row, and is scanned to the **end of the sheet** —
it is expected to be longer than block 1 (104 rows against 18).
**Block 3** starts at the next header row further down and holds the clubbed
groups that category F actually scores against.

### 3.6.3 Column headers

Headers are matched on a normalised form — lowercased, alphanumerics stripped
— so `# Deals`, `#Deals`, `Deals` and `No. of Deals` all resolve to the same
field, and a cosmetic re-export will not break the import.

**Blocks 1 and 3** (both read with the same spec):

| Field | Required | Accepted headers |
|---|---|---|
| `name` | **yes** | Sector, Name, Sector Name, Sub-Sector, Clubbed Sub-Sector, Clubbed Group, Group |
| `deal_count` | **yes** | # Deals, Deal Count, No. Deals, Number of Deals |
| `total_raised_usd_mn` | recommended | Raised $Mn, Raised, Total Raised, Amount Raised |
| `avg_ticket_usd_mn` | optional | Avg Ticket $Mn, Avg Ticket, Average Ticket |
| `investor_count` | recommended | # Investors, Investor Count, No. Investors |
| `velocity_score` | optional | Velocity Score |
| `ticket_score` | optional | Ticket Score |
| `investors_score` | optional | Investors Score, Investor Score |
| `sample_quality` | optional | Sample Quality, Quality |

**Block 2** — both columns required, or no mappings are read at all:

| Field | Accepted headers |
|---|---|
| `name` | Raw Sub-Sector, Sub-Sector, Raw Label, Name |
| `clubbed_group` | Clubbed Group, Group, Clubbed |

### 3.6.4 Four rules that are part of the format

**1. `name` and `# Deals` are mandatory in every block.** Header rows are
*found* by looking for a deal-count column, so a block without one is not seen
at all. A block whose name column is not recognised reads **zero rows** and
reports it only as a quiet `0 rows` in the block summary — always check the row
counts in the dry run against what you expect.

**2. Count columns must come before their score columns.** `Investors Score`
normalises to `investorsscore`, which *starts with* `investors` — so placed to
the **left** of `# Investors` it is captured as the investor count and the real
count is never read. Order the columns as the template does:

```
… # Investors │ Velocity Score │ Ticket Score │ Investors Score
        ^ counts first                              ^ scores after
```

**3. Each block ends at its first blank name row.** This is what keeps the
sheet's own integrity checks — which sit below the tables carrying numbers in
the same columns — out of the benchmark table. Leave at least one fully blank
row between the last data row and anything below it. Do **not** leave blank
rows *inside* a block; the block will end there.

**4. Totals rows are filtered by name.** Anything starting with `Total` or
`Grand Total`, and the literals `Name`, `None`, `Sr. No.`, are dropped. A
totals row named otherwise would import as a sector, so keep the `Total —`
prefix.

### 3.6.5 Values

* **Currency**: `Raised $Mn` and `Avg Ticket $Mn` are US$ millions. Numbers
  only — currency symbols, commas and `%` are stripped, but keep the unit out
  of the cell.
* **Scores**: 0–10. Supplied scores are **preserved**; blanks are filled by
  percentile ranking across that block. So you can supply all, none, or some.
* **`Avg Ticket`** is derived as `Raised ÷ Deals` when left blank.
* **Blank ≠ zero.** A blank cell means "not stated" and is treated as such.
  Enter `0` only when zero is the fact.
* **Rows under 5 deals** are flagged `Thin` automatically — the percentile is
  reported but the population is too small to lean on.
* A row with a name but **no deal count** is skipped and listed in the dry
  run's skipped section with its row number.

### 3.6.6 Getting the sub-sector mapping right

Block 2 is the layer that decides which benchmark group a company is judged
against, in a category worth **20% of the rating**. Two things to get right:

* Every `Clubbed Group` value in block 2 should exist as a **row name in
  block 3**. A raw label mapping to a group with no benchmark row resolves and
  then finds nothing to score against.
* Include every spelling variant you see in your deal flow, one row each. This
  is the cheapest place to fix a matching failure — and if you find one after
  the import, add it in **Admin → Assessment → Sector mappings** rather than
  re-importing.

## 3.7 Running it — Admin UI

Equivalent to the CLI, and better when a non-engineer owns the refresh:

**Assessment → Sector deal data → Import benchmarks**

1. Upload the `.xlsx`; optionally set the sheet name and as-of date.
2. Leave **Write the changes** unticked → the screen returns the detected
   column mapping per block, the raw mapping pairs, the skipped rows with
   reasons, and the full dry-run diff. **Nothing is written.**
3. Review — particularly the per-block row counts and the detected column
   mapping — then re-submit with **Write the changes** ticked to commit.

The upload is spooled to a temp file (openpyxl needs a real path) and removed
either way.

## 3.8 Verify the load

```bash
python manage.py shell -c "
from fundos.assessment.models import SectorDealData as S, SectorMapping as M
print('sectors:', S.objects.filter(level='sector').count())          # 18
print('sub-sectors:', S.objects.filter(level='sub_sector').count())  # 33
print('mappings:', M.objects.count())                                # 104
print('thin:', S.objects.filter(sample_quality='Thin').count())
r = S.objects.get(level='sector', name__iexact='Ecommerce')
print(r.deal_count, r.total_raised_usd_mn, r.avg_ticket_usd_mn,
      r.velocity_score, r.ticket_score, r.investors_score)
"
```

Ecommerce must read **606 / 4977.55 / 8.21 / velocity 10 / ticket 2.5 /
investors 10**, matching the sheet exactly. If it does not, the column mapping
is wrong — go back to the dry run rather than patching rows by hand.

## 3.9 Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `no sheet named 'Sector Deal Data'` | Sheet renamed | `--sheet "<actual name>"`; the error lists the sheets present |
| `no header row found — expected a column of deal counts` | Header changed beyond the alias list | Add the alias, or rename the column in the sheet |
| ~55 sectors, 0 sub-sectors | Block 3 read as block 1 | The second header lost its deal-count column; restore it |
| 21 mappings instead of 104 | Block 2 bounded to block 1's height | Re-export; the raw table must run past the sector block |
| A sector called `Total — Sector Table` | Totals row not filtered | Should not occur; if it does, send me the dry run |
| `Nothing to import` (exit 1) | Wrong sheet, or no deal-count column | Check `--sheet` first |

## 3.10 Refreshing later

Re-run 3.5 with a new `--as-of`. Because it updates in place, an assessment
re-run afterwards will score against the new data — intentionally. Assessments
already generated keep their stored scores and stay reproducible; they are only
re-rated if someone re-runs them.

Track which labels went unresolved between refreshes. A raw label appearing in
your deal flow but absent from `SectorMapping` is a 20%-weight gap on every
company carrying it, and it is fixed as data (Part 4.2), never as a deploy.

---

# Part 4 — Post-deployment verification

Use **one real company you know well**. The point is checking values against
your own knowledge, which no automated test can do.

## 4.1 Generate the company profile

```
POST /api/v1/companies/{companyId}/profile/deep-generate?force=true
```

`force=true` matters. Without it the skip-if-unchanged gate returns
immediately when the sources have not moved, and you will conclude the deploy
failed when nothing ran at all.

Poll the job, then read the new Phase 3 block:

```json
"assessmentInputs": {
  "asked": 34, "written": 21, "bands": 6, "benchmarks": 6,
  "low_confidence": 2, "sub_sector_method": "exact",
  "sector": "...", "subSector": "...", "subSectorGroup": "..."
}
```

| Field | Healthy | Otherwise |
|---|---|---|
| `written` | 15–30 | `0` — extraction returned nothing usable; check the website payload |
| `sub_sector_method` | `exact` / `group` | `unresolved` — **category F (20%) scores blank.** See 4.2 |
| `benchmarks` | `6` | `0` or `3` — a sector or sub-sector has no benchmark row |
| `low_confidence` | a few | mostly — sources are thin; values stored but weak |

Then open **Admin → Company Profile → Profile assessment inputs**, filter to
this company, and check ten rows against their source URLs. This is the
highest-value ten minutes in the exercise: a wrong value here propagates into a
weighted score with a plausible citation attached.

## 4.2 If `sub_sector_method` is `unresolved`

The returned label matches none of the 104 raw labels. Fix it as data:

```
Admin → Assessment → Sector mappings → Add
  raw_label     = <the label from the extraction>
  clubbed_group = <one of the 33 groups>
```

Re-run 4.1. **Do not skip this.** An unresolved sub-sector leaves 20% of the
rating unscored, and the redistribution quietly inflates every other category —
the scorecard looks healthy while being materially wrong.

## 4.3 Generate the assessment

```
POST /api/v1/deals/{dealId}/assessment/generate
     { "dealStage": "Series A" }
```

Pass the stage explicitly. Stage selects the cut-point column for every
numeric parameter — a 14-month runway is Fair at Series A and Good at Growth.

## 4.4 Read the scorecard

```
GET /api/v1/deals/{dealId}/assessment
```

In order:

1. `stageConfirmed: true`, `stageWasDefaulted: false`.
2. **Categories E and F both carry a non-null `score`.** This is the headline
   fix. Null means the benchmark lookup never reached the assessment — return
   to 4.1 and check `benchmarks` and `sub_sector_method`.
3. `coveragePct` believable, not 100. With no financial model, expect ~25–45%.
4. `scoreSuppressed` true below 60%, with `missingEvidence` naming the
   documents that would recover the most weight.
5. `evidenceMix` — the split across `profile`, `document`, `benchmark`.

## 4.5 The precedence test

Upload the financial model and re-run 4.3. Category B should score and
coverage should rise. Then check a parameter both sources answered:

```
GET /api/v1/deals/{dealId}/assessment/parameters/FIN_REV_SCALE
```

`sourceType` must now be `document`. Documents are tier 1 and outrank research
regardless of confidence. If a researched value survived, the merge is not
applying — send me that response.

A re-run **supersedes** rather than overwrites. `GET …/assessment/versions`
shows both with a parameter-level diff; use it to confirm only what you
expected moved.

## 4.6 Phase 4 endpoints

```
D=/api/v1/deals/{dealId}/assessment

GET   $D/stage
POST  $D/stage        { "dealStage": "Growth" }
GET   $D/coverage
POST  $D/review-log   { "fieldOrTopic": "TEAM_FDR_EXP", "founderValue": "11",
                        "founderReason": "Counts incorporated years only." }
PATCH $D/review-log/{entryId}
                      { "status": "closed", "agentAction": "Corrected from ROC filing." }
POST  $D/parameters/TEAM_FDR_EXP/override
                      { "score": 9, "reason": "Two prior exits in category." }
GET   $D/review-log
```

* The stage response reports `previousStage` / `previousScore` — confirm the
  score actually moved.
* In `/coverage`, work down `byPriority`. `unevidencedWeight` is rating weight
  left unevidenced, so a Sub-Sector gap (20%) outranks a Business Quality one
  (10%) at similar parameter counts.
* `agentAction` is required when closing or rejecting. Rejecting silently is
  the failure this table exists to prevent.
* The override must appear in the review log automatically, and the parameter
  drawer must still show the original `system_band` beside it.

## 4.7 Contradiction checks

These have **never fired in production** — the ref rows were orphaned into
their own parent bucket and could never be compared against what they
cross-check. Check `GET $D/findings` for `ref_contradiction`. The clearest
case is healthy top-5 client concentration alongside a top-1 client that is
most of revenue. Expect findings on deals that previously showed none.

---

# Part 5 — Expected regressions

Re-run the Part 1 snapshot. Every existing assessment should show:

* **Lower coverage**, often dramatically.
* **A moved overall score**, as E and F now contribute instead of
  redistributing their weight away.
* **Some deals newly suppressed** below 60%.

All three are the fixes landing. Genuine faults look different:

| Observed | Means |
|---|---|
| Coverage still ~100% | Config did not activate — recheck 2.4 |
| E and F still null | Lookups not reaching assessments — recheck 2.4's six keys and Part 3 |
| Scores identical everywhere | New config seeded but inactive |
| `benchmarks: 0` in 4.1 | Sector name mismatch between research and the workbook |

---

# Part 6 — Rollback

Config is versioned, so the scoring model reverts without touching code:

```bash
python manage.py seed_assessment_config --config-version 1 --activate
```

Assessments generated under v2 keep their `config_version = 2` stamp and stay
reproducible. For a full rollback, restore the Part 1 dump — note that the two
migrations are additive, so v22 code runs against the v23 schema without issue.

The benchmark table has **no version to roll back to**. If an import was
wrong, re-import from the correct workbook; the dry run on that re-import will
show you exactly what it restores.

---

# Part 7 — What to send back

One caveat worth knowing: **until v23, `fundos.assessment` was not routed to
the diagnostic file at all.** The profile half of the pipeline was captured and
the scoring half was not, so every log you have sent me had nothing in it about
extraction, the bridge, or scoring. Fixed in this release — the file now covers
both halves.

Send:

1. **`var/logs/silk_generation.log`** (or `$FUNDOS_LOG_DIR/silk_generation.log`).
   Trim to the run window if large.
2. **Raw JSON** from 4.4 (scorecard), 4.6 (`/coverage`) and `GET $D/findings`.
3. **Output of** `diagnose_config` and `seed_assessment_config --check`.
4. **The benchmark dry-run output** from 3.5, if anything in it surprised you.

What I read, so you know what matters if you trim:

| Line | Question it answers |
|---|---|
| `ASSESSMENT INPUTS <id>: {...}` | How much did step 1 extract, and did the sub-sector resolve? |
| `BRIDGE <id>: {...}` | How much reached the scorecard; what was dropped as unknown or blank |
| `EXTRACT <id>: {...}` | What documents added, and how much the merge kept over research |
| `no rubric for X at stage Y` | A parameter blank for every company at that stage — a config gap |
| `sub-sector %r matched no benchmark group` | 20% unscored; needs a `SectorMapping` row |
| `context_chars=` / `searches=` | Cost, and whether the advanced tier is actually searching |
| `finish_reason=` / `was_repaired=` | Truncation and repair — the v21 failure mode |

The first two numbers I look at are **`written`** on the extraction and
**`seeded`** on the bridge. A healthy first with a much smaller second means
the workbook and the prompt have drifted apart — a config fix, not code.
