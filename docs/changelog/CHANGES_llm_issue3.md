# LLM_ISSUE3 — Delete Company, Logo support, prompt + deploy guidance (Aug 2026)

Four items. Two are code (delete API, logo support) — shipped in this tar with
a migration. Two are guidance (prompt wording, seed_platform_config failure) —
no code needed; explained below.

## Item 1 — DELETE /api/v1/companies/{companyId}  (CODE)

New owner-gated endpoint that permanently deletes a company workspace and ALL
associated data. Returns 204 on success, 401/403 for non-owners, 404 (as
AuthzError, to avoid revealing existence) when absent.

Cascade (fundos/core/services/onboarding.py :: delete_company):
  * Deals — Deal.company is on_delete=PROTECT, so all deal-scoped rows across
    every app are cleared first (discovered dynamically via the app registry,
    so a new deal-scoped model is covered automatically), then the deals.
  * Profile data — CompanyProfile delete CASCADEs to Founders, KeyPeople,
    Competitors, FundingRound, NewsItem, sections and profile documents;
    company-scoped rows (e.g. cash position) are also purged.
  * Documents / Materials — their PHYSICAL files are removed from object
    storage first (best-effort; a blob that fails to delete is logged, never
    aborts the DB transaction) so no orphaned blobs remain.
  * Contexts — membership rows ARE the me/contexts source, so deleting the
    company's and its deals' memberships makes it stop appearing on the
    dashboard immediately.
  * The company row itself, last.
Whole operation is a single transaction: it fully succeeds or rolls back.

## Item 3 — Company logo support  (CODE, migration)

New CharField CompanyProfile.logo_url (migration companyprofile/0004).

  * POST /profile/onboard now accepts optional `logoUrl` and `logoBase64`.
    A direct URL is stored as-is; a base64 image is decoded, size/type checked
    (5 MB cap), saved to object storage, and its long-lived signed URL stored.
    Logo handling never fails onboarding (fundos/profile/logo.py).
  * GET /profile returns `logoUrl` at the top level of the response.
  * GET /me/contexts returns `logoUrl` on every scope:"company" item.

## Item 2 — Company Overview description via AI  (GUIDANCE, no code)

The section prompt controls this. The ONGC-style multi-point overview the
team wants is produced by making the company_profile_section prompt explicit.
Recommended System prompt for company_profile_section (edit in Admin → AI
Prompts, or it ships as the default after seed_platform_config):

  "You are an investment analyst writing the '{section_label}' section of a
   company profile. Using ONLY the supplied context (website extract, uploaded
   documents, public research, founder input), write a comprehensive company
   overview of 12–20 short factual sentences covering, where supported:
   what the company is and its legal/ownership status; year founded and HQ;
   scale and market position; core business and how it operates; key
   products/segments and geographies; notable subsidiaries/investments;
   strategic priorities and current initiatives; and future/vision. Do NOT
   invent facts or numbers — omit anything the context does not support.
   Return ONLY JSON: {\"content\": \"<the overview>\", \"needsInput\": false,
   \"missing\": []}."

Key point: the model must return the prose under the `content` key (the code
reads content/summary). With the response-normalizer already shipped, other
keys are tolerated, but the prompt should still ask for `content`. Richness
(the 12–20 point overview) comes from the prompt instruction above, plus a
capable model (gpt-4o rather than a mini model) and real website/research
context being available.

## Item 4 — `seed_platform_config` failed: "no such table: brand_config"  (GUIDANCE)

The traceback shows the command ran against SQLITE
(django/db/backends/sqlite3) — i.e. the DEV settings — not the production
Postgres DB configured in /etc/fundos/fundos.env. Two things to fix on the
server:

  1) Run management commands with the SAME settings the app uses. The env file
     sets DJANGO_SETTINGS_MODULE=fundos.settings.prod, but the failing shell
     didn't have it exported. Run, from the app dir with the app venv:
        export DJANGO_SETTINGS_MODULE=fundos.settings.prod
        # ensure the Postgres env vars are loaded:
        set -a; source /etc/fundos/fundos.env; set +a
        python manage.py migrate            # apply ALL migrations first
        python manage.py seed_platform_config
  2) "no such table" means migrations were never applied to that database.
     `migrate` must be run before `seed_platform_config`. This is also required
     for the new columns shipped recently (competitor fy_year/revenue/investors
     in 0003, and logo_url in 0004) — without `migrate`, the app will error on
     those fields.

If the intent really is SQLite for a quick test, the same applies: run
`migrate` first so the tables exist. But production should be on Postgres via
fundos.settings.prod.

## Migration note

This tar adds companyprofile/0004 (logo_url). Run `python manage.py migrate`
(with prod settings) on deploy.

## Tests

tests/api/test_llm_issue3.py: delete cascade + contexts removal, owner-gating,
logo via URL (returned on /profile and /me/contexts), logo via base64.
Full suite green.
