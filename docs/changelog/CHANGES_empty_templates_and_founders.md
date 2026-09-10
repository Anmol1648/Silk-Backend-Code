# Company Profile — Empty-field templates + founder generation (Aug 2026)

Addresses the 4th report on the "empty founders array". Two distinct things,
both fixed, as requested ("populate now AND always return field templates").

## Verified first (not guessed)

Checked at the DB level: when founder rows exist, the API already serializes
the full array correctly (name/role/background/linkedin_url/is_full_time,
isComplete true). So `data: []` was NOT a serialization bug — it was two real
problems below.

## 1. Populate — generation never created founder rows

Root cause: `founders` was absent from the record-generating path. Unlike
key_people / competitors / funding_history / recent_news (which materialise
typed rows during generation), founders only had a prose `founder_profile`
role, so `generate_profile` never inserted Founder rows — the array stayed
empty even after a successful generation.

Fix: founders is now a first-class record section —
  * added to `RECORD_TYPES` (records.py) so the generic record path + CRUD
    create Founder rows,
  * added to `STRUCTURED_GEN_SECTIONS` and `_RECORD_SECTION_MODELS`
    (services.py) so generation materialises rows (only when the section is
    empty — never clobbers founder-entered data),
  * added a `founders` fixture to the `company_profile_records` mock.

The `backfill_profile_sections` command (shipped previously) now also
populates founders for existing companies.

## 2. Empty-field templates — never return a bare []

Every LIST section now returns ONE template row with all §7 fields present
and empty when it has no stored rows, instead of `[]`. The client always sees
the field structure. Applies to: founders, key_people, products_services,
customers_markets, competitive_advantages, revenue_model, company_metrics,
funding_history, competitors, recent_news.

Consistency guard: an all-empty template row does NOT count as data, so a
section showing only a template stays `isComplete: false`. `_has_data` /
`_row_has_value` enforce this, and the completeness roll-up is unaffected.

Object sections (company_profile, business_model, financial_summary,
ai_company_summary) were already returning their full field structure, so
they are unchanged.

## Tests

`tests/api/test_spec_contract.py`:
  * `test_empty_list_sections_return_field_template` — every list section
    returns one template row exposing all §7 fields and stays incomplete.
  * `test_generation_creates_founder_rows` — generation inserts real Founder
    rows and the array fills.

Full suite green: 226 tests pass.
