# Company Profile Generation — Gap closure (Aug 2026)

Implements the "Company Profile Generation — End-to-End Flow" gap document
(Sections 6–8, §10.1, §10.2). Changes are contract-only where possible; the
internal storage model (ProfileSection + entity tables) is unchanged.

## GET /api/v1/companies/{id}/profile  (§6 / §7 / §8 / §10.2)

- `sections` is now an **object keyed by section key**, each value wrapped as
  `{ sectionKey, isComplete, lastUpdatedAt, data }` (§8). Previously an array
  of editorial rows.
- Each section's `data` matches the §7 schema exactly. Notably:
  - founders → `name, role, background, linkedin_url, is_full_time`
    (`background` is a short 2–3 line summary sourced from experience/bio).
  - competitive_advantages → always `{title, description}` objects.
  - competitors → `investors` is an **array**.
  - funding_history → `round`, `amount_usd_mn`, `pre_money_usd_mn`, `investors[]`.
- Internal key `company_overview` is surfaced under the spec name
  `company_profile` (external rename via a mapping; the DB key is unchanged to
  avoid destabilising CKB/materials references).
- The three internal-only sections (`knowledge_base`, `document_center`,
  `readiness`) are excluded from the contract.
- The duplicate top-level entity arrays (`founders`, `keyPeople`,
  `competitors`, `fundingRounds`, `news`) are removed from the spec surface.

New file: `fundos/profile/spec_serializer.py` — the single place that maps the
internal model onto the clean external contract.

### Editor support (additive, not part of the §8 contract)

The existing rich section editor still needs per-section metadata, ids and
camelCase fields for its CRUD forms. These are provided as **sibling keys**
alongside the spec contract, so the external shape is exactly as specified:

- `sectionsEditor` — the ordered, metadata-rich section array (kind, content,
  needsInput, isRegenerable, versionNo, …) the editor renders from.
- `editorRecords` — `{ founders, keyPeople, competitors, fundingRounds, news }`
  with ids + camelCase fields for the record add/edit/delete forms.
- `documents` — Document Center listing with signed download URLs.

## GET /api/v1/me/contexts  (§3.2 / §10.1)

`scope: "company"` items now carry the four All-Companies-view fields:
`sector`, `lastRaise {round, date}`, `totalFundingReceivedUsdMn`,
`attachmentLinks {founderProfile, companyUrl, productDeck, financialModel,
other[]}`. `scope: "deal"` items are unchanged. Computed in bulk to avoid an
N+1; defensive so a half-set-up company never breaks the listing.

New file: `fundos/profile/summary.py` — per-company summary derivation.

Note: `totalFundingReceivedUsdMn` treats stored round amounts as USD (the data
set defaults to USD). Mixed-currency portfolios would need FX conversion here.

## Tests

- `tests/api/test_spec_contract.py` — asserts the new object-keyed sections
  contract and the enriched contexts company items.
- Existing profile tests updated to read the new contract (via `sections.data`,
  `sectionsEditor`, `editorRecords`). Full API suite green.
