# Silk 2.0 — Implementation Summary

What changed, why, and where. Written against the Silk 2.0 PRD, the FundOS
Outline, and the clarifications document (C1–C12), which overrides both
where they disagree.

Verified: **136 backend tests pass** (107 pre-existing, 29 new covering the
clarifications); frontend builds clean; cross-tenant isolation confirmed on
every new endpoint.

---

## How each clarification was implemented

### C1 — A company is independent; it may have several concurrent deals

This **reversed** the earlier recommendation of one active raise per
company, so the model is company-centric rather than deal-centric where it
matters.

- `CompanyProfile` is keyed one-per-**company**, not per deal.
- A company may exist with no deal at all, and may run concurrent raises
  (equity and debt at once) — nothing constrains the count.
- Profile, founders, cash position and fund-raise recommendation are all
  company-scoped: `/api/v1/companies/{id}/…`.
- Deal-scoped infrastructure (generation jobs, knowledge base, materials)
  is untouched. `_default_deal()` resolves a deal when one is needed for
  that machinery, creating one silently if absent.

### C2 — Complete = mandatory sections populated **and** owner confirms review

- Two independent flags on `ProfileSectionConfig`:
  `required_for_creation` and `required_for_completeness`. The
  clarification noted these sets differ, so they are separate fields and a
  test asserts they are not identical.
- `CompanyProfile.is_complete` requires both conditions. Confirming review
  without filling mandatory sections does not make a profile complete, and
  neither does the reverse.

### C3 — LinkedIn optional, processed from public profiles, founder confirms

- Founder LinkedIn is **not mandatory**; onboarding succeeds without it.
- When supplied, `Founder.self_confirmed` and `self_confirmed_at` record
  the founder's confirmation that they have reviewed the information and
  provide it as their own input. The frontend requires that checkbox before
  submitting when a URL is present.
- The `founder_profile` prompt is deliberately conservative: public
  professional background only, nothing inferred.

### C4 — Every source is primary; generate from whatever is available

- `collect_sources()` gathers website, uploaded documents, founder input
  and public research, and **isolates every failure**. A failed source is
  reported, never fatal.
- `generate_profile()` completes even when sources fail. Affected sections
  are marked `needs_input` with an explanatory message rather than left
  blank or erroring.

### C5 — Burn is an input; the company may be profitable

- `CashPosition.monthly_burn_value` is an input field with the definition
  shown beside it in the UI. It may be zero or negative.
- When burn ≤ 0 the response sets `isProfitable` and returns `null` for
  runway and exhaustion date — the interface says *"runway isn't your
  constraint"* rather than showing an implausible number.
- Runway, exhaustion date and additional capital required are computed
  deterministically in `compute_cash_position()`, never by the LLM.

### C6 — Strategy is advisory; the outputs matter, not the process

- **No stage is ever locked.** `ensure_stage_states` creates every stage as
  available; `hard_gate_met()` always returns `True`.
- Prerequisites are reported as met/pending through
  `prerequisite_status()` and rendered by `PrereqPanel`. Only the specific
  actions that need missing data are disabled.
- A founder who already knows their numbers may enter the headline terms
  directly through `DealTargets`; that satisfies the Investor Discovery
  prerequisite and counts as 100% Stage 2 completion.

  **Four figures, two entered and two calculated:**

  | # | Field | How |
  |---|---|---|
  | 1 | Target raise | Entered |
  | 2 | Pre-money valuation | Entered |
  | 3 | Post-money valuation | Calculated — pre-money + raise |
  | 4 | Dilution % | Calculated — raise ÷ post-money × 100 |

  Post-money and dilution are arithmetic, so they are derived rather than
  typed. `recalculate()` runs on every save, so the calculated pair can
  never drift from the inputs, and a client that sends them anyway is
  ignored — a test asserts this. Both are left null when the inputs are
  incomplete rather than being guessed at.

  All four are mirrored into the knowledge base in USD, so a manually
  entered raise is indistinguishable from a strategy-derived one at the
  point of use. Dilution is currency-invariant, which is also tested.
- `StageGuard` deleted. The override endpoint is retained as a harmless
  no-op so older clients don't break.

### C7 — Stage 3 artefacts become read-only Company Profile addenda

- `migrate_to_silk` links existing teasers, decks, IMs and models to the
  Document Center with `is_readonly=True` and
  `origin_stage="legacy_stage_3"`.
- Nothing is deleted, and nothing is reassigned to the new Stage 3
  (Investor Discovery), which would be wrong.
- The Document Center states that new material generation is being rebuilt
  as a dedicated assistant.

### C8 — Readiness retained, surfaced inside the profile

- The engine, its six dimensions and the gap list are all preserved.
- Exposed as a `readiness` section within the Company Profile rather than a
  separate wizard step, with a reassess action.
- Stage 1 completion is now **profile-section completeness**, not readiness
  score — that calculation was rewritten.

### C9 — Seven values; the Outline's traction rules pick the default

- `FundraiseBucket`: seven discrete amounts ($100K → $5M), each with
  typical stage, investor types and dilution range.
- `TractionRule`: admin-editable conditions driving the default selection,
  evaluated in priority order, first match wins.
- `UpliftRule`: star founder / exceptional sector adds levels (seeded at
  +2, per Outline §3.3g).
- **A bug worth flagging:** my first seeding ordered the rules so that
  every company above $0.5M ARR collapsed to $1M, because the least
  demanding rule matched first. The ladder is cumulative — a company at
  $1.8M ARR also satisfies every lower rung — so the most demanding
  thresholds must evaluate first. Fixed and locked in by
  `test_traction_ladder_matches_the_outline`.
- The recommendation is a default: all seven remain selectable, with the
  reasoning shown so an override is informed.

### C11 — Home currency for the company, USD for fund-raise comparison

- `CompanyProfile.home_currency`, defaulted from the country master.
- Founders enter amounts in home currency; the system computes and
  classifies in USD and displays both.
- `FxRate` holds **dated** rows, and the rate used is stored against each
  artefact (`fx_rate_used`, `fx_as_of`) so a valuation stays reproducible.
- With no rate configured the system falls back to a documented default and
  labels the source `default` — visible degradation, not silent drift.

### C12 — Outline is direction; PRD is this phase

Built to the PRD. Two Outline items that bear on current decisions are
implemented because they were needed now: the traction-based sizing
thresholds (C9) and the readiness/do-ability assessment (C8).

---

## The Django Admin panel

Per your instruction, everything that changes user-facing behaviour is
admin-configurable. See `ADMIN_CONFIGURATION.md` for the full guide.

New app `fundos.platformcfg`:

| Model | Controls |
|---|---|
| `PromptTemplate` | All 22 AI prompts, per role, with context-key reference and reset-to-default |
| `BrandConfig` | Product name, logos, browser title, colour, export branding |
| `StageDefinition` | Stage numbering, labels, purpose, advisory prerequisites |
| `FundraiseBucket` | The seven target amounts |
| `TractionRule` / `UpliftRule` | Which bucket is recommended, and uplifts |
| `ProfileSectionConfig` | Profile sections, order, completeness rules |
| `Country` | HQ dropdown (108 seeded) and home currency |
| `FxRate` | Dated conversion rates |
| `UiCopy` | Editable user-visible strings |

Defaults ship in code (`fundos/llm/default_prompts.py` and the seed
command), so a fresh install works immediately and an empty table never
breaks anything. The DB row wins when present.

---

## What is preserved

Four properties make the output defensible to an investor, and no change
here weakens them:

1. **The LLM never produces numbers.** Deterministic engines compute every
   figure; prompts explain them; response schemas reject numeric authority
   fields.
2. **Provenance on every field** — source, confidence, fact-vs-inference,
   verified flag. Extended to the new profile sections.
3. **Approved artefacts are immutable**, superseded rather than edited.
4. **Money is always a triple** (value, currency, basis).

Regeneration also refuses to silently discard a human edit — it asks first,
and the previous version stays in history.

---

## Still blocked on external dependencies

Neither is engineering work:

1. **Market benchmarking data.** Peer selection, trading and transaction
   comparables need a licensed subscription. The engines are built and
   tested; they are waiting on data. Manual peer entry works meanwhile and
   the UI states the limitation.
2. **Broader LinkedIn enrichment** beyond the public-profile,
   founder-confirmed path implemented under C3.

---

## Files at a glance

**New backend:** `fundos/platformcfg/` (models, admin, services, views,
seed command) · `fundos/profile/` (models, services, views, tasks,
migration command) · `fundos/llm/default_prompts.py` ·
`tests/api/test_clarifications.py`

**Rewritten:** `fundos/core/services/stage_state.py` (advisory
prerequisites) · `fundos/llm/adapter.py` (admin-resolved prompts)

**New frontend:** `context/ConfigContext.jsx` · `pages/Dashboard.jsx` ·
`pages/CompanyLayout.jsx` · `pages/profile/` (OnboardingForm,
CompanyProfile) · `pages/stage2/CashRunwayStep.jsx` ·
`pages/stage2/FundraiseBuckets.jsx` · `components/PrereqPanel.jsx` ·
`lib/download.js`

**Removed:** `components/StageGuard.jsx` · `pages/Picker.jsx` ·
`pages/stage1/`

---

## Deal terms — resolved

The earlier open question about which fields represent manually entered
terms is closed. Your correction was right: dilution and post-money are
consequences of the two inputs, not separate opinions, so they are
computed and displayed as read-only results. See C6 above for the
implementation.
