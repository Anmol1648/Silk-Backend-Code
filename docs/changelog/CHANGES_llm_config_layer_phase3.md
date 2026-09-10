# Multi-LLM Capability Layer — Phase 3: Claude (Anthropic) capability adapter

Builds on Phases 1–2. Phase 3 makes the config-profile toggles reach Anthropic
Claude. No migration (models shipped in Phase 1's llm/0003); adapter-only.

## Claude capability adapter (fundos/llm/adapter.py :: _run_anthropic)

Translates the normalized capability payload into real Anthropic params:

  * web_search        → web_search_20250305 tool, with the sub-controls:
                        max_uses, allowed_domains, blocked_domains,
                        user_location.
  * url_fetch         → web_fetch_20250910 tool.
  * code_execution    → code_execution_20250522 tool (this is also what
                        Claude's dynamic search-result filtering runs on; the
                        config layer already requires code execution when
                        dynamic_filter is on).
  * thinking(budget)  → thinking={type:"enabled", budget_tokens:N};
                        max_tokens is auto-bumped above the budget as the API
                        requires.
  * structured_output → a forced tool (emit_structured_output) whose
                        input_schema IS the response schema; the tool input is
                        returned as JSON so the existing parse/normalize path
                        handles it. tool_choice forces that tool (except when
                        thinking is enabled, which the API disallows combining
                        with forced tool choice).

Response handling is block-aware: a Claude response with thinking, tool-use
and web-search-result blocks is parsed correctly — text blocks are
concatenated, the structured tool_use input is preferred when structured
output was forced, and web-search/-fetch result blocks yield grounding
sources (returned in usage for Phase-5 logging). A call with no capabilities
is byte-for-byte identical to before.

## Company Profile wiring

No new wiring needed — Phase 2 already threads `config_profile` through every
profile-generation call. A section (or the Claude structuring stage) uses
Claude capabilities simply by pointing its `cp_extract.<section_key>` profile
at a Claude endpoint with the desired toggles. E.g. a structuring profile with
structured_output + a response schema, or an extraction profile with
web_search + domain filters.

## Tests
tests/api/test_llm_phase3_claude.py — web search + sub-controls, web fetch,
thinking budget (+max_tokens bump), dynamic-filter→code-exec tool, forced-tool
structured output, source extraction, plain-call-unchanged. Full suite green.

## Provider API version note
The tool type strings (web_search_20250305, web_fetch_20250910,
code_execution_20250522) are the documented dated tool versions; confirm
against the Anthropic API version you deploy, as these are periodically
revised. If a call errors on a tool type, that string is the one to update.

## Next
Phase 4 — OpenAI capability adapter (Web Search, reasoning effort, json_schema
structured output). Phase 5 — observability (persist active capabilities +
searches + grounding sources on the call log; per-profile test-call button).
