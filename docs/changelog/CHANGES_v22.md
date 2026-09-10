# v22 — Phase 1 fixes, and sector benchmark ingestion

---

# PHASE 1 — the four items from the medibuddy run

v21 landed: the website returned **31,023 characters**, the www/apex fallback
fired and logged, fixtures reported SKIP, the judgement call ran
`thinking_tokens=5910` to `finish_reason=STOP` with `was_repaired=false`, and
one call finally showed `searches=2`. These four remained.

## 1. Prompt drift is now visible

`searches=2` appeared on `market_research`, not on the deep extract — because
a `PromptTemplate` row **overrides the shipped default entirely**, so the
deep-extract prompt rewritten in v21 never reached the model. With 28 admin
prompt rows installed, no code change to any prompt has any effect.

This has now caused two silent regressions, so `diagnose_config` compares
every active prompt row against its shipped text and names the roles that
differ, with the remedy (`seed_platform_config --reset-prompts`) and the
warning that it overwrites deliberate edits.

**Run `--reset-prompts` on deploy** or the v21 deep-extract prompt still will
not be in use.

## 2. The CKB sync has its own savepoint

```
financials→CKB sync failed: An error occurred in the current transaction.
You can't execute queries until the end of the 'atomic' block.
```

The sync was not the fault — it was the next thing to touch the database
after an earlier statement in the same atomic block had aborted the
transaction. It now runs inside its own savepoint, so it cannot be poisoned
by an earlier failure, and a genuine failure here rolls back only the sync.

## 3. A described revenue model no longer fails as a broken split

`sample="{'label': 'revenue model description'}"` with *Shares must total
100% (currently 0%)*. The model named the revenue streams instead of
splitting them — a reasonable answer to a badly aimed question, not a broken
split. Failing it pointed the reader at the validator when the fix is
upstream.

Rows with no percentage at all: nothing is written, and the reason is
recorded. A partial split keeps the priced rows and reports the rest.

## 4. Fan-out roles stop re-sending the corpus

Each of ten follow-up calls carried `context_chars=24081` and ~12,000 prompt
tokens, re-reading a corpus the deep extract had already turned into
structured fields. Bulk source values are now capped at 6,000 characters for
every role **except** `company_profile_deep_extract`, which still sees
everything. The truncation marker tells the model the full corpus was read
upstream and its output is in the same context.

---

# PHASE 2 — sector benchmark ingestion

Categories **E (Sector, 10%)** and **F (Sub-Sector, 20%)** are scored against
this data, so between them a third of every deal rating rests on it. The
models existed; nothing loaded them.

## The sheet is three tables, not one

Reading the left column as a single table files the clubbed sub-sector groups
as sectors — 55 "sectors" from a sheet that has 18.

1. **Sector attractiveness** — left columns, first header.
2. **Raw sub-sector detail** — right columns of the *same* block, whose job
   is the raw-label → clubbed-group mapping rather than scoring. It is
   **taller** than the sector block beside it: 104 raw labels against 18
   sectors. Bounding it to the block kept 21 mappings and dropped 83.
3. **Sub-sector attractiveness** — left columns again, under a *second*
   header, holding the clubbed groups. Category F scores against this, not
   against block 2: a group aggregates enough deals for a percentile to mean
   something.

Two further traps, both live in your workbook: a `Total — Sector Table` row
carries a deal count and imports as a sector; and the sheet's own integrity
checks sit below the tables with numbers in the same columns
(`Deals in source extract (input)  2699`). Each block therefore ends at its
first blank name row, and totals rows are filtered by name.

Verified against `Tyreplex_Deal_Assessment_v21_1.xlsx`: **18 sectors, 33
clubbed sub-sectors, 104 mappings**, values matching the sheet exactly
(Ecommerce 606 deals / $4,977.55mn / $8.21mn ticket / velocity 10 / ticket
2.5 / investors 10).

## Re-import updates in place

As specified. A refresh is *meant* to move ratings — newer market data should
change what a fast-moving sector is, and an assessment re-run afterwards
ought to reflect it. Nothing is versioned, so **the dry run is the
safeguard**: it names every row that would move, field by field, with before
and after, and every sub-sector mapping that would be re-pointed. A
re-pointed label changes which benchmark group a company is judged against —
a category worth 20% of the rating — so those are listed separately.

## Scores

The workbook's own Velocity / Ticket / Investors scores are **preserved**
where the sheet carries them, so an import reproduces the sheet rather than
quietly recomputing it and a figure an analyst can see is the figure that
scores. Percentile ranking fills gaps only. Ties share a rank; a missing
value scores nothing rather than zero, following the model's own "blank is
not zero" rule; a single-row population gets no score, because a percentile
against yourself is not information. Average ticket is derived where absent.
Rows under five deals are flagged `Thin`.

## How to run it

```bash
# Always dry-run first.
python manage.py import_sector_benchmarks /path/to/workbook.xlsx --dry-run
python manage.py import_sector_benchmarks /path/to/workbook.xlsx --as-of 2026-08-01
```

Or in the admin: **Assessment → Sector deal data → Import from workbook**.
Upload, and the screen shows the detected column mapping per block, the raw
mapping pairs, rows skipped and why, and the full dry-run diff. Nothing is
written until *Write the changes* is ticked. `SectorMapping` is editable
inline (`list_editable`) because a new raw label is data, not a deploy.

## Schema

`SectorDealData` gains `velocity_score`, `ticket_score`, `investors_score`,
`clubbed_group`, `sample_quality`, `source_file`, `updated_at`. Migration
`assessment.0002`.

---

## Tests

`tests/api/test_sector_benchmarks.py` — 14 tests over a miniature of the real
sheet (three blocks, a totals row, an integrity-check row below, and a raw
table taller than the block beside it): block separation, totals and check
rows excluded, the full mapping table read, the workbook's own scores
preserved, thin samples flagged, dry run writing nothing, re-import updating
in place without duplicating, and a re-pointed mapping reported before it is
applied. Plus the four Phase 1 fixes.

**Full run with `GEMINI_API_KEY` set — 583 tests, all passing.**
`makemigrations --check` reports no pending model changes beyond
`assessment.0002`.

---

## Deploying

```bash
python manage.py migrate
python manage.py seed_platform_config --dry-run     # read this
python manage.py seed_platform_config --reset-prompts
sudo systemctl restart fundos-web fundos-worker
python manage.py import_sector_benchmarks /path/to/workbook.xlsx --dry-run
python manage.py import_sector_benchmarks /path/to/workbook.xlsx
python manage.py diagnose_config
```

`--reset-prompts` overwrites every prompt row with the shipped text. Review
any deliberate edits first — `diagnose_config` now lists which rows differ.

Then regenerate a company and check: `searches=` non-empty on the deep
extract, `records.form_seed` absent or WARN, no CKB transaction error, and
`context_chars` on the follow-up calls down from ~24,000 to roughly 8,000.

---

## Next: Phase 3 — the profile-to-assessment bridge

Still the largest gap. `fundos/assessment/` contains no reference to the
company profile: extraction reads uploaded documents only, so step 1's
research does not reach step 2's rating.

Roughly 45 of the 80 parameters are obtainable from company research — the
Team block, most of Business Quality, the Sector and Sub-Sector blocks, parts
of Deal Dynamics. Financials come from the uploaded model; Mandate Context is
internal. The work is an `assessment_inputs` extraction emitting the
workbook's own `input_key` names with unit, value, source URL and confidence,
per-field provenance storage, and seeding `ParameterValue` from it — leaving
genuinely unknown parameters blank so the "blank is not zero" weight
redistribution works as designed.

Sub-sector classification matters more than it looks: category F carries 20%,
and the label decides which of the 33 benchmark groups a company is judged
against. Mapping it correctly is now possible — the 104 raw labels are loaded.
