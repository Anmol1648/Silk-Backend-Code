# Company Profile — Data Integrity & Section Update API (Aug 2026)

Addresses section-data-integrity-and-update-api.docx. Six issues, all fixed.

## #5 / #6 — PATCH section returned 200 without persisting; contract not standardized

Root cause: `ProfileSectionView.patch` read `content` / `structured` from the
body, but the client sends the standardized `{ "data": ... }` contract, so
both were None and nothing was written — a 200 false positive.

Fix: new `fundos/profile/section_writer.py` implements ONE contract for all 14
sections. Body is always `{ "data": <section shape> }` — object for
company_profile / business_model / financial_summary / ai_company_summary,
array for the list sections. It maps the §7 spec field names (the same ones
GET returns — founders use role/background/is_full_time, competitors carry an
investors array, etc.) back to storage:
  * object sections   → ProfileSection.structured (content for ai summary)
  * structured lists  → ProfileSection.structured["items"]
  * entity lists      → full replacement of the section's table rows

`ProfileSectionView.patch` now uses this, PERSISTS the change, and echoes the
saved section in the exact §8 wrapper (sectionKey / isComplete / lastUpdatedAt
/ data) so the client needs no follow-up GET. The legacy
`{content, structured}` body still works for existing callers.

## #4 — isComplete true despite empty fields

`_section_is_complete` now does field-level completeness for object sections:
company_profile and business_model are complete only when ALL their core
fields are non-empty (not merely when any one field is set). financial_summary
requires at least one financials row. Recalculated server-side on every PATCH.

## #3 — readiness used `summary` instead of `data`

`readiness` now uses the standard §8 wrapper: `{ sectionKey, isComplete,
lastUpdatedAt, data: { summary } }`, consistent with every other section.

## #1 / #2 — four narrative sections empty; other lists empty

Confirmed at the DB level: the mocked generation pipeline stores AND serializes
structured data correctly for all four narrative sections (verified end-to-end).
For companies generated BEFORE the Gap2 fix, the AI output lives only as prose
in ProfileSection.content — the serializer's content-fallback surfaces that in
`data` so it is no longer invisible. To materialise proper structured data for
those existing companies, a new management command re-runs generation:

    python manage.py backfill_profile_sections [--company <uuid>] \
        [--only-empty] [--dry-run]

The remaining list sections (founders, key_people, revenue_model, …) are empty
only when no source data was generated/entered; they populate on generation
(mocked path verified) or via PATCH.

## Tests

`tests/api/test_spec_contract.py` extended with: PATCH object + list section
persistence and §8 echo, unified `{data:...}` contract, field-level isComplete,
readiness data-wrapper. `tests/api/test_backfill.py` covers the command. Full
API suite green.
