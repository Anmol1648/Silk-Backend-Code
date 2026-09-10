# Silk — Administrator Configuration Guide

Everything in this document is changed from the **Django Admin panel** at
`/admin/`. None of it is exposed to end users, and none of it requires a
code change or a release.

The rule applied throughout: *if a value changes what a user experiences,
an administrator owns it.*

---

## Quick start on a fresh environment

```bash
python manage.py migrate
python manage.py seed_platform_config     # working defaults for everything below
python manage.py createsuperuser
```

Existing installations additionally need:

```bash
python manage.py migrate_to_silk --dry-run   # report what would change
python manage.py migrate_to_silk             # apply
```

`migrate_to_silk` unlocks every stage, creates a Company Profile for each
existing company, carries founder-entered knowledge-base values into the
matching profile sections, and links artefacts from the former Stage 3 to
the Document Center as read-only addenda. It is idempotent.

---

## Platform Configuration → AI Prompts

Every prompt sent to the language model, one row per role (22 of them).

| Field | What it does |
|---|---|
| Role | The generation this prompt drives. Must match the role used in code. |
| System prompt | Behaviour, tone, and the exact JSON shape required back. |
| User prompt | Short instruction; the real payload travels in the context block. |
| Available context | Read-only. The context keys this role receives — use it to rewrite a prompt safely without reading code. |
| Notes | Why the prompt is worded as it is, for whoever edits it next. |

Edits take effect on the **next generation** — no restart. Deactivating a
row falls back to the default shipped in
`fundos/llm/default_prompts.py`. The bulk action *Reset selected prompts to
shipped defaults* restores a prompt you would rather start over on.

> **One constraint worth understanding.** Never ask the model to produce or
> recalculate a number. Valuation, dilution, runway, raise sizing and every
> score are computed by deterministic engines and injected as read-only
> context; the response schemas reject numeric authority fields, so a
> prompt that requests them fails validation rather than returning a
> plausible-looking invention. This separation is what makes the output
> defensible to an investor — please preserve it when editing.

---

## Platform Configuration → Branding

Product name, logo, favicon, browser title, primary colour, export footer.

Renaming here changes the name across the application, generated PDFs,
DOCX, PPTX and XLSX files, and email and WhatsApp notifications. Internal
identifiers and database tables are deliberately unaffected — renaming
those would touch every import and migration for no user-visible benefit.

Logo assets are referenced by URL. Upload them to your static host or
object storage and paste the URL.

---

## Platform Configuration → Stages

The journey shown in the sidebar: numbering, labels, purpose text,
prerequisites and whether a stage is implemented yet.

This is the **single source of truth**. The frontend reads it from the API
rather than hard-coding stage numbers, so the two sides can no longer drift
apart. Renaming or reordering a stage here is immediately reflected in the
UI.

**Prerequisites are advisory.** Stages are never locked — a user can open
any stage at any time. Prerequisites display as met or pending so the user
knows what is outstanding, and only the specific *actions* that depend on
missing data are disabled. Format:

```json
[{"key": "company_profile_complete",
  "label": "Company Profile completed",
  "explanation": "Strategy quality depends on profile completeness."}]
```

Recognised keys are `company_profile_complete` and
`strategy_outputs_available`. An unrecognised key is treated as met, so a
typo can never block anyone.

---

## Platform Configuration → Fund-raise Buckets

The seven target amounts: $100K, $250K, $500K, $1M, $2M, $3M, $5M. Each
carries its typical stage, the investor types that fund it, and a typical
dilution range.

## Platform Configuration → Traction Rules

Which bucket is *recommended* by default. Rules are evaluated in priority
order (lowest number first) and the **first match wins**, so the most
demanding thresholds must sit at the lowest priority numbers — the seeded
rules are already ordered this way.

Conditions are JSON. All keys are optional and combine with AND:

| Key | Meaning |
|---|---|
| `arr_usd_min` / `arr_usd_max` | Annual recurring revenue in USD |
| `capital_raised_usd_min` / `_max` | Capital raised to date, in USD |
| `paying_customers_min` | Number of paying customers |
| `months_full_time_min` | Longest time any founder has been full-time |
| `founder_full_time` | `true` / `false` |
| `company_registered` | `true` / `false` |

Example: `{"arr_usd_min": 500000, "founder_full_time": true}`

The seeded ladder reproduces the Outline:

| Company state | Recommended |
|---|---|
| Idea stage, unregistered, no traction | $100K |
| Full-time 3+ months, early traction | $250K |
| Early ARR, full-time, paying clients | $1M |
| ARR or raised ≥ $0.5M | $2M |
| ARR or raised ≥ $1M | $3M |
| ARR or raised ≥ $1.5M | $5M |

**Uplift Rules** add levels on top — the seeded rule adds two for a star
founder or an exceptional sector, per Outline §3.3(g).

The recommendation is a **default, not a constraint**: the founder or
advisor may select any of the seven, and the reasoning for the default
stays visible so an override is an informed one.

---

## Platform Configuration → Company Profile Sections

Which sections the profile has, their order, and which LLM role regenerates
each one. Adding a section is a configuration change, not a release.

Two independent flags, which are deliberately different sets:

- **Required for creation** — must be present before a profile can be
  generated at all.
- **Required for completeness** — counts toward the *complete* state. A
  complete profile plus the owner's confirmation of review is what routes
  the dashboard to the last active stage rather than back to Stage 1.

---

## Platform Configuration → Countries

The HQ dropdown, seeded with 108 countries. `sort_order` pins common
markets to the top (India, US, UK, Singapore, UAE, Australia, Canada,
Germany); everything else sorts alphabetically below.

`home_currency` sets the currency of record for companies headquartered
there. Founders enter amounts in their home currency; the system computes
and classifies in USD and displays both.

---

## Platform Configuration → FX Rates

Dated conversion rates. Rates are **dated rows rather than a mutable
constant**, because the rate used is recorded against every generated
artefact — a valuation produced in March must remain reproducible in
September.

Add a row per day per pair. With no row present the system falls back to a
documented default (USD/INR 84.0) and labels the source `default`, so a
missing rate degrades visibly rather than silently.

---

## Platform Configuration → UI Copy

User-visible strings an administrator may reword: empty-state messages,
advisory notes, the regulatory disclaimer on valuation output. Each row has
a `key` the frontend looks up and a `description` saying where it appears.

---

## Existing configuration (unchanged)

These were already admin-owned and continue to work as before:

- **Application Settings** — feature flags, SMTP, WhatsApp, OTP channel,
  the global AI mock switch.
- **LLM Endpoints / Role Bindings / Model Costs** — which provider serves
  which role, with fallbacks. API keys travel as environment variable
  *names*; values never enter the database.
- **Scoring Config** — versioned engine weights and thresholds. Every
  generated artefact records the version it used.
- **Research Source Flags** — per-adapter enable and legal-clearance
  switches.
- **Master Values, Sector Taxonomy, Investor Category Map**.

---

## What remains blocked on external dependencies

Two capabilities are built but cannot produce meaningful output yet. Both
are procurement or legal decisions rather than engineering work:

1. **Market benchmarking data.** Peer selection, trading comparables and
   transaction comparables need a licensed financial-data subscription.
   The engines are implemented and tested; they are waiting on data. Until
   then, peers can be entered manually and the analysis uses those. The
   interface says so plainly rather than showing an empty result.

2. **LinkedIn enrichment.** Founder profile data is processed from public
   profiles with explicit founder confirmation, per the clarification.
   Broader enrichment through a licensed provider remains available to
   enable via Research Source Flags once terms are in place.
