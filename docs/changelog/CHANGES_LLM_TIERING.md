# CHANGES — Claude + Two-Tier LLM + Exhaustive Company Profile

This build adds Claude, a per-tenant Simple/Advanced LLM split, and the
exhaustive Company Profile bundle (A–E). All edits are additive and
backward-compatible; existing call sites are unchanged.

## 1. Claude / provider
- `requirements.txt`: enabled `anthropic>=0.39` (adapter already had `_run_anthropic`).
- `seed_platform_config._llm_tiers()`: creates the `ANTHROPIC` endpoint
  (reads `ANTHROPIC_API_KEY` from the env — only the var NAME is stored),
  a `claude.advanced.websearch` config profile (web search ON) and a
  `claude.simple` config profile (no tools), plus the GLOBAL default tiers.
- Model catalog seed gains `claude-haiku-4-5`.
- **Set `ANTHROPIC_API_KEY` in the environment/secrets before enabling live AI.**

## 2. Prompt = source of truth; config overrides; tier is admin-set
- `platformcfg.PromptTemplate` gains `tier` (blank|simple|advanced) and
  `purpose`. Admin (Django Admin → AI Prompts) explains each prompt and lets
  the admin choose Simple vs Advanced (Advanced = web search). Blank = the
  shipped default tier. An admin row still overrides the code default.
- `llm/default_prompts.py`: every role carries a default tier via `get_tier()`;
  new prompts added: `company_profile_deep_extract` (advanced),
  `profile_qa` (simple), and the previously-missing `company_profile_field`.

## 3. Master-endpoint flag + tenant tiers
- `llm.LLMConfigProfile.tier` (simple|advanced) — the flag marking a configured
  LLM as the Simple or Advanced model (Advanced ⇒ web search).
- `llm.TenantLLMTier(tenant_id, tier → config_profile)` — the two LLMs each
  tenant uses; a `tenant_id=NULL` row is the global default.
- `llm/tiers.py` resolves `role → prompt.tier → tenant config` and
  `llm/adapter.py` auto-resolves it when no explicit `config_profile` is passed.

## 4. Exhaustive Company Profile (A–E)
- (A) `company_profile_deep_extract` prompt — one dossier, raw figures only.
- (B) web-search Advanced profile seeded (see §1).
- (C) `profile.Investor` model (cap table) + serializer builders for
  `cap_table`, `company_story`, `market_research`, `investment_thesis`,
  `leadership_detail` (CFO first), `competitors_detail`, `derived_multiples`.
  Editable cap table via the generic records endpoint (`records.cap_table`).
- (D) `profile.services.compute_profile_ratios` — deterministic EV/Revenue,
  YoY, implied multiples (never asked of the model).
- (E) **Employee Strength removed** (serializer + core-fields + frontend + mock);
  **CFO floated to the top** of the leadership section.
- `services.generate_deep_profile` orchestrates the one-shot extract + fan-out.
- `services.answer_profile_question` — grounded Q&A (Simple tier).
- New endpoints: `POST /companies/{id}/profile/deep-generate`,
  `POST /companies/{id}/profile/qa`.

## Migrations to apply
    python manage.py migrate llm
    python manage.py migrate platformcfg
    python manage.py migrate companyprofile
    python manage.py seed_platform_config          # seeds tiers + prompts

## Frontend
- `api/endpoints.js`: `profile.deepGenerate`, `profile.ask`.
- `CompanyProfile.jsx`: a "Generate full profile" button + an "Ask about this
  company" Q&A panel; Employee Strength display removed; new sections render
  through the generic renderer.
