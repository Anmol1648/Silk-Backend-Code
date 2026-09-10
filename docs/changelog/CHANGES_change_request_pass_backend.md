# Company Profile change-request pass — backend

Implements CR-01 to CR-14 from the Company Profile Feature Change List, plus
three further defects found while tracing them.

## Deploy — required

```bash
pip install -r requirements.txt
python manage.py migrate                 # adds 0010_company_linkedin_url
python manage.py seed_platform_config    # REQUIRED — idempotent
```

Then check the four critical settings in the Configuration Guide. Three of the
four causes behind CR-03 are configuration, not code, and the code fix alone
will not resolve it.

---

## CR-03 / CR-04 — generation ignored documents and web research

The reported symptom was that only company name, URL and country drove
generation. The pipeline to do better already existed; four independent things
defeated it.

### 1. Every research adapter threw on a keyword mismatch (code — fixed)

`collect_sources` constructed adapters as `WebsiteAdapter(deal=deal, ckb={})`.
The parameter is `ckb_snapshot`. The resulting `TypeError` was caught by the
surrounding `except` and logged at WARNING, so the failure was invisible. What
actually reached the model was:

```json
"market":         {"error": "…unexpected keyword argument 'ckb'"},
"competitor":     {"error": "…unexpected keyword argument 'ckb'"},
"industry":       {"error": "…unexpected keyword argument 'ckb'"},
"public_funding": {"error": "…unexpected keyword argument 'ckb'"}
```

Website research and all four market sources had never run. Fixed at both call
sites.

### 2. Every source was gated on a pre-existing Deal (code — fixed)

Website, documents and research were each gated on
`Deal.objects.filter(company=...).first()` returning something. At Create
Company time no deal exists — one is only created as a side effect of the first
document upload. So on the path the user actually takes, the source bundle was
empty. Reproduced before the fix:

```
Deals for this company    : 0
ProfileDocuments uploaded : 3
  website   : KEY ABSENT ENTIRELY
  documents : KEY ABSENT ENTIRELY
  research  : KEY ABSENT ENTIRELY
```

`collect_sources` now resolves a context deal on demand, as the upload path
already did.

### 3. Adapter error strings were fed to the model as context (code — fixed)

A failed adapter stored `{"error": "..."}` in the payload, which was then
serialised into the prompt. Error text is not evidence. Failures are now
stripped from the payload and surfaced as run diagnostics.

### 4. Two configuration causes (NOT code)

- `AppConfiguration.ai_mocked` defaults to `True`. Every LLM call returns a
  canned mock. On dev settings this stays on deliberately; on uat/prod the
  seeder sets it off.
- Document text extraction runs as a Celery task
  (`scan_and_analyse_material.delay(...)`). With no worker, materials sit at
  `scan_status="pending"` with empty summaries and contribute nothing.

Added `services.document_readiness(profile)` reporting uploaded / analysed /
pending counts, so "stored but never extracted" is distinguishable from "the AI
ignored my documents" — previously identical from the outside.

## Three further defects found in the deep-extract fan-out

Not on the change list; found while implementing CR-12.

1. **Competitor `description` held the business model.** The fan-out wrote
   `description=(c.get("business_model") or "")`. This is *why* CR-12 was
   raised: the field the UI would have shown was populated with the wrong
   thing.
2. **`differentiators` were written into `investors`.** Two unrelated fields —
   the Competitors section listed product differentiators under "Investors".
3. **Req 8 analysis fields were written to a section that no longer exists.**
   The fan-out wrote them to `competitors_detail`, folded into `competitors`
   under Req 8, so valuation, strengths, weaknesses and recent activity never
   reached the competitor rows. They now populate `Competitor.extra`.

## CR-01 / CR-02 — company LinkedIn

`CompanyProfile.linkedin_url` added (migration `0010`), accepted and validated
at onboarding, exposed on the profile payload and as `companyLinkedin` in
dashboard links. The old `founderProfile` key is retained for any caller still
reading it, but the dashboard shortcut no longer substitutes a founder's
personal profile for the company page.

## CR-09 — AI Company Summary retired

Seeded inactive, and reconciled to inactive on existing installs (a plain
`get_or_create` would never have touched an existing active row). Also cleared
from `required_for_completeness`, or profiles could never reach 100%. Stored
content is untouched, so re-activating the row in admin restores it.

## CR-11 — market sizing must carry its provenance

Each of TAM / SAM / SOM now travels as `{value, source, assumptions}`, and the
section gained `market_sizing_narrative` and `methodology`. The serializer
accepts **both** the new object form and the legacy scalar, so profiles
generated before this change keep rendering — pinned by a test.

The deep-extract prompt now requires a named source per figure and instructs
the model to leave a figure empty and report it under `missing` rather than
estimate one.

## CR-12 — competitor descriptions

`Competitor.description` and `website` are now emitted and writable. The column
already existed; it was never surfaced. The prompt requires a one-or-two
sentence description of what each company does.

## CR-14 — URL validation

`link`, `website` and `linkedin_url` are validated on write. A bare domain is
normalised (`example.com/story` → `https://example.com/story`) rather than
rejected; free text raises a 422 naming the field. Empty stays permitted.

## Not done as specified — and why

**CR-05 to CR-08 (merging sections) are implemented as presentation grouping,
not storage merges.** Founders, Key People and Leadership are separate tables
with live data in each; merging them in storage means a destructive migration
and a conflict-resolution policy for rows that disagree. The frontend now
renders them as one block with sub-headings, which delivers the requested
information architecture while each still saves independently.

If a true storage merge is wanted, it needs a product decision on what happens
when the same person appears in two of the three with different details. That
is a data question, not a code one.

---

## Tests

`tests/api/test_change_request_pass.py` — 18 new tests. Notably:

- `test_sources_are_collected_without_a_pre_existing_deal` and
  `test_no_adapter_fails_with_the_wrong_keyword` pin the two CR-03 code causes
  directly, rather than asserting on output quality.
- `test_adapter_error_text_is_not_fed_to_the_model` pins cause 3.
- `test_legacy_scalar_market_sizing_still_renders` guards the CR-11 shape change
  against breaking existing profiles.
- `test_seeding_deactivates_an_already_active_row` pins the CR-09 reconcile
  path, which is the one that matters on an upgrade rather than a fresh install.

One assertion in `test_profile_defect_pass.py` was updated: market sizing is now
an object per figure. The round-trip guarantee it tests is unchanged.
