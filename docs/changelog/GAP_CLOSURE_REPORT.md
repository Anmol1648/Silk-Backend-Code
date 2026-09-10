# FundOS Backend — Gap Closure Report

Companion to `1_FundOS_Backend_Gap_Analysis.docx`. Every finding in the gap
document has been addressed in this build, plus additional gaps found in a
fresh cross-functionality sweep. Test suite: **65 passing** (34 original,
all preserved + 31 new gap-closure/export tests).

## A. Gap-document findings → fixes

| # | Finding | Resolution |
|---|---------|-----------|
| G1 | OTP verify expected only `code` | `otp` accepted as an alias; missing field → **422** `{"code":"required"}`, distinct from wrong code → 403. Contract documents `code` as canonical. `fundos/core/api/views.py` |
| G2 | No company/self-signup/deal creation | New onboarding surface: `POST /auth/signup` (JIT tenant+user+company+owner membership), `GET|POST /companies`, `GET|PATCH /companies/{id}`, `GET|POST /companies/{id}/deals` (owner-gated; provisions membership, stage states, CKB). `fundos/core/services/onboarding.py` |
| G3 | No `/contexts/switch` | `POST /contexts/switch` stores `user.last_active_deal_id` (migration 0003); `GET /me/contexts` returns `lastActiveDealId`. Pure UX — never authz. |
| G4/G14 | No export/download endpoints, no rendering wired | Full export layer using the Doc 6 Appendix F stack: ReportLab platypus (PDF + CONFIDENTIAL watermark), python-pptx (deck/teaser), openpyxl (model XLSX **with live =SUM() formulas**), python-docx. Files stored to the object bucket; short-lived signed URL returned. `fundos/exports/` |
| G5 | Valuation persisted zeros out of order | `generate_valuation` refuses to run without a current raise (**409**, ordering enforced) and refuses to persist a degenerate all-methods-inapplicable result (**422** "insufficient financial inputs — complete ARR/revenue in the CKB"). The generate endpoint also pre-checks synchronously so the caller sees 409/422 immediately instead of a silently-failing 202. |
| G6 | 202 with no job status | `GenerationJob` model + `GET /deals/{id}/jobs[/{jobId}]`. Every generator returns `{jobId, jobStatus, poll}`. Tasks run under tracking (queued→running→succeeded/failed with error text and artifact id); completion emits `fundos.generation.completed|failed` alerts and an in-app notification. |
| G7 | Approval accepted a zero valuation | Pre-approval invariant in `approve_strategy_profile`: zero/meaningless valuation range or zero approved raise → **409** with a specific message. Combined with G5, the degenerate state can neither be produced nor approved. |
| G8 | Virus scan stub | Real clamd INSTREAM scan of the stored object. Infected → quarantined + `fundos.material.infected` alert; scan errors **fail closed** where scanning is required (uat/prod: `FUNDOS_VIRUS_SCAN_REQUIRED=True`, ClamAV host mandatory); analysis is blocked until a file scans clean. Dev degrades to clean with a logged warning only. |
| G9 | Undocumented verb/shape differences | `API_CONTRACT.md` publishes the verified contract; plus pragmatic aliases: `POST /strategy/objectives` (= PUT), `POST /workspace` (idempotent create-or-return), `PATCH /ckb/fields/{fieldKey}` per-field verb. |
| G10 | Deck outline gate not obvious | Documented explicitly (outline → approve-outline → slides; 423 before approval). No backend change — correct by design. |
| G11 | Thin prod settings | `prod.py` completed: explicit CORS allowlist (required env), JSON logging with request id, DB pooling/timeouts, Redis cache + separated Celery broker/result, SMTP, OCI/India storage vars, ClamAV host (required), throttle rates. All env-driven. |
| G12 | Stage 4+ out of scope | Unchanged (by design). |
| G13 | Currency inconsistency | Single presentation currency (INR) converted at the presentation edge; every money field carries `{value(INR), ccy, usdValue}` and responses expose `fx: {usdInr, presentationCcy}`. The USD/INR rate is settings-driven (`FUNDOS_USD_INR`) and recorded in valuation assumptions. Engines/persistence stay in USD so history and fixtures remain stable. |
| G15 | No rate limiting | DRF throttles: OTP request 6/hour per email+IP, OTP verify 30/hour, all generate endpoints 60/hour per user (LLM budget). Env-tunable per environment; 429 `E-RATE-429`. |

## B. Additional gaps found in the sweep (not in the gap document)

| Finding | Resolution |
|---|---|
| **Refresh tokens unusable** — issued at login but no `/auth/refresh` endpoint existed | `POST /auth/refresh` added; rejects access tokens presented as refresh. |
| **OTP TTL inconsistency** — settings/design say 5 min (`FUNDOS_OTP_TTL_SECONDS=300`) but the view hardcoded 10 min and reimplemented hashing | View now uses the model's `OtpToken.issue/verify` helpers with settings-driven TTL/attempts. |
| **`otp_option=whatsapp` ignored** — AppConfiguration supports WhatsApp OTP but only email was ever sent | `otp_delivery.py` (ported from Gyain `documents/whatsapp_otp.py`): Meta AUTHENTICATION template with `{{1}}` body + copy-code button, never the alert templates; graceful fallback to email. |
| **Broken signed URLs** — the local storage connector emitted `/api/v1/files/…?expires&sig` URLs but no such route existed | `GET /files/{path}` added: HMAC-validated (constant-time compare), expiry-checked, path-traversal-safe download. Round-trip + tamper tests included. |
| **BR-S2-013 not enforced** — peers could be removed below 3 comparables | Enforced (409) in both `/peers/curate` remove and the new `DELETE /strategy/peers/{peerId}`. |
| **Missing design surfaces** | Added: `GET /deals/{id}/dashboard`, `GET /deals/{id}/stage-state`, `PATCH /strategy/raise/scenario` (scenario selection), `PATCH /strategy/instruments/{id}` (selection), `GET /strategy/peers/{peerId}`, `POST /strategy/peers/add`. |
| **Raise generation queued silently without objectives** | Synchronous 422 pre-check on `/strategy/raise/generate`. |

## C. Gyain code incorporated (per Doc 6)

- WhatsApp **OTP** delivery pattern — `documents/whatsapp_otp.py` →
  `fundos/core/services/otp_delivery.py` (auth-template payload, msisdn
  normalisation, timeout/error handling).
- OTP store semantics (hashed, TTL, attempt counter, resend-supersedes)
  were already ported into `OtpToken`; the login view now actually uses
  them (they were bypassed).
- Export stack choices from the Gyain features spec (ReportLab platypus /
  python-pptx / openpyxl live formulas) implemented in `fundos/exports/`.
- Email/WhatsApp alert services and the alerting engine were already
  ported; job completion now feeds them (`fundos.generation.*`,
  `fundos.material.infected`, `fundos.deal.created` events).

## D. Known items intentionally deferred (documented, not hidden)

- Document **versions/restore** REST surface (`GET …/versions`,
  `POST …/restore`) — the model layer (`Document.restore_version`) exists;
  the HTTP surface is small but was deprioritised behind the FE blockers.
- `GET /deals/{id}/stage-outputs/1` typed handoff — Stage-2 reads the CKB
  directly today.
- Live market-deal peer feed — fixture catalogue remains until the feed is
  ratified (Doc 7 critical path).

## E. Migration & deploy notes

- New migration: `core/0003_user_last_active_deal_id_generationjob.py`
  (additive only — safe on existing data).
- New required prod env vars: `FUNDOS_CORS_ALLOWED_ORIGINS`,
  `FUNDOS_REDIS_URL`, `FUNDOS_SMTP_HOST`, `FUNDOS_STORAGE_BUCKET`,
  `FUNDOS_CLAMAV_HOST` (see `fundos/settings/prod.py`).
- New dependencies: reportlab, python-pptx, openpyxl, XlsxWriter,
  python-docx, clamd (see `requirements.txt`).
