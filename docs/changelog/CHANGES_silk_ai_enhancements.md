# Silk AI — Backend Changes

What was added, why, and how to run it. Everything here is verified against a
real build: `manage.py check` passes, migrations generate and apply, the full
workbook imports, and the test suite runs green.

---

## 1. What was added

### `fundos/investors/` — new app (Stage 3)

| File | Purpose |
|---|---|
| `models.py` | Investor universe, deal data, taxonomy, matching config, refresh history |
| `matching.py` | The matching engine, ported from the source workbook |
| `normalizer.py` | Sector normalisation, DB-backed |
| `identity.py` | Investor identity resolution + manual LLM adjudication |
| `classify.py` | LLM investor classification |
| `pipeline.py` | The five-stage refresh with dry-run support |
| `importer.py` | Bulk workbook import |
| `sources.py` | Source adapters, circuit breaker, retry, sanitisation |
| `admin.py` | Configuration surface + refresh console + merge tools |
| `views.py`, `serializers.py`, `urls.py` | Discovery API |

### `fundos/assessment/` — the missing connection

The scoring engine was built, configured, and referenced by nothing outside
its own seeder and tests. Added:

- `views.py` — scorecard, parameter drawer, override, findings, band advancement, versions
- `urls.py` — the routes, which did not exist
- `tasks.py` — generation orchestration and the AI rubric review pass
- `extraction.py` — document readers and evidence extraction

### `fundos/diagnostics/` — new app

`models.py`, `services.py`, `views.py`, `admin.py`, `urls.py`. Run records,
correlated events, a database log handler, correlation middleware, health
checks, and an admin dashboard.

### Wiring

`fundos/settings/base.py` — two apps registered, correlation middleware added.
`fundos/urls.py` — three URL modules included.

---

## 2. Running it

```bash
pip install -r requirements.txt

python manage.py migrate
python manage.py seed_investor_config
python manage.py import_investor_workbook /path/to/India_D2C_Investors_Jun26_v27_1.xlsx
```

Verify the engine reproduces the workbook:

**Admin → Investors → Match config → Run preview**, or:

```bash
python manage.py shell -c "
from decimal import Decimal; import datetime as dt
from fundos.investors import matching
from fundos.investors.models import CanonicalSector
subs = [str(s.id) for s in CanonicalSector.objects.filter(level='sub_sector', name__startswith='D2C')]
r = matching.run_match(matching.MatchQuery(raise_size_usd_mn=Decimal('5'),
    sub_sector_ids=subs, date_from=dt.date(2023,1,1), date_to=dt.date(2026,6,5)))
t1 = [x for x in r['results'] if x['tier']==1]
print(len(t1), 'investors /', sum(x['trace']['matchedDeals'] for x in t1), 'matched deals')"
```

Refresh:

```bash
python manage.py run_refresh --dry-run              # writes nothing
python manage.py run_refresh --dry-run --limit-articles 2
python manage.py run_refresh --commit --stages enrich   # one stage only
python manage.py run_refresh --commit
```

Or **Admin → Investors → Refresh runs → Open refresh console**.

---

## 3. Verification results

### Import

```
rows: 7777 · investors_created: 3450 · deals_created: 2561
investor_deals: 7777 · sector_created: 110 · sector_rows: 123
```

7,777 rows and 3,450 investors. The source workbook has 3,454 distinct investor
name strings; four pairs collapse under normalisation and all four are the same
entity — `Foundamental` / `Foundamental GmbH`, `Michael & Susan Dell Foundation`
/ `Michael and Susan Dell Foundation`, and two similar. `sector_rows: 123`
populates `SectorDealData` for scorecard categories E and F, which is the D-04
answer implemented: one data load, two consumers.

### Matching engine against the reference query

Sub-sectors D2C + D2C (Consumer Electronics) + D2C (F&B), $5M raise,
2023-01-01 to 2026-06-05:

| | Matched deals | Investors |
|---|---|---|
| Control Panel C15/C16 (live formulas) | **477** | 301 |
| Dyn Engine cached `Qualify` column | 475 | 300 |
| **This implementation** | **477** ✓ | 302 |

**Matched deals reproduce exactly.** The investor count needs explaining, and
the explanation is that the workbook does not agree with itself: its Control
Panel says 301 and its own Dyn Engine column says 300, because those are
spill-formula caches recalculated in different orders.

Our 302 is a strict superset — every investor the workbook finds, we find, plus
two: **100Unicorns** (three D2C deals at $3.0M, $2.5M, $1.2M) and **Turbostart**
(one at $6.0M). Both have deals comfortably inside the $2.0–7.5M band and inside
the date window; neither sits on a boundary. These look like rows the workbook's
cached values missed rather than rows we wrongly include, **but that needs
business confirmation** before the regression test asserts a specific number.

The test suite therefore asserts the mechanics — band inclusivity, tier
precedence, the two RelScore factors, ranking order — rather than encoding a
count the source is inconsistent about.

### Tests

```
tests.investors  32 tests   OK
tests.engines    18 tests   OK
tests.api (assessment + spec contract)  30 tests   OK — no regressions
```

---

## 4. Two bugs found during verification

**Sub-sector variants were being fuzzy-collapsed.** The reference query checks
*three* sub-sectors, not one. The importer was running sector strings through
the fuzzy normaliser, and rapidfuzz scores `D2C (F&B)` against `D2C` above the
78 threshold — merging three genuinely distinct sub-sectors into one and
changing every downstream count.

Fuzzy matching is right for a live scrape, where the source spells things
inconsistently. It is wrong for import, where the workbook *is* the taxonomy and
its labels are deliberate distinctions. Import now takes labels literally and
creates what it lacks. Fixed in `importer.resolve_sector`.

**Sector name uniqueness was global.** `D2C` exists as both a sector and a
sub-sector in the source data, so `unique=True` on name rejected the second.
Now `unique_together = ("name", "level")`. This raised the import from 7,776 to
7,777 rows.

---

## 5. Decisions as implemented

| Decision | Where it lives |
|---|---|
| **Band basis = deal size** | `MatchConfig.band_test_basis`, default `deal_size`, with the reasoning in the admin help text. `matching._row_flags` honours it. |
| **AI review may re-band when very confident** | `assessment/tasks.py::run_rubric_review`. Fires at ≥0.90 confidence (configurable), moves **one band step only**, never over a human override, always writes an audit finding naming old band, new band, confidence and reason. `system_band`/`system_score` untouched, so the rule-based result survives beside the adjustment. |
| **Identity merges below 90% → LLM, manually triggered** | `identity.py`. ≥90% auto-merges; below queues an `InvestorAliasCandidate`. The LLM run is an admin action, never part of a refresh. The verdict is evidence for the admin, not an instruction to the system. |
| **Manual merge / edit in admin** | `InvestorAdmin.merge_selected`, plus every field editable including the relationship block. `InvestorAliasCandidateAdmin` has merge / keep-separate / adjudicate. |
| **API keys: one paid key + backups** | Configure the primary on the LLM endpoint and the fallbacks as the rotation list. The 19 keys hardcoded in the supplied `config.py` are **not** carried over — see §7. |
| **D-13: contacts not for founders** | `serializers.is_internal()` gates the relationship block; the founder payload omits it entirely rather than blurring it. `DiscoveryExportView` excludes it for everyone. |
| **D-09: coverage gate** | `AssessmentView` suppresses the overall score below 60% and returns `missingEvidence` naming the documents that would close the gap, ordered by weight recovered. |

---

## 6. Configuration — all of it in admin

Nothing tuneable is a Python constant. The bar is not "will this change" but
"would a change require a developer".

| Admin section | Replaces |
|---|---|
| Canonical sectors + Sector aliases | `CANONICAL_ALIASES` (30 sectors, hardcoded) |
| Sector groups | `IMPACTECH_SECTORS`, `FIG_FINTECH_SECTORS`, `DEEPTECH_AI_SECTORS`, `HIGHLIGHT_SECTORS` |
| Sector review items | `sector_unknowns.log` — now a queue, with one-click alias creation |
| Investor buckets + categories | The 14→9 mapping |
| Raise bands | The three-row lookup |
| Match config | RelScore weights (`AH23`/`AH24`), activity constant, exclusivity, PE split, band basis |
| Data sources | Inc42 URL, article window, manual URLs, delays, retries, breaker thresholds, browser path |
| Refresh schedules | The Saturday 18:00 cadence, with a resolved next-run time |
| Refresh runs + console | Dry run, scoped run, stage isolation, commit, history |

**Test for agnosticism:** onboarding a US SaaS company raising $40M should be a
data load — new raise band rows, new sector rows — with no migration and no
deploy. The one gap is the raise band table, which tops out at a $5M raise and
needs Series B and Growth rows added. That is a data decision, not a code one.

---

## 7. Two things needing attention

**Rotate the Gemini API keys.** The supplied `config.py` carries 19 live keys in
source, and that file has now travelled through a document-sharing chain. They
should be treated as compromised regardless of anything else here. The new code
reads keys from the LLM endpoint configuration; none are committed.

**Playwright needs installing for live scraping.** The adapter is written and
the resilience patterns are ported, but the scrape stage needs
`pip install playwright && playwright install chromium`. Until then, the import
path works fully and `run_refresh --stages enrich merge derive` works against
imported data.

---

## 8. Not yet built

From the plan, still outstanding: the extraction accuracy harness (needs the 5
sample companies), Stage 2 Phase 2 and Phase 3 screens, the founder view, and
the Inc42 refresh job's live verification against a real scrape.
