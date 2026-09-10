# Company Profile defect pass — backend

Fixes the crash reported on the Company Profile page after Create Company, plus
the defects found while tracing it. Verified against the full suite:
**428 tests, all passing** (previously 401 with 1 failure).

## Deploy step — required

```bash
pip install -r requirements.txt          # adds rapidfuzz (see §7)
python manage.py migrate                 # no new migrations in this pass
python manage.py seed_platform_config    # idempotent; REQUIRED
```

`seed_platform_config` registers six profile sections that were never in the
registry (§2). Without it, saving any of those six sections still fails. The
seeder uses `get_or_create`, so it will not disturb existing configuration —
the section count goes from 17 to 23.

---

## 1. The reported crash — bare `{}` in the API payload

The client logged `Minified React error #31 ... object with keys {}` — React's
"Objects are not valid as a React child", naming an **empty object**.

`_data_leadership_detail` returned:

```python
return {"cfo": s.get("cfo") or {},      # {} on every new company
        "founders": s.get("founders") or [],
        "ceo": s.get("ceo") or {},      # {}
        ...}
```

An empty object carries no field structure, so a generic renderer has nothing
to show and — being truthy — cannot detect it as absent either. This is the one
section that broke the convention the rest of the file already follows
(`_LIST_ROW_TEMPLATES`): when a section has no data, send the **field template**,
not an empty container.

`fundos/profile/spec_serializer.py`:

- `_data_leadership_detail` emits the full person key set
  (`name / role / background / tenure / linkedin_url`) for `cfo` and `ceo`, and
  falls back to the profile's real `Founder` rows when the AI has not written its
  own leadership block — so the section shows something true rather than blank.
- `_data_derived_multiples` gets the same self-describing empty row treatment.

The frontend is hardened independently, so neither side alone can reproduce the
crash.

### Completeness had to be fixed alongside it

Adding template rows exposed a latent bug: `_has_data` treated *any* non-empty
list inside an object as data, so `leadership_detail` reported
`isComplete: true` on a brand-new, entirely empty profile. `_has_data` and
`_row_has_value` now recurse — a blank template row, and a person block of empty
strings, both correctly read as "no data". A regression test pins this.

## 2. Six sections were emitted, offered for edit, and impossible to save

`investors_and_cap_table`, `company_story_and_usp`,
`industry_and_market_research`, `investment_thesis`, `leadership_detail` and
`derived_multiples` were returned by GET and given an Edit button by the client,
but **every save returned 422**. Two independent causes:

1. **Not in the section registry.** `update_section_from_data` validates the key
   against `platformcfg.profile_sections()`, and `seed_platform_config.SECTIONS`
   listed only the original 17 — so these six raised `Unknown section`. Now
   registered under their storage keys (`cap_table`, `company_story`,
   `market_research`, `investment_thesis`, `leadership_detail`,
   `derived_multiples`).
2. **No writer branch.** They fell through to
   `raise SectionUpdateError("... is not editable")`.

`fundos/profile/section_writer.py` gains `_NESTED_OBJECT_SECTIONS`, a
declarative shape spec (`text` / `number` / `text_list` / nested `object` /
`list`) with a `_coerce_shaped` walker, so read and write schemas sit together
rather than drifting apart in hand-written branches. All-blank template rows are
dropped on write, so saving an untouched form does not read back as real data.

## 3. Silent data loss on save

Fields the serializer emitted were dropped by the writer, so a client that read
a section and PATCHed it back verbatim lost data **with no error** — despite the
module docstring promising a lossless round-trip.

| Section | Lost on save |
|---|---|
| `business_model` | `business_model_types` (Req 4) |
| `company_profile` | all four funding-summary figures (Req 1) — so an editor-corrected total could never override the derived one, which the serializer explicitly intends |
| `competitors` | the eleven Req 8 analysis fields held in `Competitor.extra` — `market_positioning`, `strengths`, `weaknesses`, `recent_activity`, multiples… |
| `funding_history` | `post_money_usd_mn`, `lead_investors` (Req 1) |

All now mapped, with list/numeric coercion and an `extra` JSON-bag spec for
competitors. `business_model_types` is filtered against the allowed set on
write, mirroring the read side.

## 4. Non-canonical provenance duplicated founders

`_replace_entity_rows` stamped `source="founder_entered"`. The canonical set
(`core/models/base.py`) is `ai_research | founder | document_extracted | stage0`,
and `ProfileOnboardView` cleans up with `filter(source="founder").delete()`.

Edited founders were therefore invisible to that cleanup and **duplicated on
every re-submission of the onboarding form**. Now writes `"founder"`.

## 5. Section history was permanently empty for four sections

`ProfileSectionHistoryView` looked up the **wire** key. Three sections are
stored under a different key and `company_profile` is stored as
`company_overview`, so their history always returned `{"items": []}`. Now
resolved through `storage_key_for()`.

## 6. Dashboard and profile disagreed about funding totals

`profile/summary.py::_to_usd_mn` divided `FundingRound.amount_value` by
1,000,000. Every other reader and writer treats that column as **USD millions**
(`spec_serializer`: `amount_usd_mn: _f(r.amount_value)`; `section_writer`:
`amount_usd_mn → amount_value`). A $5M round entered as `5` was reported to the
dashboard as `0.000005` and rounded to `0.0`, while the profile page showed
"$5 Mn". Now consistent with the rest of the codebase; a test asserts the two
figures match.

## 7. `rapidfuzz` was used but never declared

`investors/identity.py` and `investors/normalizer.py` import it inside
`try/except` and fall back to exact-match-only with a **warning**. It was absent
from `requirements.txt`, so a clean install silently lost fuzzy investor identity
resolution and sector normalisation — near-miss aliases were never queued for
adjudication. This was failing `tests.investors.test_matching` on a clean
environment. Added `rapidfuzz>=3.9`.

## 8. Missing endpoint: company logo

`PATCH /companies/{id}/logo` was called by the client from day one but had no
route and no handler — every logo change 404'd and reverted on reload. The logo
could only ever be set as a side effect of onboarding.

`core/api/views.py::CompanyLogoView` implements `PATCH` (accepting `logoUrl` or
`logoBase64`, reusing `profile.logo.resolve_logo`) and `DELETE` (clear), tenant-
scoped and audited. Route mounted in `fundos/urls.py`.

## 9. `/me/contexts` omitted fields the dashboard depends on

The dashboard sorts on `updatedAt`, renders it as the "last touched" chip, and
branches on `profileComplete` to choose between opening the profile and resuming
the deal workspace. None were sent, so all three behaviours were dead code.
Company and deal items now carry `updatedAt` / `createdAt`; company summaries
carry `profileComplete` / `profileStatus`. Internal sort-only keys remain
stripped.

---

## Tests

`tests/api/test_profile_defect_pass.py` — 27 new tests, one class per defect,
each docstring naming the behaviour it pins. Notably:

- `test_no_section_emits_an_empty_object` walks **every** section of a fresh
  profile and fails on any empty object anywhere in the tree — a general guard,
  not just a fix for `leadership_detail`.
- `test_all_enhancement_sections_accept_their_own_get_payload` reads each
  section and PATCHes it back verbatim, which is what the client does.
- `test_dashboard_total_matches_the_profile_figure` asserts the two surfaces
  agree rather than pinning either number in isolation.
- `test_re_onboarding_does_not_duplicate_edited_founders` reproduces the
  onboarding cleanup directly.

```
Ran 428 tests — OK
```

## Not changed

Stage 2 / Stage 3 screens, the exports layer and the auth/permission model were
**not** reviewed in this pass.

## Unrelated but urgent

The `config.py` supplied alongside this work contains **19 live Gemini API keys
in plaintext**, and that file has travelled through a document-sharing chain.
Treat them as compromised and rotate them regardless of anything here. This
backend reads keys from LLM endpoint configuration and commits none.
