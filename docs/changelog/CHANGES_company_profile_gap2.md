# Company Profile — Gap2 closure (Aug 2026)

Addresses the "Company Profile — Section-by-Section: Current vs Expected"
(Gap2) findings against the live `GET /api/v1/companies/{id}/profile` response.

## Root cause

The four narrative sections — products_services, customers_markets,
competitive_advantages, business_model — were generated as PROSE only. The AI
output landed in `ProfileSection.content` but never in the structured store,
so the clean `sections.data` came back empty even though real content existed
in the legacy field. (Gap2: "content migration lagged the structural change.")

## Fixes

1. **Narrative sections now populate `sections.data`.**
   - New LLM role `company_profile_structured` (prompt + mock fixtures) returns
     the exact §7 shapes for the four sections.
   - New generation path `_generate_structured_narrative` stores the result in
     `ProfileSection.structured` (keeping the prose summary in `content` as a
     fallback), so `sections.data` is populated after generation.
   - `STRUCTURED_NARRATIVE_SECTIONS` routes these four in `generate_profile`.

2. **Content-fallback for already-generated profiles.** For profiles generated
   before this change (data stranded in `content`), the serializer surfaces the
   prose into a sensible `data` field so nothing reads as blank without a
   re-generation.

3. **Legacy duplication removed (Open Item #13).** `sectionsEditor` and
   `editorRecords` are removed from the `/profile` response. The frontend now
   consumes only the clean `sections` object. `documents` stays top-level
   (the §5.2 Document Center shape).

4. **`readiness` now has a home.** Exposed as a top-level object
   `{ isComplete, lastUpdatedAt, summary }`, outside the 14-section schema,
   instead of being lost with the removed editor array.

5. **Live-mode binding fix.** The profile generation roles
   (`company_profile_section`, `company_profile_records`,
   `company_profile_structured`, `founder_profile`, `company_profile_field`)
   were missing from the seeded LLM role list, so in a non-mocked (production)
   environment they raised "no LLM binding configured". Now seeded.

## Tests

- `tests/api/test_spec_contract.py` extended:
  - legacy keys removed, `readiness` present, `documents` present.
  - the four narrative sections populate `data` after (mocked) generation.
- Profile behavioural tests re-pointed from the removed response keys to the
  ProfileSection source of truth. Full API suite green.

## Note for existing data

The structured fix applies to newly generated profiles. Existing companies
would show the content-fallback prose until re-generated. A backfill management
command can be added if a one-time migration of existing profiles is wanted.
