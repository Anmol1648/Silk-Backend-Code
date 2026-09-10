# Configurable Multi-LLM Capability Layer — Phase 1 (Aug 2026)

Feature Requirement Doc §4: the admin-controllable per-call capability layer.
Phase 1 of the agreed plan — the foundation everything else plugs into. No
provider dispatch changes yet (those are Phases 2–4: Gemini → Claude → OpenAI).

Scope confirmed with product: web search + URL fetch + per-call capability
toggles now; own-document retrieval (File Search / citation-block retrieval)
deferred. Config profiles REFERENCE the existing LLMEndpoint for connection,
so fallback + logging stay intact.

## What's in this tar

### 1. LLMModelCatalog (editable model list per provider — §4.4)
Admin-managed list of available models per provider. New model releases are
added here with no code deploy. Retiring a model = untick is_active (HIDDEN),
which removes it from selection WITHOUT deleting config profiles that
reference it. Seeded with a starter set (Claude Sonnet 4.6 / Opus 4.1,
Gemini 2.5 Flash / Pro, GPT-4o / mini) via seed_platform_config.

### 2. LLMConfigProfile (per-call capability profile — §4.2 / §4.3)
A reusable profile a feature attaches by `code`, carrying:
  * provider + model (from the catalog) + a referenced LLMEndpoint (connection).
  * system_instructions.
  * tool toggles: web_search, web_fetch/url_context, maps_grounding,
    function_calling, code_execution.
  * web-search sub-controls: max_uses, allowed_domains, blocked_domains,
    user_location, dynamic_filter.
  * structured_output + output_schema (JSON schema).
  * thinking_mode (off | token budget | effort level) + thinking_budget.

Validation (model.clean(), enforced in admin) encodes the real provider
capability matrix and the hard constraints, so an invalid config CANNOT be
saved:
  * Gemini: search/grounding tools cannot be combined with function calling.
  * Claude: dynamic result filtering requires code execution.
  * Domain allow-list and block-list are mutually exclusive.
  * Web-search sub-controls require web_search on.
  * Capabilities the provider doesn't support at all are rejected (so an
    admin can't rely on a toggle that would be silently dropped).
  * The referenced endpoint's provider must match the profile's provider.

`capabilities_payload()` produces the normalized, provider-supported-only
capability dict that Phases 2–4 will translate into provider API params.

### 3. Admin
Both models registered. LLMConfigProfile uses grouped fieldsets (Tools /
Web-search sub-controls / Structured output / Thinking) so the switchboard is
readable; invalid combinations are blocked on save by the validation above.
LLMModelCatalog supports inline is_active editing to add/hide models fast.

## Migration
Adds llm/0003 (LLMModelCatalog, LLMConfigProfile). Run `migrate`.

## Tests
tests/api/test_llm_config_layer.py — all constraint paths + catalog hide +
seed. Full suite green.

## Next (not in this tar)
Phase 2: Gemini capability adapter (Search grounding + URL context +
responseSchema) and wire the Company Profile extraction to consume config
profiles per section — so the pipeline does real web-grounded research end to
end. Then Phase 3 Claude, Phase 4 OpenAI, Phase 5 observability.
