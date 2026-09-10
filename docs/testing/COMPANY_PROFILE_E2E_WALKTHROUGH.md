# Company Profile — end-to-end walkthrough (verified 21 Aug 2026)

This is the exact call sequence a frontend needs to take a brand-new user from
signup to a fully generated Company Profile, verified twice against a live dev
server (`fundos.settings.dev`, `CELERY_TASK_ALWAYS_EAGER`): once with
`ai_mocked=True`, and once against **real Gemini with a real company**. Every
request/response pair below was captured from a real run, not written from the
code. See [`../../API_CONTRACT.md`](../../API_CONTRACT.md) for the full
contract; this is the one path through it.

## 1. Sign up and sign in

```
POST /api/v1/auth/signup      {email, name, companyName}       -> 202
POST /api/v1/auth/otp/verify  {email, code}                    -> 200
                               {accessToken, refreshToken, user}
```

**`companyName` on signup does NOT create a company.** This is deliberate
(see `fundos/core/services/onboarding.py::signup` docstring): a JIT account
lands on an empty "no companies" state, and the founder creates a company
explicitly. Don't skip step 2 below expecting one to already exist.

## 2. Create the company

```
POST /api/v1/companies   {name, domain?, hqCountry?}   -> 201   {id, name, domain, hqCountry, createdAt}
```

`domain` is optional and is a convenience field only — the authoritative
website lives on the profile (step 3). **It is globally unique across every
tenant on the platform**, not per tenant, so a generic guess ("acme.com") can
collide with an unrelated company. A collision is handled server-side: the
domain is silently dropped (serialises as `null`) rather than the request
failing. Prefer leaving `domain` unset and letting `profile/onboard`'s
`websiteUrl` derive it instead.

*(This exact collision — creating a company with an already-claimed domain —
raised an unhandled 500 until this was tested end-to-end today. Now fixed;
see [`../changelog/CHANGES_production_hardening.md`](../changelog/CHANGES_production_hardening.md)
§9 and `tests/api/test_qa_24jul_fixes.py::OnboardingIsRepeatable`.)*

## 3. Onboard — this is what starts generation

```
POST /api/v1/companies/{id}/profile/onboard
{
  "websiteUrl": "https://acme.example",     // required
  "hqCountry": "IN",                        // required, ISO-2
  "linkedinUrl": "...",                     // optional, company page
  "founders": [{"name", "designation", "linkedinUrl"?, "selfConfirmed", "isFullTime"}],
  "force": false                            // bypass the sources-unchanged skip gate
}
-> 202
```

This dispatches `generate_profile_task` (async in prod, synchronous in dev —
the 202 has already resolved by the time it returns there). It runs the full
Company Master Data Pipeline: ten search-grounded research batches +
document extraction, merged into one dossier, then one synthesis call
against the 17-section schema.

**A website that does not resolve does not fail the run.** In the mocked
walkthrough `https://example.test` was unreachable; the pipeline logged the
source failure, fell back to the research batches, and still produced a
complete profile. Real behaviour to expect from a founder who mistypes a URL.
The live run used `https://zerodha.com`, several of whose sub-pages answered
429 to the scraper — also logged, also not fatal, since the ten search-grounded
batches are the primary source and the website is one input among many.

## 4. Upload documents (optional, any time)

```
POST /api/v1/companies/{id}/documents   multipart: {file, category}   -> 201
```

`category` is one of `company_presentation | financial_model | annual_report |
business_plan | other`; anything else is coerced to `other`, never rejected.
Uploaded documents feed the `document_center` section and are read by the
pipeline's document-extraction stage (MarkItDown + OCR) on the *next*
generation run — upload before you (re)generate, not after.

## 5. Force a rebuild after adding sources

```
POST /api/v1/companies/{id}/profile/deep-generate   -> 200   (runs synchronously)
```

Use this after uploading documents or fixing a bad website URL. It always
runs — bypassing the skip-if-unchanged gate — so don't call it on every
render; it is the "regenerate everything" button, not a status check.

## 6. Poll — the actual progress surface

```
GET /api/v1/companies/{id}/profile/runs            -> {items: [...]}   (newest first)
GET /api/v1/companies/{id}/profile/runs/{runId}     -> full detail
```

The list item and the `_run_summary` fields embedded in the detail response
are IDENTICAL — `runId`, `status`, `stage`, `stageLabel`, `batchesTotal`,
`batchesSucceeded`, `dossierChars`, `llmCalls`, `searches`, `completenessPct`.
The detail response adds `stageTimings`, `activity` (human-readable log),
`documents` (per-file extraction detail), `sectionsGenerated` /
`sectionsNeedingInput`, and:

```json
"dossier": {
  "characters": 6483,
  "downloadUrl": "https://.../consolidated.md?expires=...&sig=...",
  "format": "md",
  "expiresInSeconds": 900
}
```

The URL is short-lived (15 minutes) and generated fresh on every request —
never store it, fetch it when the user actually clicks download.

`status` is `queued | running | succeeded | failed | refused`. **`refused` is
not an error state to alarm the user over** — it means nothing was retrieved
(no search succeeded, no document had text), so the pipeline declined to
publish a profile assembled from a model's unsourced recollection. The
existing profile is left untouched; surface `refusedReason` and point the
user at what to fix (usually: add a document, or fix the website URL).

## 7. Read the profile

```
GET /api/v1/companies/{id}/profile   -> {sections: {...}, documents: [...], defaultDealId, ...}
```

`sections` is an OBJECT keyed by section key (not an array), each entry
`{sectionKey, isComplete, lastUpdatedAt, data}`. **All 17 keys are always
present** — a section with nothing in it is still there, `isComplete: false`,
`data` holding the empty field template rather than `[]` or `null`, so a form
can render its fields before anything has filled them.

Populated counts: 17 / 17 mocked (with a document uploaded), 16 / 17 live
(`document_center` empty, because nothing was uploaded to that company).

An empty section is **not** necessarily a failure — the model looked and found
nothing, which is the right answer for a bootstrapped company's
`funding_history`. `GET /profile/runs/{runId}` distinguishes the two:
`sectionsGenerated` versus `sectionsNeedingInput`.

## 8. Everyday writes

```
POST   /profile/records/{sectionKey}              -> create a row (founders, competitors, funding_history, news)
PATCH  /profile/records/{sectionKey}/{recordId}    -> edit a row
DELETE /profile/records/{sectionKey}/{recordId}    -> remove a row
POST   /profile/sections/{sectionKey}/regenerate   -> AI regenerate one section
```

A human write to a record never gets clobbered by the next AI generation —
generation only replaces rows it itself created (`source="ai_research"`);
anything a founder entered survives untouched, and the section immediately
stops reporting `needsInput` once real data exists.

## Numbers from the verification runs

Two runs, and you should size your UI against the second one.

| | Mocked (`ai_mocked=True`) | **Live Gemini** |
|---|---|---|
| Company | fictional | Zerodha (`https://zerodha.com`) |
| Research batches | 10 / 10 | 10 / 10 |
| LLM calls attributed | 11 | 11 (10 research + 1 synthesis) |
| Web searches | 0 (mock) | **169** |
| Dossier size | ~6,500 chars | **258,792 chars** |
| Sections populated | 17 / 17 | 16 / 17 |
| Sections that failed to store | 0 | 0 |
| Wall clock | ~12 seconds | **~11 minutes** |

The mocked run proves the plumbing: every endpoint, every status transition,
the write paths, the document pipeline, the dossier link. The live run proves
the research — `searches > 0` had never been measured before this, and
[`../changelog/CHANGES_production_hardening.md`](../changelog/CHANGES_production_hardening.md)
§10 records the four defects that only measuring it could find.

The one section still empty on the live run is `document_center`, correctly —
no document was uploaded to that company.

### What ~11 minutes means for the frontend

The research batches run **one at a time on SQLite** and concurrently on
Postgres (`profile_research_concurrency`, default 3), so production wall clock
is lower — but it is still minutes, not seconds. Concretely:

* **Never block a render on `POST /profile/onboard`.** In production it returns
  202 immediately and the work happens on a worker. Treat the 202 as "started".
* **Poll `GET /profile/runs/{runId}`.** Every 5–10 seconds is plenty. `stage`
  moves `researching → processing_documents → synthesizing → completed`, and
  `batchesSucceeded / batchesTotal` gives you a real progress bar during the
  longest stage.
* **`synthesizing` is one uninterrupted call and shows no incremental
  progress** — it took ~4 minutes here on a 259k dossier. Say so in the UI
  rather than letting a stalled-looking bar imply a hang.

### Dates are reported at the precision the source gave

`funding_history[].date` is `YYYY-MM-DD`, `YYYY-MM` **or** `YYYY`. A round
announced in "August 2010" comes back as `"2010-08"`, because that is what was
reported — rendering it as 1 August would assert a day no source stated. Parse
accordingly; a strict full-ISO parser will throw. This is verified live: the
Zerodha run stored `Seed Capital (Self-funded)` at `2010-08`, month precision.
