# Multi-LLM Capability Layer — Phase 4: OpenAI capability adapter

Builds on Phases 1–3. Phase 4 makes the config-profile toggles reach OpenAI.
All three providers (Gemini, Claude, OpenAI) now honor the same admin switches.
No migration (adapter-only).

## OpenAI capability adapter (fundos/llm/adapter.py :: _run_openai_chat)

Capability mapping:
  * structured_output → response_format {type: json_schema, ...} on Chat
                        Completions.
  * reasoning_effort  → reasoning_effort param (reasoning models). Sourced
                        from the profile's thinking_mode="effort" +
                        thinking_budget (low|medium|high).
  * web_search        → routed to the RESPONSES API with the hosted
                        {type:"web_search"} tool (that's where OpenAI's web
                        search lives; Chat Completions has no web search).
                        The Responses path also carries reasoning + structured
                        output and extracts url_citation annotations as
                        grounding sources.

Design notes:
  * A call with NO capabilities is byte-for-byte the previous Chat Completions
    request — nothing changes for existing callers.
  * Web search transparently switches to the Responses API only when
    requested; everything else stays on Chat Completions, so the working path
    is preserved.
  * URL-fetch / maps grounding are (correctly) not OpenAI capabilities — the
    config layer rejects them for OpenAI profiles.

## Tests
tests/api/test_llm_phase4_openai.py — json_schema structured output; reasoning
effort; web_search routes to Responses API with citation extraction;
plain-call-unchanged; maps-grounding rejected for OpenAI. Full suite green.

## Provider API note
OpenAI's Responses API surface (web_search tool, text.format json_schema,
reasoning.effort) and Chat Completions response_format json_schema are the
documented shapes; validate against the SDK/model versions you deploy. If a
web-search call errors, it's the Responses API path to check; the Chat
Completions path (structured output / reasoning) is unaffected.

## Status
Providers complete: Gemini (Phase 2), Claude (Phase 3), OpenAI (Phase 4). Next
and final: Phase 5 — observability (persist active capabilities + search count
+ grounding sources on the call log; a per-profile "test call" button in
admin to verify a configuration live before wiring it to a feature).
