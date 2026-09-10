# Backend Issues — Combined Report fixes (Aug 2026)

Addresses backend-issues-combined.docx — five issues. Each verified at the
DB/behaviour level, not just by shape.

## 1. financial_summary empty on GET (High)

Root cause (confirmed at DB level): the section WAS generated, but stored as
the generic numeric-form shape `{kind:"fiscal_years", items:[{fiscalYear,
revenue, grossMarginPct, ebitda, ccy}]}`, while the serializer read
`financials` / `observations`. Serialization mismatch → empty arrays.

Fix:
* Serializer `_data_financial_summary` reads whichever shape is stored (native
  `{financials, observations}` OR the numeric-form `items`) and maps years /
  revenue / ebitda / pat, and turns string observations into {title,description}.
* Generation now stores financial_summary in the spec shape and PRESERVES
  observations (the numeric-form path was dropping them).
* `recompute_structured_sections` handles the new shape so isComplete is right.

## 2. competitors PATCH drops fy_year, revenue, investors (High)

Root cause (confirmed): the Competitor table had NO columns for these three
fields — writes stashed them in a structured blob but the serializer read them
from elsewhere, so they were effectively dropped.

Fix (columns added, per instruction):
* New DB columns on Competitor: `fy_year` (int), `revenue` (decimal),
  `investors` (JSON). Migration companyprofile/0003.
* PATCH writer + serializer persist/read them directly.
* AI-generation record schema (RECORD_TYPES.competitors) also accepts them.

## 3. POST /auth/signup auto-creates a company (Critical)

Root cause: `onboarding.signup` created a Company + owner Membership whenever
`companyName` was present — violating the "no company on account creation"
rule (company-profile-generation-flow.md §1 / §2.2).

Fix: removed the company/membership creation from signup entirely. Signup now
creates only tenant + user; `companyName` is used solely as a tenant label.
Companies are created only via the explicit `POST /api/v1/companies` flow, so a
new user lands on the empty "no companies" state.

## 4. completenessPct frozen (Medium)

Root cause: it counted only sections with non-empty narrative `content`, so
record/structured sections (founders, competitors, financial_summary, …) —
whose data lives in tables or `structured`, never `content` — never moved it.

Fix: `recompute_completeness` now measures with the SAME per-section
`isComplete` logic the serializer uses, across all section types, and is
recalculated on every PATCH write and after generation. (Also fixes Open
Item #15: company_profile.isComplete requires its core fields, not any one.)

Bonus, found while fixing #4: the revenue_model / company_metrics serializers
were reading the wrong keys (`stream`/`metric` vs the stored `label`), so those
arrays surfaced blank. Now mapped from the stored shape.

## 5. attachmentLinks field names (Medium)

Renamed to match the four upload categories (§5.2):
`productDeck → companyPresentation`, `other → otherDocuments`, ADDED
`annualReportFinancialStatements`; kept `founderProfile`, `companyUrl`,
`financialModel`.

## Migration note

This release adds DB columns (companyprofile/0003) — run migrations on deploy.

## Tests

`tests/api/test_spec_contract.py` covers all five (financial_summary populate,
competitors round-trip, completeness recalculation, attachment link names) plus
the existing contract. Signup-no-company enforced in
`tests/api/test_gap_fixes.py`. Full suite green: 230 tests pass.
