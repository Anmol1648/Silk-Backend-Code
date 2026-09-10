# FundOS Backend — Verified API Contract (frontend reference)

This is the authoritative request/response contract for the delivered
backend, closing Gap **G9** (verbs a frontend might not guess) and **G10**
(deck sequencing). Everything below is exercised by the test suite.

Base path: `/api/v1`. Errors use the envelope
`{"error": CODE, "detail": msg, "fields": {...}}` with codes
`E-VAL-422, E-AUTH-401, E-AUTHZ-403, E-NOTFOUND-404, E-CONFLICT-409,
E-GATE-423, E-RATE-429, E-PENDING-409`.

## 1. Auth & onboarding

| Endpoint | Verb | Notes |
|---|---|---|
| `/auth/signup` | POST | `{email, name?, companyName?}` → 202. JIT tenant + user (+ company & owner membership). Uniform response whether the account existed. Then complete OTP login. **(G2)** |
| `/auth/otp/request` | POST | `{email}` → 200 uniform. TTL **5 min**, 5 attempts, single-use. Delivery per `AppConfiguration.otp_option` (email default; WhatsApp uses the AUTHENTICATION template, falls back to email). Throttled 6/hour per email+IP **(G15)**. |
| `/auth/otp/verify` | POST | `{email, code}` — **`otp` accepted as an alias (G1)**. Missing field → 422 `{"code":"required"}` (distinct from a wrong code → 403). Returns `{accessToken, refreshToken, user}`. |
| `/auth/refresh` | POST | `{refreshToken}` → new token pair. (New — refresh tokens were previously unusable.) |
| `/me/contexts` | GET | `{items:[…], lastActiveDealId}` |
| `/contexts/switch` | POST | `{dealId}` → records the active deal (UX only, never authz). **(G3)** |
| `/companies` | GET/POST | POST `{name, domain?, hqCountry?}` → 201; creates an owner membership. **(G2)** |
| `/companies/{id}` | GET/PATCH | PATCH edits the profile (owner-gated). |
| `/companies/{id}/deals` | GET/POST | POST `{name, roundType}` → 201 `{dealId}`; owner-gated; provisions membership + stage states + CKB. **(G2)** |

## 1a. Company Profile — sections and generation runs

`GET /companies/{id}/profile` returns `sections` as an OBJECT keyed by
section key, each `{sectionKey, isComplete, lastUpdatedAt, data}`. Seventeen
sections, in order:

```
company_profile   founders            products_services   customers_markets
competitive_advantages   business_model   revenue_model    company_metrics
financial_summary  funding_history     competitors         news
investors_cap_table   company_story    industry_research   investment_thesis
document_center
```

**Renamed** from the previous contract — storage is unchanged and the old
names are still accepted on PATCH, so a client can migrate at its own pace:
`recent_news`→`news`, `investors_and_cap_table`→`investors_cap_table`,
`company_story_and_usp`→`company_story`,
`industry_and_market_research`→`industry_research`.

**Merged:** `key_people` is gone as a section. `founders` now carries everyone,
with `is_founder: boolean` distinguishing a founder from another key person.
The `/profile/records/key_people` CRUD endpoint is unchanged.

**Retired:** `ai_company_summary`, `leadership_detail`, `derived_multiples`.
Deactivated rather than deleted — an administrator can re-enable any of them
in Django admin and it returns to the payload. Derived multiples are still
computed; they are no longer their own top-level section.

Generation is long-running (roughly ten search-grounded research calls, then
document extraction, then one large synthesis call), so it has its own
progress surface:

| Endpoint | Verb | Notes |
|---|---|---|
| `/companies/{id}/profile/runs` | GET | `{items: [...]}` — recent runs, newest first. Each carries `status`, `stage`, `stageLabel`, batch counts, `dossierChars`, `llmCalls`, `searches`. |
| `/companies/{id}/profile/runs/{runId}` | GET | Everything about one run: `stageTimings`, `activity` (a human-readable log), `documents` (per file: how it was read, whether OCR ran and why), `sectionsGenerated` / `sectionsNeedingInput`, and `dossier`. |
| `/companies/{id}/profile/deep-generate` | POST | Runs the pipeline with `force=true`, bypassing the sources-unchanged skip. |

`dossier` is the **evidence file** — the merged research and document text the
profile was derived from — as `{characters, downloadUrl, format:"md",
expiresInSeconds}`. The URL is signed and short-lived, so fetch it when you
need it rather than storing it.

`status` is `queued | running | succeeded | failed | refused`. **`refused` is
not a failure**: the run declined to publish because nothing was retrieved
(no web search succeeded and no document yielded text), and writing a profile
from that would mean presenting the model's recollection as research. The
existing profile is left untouched and `refusedReason` says what to fix.

### 1b. What a generated field is guaranteed to be

Everything the model returns is typed against the section's own field spec
before it is stored, so the contract below is enforced rather than hoped for.

* **Strings are plain text.** No markdown, no HTML — these strings go into PDF,
  PPTX and DOCX, none of which render markup.
* **`number|null` means exactly that.** Never `""`, never a numeric string.
  This changed: `competitors[].revenue` and `competitors[].fy_year` previously
  came back as `""` when unset, including in the empty-row template. They are
  now `null`. A client that special-cased `""` can drop that branch.
* **Enumerated fields** (`funding_status_name`, `revenue_size_name`) hold a
  listed value or `""` — never an approximation of one.
* **`macro_sector` and `sub_sector` hold ONE term.** They are matched against
  the benchmark table by exact name; a comma-separated list matched nothing and
  cost the company its peer comparables.
* **URLs are `http(s)`, publicly resolvable, and not search-redirects.** A
  citation the model gave as a grounding redirect is dropped, because those
  expire within weeks — read `source` (the publication name) as the durable
  reference. An empty `link` beside a populated `source` is expected, not a bug.

`financial_summary[].financials[]` gains two fields that the schema always asked
for and the serializer never emitted:

| Field | Notes |
|---|---|
| `pat_m` | `number\|null` — profit after tax |
| `currency` | ISO code for that row's figures; falls back to the company's `homeCurrency` |
| `is_estimate` | already present — **now load-bearing.** A row with `true` is a projection and is excluded from anything that values the company |

Money fields named `_m` or `_usd_mn` are in millions of `currency`. Check
`currency` before doing arithmetic across rows.

`GET /profile/runs/{runId}` carries the corrections the boundary applied, under
`progress.synthesizing.corrections` (with an exact `corrections_total`), and the
first twelve also appear in `activity`. A recurring correction is a prompt or
field-spec problem worth reporting, not a per-run anomaly.

## 2. Deal navigation

- `GET /deals/{id}/masterplan`, `GET /deals/{id}/stage-state`,
  `GET /deals/{id}/dashboard` (stages, CKB completeness, pending
  artefacts, unread notifications, recent jobs).
- `POST /deals/{id}/stages/{n}/override` — owner only.

## 3. Generation jobs (G6)

Every `…/generate` (and `/research/run`, `/review/run`) returns:
```json
{"status":"queued", "jobId":"…", "jobStatus":"queued|succeeded|failed",
 "poll":"/api/v1/deals/{id}/jobs/{jobId}"}
```
Poll `GET /deals/{id}/jobs/{jobId}` → `{status: queued|running|succeeded|
failed, error, artifactId, …}`; list with `GET /deals/{id}/jobs?kind=…`.
Completion also raises an in-app notification (`generation.completed|failed`)
and the `fundos.generation.completed|failed` alert events. In dev Celery is
eager, so `jobStatus` is usually already terminal in the 202 body.

## 4. CKB

- `GET /deals/{id}/ckb`; bulk edit `PATCH /deals/{id}/ckb`
  `{fields:[{fieldKey, value, ccy?, basis?}]}`.
- Per-field (Doc 4 shape): `PATCH /deals/{id}/ckb/fields/{fieldKey}`
  `{value, ccy?, basis?}` or `{action: "accept"|"reject"}` for parked AI
  suggestions. `POST /deals/{id}/ckb/suggestions/{fieldKey}` also works.

## 5. Stage 2 — verbs to know (G9)

- **Objectives:** `PUT /strategy/objectives` (upsert). `POST` is accepted
  as an alias.
- **Peers:** generate → `POST /strategy/peers/generate`; curate via
  `POST /strategy/peers/curate` `{action: add|remove|approve}` **or** REST:
  `POST /strategy/peers/add`, `GET|DELETE /strategy/peers/{peerId}`.
  Removal below **3 comparables** → 409 (BR-S2-013).
- **Raise:** `POST /strategy/raise/generate` (**requires objectives**, else
  422); select a scenario with `PATCH /strategy/raise/scenario`
  `{scenarioId}`.
- **Valuation (G5):** `POST /strategy/valuation/generate` **requires a
  current raise** (else 409) **and ARR/revenue in the CKB** (else 422
  `{"valuation":"needs_financials"}`). A degenerate all-methods-inapplicable
  result is never persisted.
- **Instruments:** `POST /strategy/instruments` generates options;
  `PATCH /strategy/instruments/{id}` `{selected:true}` records an
  informational preference.
- **Approve (G7):** `POST /strategy/approve` → 409 if the valuation range
  is zero/meaningless or the approved raise is zero.

### Phase 1 — Deal Scorecard

Two endpoints, and they are **company-scoped**, unlike the rest of Stage 2:

- `GET /api/v1/companies/{companyId}/fundraising/phase1`
- `POST /api/v1/companies/{companyId}/fundraising/phase1/override`

**`{companyId}` is a real `core.Company` UUID.** There is no job id in this
system, and none is accepted: `Assessment.company` is a required foreign key
while `Assessment.deal` is nullable (`SET_NULL`), so an assessment belongs to a
company and outlives the deal it was raised under. A company may also have
several concurrent raises, and scoping this screen to one of them would mean
picking which raise is the real one.

The GET returns the whole screen in one response — the summary strip, then the
four blocks. All of it derives from the same scored rows, so splitting it would
mean walking the model several times for one page load:

```
company, sector, sub_sector, deal_stage, ask_amount, capital_raised,
assessment_date, overall_score, deal_rating, input_coverage,
strongest_category, weakest_category,
executiveSummaryData {narrative, tags}, dealScorecardData,
diligenceFindingsData, bandRecommendationsData
```

`overall_score`, `deal_rating` and `input_coverage` are the engine's own
persisted figures. `strongest_category` / `weakest_category` are the best and
worst SCORED category rollups, labelled with their score (`"Team 8.2"`), and
empty when nothing scored. `ask_amount` prefers the founder's `DealTargets`
target raise and falls back to the figure the assessment banded against.

`dealScorecardData.ratingGap` turns the rating label back into a distance,
because the ladder is a step function and the label is the part that hides it:

```
ratingGap {rating, nextRating, nextRatingAt, pointsToNext,
           heldAt, pointsToLose}
```

`pointsToNext` is what reaching the next rung costs; `pointsToLose` is how much
score would have to go before the current label drops. Both are `null` at the
ends of the ladder, and `ratingGap` itself is `null` when nothing scored. The
cut-points come from the engine's own `RATING_LADDER`, so the gap can never
describe a ladder the rating did not come from.

**Lifecycle.** Every response carries a `status`, and that is the only field a
client should branch on — the HTTP code says whether to retry, `status` says
what to render:

| `status` | HTTP | Meaning |
|---|---|---|
| `generating` | 202 | A run is in flight. Poll `poll`, then re-request. |
| `ready` | 200 | Scored, with evidence. Render the scorecard. |
| `no_evidence` | 202 | The run finished and established nothing. |
| `failed` | 502 | The run errored; `reason` carries the job's own error. |

Non-ready states return every contract key, present and empty, so a client can
destructure the response without branching on whether scoring has finished.
`overall_score` and `deal_rating` are `null` and `dealScorecardData` is `{}` —
**the endpoint never returns a fabricated score.**

**The client never generates an assessment itself.** If a company has none, the
GET starts the existing pipeline (`jobs.create_job` + `generate_assessment_task`
— the same pair `queue_generation` uses everywhere) and answers `generating`.

**Polling is safe and cheap.** The GET is a read endpoint: it writes nothing,
and it will not start a second run while a `queued` or `running`
`GenerationJob` exists for the deal — otherwise every poll during a wait would
queue another extraction. All page-wide config is read once per request
(`phase1.Reference`), so the query count does not scale with the size of the
scoring model: ~27 queries for a 62-leaf scorecard, and 0 writes.

Under the dev setting `CELERY_TASK_ALWAYS_EAGER` the task runs inline, so the
first call may return `ready` directly. That is the dev harness's behaviour,
not the endpoint's contract.

Bands anywhere in the response use the engine's own capitalised vocabulary
(`Excellent` / `Good` / `Fair` / `Poor`, plus override-only `Exceptional`).

**Audit findings.** `Assessment.audit_findings` is written at scoring time and
carries `{source, severity, check, message}`, plus `parameters` / `inputKey` /
`category` where the finding concerns specific rows. Three sources answer three
different questions, and a reader should not conflate them:

| `source` | Asks | Examples |
|---|---|---|
| *(absent)* | Was it BUILT correctly? | `weights_sum`, `stage_defaulted`, `override_uncommented`, `exceptional_without_override`, `low_coverage`, `ref_contradiction` |
| `rules` | Are the VALUES sane? | `implausible_value`, `thin_category`, `thin_benchmark_sample`, `manual_overrides` |
| `integrity` | Is it WORTH quoting? | `uncited_value`, `derived_without_workings`, `unevidenced_judgement`, `fragile_weight`, `confidence_carried_weight`, `thin_verified_share`, `excluded_rows`, `tam_inverted`, `sector_unresolved`, `cohort_rows_blank`, `ask_outside_cohort` |

`severity` is `error` / `warning` / `info`, and `error` has a consequence:
`audit_status` becomes `failed`, which is a claim that the score should not be
quoted at all. It is therefore reserved for what cannot be true of a sound
assessment — no score, a value scored with no evidence tier, a sub-sector TAM
larger than its own sector. A thin evidence pack is a `warning`, not an
`error`: conflating the two would make a failed audit mean nothing.

A clean assessment adds no `integrity` findings, and the block runs at scoring
time only — it costs the read path nothing.

The POST takes `{parameter_ref, override_score, reason}`. `parameter_ref` is
the ref code the scorecard displays (`A.1.a`); `inputKey` is accepted where a
ref code is ambiguous. An empty `reason` is **400** — the integrity checks
count uncommented overrides and block sign-off on them, so it is refused at
entry rather than recorded and flagged later. `override_score` is 0-10; above
the Excellent cut-point the response sets `beyondAutomatedCeiling`, because
only a human may award more than the rules can reach. The response carries
`recalculated_total_score` and `updated_band` from the engine's own re-roll:
the leaf is re-banded, every parent above it re-weighted around it, and the
overall score and rating rebuilt.

### Money & currency (G13)
Engines compute in USD; every money field is presented as
`{"value": <INR>, "ccy": "INR", "usdValue": <USD>, "basis"?}` and
money-bearing responses carry `"fx": {"usdInr": <rate>, "presentationCcy":
"INR"}`. The rate is also written into the valuation's assumptions.
Configure with `FUNDOS_PRESENTATION_CCY` / `FUNDOS_USD_INR`.

## 6. Stage 3 sequencing (G10)

Deck is a **two-step**: `POST /deck/outline` → founder reviews →
`POST /deck/approve-outline` → `POST /deck/slides`. Calling `/deck/slides`
before outline approval returns **423** by design. The workspace is created
implicitly behind the Stage-3 hard gate; `GET /workspace` (or `POST`,
accepted as an idempotent alias) returns 423 until the Strategy Profile is
approved.

## 7. Exports (G4/G14)

All return `{"downloadUrl", "storageUri", "format", "expiresInSeconds"}`
(short-lived signed URL; local backend serves via
`GET /files/{path}?expires&sig`).

| Endpoint | Formats |
|---|---|
| `POST /deals/{id}/teaser/export` | `pdf` (default), `pptx`, `docx` |
| `POST /deals/{id}/deck/export` | `pptx` (default), `pdf`, `notes` |
| `POST /deals/{id}/model/export` | `xlsx` (default — **live formulas**), `csv`, `pdf` |
| `POST /deals/{id}/im/export` | `pdf` (default), `docx`, `watermarked` |
| `GET /deals/{id}/readiness/report` | pdf |
| `GET /deals/{id}/strategy/report` | pdf |
| `GET /deals/{id}/review/report` | pdf |

## 8. Rate limits (G15)

429 `E-RATE-429` with `Retry-After`. Defaults: OTP request 6/hour
(email+IP), OTP verify 30/hour, all generate endpoints 60/hour per user.
Env-tunable (`FUNDOS_THROTTLE_*`); dev settings are generous.

## 9. Materials & virus scanning (G8)

Upload responses carry `scanStatus`. A file is only analysed/available once
it scans **clean**; `infected` is quarantined; scanner errors in uat/prod
**fail closed** (status stays `pending`). Configure `FUNDOS_CLAMAV_HOST`,
`FUNDOS_CLAMAV_PORT`; prod requires them.
