# v24 — deployment, ingestion and post-deployment

Follow in order. **Part 3 is the one that decides whether Stage 2 can be
tested** — the ingestion in Part 2 will load cleanly regardless, but Stage 1
will keep collecting almost nothing until web search is on.

Set this once:

```bash
export WB=/path/to/Tyreplex_Deal_Assessment_v21_1.xlsx
```

Two things change existing numbers. Say so before anyone sees a dashboard:

* **Scores will move.** Categories E and F begin scoring, percentiles are
  re-banded, and E.1/F.1 now take the worse of TAM and growth.
* **Coverage will drop and some deals will suppress their headline.** That is
  the fix, not a regression.

---

# Part 1 — Deploy

## 1.1 Back up first

Config activation re-rates live assessments and there is no `--unactivate`.

```bash
pg_dump -Fc fundos > ~/fundos-pre-v24-$(date +%F-%H%M).dump

python manage.py shell -c "
from fundos.assessment.models import Assessment, ConfigParameter, SectorDealData
print('active params:', ConfigParameter.objects.filter(is_active=True).count())
print('benchmark rows:', SectorDealData.objects.count())
for a in Assessment.objects.exclude(status='superseded')[:10]:
    print(a.id, a.deal_stage, a.overall_score, a.input_coverage_pct)
"
```

Keep that output — Part 5 compares against it.

## 1.2 Code and migrations

```bash
tar -xzf fundos-backend_silk_ai_v24.tar.gz -C /path/to/releases/
# repoint 'current' per your convention
cd /path/to/current/fundos-backend
pip install -r requirements.txt

python manage.py migrate
python manage.py makemigrations --check --dry-run     # expect: No changes detected
```

Migrations are additive — new fields on `ConfigParameter` and the new
`ConfigConstant` table:

```
companyprofile.0012_assessment_inputs
llm.0012_assessment_inputs_role
assessment.0003_workbook_config
```

## 1.3 Prompts

```bash
python manage.py seed_platform_config --dry-run       # read this
python manage.py seed_platform_config --reset-prompts
```

`--reset-prompts` is required. A `PromptTemplate` row overrides the shipped
default entirely, so the rewritten `assessment_inputs` prompt — the one that
now carries the Data Dictionary guidance — will not reach the model without it.

---

# Part 2 — Ingest the workbook

The workbook is ingested **twice, for two different purposes**. Both read the
same file; neither replaces the other.

| Command | Reads | Produces |
|---|---|---|
| `import_assessment_workbook` | Company Profile, Rubrics, Anchors, A–G, Deal Scorecard, Data Dictionary | the **scoring model** |
| `import_sector_benchmarks` | Sector Deal Data | the **peer populations** for E and F |

## 2.1 Scoring model — dry run first

```bash
python manage.py import_assessment_workbook "$WB" --check
```

This parses and audits, writing nothing. Check the counts against your sheet:

```
input contract rows      80
monotonic rubrics        49
range rubrics             3
qualitative anchors      24
categories                7
sub-items                46
children                 19
scored leaves            60
percentile nodes          6
data dictionary rows     74
```

And the constants, which are read rather than assumed:

```
score_excellent 9   score_good 7   score_fair 5   score_poor 3
percentile_excellent 8   percentile_good 6   percentile_fair 4
min_deals_for_percentile 5
rating_excellent 8 … rating_challenging 4
```

**If any count is zero or the command refuses**, stop and send me the output.
It refuses rather than half-importing on three conditions: a duplicate input
key, category weights not summing to 100, or a key scored on a category sheet
that the input sheet does not collect.

`percentile nodes 6` is worth a second look — those are read from the Value
*formulae* on E.2/E.3/E.6 and F.2/F.3/F.6. Zero there means the file was saved
in a way that dropped formulae, and categories E and F will not score.

## 2.2 Scoring model — commit

```bash
python manage.py import_assessment_workbook "$WB" --config-version 2
python manage.py import_assessment_workbook "$WB" --config-version 2 --activate
```

All three integrity checks must pass before you activate:

```
check 1: category weights sum to 100 ✓
check 2: every weighted sub-item has parameters ✓
check 3: the input contract is exactly 80 rows ✓
config integrity: all checks passed
```

Verify:

```bash
python manage.py shell -c "
from fundos.assessment.models import ConfigParameter as P, ConfigConstant as K
q = P.objects.filter(is_active=True)
print('contract rows :', q.filter(is_workbook_input=True).count())    # 80
print('percentile    :', q.filter(scoring_type='lookup').count())     # 6
print('scored total  :', q.filter(feeds_score=True).count())          # 60
print('sub keys      :', sorted(q.filter(input_key__endswith='_SUB')
                                 .values_list('input_key', flat=True)))
print('constants     :', K.objects.filter(is_active=True).count())    # 13
"
```

The `_SUB` list must read exactly:

```
['SEC_PEER_AGE_SUB', 'SEC_PEER_RAISE_MTHS_SUB', 'SEC_TAM_CAGR_SUB', 'SEC_TAM_SUB']
```

If you see `SUB_TAM` instead, the old config is still active — re-check 2.2.

## 2.3 Benchmarks — dry run first

A re-import updates in place, so the dry run is the only safeguard.

```bash
python manage.py import_sector_benchmarks "$WB" --dry-run
```

Read four things:

1. **Three blocks** — 18 sectors, 33 clubbed groups, 104 raw mappings. If you
   see roughly 55 "sectors", block 3 is being read as block 1; do not commit.
2. **Skipped rows** — the `Total —` rows and the integrity-check rows below
   the tables, each with a reason.
3. **The diff** — created / updated / unchanged, field by field.
4. **Re-pointed mappings**, listed separately. A re-pointed label changes which
   population a company is judged against in a category worth 20%.

## 2.4 Benchmarks — commit and verify

```bash
python manage.py import_sector_benchmarks "$WB" --as-of $(date +%F)

python manage.py shell -c "
from fundos.assessment.models import SectorDealData as S, SectorMapping as M
print('sectors', S.objects.filter(level='sector').count(),
      '| subs', S.objects.filter(level='sub_sector').count(),
      '| maps', M.objects.count())
f = S.objects.get(level='sector', name='Fintech')
print('Fintech  ', f.deal_count, f.velocity_score, f.ticket_score, f.investors_score)
i = S.objects.get(level='sub_sector', name='Insurtech')
print('Insurtech', i.deal_count, i.velocity_score, i.ticket_score, i.investors_score)
w = S.objects.get(level='sector', name='Web3')
print('Web3     ', w.deal_count, w.velocity_score, w.ticket_score, w.investors_score)
"
```

Expected: **18 / 33 / 104**; Fintech `373 9.37 10.00 9.37`; Insurtech `15`
with three scores; **Web3 `4 None None None`** — four deals is under the
sheet's floor of five, so its percentiles are withheld exactly as the workbook
leaves those cells empty.

`Fintech` and `Insurtech` both existing is the point: the last run reported
them missing because this table was never loaded.

**Admin route (equivalent):** *Assessment → Sector deal data → Import
benchmarks*. Upload, leave **Write the changes** unticked for the dry run,
review, then re-submit ticked.

---

# Part 3 — Turn web search on

**Do this before regenerating anything.** Without it Stage 1 will now correctly
report that it has nothing to work with, which is honest but not useful.

Both live runs showed `searches=""` on every call and nine research adapters
returning fixtures. That is why yield was 4/24 and 3/24.

## 3.1 The advanced tier profile

Gemini rejects web search and structured output together, so one must be off.

**Admin → LLM → Config profiles → the advanced profile** (the code shown by
`diagnose_config` under the model chain, typically `tier.advanced.gemini`):

* `web_search` → **ON**
* `structured_output` → **OFF**

Or from the shell:

```bash
python manage.py shell -c "
from fundos.llm.models import LLMConfigProfile
from fundos.llm.tiers import resolve_tier_profile_code
code = resolve_tier_profile_code(role='assessment_inputs')
p = LLMConfigProfile.objects.get(code=code)
p.web_search, p.structured_output = True, False
p.save()
print(code, 'web_search', p.web_search, '| structured_output', p.structured_output)
"
```

`assessment_inputs` and `company_profile_deep_extract` both run on the advanced
tier, so this fixes both.

## 3.2 The research adapters

Nine adapters report `SKIP — fixture adapter`. A fixture reporting SKIP looks
identical to a company with no public data, which is why the runs looked
healthier than they were. Either point them at live feeds, or switch them off
in **Admin → Config → research sources** so the trace stops implying coverage
that does not exist.

## 3.3 Restart and confirm

```bash
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
curl -s https://<host>/api/v1/health | jq .
```

`diagnose_config` should report PASS on the model chain. Read every WARN.

---

# Part 4 — Ingest a company

Use one you know well — the point is checking values against your own
knowledge, which no automated test can do. **Plum is a good choice**: it
returned a live website last time, and both its sector (Fintech) and
sub-sector (Insurtech) exist in the benchmark table.

## 4.1 Generate the profile

```
POST /api/v1/companies/{companyId}/profile/deep-generate?force=true
```

`force=true` matters — without it the skip-if-unchanged gate returns
immediately and you will think the deploy failed when nothing ran.

Read the `assessmentInputs` block in the response:

```json
"assessmentInputs": {
  "asked": 61, "written": 30, "bands": 8, "benchmarks": 6,
  "unsourced_rejected": 4, "low_confidence": 3,
  "sub_sector_method": "exact", "coveragePct": 49.2,
  "sector": "Fintech", "subSector": "Insurtech", "subSectorGroup": "Insurtech"
}
```

| Field | Healthy | What it means otherwise |
|---|---|---|
| `asked` | **61** | anything else — config did not activate; re-check 2.2 |
| `written` | 25–45 | `0` with `error: ungrounded` → the grounding gate fired; see 4.2 |
| `benchmarks` | **6** | `0` → the benchmark table is empty or the sector name did not match |
| `sub_sector_method` | `exact` / `group` | `unresolved` → category F scores blank; see 4.3 |
| `unsourced_rejected` | a few | high → the model is still answering from memory without URLs |

Then open **Admin → Company Profile → Profile assessment inputs**, filter to
this company, and check ten rows against their source URLs. Ten minutes here is
worth more than any other check: a wrong value propagates into a weighted score
with a plausible citation attached.

## 4.2 If `error: ungrounded`

The run had under 400 characters of source. It refused rather than writing a
dossier from memory. Look at the `sources.*` steps in the log:

* website 403 or empty → upload the company's materials instead
* all research SKIP → Part 3.2
* documents 0 → upload the deck and financial model

This is the guard working. It is not a bug to route around.

## 4.3 If `sub_sector_method` is `unresolved`

The returned label matched none of the 104 raw labels. Fix as data:

```
Admin → Assessment → Sector mappings → Add
  raw_label     = <the label from the extraction>
  clubbed_group = <one of the 33 groups>
```

Re-run 4.1. Do not skip it — an unresolved sub-sector leaves 20% of the rating
unscored, and the redistribution quietly inflates every other category.

## 4.4 Generate the assessment

```
POST /api/v1/deals/{dealId}/assessment/generate
     { "dealStage": "Series A" }
GET  /api/v1/deals/{dealId}/assessment
```

Check, in order:

1. `stageConfirmed: true`
2. **Categories A, C, E and F all carry a non-null score.** B, D and G blank is
   correct without a financial model, deal terms and mandate data.
3. `coveragePct` believable — expect roughly 30–50% on research alone.
4. `evidenceMix` shows `profile` and `benchmark`.
5. Spot-check one percentile row in the drawer:
   `GET .../assessment/parameters/PCTL_E_2` — for Fintech the raw value should
   be `9.37`, the band **Excellent**, and the score **9.00**. If the score
   reads `9.37`, the old engine is still live.

## 4.5 Then upload the financial model and re-run

Category B should score, coverage should rise, and any parameter both research
and the model answered must flip to `sourceType: "document"` — documents are
tier 1 and outrank research regardless of confidence.

---

# Part 5 — Expected regressions

Re-run the Part 1 snapshot. Every existing assessment should show lower
coverage, a moved score, and some newly suppressed headlines. All three are the
fixes landing.

Genuine faults look different:

| Observed | Means |
|---|---|
| `asked` is 24, not 61 | old config still active — re-check 2.2 |
| `SUB_TAM` present in config | same |
| percentile score equals the raw percentile | old engine — code did not deploy |
| Web3 carries scores | benchmarks imported under the old rule — re-run 2.4 |
| E and F still null | benchmark table empty, or sub-sector unresolved |
| `written: 0`, `error: ungrounded` | Part 3 not done |

---

# Part 6 — Rollback

```bash
python manage.py import_assessment_workbook "$WB" --config-version 1 --activate
```

Config is versioned, so the scoring model reverts without touching code.
Assessments generated under v2 keep their `config_version` stamp and stay
reproducible. The benchmark table has **no version** — if an import was wrong,
re-import from the correct workbook and the dry run will show what it restores.

---

# Part 7 — What to send back

1. **`var/logs/silk_generation.log`**, trimmed to the run window.
2. **Raw JSON** from 4.1 (the generation response) and 4.4 (the scorecard).
3. **Output of** `import_assessment_workbook --check` and
   `import_sector_benchmarks --dry-run`.
4. `diagnose_config`.

The lines I read first:

| Line | Question |
|---|---|
| `step="llm.call.assessment_inputs" … searches=` | did web search actually fire? |
| `ASSESSMENT INPUTS …: {asked, written, unsourced_rejected}` | how much was collected, and how much was thrown away for having no source |
| `BRIDGE …: {seeded}` | how much reached the scorecard |
| `refusing to extract — only N characters` | the grounding gate fired, and why |
| `is not in the sector benchmark table` | a name mismatch between research and the sheet |

`asked=61` with `searches` non-empty and `written` in the twenties or thirties
is the shape of a healthy run. `asked=61` with `written=4` means Part 3 did not
take.
