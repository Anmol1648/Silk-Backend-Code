# Multi-LLM Capability Layer — Phase 2: Gemini adapter + Company Profile wiring

Builds on Phase 1 (config profiles + model catalog). Phase 2 makes the toggles
actually reach Google Gemini, and wires the Company Profile extraction to
consume a per-section config profile.

## 1. Gemini capability adapter (fundos/llm/adapter.py)

`_run_gemini` now translates the normalized capability payload from a config
profile into real Gemini API params:
  * web_search        → tools:[{google_search:{}}]   (Search grounding)
  * web_fetch         → tools:[{url_context:{}}]      (URL context)
  * maps_grounding    → tools:[{google_maps:{}}]
  * code_execution    → tools:[{code_execution:{}}]
  * structured_output → generationConfig.responseMimeType=application/json
                        + responseSchema
Grounding sources (groundingChunks) are extracted and returned in the usage
dict for observability. Multi-part responses are concatenated safely. When a
call has no capabilities, the request is exactly as before (plain
generateContent) — so nothing changes for existing callers.

The §5.4 "Basic Info = URL context ON, search OFF" exception is expressed
simply by a profile with web_fetch=true and web_search=false.

## 2. Capability threading (adapter)

`llm_generate(..., config_profile=<code>)` resolves an active LLMConfigProfile,
applies its system instructions + model, and passes its capabilities through
`_dispatch` → `_run_gemini`. Connection, fallback and logging still come from
the role's bound endpoint(s) — unchanged. Unknown/inactive codes are ignored.

## 3. Company Profile extraction wiring (fundos/profile/services.py)

Every profile-generation LLM call now passes
`config_profile="cp_extract.<section_key>"`. Convention: create an admin
LLMConfigProfile with that code (e.g. `cp_extract.founders`,
`cp_extract.company_overview`) to drive that section with Gemini grounding /
URL context / a response schema. If no such profile exists, generation runs
exactly as before — this is fully opt-in and backward-compatible per section.

To turn on grounded extraction for a section, in admin:
  1. Ensure a Gemini LLMEndpoint exists (GEMINI code, key env set).
  2. Create an LLMConfigProfile: code `cp_extract.<section_key>`,
     provider gemini, model from the catalog, endpoint = that Gemini endpoint,
     web_search on (or web_fetch-only for Basic Info), optionally a
     structured_output schema. Save (validation enforces the Gemini rules).
  3. Regenerate the profile — that section now runs grounded.

## Tests
tests/api/test_llm_phase2_gemini.py:
  * web_search → google_search tool; url_context (search off); responseSchema;
    plain request when no capabilities; grounding-source extraction.
  * section→profile code convention; missing-profile backward compatibility.
Existing tests updated where a mock dispatch signature needed the new kwarg.
Full suite green.

## Migration
None (Phase 2 is adapter + wiring only; the models shipped in Phase 1's
llm/0003).

## Next
Phase 3 — Claude capability adapter (Web Search + sub-controls, Web Fetch,
extended thinking, structured output). Phase 4 — OpenAI. Phase 5 —
observability (log active capabilities + searches + sources; per-profile test
call).
