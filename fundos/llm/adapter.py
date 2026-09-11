"""
LLM adapter (Doc 6 §3.3 / Appendix C-D and Doc 8 §5).

Single entry point:
    llm_generate(role, system, prompt, context) -> dict

Behaviour: resolve binding → if is_mocked or ai_mocked() return the role's
canned fixture → call primary with provider_kind wire shape → on timeout/5xx
retry once → fall back to fallback endpoint → raise LLMUnavailable (mapped to
E-LLM-502). Response must be JSON; validated against the role's schema with
ONE repair attempt ("return valid JSON only") before failing.

Tracking is implemented IN the adapter (no monkey-patching): tokens in/out,
latency, endpoint code, success/fallback/error class; tracking failure never
breaks the call.

Numbers are injected INTO prompts, never accepted FROM responses — schemas
forbid numeric-authority fields (Doc 8 §5).

COST OPTIMISATION LAYER
-----------------------
Four changes here cut spend without changing any call site's contract:

1. CONTEXT FILTERING — every role in default_prompts declares `context_keys`.
   That declaration used to be documentation only; it is now enforced, so a
   role is billed for the sources it actually needs instead of the whole
   payload bundle. Roles that declare nothing keep the full context.
2. PROMPT CACHING — the context block is lifted OUT of the user message into
   a separate cacheable system block. Across a profile run the same source
   bundle is sent to ~15 section calls; cached reads bill at 0.1x input.
   Two breakpoints are used: the static role instructions (identical for every
   company, forever) and the per-run context bundle.
3. BOUNDED RETRIES — the ladder was `2 attempts x 2 endpoints` = up to four
   full-price calls on a bad afternoon. Retries are now capped globally per
   call and spaced with backoff.
4. BUDGET CEILING — a tenant's month-to-date spend is checked before dispatch
   so a runaway loop fails fast instead of billing.

Cache-aware token accounting is logged (cache_read/cache_write) so the spend
dashboard stays truthful once caching is on.
"""
import base64
import hashlib
import json
import logging
import time

import requests

from fundos.core.exceptions import LLMUnavailable, UngroundableCall

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Raw LLM body capture (opt-in via settings.FUNDOS_LOG_LLM_BODIES).
#
# Writes the outgoing prompt and the raw incoming response to a DEDICATED
# logger (fundos.llm.bodies -> llm_bodies.log), tagged with the current
# GEN[<run_id>] so the trace scripts can pull bodies for one run. Gated OFF by
# default; truncated by FUNDOS_LOG_LLM_BODY_MAXCHARS (0 = no cap); every body
# passes through _redact so a key can never land in the file; never raises.
# --------------------------------------------------------------------------
_bodies_logger = logging.getLogger("fundos.llm.bodies")


def _current_run_id():
    try:
        from fundos.profile.trace import current_trace
        tr = current_trace()
        return getattr(tr, "run_id", None) or "-"
    except Exception:
        return "-"


def _log_llm_body(kind, role, *parts):
    """Gated, redacted, best-effort capture of one LLM request/response body."""
    try:
        from django.conf import settings
        if not getattr(settings, "FUNDOS_LOG_LLM_BODIES", False):
            return
        text = "\n".join(p for p in parts if p)
        full_len = len(text)
        text = _redact(text)
        cap = int(getattr(settings, "FUNDOS_LOG_LLM_BODY_MAXCHARS", 8000) or 0)
        if cap and len(text) > cap:
            text = text[:cap] + f"\n...[+{full_len - cap} chars truncated]"
        _bodies_logger.debug("GEN[%s] llm.body.%s role=%s chars=%d\n%s",
                             _current_run_id(), kind, role, full_len, text)
    except Exception:
        pass
# Anthropic bills a cache WRITE at 1.25x input, so caching a tiny block is a
# net loss. Only mark a block cacheable once it is worth it. ~4 chars/token.
MIN_CACHEABLE_CHARS = 4096

# Global cap on retries for ONE llm_generate call, across all endpoints.
MAX_TOTAL_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.75

# ---------------------------------------------------------------------------
# WEB-SEARCH CONTAINMENT
#
# `max_uses` caps how many times a model may search inside ONE request. Only
# Anthropic exposes it as a tool parameter. Gemini and OpenAI run search
# SERVER-SIDE within a single request: by the time the response arrives, every
# search has already run and been billed.
#
# So for those providers an in-flight hard cap is NOT possible. What follows
# is defence in depth instead, and the limits of each layer are stated plainly
# because pretending otherwise is worse than the leak:
#
#   1. ADVISORY   — the cap is stated in the system prompt. Models generally
#                   respect it. Nothing enforces it. LEAKY.
#   2. PRE-FLIGHT — before dispatch, the worst-case cost of an uncapped search
#                   call is reserved against the tenant budget. A call that
#                   could not be afforded at its worst case is refused. HARD,
#                   but protects the BUDGET, not the individual call.
#   3. DETECTION  — actual searches are counted from response metadata and
#                   logged per call. Exact, but after the fact.
#   4. BREAKER    — repeated overruns on a profile trip a breaker that strips
#                   web search from subsequent calls. Prevents the SECOND and
#                   later overruns, never the first.
#
# Net effect: on an advisory provider a single call can still exceed max_uses
# and you will pay for it. What cannot happen is that it goes unnoticed, or
# that it repeats, or that it runs when the budget cannot absorb it.
# ---------------------------------------------------------------------------
SEARCH_ENFORCEMENT = {
    "anthropic": "native",    # tool-level max_uses, enforced by the provider
    "gemini": "advisory",     # google_search grounding — no count parameter
    "openai": "advisory",     # hosted web_search tool — no count parameter
}

# Worst case assumed for an advisory provider that ignores the directive.
# Used only for the pre-flight budget reservation, so it should be
# pessimistic — under-estimating here is what lets a budget be blown.
ADVISORY_OVERRUN_FACTOR = 3

# Rough tokens added to the accumulated context per search round trip.
# Used for cost estimation only, never for billing.
TOKENS_PER_SEARCH_ESTIMATE = 3500

# Consecutive overruns on one config profile before the breaker trips.
SEARCH_BREAKER_THRESHOLD = 3

# How long the breaker keeps search disabled for that profile.
SEARCH_BREAKER_COOLDOWN_SECONDS = 3600


def _default_search_max_uses():
    from fundos.llm.models import DEFAULT_SEARCH_MAX_USES
    return DEFAULT_SEARCH_MAX_USES


def search_enforcement_for(provider_kind):
    """'native' | 'advisory' | None for a provider kind."""
    from fundos.llm.models import ENDPOINT_KIND_TO_PROVIDER
    family = ENDPOINT_KIND_TO_PROVIDER.get(provider_kind)
    return SEARCH_ENFORCEMENT.get(family)


def _search_directive(max_uses, expect_search=False):
    """Prompt text about searching, for providers with no tool parameter.

    THE BUG THIS FIXES
    ------------------
    On Gemini, `google_search` is a MODEL-DECIDED tool: sending it grants
    permission to search, it does not cause a search. The only sentence the
    model ever received about searching was this one — and it was written
    purely as a CAP: "you may run AT MOST 6 searches… when the budget is
    spent, answer from what you have."

    Read that from the model's position, holding 24,000 characters of
    supplied context. It is told there is a ceiling and told it may answer
    from what it has. Nothing tells it that answering from context alone is
    the wrong outcome. So it never searched, on any call, in any run — and
    because the tool was correctly attached and the response correctly
    parsed, every layer of the stack reported healthy.

    `expect_search` is set for roles whose parameters CANNOT be answered from
    the supplied material — sector TAM, peer raise recency, funding history.
    Those roles now get an instruction to search first, with the cap stated
    afterwards as a bound on that instruction rather than instead of it.
    """
    if not max_uses and not expect_search:
        # No cap and no expectation: nothing useful to say.
        return ""
    if not max_uses:
        max_uses = _default_search_max_uses()
    if not expect_search:
        return (
            f"\n\nSEARCH BUDGET: you may run AT MOST {max_uses} web searches "
            f"for this task. Prefer one precise query over several broad "
            f"ones. When the budget is spent, answer from what you have and "
            f"record anything still unresolved in `missing`.")
    return (
        f"\n\nSEARCH FIRST — THIS IS NOT OPTIONAL.\n"
        f"The context you have been given is a starting point, not the "
        f"answer. Several of the parameters requested — market size, growth "
        f"rates, peer funding history, investor names, headcount — are NOT "
        f"in the supplied material and cannot be. If you answer them without "
        f"searching you are answering from recollection, which is the one "
        f"failure this task cannot tolerate.\n"
        f"Run web searches BEFORE you answer. Use up to {max_uses} of them; "
        f"plan the queries so each one covers several parameters. Answering "
        f"entirely from the supplied context is a FAILED response, even if "
        f"every field is populated.\n"
        f"When the budget is spent, leave what remains unresolved in "
        f"`missing` rather than filling it from memory.")


# --------------------------------------------------------------------------
# Call counting (v27.3).
#
# `_generate_consolidated` used to DERIVE its llmCalls figure from section
# bookkeeping, which silently omitted every call that was not a section:
# judgment, readiness and assessment. Only the adapter knows what it
# dispatched, so it keeps the tally. Thread-local because Celery workers run
# concurrent generations in one process and a module-level int would blend
# two runs together.
# --------------------------------------------------------------------------
# v27.4 — the bare counter is replaced by a full ledger (fundos/llm/ledger.py).
#
# A count alone answered "how many calls?" and nothing else, so the cost of a
# run still had to be reconstructed by parsing eleven trace lines by hand. The
# ledger records tokens, money, latency and searches per call at the same choke
# point, which is what makes the `run.economics` trace line possible.
#
# These three names are kept as thin wrappers because they are the public
# surface other modules already import.
from fundos.llm import ledger                      # noqa: E402


def llm_call_count():
    """Calls dispatched on this thread since the last ledger reset."""
    return ledger.mark()


def llm_calls_since(mark):
    """Calls dispatched since `mark`, a value previously from llm_call_count()."""
    return len(ledger.since(mark))


def llm_ledger_since(mark):
    """The full per-call book since `mark`. Feeds run-level economics."""
    return ledger.since(mark)


def _search_reminder():
    """Short restatement of the search instruction, placed AFTER the context.

    Deliberately terse. The full directive is already in the system prompt;
    this exists because on a long context that directive is thousands of
    characters from the task, and the last thing a model reads before
    answering is the thing it weighs most heavily. Kept short so it adds
    negligible cost and cannot itself become the thing that gets skimmed.
    """
    return ("\n\nREMINDER, and this overrides any impression the material "
            "above may have given: the context you have just read is NOT "
            "sufficient. Run your web searches BEFORE answering. Answering "
            "entirely from the material above is a failed response even if "
            "every field comes back populated.")


# --------------------------------------------------------------------------
# Search role sets (v27.4 — DERIVED, not maintained in parallel).
#
# There used to be two hand-maintained lists that had already drifted apart:
#
#   tier_contract.SEARCH_REQUIRED_ROLES   what must be TRUE for the output to
#                                         be worth publishing -- drives tier
#                                         resolution and the contract
#   adapter.SEARCH_EXPECTED_ROLES         what we HOPE a call does -- drives
#                                         the prompt directive, the tail
#                                         reminder and the no-search warning
#
# On 18 Aug they disagreed three ways. `peer_insight` was required but not
# expected, so it was forced onto a searching tier and then received neither
# the "SEARCH FIRST" directive, nor the tail reminder, nor any warning when it
# came back with zero searches -- the one role guaranteed to fail silently.
# `market_research` and `company_profile_consolidated` were expected but not
# required, so they could still resolve to `simple`, lose the tool, and only
# then produce a warning about the tool they no longer had.
#
# REQUIRED is now the single authority and lives with the contract that
# enforces it. BENEFICIAL is the genuine second category: retrieval improves
# the answer but its absence does not make the answer dishonest. EXPECTED is
# their union and is never edited directly.
# --------------------------------------------------------------------------
from fundos.llm.tier_contract import (          # noqa: E402  (cycle-free:
    SEARCH_REQUIRED_ROLES,                      # tier_contract imports only
    TierContractViolation,                      # tiers.py constants)
)

# Search sharpens these but they remain publishable without it: they summarise
# or reframe material another call already retrieved.
#
# v29 — EMPTIED, because both former members were not roles.
#
#   "company_profile_consolidated"   not in LLM_ROLES
#   "market_research"                not in LLM_ROLES
#
# `market_research` is a config-profile SUFFIX (`cp_extract.market_research`)
# and a SECTION key; `company_profile_consolidated` is a generation MODE. The
# role that writes those sections is `company_profile_section`. So neither
# string could ever equal `role` at a call site, and the no-search warning
# they were added to raise could never fire for either. They read as coverage
# and provided none — the same class of defect as the four roles missing from
# ROLE_TIER_DEFAULTS, and caught the same way: by checking the names against
# the real role list instead of trusting them.
#
# Left as an empty set rather than deleted: the DISTINCTION between "must
# retrieve" and "retrieval helps" is sound and the next role that genuinely
# fits belongs here, not in SEARCH_REQUIRED_ROLES.
SEARCH_BENEFICIAL_ROLES = set()

# Roles whose questions cannot be answered from the material we supply, so a
# call that performs no search has answered from memory whatever it returns.
# DERIVED — add roles to SEARCH_REQUIRED_ROLES or SEARCH_BENEFICIAL_ROLES.
SEARCH_EXPECTED_ROLES = SEARCH_REQUIRED_ROLES | SEARCH_BENEFICIAL_ROLES


def _count_searches(provider_kind, resp_meta, *, tool_attached=False):
    """Actual searches performed, from provider response metadata.

    Returns None when the provider gives us nothing to count from, so a
    genuine zero is never confused with "unknown".

    v27.5 — on Gemini, absent metadata IS a zero when the tool was attached.

    The 19 Aug logs settled this empirically. `company_profile_section` ran
    three times with `web_search` attached and returned `webSearchQueries` on
    every call that searched (2 in one run, 6 in the next), so the field is
    present whenever a search occurs. Its absence therefore means the model
    declined — not that the provider went quiet.

    Reporting those as "unknown" understated the headline badly: run 2 read
    `searches=6 searches_unknown=8` when the truth was six searches across
    three calls and none at all across the other eight. `searches_unknown` is
    still reserved for the case it was built for -- the tool was never
    attached, so there was nothing for the model to decline.
    """
    from fundos.llm.models import ENDPOINT_KIND_TO_PROVIDER
    family = ENDPOINT_KIND_TO_PROVIDER.get(provider_kind)
    if not resp_meta:
        return 0 if (family == "gemini" and tool_attached) else None
    if family == "anthropic":
        return resp_meta.get("server_tool_use_web_search")
    if family == "gemini":
        # webSearchQueries is the list of queries actually issued, which is
        # what we want; groundingChunks counts SOURCES and over-reports.
        q = resp_meta.get("web_search_queries")
        if q is not None:
            return len(q)
        return 0 if tool_attached else None
    if family == "openai":
        return resp_meta.get("web_search_call_count")
    return None


def _search_breaker_key(profile_code):
    return f"llm:searchbreaker:{profile_code}"


def search_breaker_tripped(profile_code):
    """True when this profile has overrun repeatedly and is in cooldown."""
    if not profile_code:
        return False
    try:
        from django.core.cache import cache
        return bool(cache.get(_search_breaker_key(profile_code) + ":open"))
    except Exception:
        return False


def record_search_outcome(profile_code, allowed, actual):
    """Track overruns; trip the breaker after repeated offences.

    Deliberately resets on a compliant call — the breaker is for a model or
    configuration that consistently overruns, not for one unlucky company
    whose dossier genuinely needed more searching.
    """
    if not profile_code or not allowed or actual is None:
        return
    try:
        from django.core.cache import cache
        key = _search_breaker_key(profile_code)
        if actual <= allowed:
            cache.delete(key)
            return
        strikes = (cache.get(key) or 0) + 1
        cache.set(key, strikes, SEARCH_BREAKER_COOLDOWN_SECONDS)
        logger.warning(
            "LLM SEARCH OVERRUN: profile %s used %d searches against a cap of "
            "%d (strike %d/%d). The provider does not enforce max_uses, so "
            "this call was billed in full.",
            profile_code, actual, allowed, strikes, SEARCH_BREAKER_THRESHOLD)
        if strikes >= SEARCH_BREAKER_THRESHOLD:
            cache.set(key + ":open", True, SEARCH_BREAKER_COOLDOWN_SECONDS)
            logger.error(
                "LLM SEARCH BREAKER OPEN: profile %s exceeded its search cap "
                "%d times running. Web search is disabled for this profile "
                "for %ds. Move the tier to a provider that enforces max_uses "
                "(Anthropic), or raise the cap if the overruns are "
                "legitimate.",
                profile_code, strikes, SEARCH_BREAKER_COOLDOWN_SECONDS)
    except Exception as e:
        logger.debug("LLM: search-outcome tracking skipped: %s", e)


def _worst_case_search_tokens(capabilities, enforcement):
    """Pessimistic input-token estimate for the pre-flight reservation."""
    ws = (capabilities or {}).get("web_search") or {}
    if not ws:
        return 0
    cap = ws.get("max_uses") or 8
    if enforcement != "native":
        # Nothing stops the model exceeding the cap, so reserve for it.
        cap = cap * ADVISORY_OVERRUN_FACTOR
    return cap * TOKENS_PER_SEARCH_ESTIMATE


# Extraction roles: the task is lifting values out of supplied text, so
# extended thinking adds cost (billed at the OUTPUT rate) and no quality.
# v27.4 — `research_synthesis` REMOVED. It is a SEARCH_REQUIRED role, and a
# role that must retrieve needs the deliberation budget to decide to retrieve.
# Declaring it no-thinking was a direct contradiction of its tier contract; the
# guard in _call_llm now catches the general case, and this removes the one
# instance that actually existed. The invariant is asserted at import time
# below, so the two sets cannot drift back into conflict unnoticed.
NO_THINKING_ROLES = {
    "document_read",
    "company_profile_records", "company_profile_structured",
    "company_profile_field", "company_profile_section", "profile_qa",
}

# v27.5 — roles where a deliberation budget is justified by the WORK, not by
# a tool it was once believed to enable.
#
# The 19 Aug diagnostic proved thinking has no effect on whether Gemini
# searches, so "this role must retrieve" is no longer a reason to fund
# thinking. What remains is the original one: some roles weigh evidence and
# reach a conclusion, and those are worth the output tokens. Everything else
# is lifting values out of text it has been handed.
#
# Deliberately SMALL. Every role added here costs real money on every run —
# on 19 Aug the over-broad v27.4 rule cost ₹3.6 and 56 seconds per run and
# truncated the assessment call in both runs it ran.
THINKING_JUSTIFIED_ROLES = {
    "company_profile_judgment",
    "peer_insight",
    "research_synthesis",
}

# --------------------------------------------------------------------------
# Import-time coherence check.
#
# Two role sets that contradict each other is how v27.3 shipped a contract
# that the adapter then overrode. A conflict is a PROGRAMMING error in a
# constant, so it is caught when the module loads rather than 90 seconds into
# a generation. Logged rather than raised: a hard failure at import time takes
# the whole service down, and the runtime guard in _call_llm already keeps the
# behaviour correct — this exists so nobody has to discover it from a log.
# --------------------------------------------------------------------------
#
# v29 — every member of every role set must be a REAL role.
#
# Four of the five sets in this module are hand-maintained string sets, and
# nothing checked them against LLM_ROLES. Two entries in
# SEARCH_BENEFICIAL_ROLES were section keys rather than roles, so the warning
# they existed to raise could never fire, and the set read as coverage while
# providing none. A typo in any of these sets fails the same silent way.
def _validate_role_sets():                                  # pragma: no cover
    from fundos.llm.models import LLM_ROLES

    known = set(LLM_ROLES)
    for name, members in (("SEARCH_REQUIRED_ROLES", SEARCH_REQUIRED_ROLES),
                          ("SEARCH_BENEFICIAL_ROLES", SEARCH_BENEFICIAL_ROLES),
                          ("NO_THINKING_ROLES", NO_THINKING_ROLES),
                          ("THINKING_JUSTIFIED_ROLES",
                           THINKING_JUSTIFIED_ROLES),
                          ("ROLE_MAX_OUTPUT_TOKENS",
                           set(ROLE_MAX_OUTPUT_TOKENS)),
                          ("ROLE_TIMEOUT_SECONDS",
                           set(ROLE_TIMEOUT_SECONDS))):
        unknown = sorted(members - known)
        if unknown:
            logger.error(
                "LLM CONFIG: %s contains %s, which are not in LLM_ROLES. "
                "These entries can never match a call and are silently "
                "inert — correct the spelling or remove them.",
                name, ", ".join(unknown))


_ROLE_SET_CONFLICT = THINKING_JUSTIFIED_ROLES & NO_THINKING_ROLES
if _ROLE_SET_CONFLICT:                                      # pragma: no cover
    logger.error(
        "LLM CONFIG CONFLICT: %s appear in BOTH THINKING_JUSTIFIED_ROLES and "
        "NO_THINKING_ROLES. The adapter will keep the budget; remove them "
        "from one set to make the intent explicit.",
        ", ".join(sorted(_ROLE_SET_CONFLICT)))

# Applied to any role with no entry in the table below. Output is the
# expensive direction, so the fallback is deliberately modest.
#: What each model will actually return, whatever we ask for. A request above
#: this is not a bigger answer, it is a ceiling the model ignores — and a
#: trace that then blames a configured limit the administrator cannot raise.
_PROVIDER_OUTPUT_CAPS = {
    "gemini-2.5-flash": 65536,
    "gemini-2.5-pro": 65536,
    "gemini-2.0-flash": 8192,
}

#: Used when the model is not in the table above. Deliberately absent rather
#: than guessed: clamping to a number we invented would cut answers short on
#: a model that could have finished.
_DEFAULT_OUTPUT_CAP = None


def _provider_output_cap(model_name):
    """The model's own output limit, or None when it is not recorded."""
    key = str(model_name or "").strip().lower()
    if key in _PROVIDER_OUTPUT_CAPS:
        return _PROVIDER_OUTPUT_CAPS[key]
    for known, cap in _PROVIDER_OUTPUT_CAPS.items():
        if key.startswith(known):
            return cap
    return _DEFAULT_OUTPUT_CAP


DEFAULT_MAX_OUTPUT_TOKENS = 2048

# Per-role output ceilings — the budget each role was DESIGNED for, and the
# value used whenever the role binding does not state a lower one.
ROLE_MAX_OUTPUT_TOKENS = {
    # A transcription, not a summary: the ceiling has to hold an entire
    # document's text. A deck runs to a few thousand tokens; a scanned
    # financial statement considerably more, and being cut off mid-table is
    # the failure that matters here, because the truncation lands in the
    # middle of the numbers rather than at the end of some prose.
    "document_read": 32768,
    "company_profile_field": 512,
    "profile_qa": 1024,
    "company_profile_records": 2048,
    "company_profile_structured": 2048,
    "company_profile_deep_extract": 8192,
    # v27.5 — was absent, so it inherited DEFAULT_MAX_OUTPUT_TOKENS (2048).
    #
    # This role answers 16-30 assessment parameters, each with a value, a
    # confidence and a source citation. 2,048 tokens does not hold that, and
    # once v27.4 left a thinking budget attached there was nothing left at
    # all. Both 19 Aug runs prove it:
    #
    #   run 1  JSONDecodeError at char 4492         whole assessment lost
    #   run 2  finish_reason=MAX_TOKENS, thinking=1945, completion=0,
    #          was_repaired=true                    7 of 30 fields survived
    #
    # v30 — RAISED from 8192, which was measured against a smaller job.
    #
    # "The input here is ~3K tokens" was true when this role saw only the
    # per-file summaries. It now receives the consolidated dossier, so it can
    # actually answer the parameters it is asked about — and answering more
    # of them costs more output, not less.
    #
    # A 27 Aug run hit the ceiling exactly:
    #
    #   finish_reason=MAX_TOKENS  completion=10629  thinking=3022
    #   -> "returned JSON that was cut off; recovered the complete prefix"
    #   -> sector="" subSector=""  benchmarks=0
    #
    # Two things share this allowance and both grew. The thinking budget is
    # drawn from it before a single value is written — 3,022 tokens in that
    # run — and the body itself must hold up to 61 values and 24 bands, each
    # with a unit, a tier, a confidence, a citation and a justification.
    # 8,192 does not hold both.
    #
    # Truncation here is not a partial answer, it is a silently WRONG one:
    # the prefix salvage returns valid JSON, so nothing downstream can tell a
    # complete extraction from one that stopped halfway. Output is the
    # expensive direction, but a cut-off call is paid for in full and yields
    # a scorecard that under-reports the company.
    #"assessment_inputs": 24576,
    "assessment_inputs": 32768,
    "assessment_extraction": 32768,
    # Judgement stage: thesis + conflict adjudication needs room, but its
    # INPUT is small (~8K), so a generous output cap is still cheap here.
    "company_profile_judgment": 4096,

    # --- Company Master Data Pipeline -----------------------------------
    # A research batch answers ten due-diligence questions in prose, with
    # citations, having read many search results. Its output is the evidence
    # the whole profile is built from, so truncating it silently deletes
    # findings that were already paid for.
    "profile_research_batch": 32768,
    # Synthesis emits all 17 sections in one object. This is the largest
    # legitimate output in the system: the competitors section alone can carry
    # eighteen fields across several companies. v29 §1.1/§1.2 document what a
    # too-small ceiling costs here — a truncated JSON body that is either lost
    # entirely or salvaged down to a fraction of its fields.
    #"profile_synthesis": 65536,
    "profile_synthesis": 98304,
}

# Per-role read-timeout FLOORS, in seconds — the same argument as the table
# above, applied to time instead of tokens.
#
# `LLMEndpoint.timeout_seconds` is one number for every call the endpoint
# serves, and it is set for the ordinary ones. A role designed to emit 65,536
# tokens over a 273,000-character dossier is not an ordinary call, and it does
# not get slower because anything is wrong: reading the dossier, thinking, and
# writing seventeen sections of JSON is simply minutes of work. Measured on
# the first complete live run — ten batches, 254 searches, a 273k dossier —
# synthesis was cut off by the endpoint's 120s timeout with the response still
# streaming, losing the entire run at the last of its eleven calls.
#
# A floor, not an override: an administrator raising the endpoint timeout
# raises every role including these. What they cannot do is set a value below
# what a role provably needs, because that is not a cost control, it is a
# guaranteed failure — the reasoning `ROLE_MAX_OUTPUT_TOKENS` already applies
# to a binding that under-states an output budget.
ROLE_TIMEOUT_SECONDS = {
    # Reading a whole PDF is slower than answering a question about one, and
    # the file is uploaded on every call.
    "document_read": 600,
    # Ten questions, each answered from live search results.
    "profile_research_batch": 300,
    # The largest single call in the system. Observed: 120s was not enough to
    # finish; the ceiling is set well clear rather than at the observed edge,
    # since the dossier grows with the number of active research batches.
    "profile_synthesis": 900,
    "company_profile_deep_extract": 180,
    "assessment_inputs": 180,
    "assessment_extraction": 180,
}


def llm_generate(role: str, system: str = None, prompt: str = None,
                 context: dict | None = None,
                 *, deal_id=None, user=None, calling_context: str = "",
                 model_override: str = None,
                 section_context: dict | None = None,
                 config_profile: str = None,
                 tier: str = None, tenant_id=None,
                 response_kind: str = "json",
                 attachments: list | None = None) -> dict:
    """Generate through the per-role bound endpoint. Returns validated JSON.

    `system` and `prompt` are optional. When omitted they are resolved from
    the admin-editable prompt table (Django Admin → AI Prompts), falling
    back to the defaults shipped in fundos/llm/default_prompts.py. Passing
    them explicitly still works and takes precedence, so existing call
    sites are unaffected.

    `config_profile` (Phase 2): the CODE of an admin-managed LLMConfigProfile.
    When supplied and active, it drives the call's model + capabilities (web
    search / URL context / structured-output schema / thinking) via the
    provider adapter. Connection + fallback + logging still come from the
    role's bound endpoint(s), so the established path is intact. Unknown or
    inactive codes are ignored (call proceeds without capabilities).

    `section_context` supplies values for {placeholder} substitution in the
    resolved prompt — used by per-section roles.

    `response_kind` selects what the model is expected to return:

      * ``"json"`` (default) — the established path. The response is parsed,
        repaired if malformed, normalised and validated against the role's
        schema before it is returned.
      * ``"text"`` — the response is prose and is returned as
        ``{"text", "grounding", "searches"}`` with no parsing at all. For a
        role whose output IS prose (a research answer with inline citations),
        the JSON path is not merely unnecessary, it is destructive: repair
        would rewrite the answer and `_extract_json` would discard everything
        outside the first brace it found. Every other layer — endpoint
        resolution, capabilities, budget enforcement, the breaker, search
        accounting and the ledger row — is identical, which is what makes
        routing a prose role through this function worthwhile.

    `attachments` sends FILES alongside the prompt — a list of
    ``{"mime_type", "data"}`` where `data` is raw bytes. Only a provider that
    accepts documents natively can take them (Gemini today); anything else
    raises rather than silently dropping the file and answering from the
    prompt alone, which would look like a working call that read nothing.

    Every other layer is unchanged: endpoint resolution, key rotation and its
    429 handling, the breaker, budgets, the ledger row. A document read is an
    ordinary call that happens to carry bytes.
    """
    if response_kind not in ("json", "text"):
        raise ValueError(
            f"response_kind must be 'json' or 'text', got {response_kind!r}")
    from fundos.llm.models import LLMRoleBinding
    from fundos.llm import schemas
    from fundos.llm.mocks import get_mock_response
    from fundos.profile.trace import (trace_event as _trace,
                                      count_populated as _count_populated,
                                      OK as _OK, FAIL as _FAIL, WARN as _WARN)

    # Tenant tiering (Simple / Advanced): when the caller has not pinned a
    # specific config_profile, resolve the tenant's tier profile for this role.
    # The tier itself comes from the prompt (admin PromptTemplate.tier, else the
    # code default). An explicit config_profile still wins, so existing
    # per-section overrides are unaffected.
    requested_profile = config_profile
    if config_profile is None:
        try:
            from fundos.llm.tiers import resolve_tier_profile_code
            config_profile = resolve_tier_profile_code(
                role=role, tier=tier, tenant_id=tenant_id)
        except TierContractViolation:
            # v27.4 — MUST NOT be swallowed.
            #
            # v27.3 added `assert_role_tier_coherent` so that a search-required
            # role landing on a non-searching tier would fail loudly. It then
            # called it from inside this `except Exception: config_profile =
            # None`, which caught the violation, discarded it, and let the call
            # proceed with capabilities={} -- no search, no thinking, no error.
            # The new guardrail never reached a caller: the exact silent
            # degradation the tier contract was written to end, reintroduced by
            # the handler wrapping it.
            #
            # A misconfigured tier is an ADMIN fault with a fixable cause and a
            # legible message. Failing the run is the correct outcome; the
            # alternative is publishing recollection as research.
            raise
        except Exception:
            config_profile = None

    # Resolve the optional capability profile (Phase 2).
    profile = _resolve_config_profile(config_profile)
    if profile is None and requested_profile:
        # A caller-supplied code that does not resolve used to disable BOTH
        # capabilities and tier resolution: the call fell through to the
        # endpoint's default model with no search, no schema and no thinking
        # control, silently. On a live install the per-section codes
        # ("cp_extract.<section>") had never been seeded, so most calls in
        # every run bypassed the tier configuration entirely and no tier
        # setting an administrator made had any effect on them.
        #
        # An unresolvable code now falls back to the tier and says so.
        try:
            from fundos.llm.tiers import resolve_tier_profile_code
            config_profile = resolve_tier_profile_code(
                role=role, tier=tier, tenant_id=tenant_id)
            profile = _resolve_config_profile(config_profile)
        except TierContractViolation:
            raise                       # v27.4 — see the first call site.
        except Exception:
            profile = None
        # v27.3 — severity corrected to INFO.
        #
        # The `cp_extract.<section>` profiles are OPT-IN by design: when none
        # exists the tier drives the call, which is the intended behaviour,
        # not a fault. Logging it at WARNING produced 18 warning lines per
        # run — about 40% of all warning volume — for a system working
        # exactly as designed. That is how the genuinely critical warnings in
        # the same log (no web search performed, transaction aborted) came to
        # be skimmed past for six releases. Noise at warning level is not a
        # harmless excess; it is a tax on every real warning underneath it.
        logger.info(
            "LLM: no config profile %r — using the %s tier (%s) for role %s. "
            "This is the designed fallback; seed the profile only if this "
            "section needs its own model or capabilities.",
            requested_profile, tier or "resolved",
            config_profile or "none", role)
        _trace("llm.config_profile_missing", _OK, role=role,
               requested=str(requested_profile),
               fell_back_to=str(config_profile or "endpoint default"),
               note="opt-in profile absent — tier drives the call, as designed")
    capabilities = profile.capabilities_payload() if profile else {}

    # v29 — MAKE `structured_output` MEAN SOMETHING, OR SAY THAT IT DOES NOT.
    #
    # `LLMConfigProfile.output_schema` defaults to `{}` and nothing has ever
    # written to it, so `capabilities_payload()` emitted
    # `{"structured_output": {"schema": {}}}` and every provider path skipped
    # it (`if so and so.get("schema")` — `{}` is falsy). No responseSchema has
    # ever reached Gemini, on any tier, for any role. The tick was inert for
    # six releases while two releases of reasoning were built on top of it.
    #
    # A schema is a property of the ROLE, but `output_schema` sits on the
    # PROFILE — one tier row serves eleven roles, so there is no single value
    # that could correctly live there. That is very likely why it was never
    # populated. Derive it from the role instead, and only for roles whose
    # declared contract is known to be their complete output shape.
    _so = capabilities.get("structured_output")
    if isinstance(_so, dict) and not _so.get("schema"):
        _derived = schemas.json_schema_for(role)
        if _derived:
            capabilities = dict(capabilities)
            capabilities["structured_output"] = {"schema": _derived,
                                                 "derived_from_role": True}
        else:
            # Not a defect — most roles are deliberately outside the allowlist
            # (see schemas.SCHEMA_IS_COMPLETE_OUTPUT). But an administrator
            # looking at a ticked box deserves to know the runtime will not
            # act on it, because that gap is exactly what went unnoticed.
            logger.info(
                "LLM: structured output is ON for role %s but no schema will "
                "be sent — the profile states none and %s is not in "
                "schemas.SCHEMA_IS_COMPLETE_OUTPUT. The response is validated "
                "after the fact and repaired if needed, which is the designed "
                "path; the admin tick simply has no wire effect here.",
                role, role)
            capabilities = {k: v for k, v in capabilities.items()
                            if k != "structured_output"}

    # Cost control 3: thinking tokens bill at the OUTPUT rate. On pure
    # extraction roles they buy nothing — the task is lifting values out of
    # supplied text, not reasoning about them.
    #
    # v27.4 widened the exemption to every search-driving role, on the
    # reasoning that google_search is model-decided and a model with no
    # deliberation budget has no step in which to choose to call it.
    #
    # v27.5 — THAT REASONING WAS WRONG, AND THE MEASUREMENT SAYS SO.
    #
    # The 19 Aug diagnostic varied thinking against search directly:
    # H2 has NO EFFECT. Without a schema the model searched at thinking=0
    # (4 searches) as readily as at 2048 (3 and 6). The budget bought
    # retrieval nothing. What it did buy, in production, was:
    #
    #   * +5,243 to +8,809 thinking tokens per run (₹3.6, billed as output)
    #   * +56s latency per run (92s → ~148s)
    #   * assessment_inputs FAILING on both runs — thinking consumed the
    #     output allowance and the JSON came back truncated, once repaired
    #     (7 of 30 fields written) and once not (JSONDecodeError, whole
    #     assessment lost)
    #
    # So the exemption is narrowed to what it should always have been: keep
    # thinking where the model must genuinely REASON, and nowhere else.
    # `web_search` being attached is no longer a reason on its own, because
    # it was never a real one.
    _needs_deliberation = role in THINKING_JUSTIFIED_ROLES
    if role in NO_THINKING_ROLES and _needs_deliberation:
        logger.info(
            "LLM: role %s is in NO_THINKING_ROLES but also in "
            "THINKING_JUSTIFIED_ROLES, so its budget is being left in place. "
            "One of the two memberships is wrong; the budget is kept because "
            "over-thinking costs tokens while under-thinking costs accuracy.",
            role)
    elif role in NO_THINKING_ROLES and (
            "thinking" in capabilities or "reasoning_effort" in capabilities):
        capabilities = {k: v for k, v in capabilities.items()
                        if k not in ("thinking", "reasoning_effort")}
        # On Anthropic and OpenAI, dropping the key IS "off". On Gemini it is
        # "provider default", and the default is ON — so dropping it here had
        # the opposite of the intended effect and let thoughts eat the output
        # budget on precisely the roles that gain nothing from them.
        if profile is not None and profile.provider == "gemini":
            capabilities["thinking"] = {"budget_tokens": 0}
    if profile:
        # A profile's system instructions and model take precedence when set.
        if system is None and profile.system_instructions:
            system = profile.system_instructions
        if model_override is None and profile.model_string:
            model_override = profile.model_string

    # Admin-configured prompts win when the caller does not supply one.
    if system is None or prompt is None:
        from fundos.llm.default_prompts import resolve_prompt
        resolved_system, resolved_user = resolve_prompt(role, section_context)
        system = system if system is not None else resolved_system
        prompt = prompt if prompt is not None else resolved_user

    # Cost control 1: honour the role's declared context_keys. Sending a
    # founder-bio blob to the competitors role (and vice versa) was pure
    # billed waste — each section call carried the whole payload bundle.
    context = _filter_context(role, context or {})

    binding = LLMRoleBinding.objects.filter(role=role).select_related(
        "primary_endpoint", "fallback_endpoint").first()

    # Mocked path — ONLY when explicitly requested (global flag or per-role
    # flag). Tester Issue 20: previously a missing binding also silently fell
    # back to mock content even with live AI switched on, which is how
    # mocked/static output leaked into tested environments. A missing
    # binding with live AI on now fails loudly (E-LLM-502) so the job error
    # surfaces instead of fabricated content.
    from fundos.config.feature_flags import ai_mocked as _ai_mocked
    if _ai_mocked() or (binding and binding.is_mocked):
        # Mocked output is indistinguishable from real output downstream, so
        # it MUST be recorded — a profile full of stub content otherwise looks
        # like a working pipeline that simply found nothing.
        _trace(f"llm.call.{role}", _WARN, mode="MOCKED",
               reason=("global ai_mocked flag" if _ai_mocked()
                       else "this role is flagged mocked"),
               note="no real model was called; content is canned sample data")
        data = get_mock_response(role, context)
        # Counted too. A mocked run is how the pipeline is exercised in
        # development, and "how many calls would this have made" is exactly
        # the question llmCalls exists to answer — reporting 0 there would
        # make the figure useless in the only environment that can check it.
        # v27.4: the ledger entry is written by _log_call itself.
        _log_call(role=role, endpoint_code="MOCK", model="mock", deal_id=deal_id,
                  user=user, calling_context=calling_context, latency_ms=1,
                  status="mocked", prompt_tokens=0, completion_tokens=0)
        if response_kind == "text":
            # A text role's mock must be shaped like a text response, or the
            # mocked path diverges from the live one at the caller — which
            # would make development exercise a code path production never
            # takes. `searches=0` is honest: a mock searched nothing.
            body = data if isinstance(data, str) else (
                (data or {}).get("text") or "")
            return {"text": body, "grounding": [], "searches": 0}
        return schemas.validate(role, data)
    if binding is None:
        _trace(f"llm.call.{role}", _FAIL, mode="NO_BINDING",
               error="no LLM endpoint is bound to this role",
               remedy="Admin -> LLM Integration: bind an endpoint to this role")
        _log_call(role=role, endpoint_code="NONE", model="", deal_id=deal_id,
                  user=user, calling_context=calling_context, latency_ms=0,
                  status="error", error="no LLM binding configured",
                  prompt_tokens=0, completion_tokens=0)
        raise LLMUnavailable(
            f"No live LLM endpoint is configured for role '{role}'. "
            "Bind an endpoint in the admin console, or enable mocked AI "
            "explicitly for development.")

    system = _with_admin_guidance(system)

    # --- v27.4: the contract must survive to the WIRE, not just to the row ---
    #
    # The tier contract is applied inside `capabilities_payload()`, which only
    # runs when a config profile ROW resolved. When none did — an unseeded
    # `tier.advanced.gemini`, an inactive row, a tenant override pointing at a
    # deleted code — the adapter fell through to `capabilities = {}` and the
    # contract never executed at all. A search-required role then ran with no
    # tool, no thinking and no error, and the only signal was a warning emitted
    # AFTER the tokens were spent. That is the same silent degradation the
    # contract exists to end, reached by a different door.
    #
    # This is the last checkpoint before dispatch, so it is the only place that
    # can assert on what will actually be sent. Deliberately AFTER the mocked
    # and no-binding returns: mocked runs have no grounding to protect, and a
    # missing binding already fails with its own clearer message.
    if role in SEARCH_REQUIRED_ROLES and "web_search" not in capabilities:
        _trace(f"llm.call.{role}", _FAIL, mode="CONTRACT_VIOLATION",
               resolved_profile=str(config_profile or "none"),
               profile_row_found=bool(profile),
               caps_sent=(",".join(sorted(capabilities)) if capabilities
                          else "none"),
               error="search-required role reached dispatch without web_search",
               remedy="seed the advanced tier profile (manage.py "
                      "seed_platform_config) then run manage.py diagnose_config")
        raise UngroundableCall(
            f"Role '{role}' requires retrieval, but the call assembled with no "
            f"web_search capability"
            + (f" (profile '{config_profile}' did not resolve to an active "
               f"row)" if profile is None else
               f" (profile '{config_profile}' resolved but granted none)")
            + ". Refusing to dispatch: this role cannot answer from the "
              "supplied context, so whatever it returned would be the model's "
              "recollection presented as research. Seed or activate the "
              "advanced tier profile, then run `manage.py diagnose_config`.")

    # --- Web-search containment (layers 1, 2 and 4) --------------------
    # Which provider will actually serve this call determines whether
    # max_uses is enforced by the provider or merely requested.
    _probe_endpoint = None
    if profile is not None and getattr(profile, "endpoint", None) is not None:
        _probe_endpoint = profile.endpoint
    elif binding.primary_endpoint is not None:
        _probe_endpoint = binding.primary_endpoint
    enforcement = (search_enforcement_for(_probe_endpoint.provider_kind)
                   if _probe_endpoint is not None else None)

    # Layer 4a: the breaker, for a role that CANNOT answer without retrieval.
    #
    # This check lived inside `if "web_search" in capabilities` below, which
    # is the one place it cannot do its job: a search-required role whose
    # profile has web_search switched OFF never reached it, so the breaker was
    # silently inapplicable to exactly the roles it exists to protect. The
    # live config shows the shape of it — `tier.simple.gemini` carries
    # `web_search: False` — and the run log carries the consequence:
    #
    #     role assessment_inputs performed NO web search (0 searches). Its
    #     answers came from the supplied context and the model's own
    #     recollection, and cannot be cited.
    #
    # Whether search is CONFIGURED is a different question from whether this
    # role may run without it. Conflating them let an ungroundable call
    # through under both answers.
    if role in SEARCH_REQUIRED_ROLES and search_breaker_tripped(config_profile):
        _trace(f"llm.call.{role}", _FAIL, mode="BREAKER_OPEN",
               config_profile=str(config_profile or "none"),
               error="search breaker is open on a search-required role",
               remedy="the model overran its search cap repeatedly — raise "
                      "max_uses or fix the prompt, then wait for the cooldown")
        raise UngroundableCall(
            f"The web-search breaker is open for profile "
            f"'{config_profile}', and role '{role}' cannot produce a "
            f"publishable answer without retrieval. The breaker opens after "
            f"repeated search-cap overruns: raise max_uses or correct the "
            f"prompt that is driving them. Running this role without search "
            f"would publish recollection as research, which is what the "
            f"breaker is meant to prevent paying for.")

    if "web_search" in capabilities:
        # PRESENCE, not truthiness. `web_search: {}` is a legal, falsy payload
        # and it used to skip this whole block — while the tool-assembly code
        # below tests presence and attached the tool anyway. The two tests
        # disagreeing is what sent Gemini a search tool with no instruction to
        # use it. They must ask the same question.
        cap = ((capabilities.get("web_search") or {}).get("max_uses")
               or _default_search_max_uses())
        # Layer 4: breaker. A profile that has repeatedly overrun loses
        # search entirely until the cooldown expires — better a thinner
        # answer than an unbounded bill.
        if search_breaker_tripped(config_profile):
            # v27.4 — the breaker may thin an answer; it may not falsify one.
            #
            # Stripping search and continuing is the right trade for a role
            # that searches to IMPROVE its answer. For a role that searches
            # because it cannot otherwise answer, "better a thinner answer
            # than an unbounded bill" is a false choice: what it actually
            # produces is a confident dossier built from recollection, at a
            # cost the breaker was opened to avoid paying for real evidence.
            # A search-required role has already been refused above; what
            # reaches here searches to IMPROVE its answer, so thinning it is
            # the right trade rather than a false choice.
            logger.error(
                "LLM: search breaker is OPEN for profile %s — running %s "
                "WITHOUT web search.", config_profile, role)
            capabilities = {k: v for k, v in capabilities.items()
                            if k != "web_search"}
        elif enforcement == "advisory" and cap:
            # Layer 1: the provider has no max_uses parameter, so state the
            # cap in the prompt. The admin sets one number; how it is
            # enforced differs per provider and is not their problem.
            #
            # For roles that MUST search, the directive leads with the
            # instruction and states the cap as a bound on it. Sending only a
            # ceiling to a model already holding a large context is an
            # invitation to skip searching entirely, which is what happened.
            # v27.5 — keyed off the TOOL, not the role list.
            #
            # This block only runs when `web_search` is in capabilities, so
            # the tool is on the wire regardless. Gating the *instruction* on
            # a separate hand-maintained role list meant a role could be
            # handed a search tool and told nothing about it — which is
            # exactly what an admin tier override did to
            # `company_profile_section` on 19 Aug. It searched anyway, twice
            # in one run and six times in the next, with no directive at all.
            # Paying for a tool and withholding the instruction is the worst
            # of both.
            system = (system or "") + _search_directive(
                cap, expect_search=True)

    # Cost control 4: refuse to spend past the tenant's monthly ceiling.
    # Layer 2: reserve the WORST case for a search call, not the expected
    # case, so an uncapped provider cannot overshoot a nearly-spent budget.
    _enforce_budget(
        role=role, calling_context=calling_context,
        reserve_tokens=_worst_case_search_tokens(capabilities, enforcement))

    # Cost control 2: keep the context OUT of the user turn so it can be sent
    # as its own cacheable block. Providers without cache support get the old
    # inline composition, so behaviour is unchanged for them.
    cache_enabled = _cache_enabled(profile)
    context_block = _render_context(context)
    if cache_enabled:
        full_prompt = prompt
    else:
        full_prompt = _compose_prompt(prompt, context)
        context_block = ""

    # --- Endpoint selection -------------------------------------------
    # The endpoint has historically come from the role BINDING while the
    # model comes from the config PROFILE. That is safe only while every
    # profile happens to sit on the same endpoint as the binding.
    #
    # It breaks the moment a tenant switches a tier to another vendor: the
    # profile says "gemini-3.1-pro", the binding still says ANTHROPIC, and
    # the adapter sends a Gemini model string to the Anthropic API — a 404
    # at call time with a confusing message.
    #
    # A profile that names a model has, implicitly, named the provider that
    # serves it. So when the resolved profile carries an active endpoint, it
    # wins. The binding's fallback is kept only when it speaks the same
    # provider dialect, because falling back across vendors reintroduces the
    # exact mismatch.
    primary = binding.primary_endpoint
    fallback = binding.fallback_endpoint
    if profile is not None and getattr(profile, "endpoint", None) is not None:
        if profile.endpoint.is_active:
            if primary is None or profile.endpoint.code != primary.code:
                logger.debug(
                    "LLM: %s using endpoint %s from config profile %s "
                    "(binding said %s).", role, profile.endpoint.code,
                    profile.code, getattr(primary, "code", "none"))
            primary = profile.endpoint
            if fallback is not None and \
                    fallback.provider_kind != primary.provider_kind:
                fallback = None

    attempts = [("primary", primary)]
    if fallback is not None:
        attempts.append(("fallback", fallback))

    # Capture the fully-assembled outgoing prompt ONCE per call (not per
    # retry — the prompt is identical across attempts). Gated.
    _log_llm_body("request", role, system, context_block, full_prompt)

    last_error = None
    attempts_used = 0
    for label, endpoint in attempts:
        if endpoint is None or not endpoint.is_active:
            continue
        while attempts_used < MAX_TOTAL_ATTEMPTS:
            attempts_used += 1
            started = time.monotonic()
            # v27.5 — visible to the failure handler below.
            #
            # A call that failed while PARSING has already been billed in
            # full: the provider generated every token, we simply could not
            # read the result. On 19 Aug `assessment_inputs` failed with a
            # JSONDecodeError after 43.2 seconds and the run reported
            # `prompt_tokens=0 cost_inr=0.0` for it — the most expensive
            # failure mode in the system, recorded as free.
            #
            # Seeded empty so a failure BEFORE dispatch (DNS, timeout,
            # 4xx) still reports honest zeros: nothing was generated, so
            # nothing was billed.
            usage = {}
            text = ""
            try:
                text, usage = _dispatch(endpoint, system, full_prompt,
                                        binding, model_override,
                                        capabilities=capabilities,
                                        context_block=context_block,
                                        role=role,
                                        attachments=attachments)
                latency = int((time.monotonic() - started) * 1000)
                # Capture the raw provider response on the winning attempt
                # (after a fallback, this is the fallback's output). Gated.
                _log_llm_body("response", role, text)
                if response_kind == "text":
                    # A prose role. Parsing, repairing and schema-validating
                    # markdown would at best be a no-op and at worst destroy
                    # it — `_extract_json` on a research answer containing a
                    # JSON snippet in a code fence would return the snippet
                    # and throw away the answer. The text is the deliverable.
                    #
                    # Everything AROUND the parse still applies: this branch
                    # rejoins the shared path immediately below for search
                    # accounting, the ledger row, the breaker and the trace,
                    # which is the whole reason a prose role goes through this
                    # function rather than calling a provider directly.
                    data = {"text": text or "", "grounding": [], "searches": 0}
                    repaired = False
                    _normalized = False
                else:
                    data, repaired = _parse_json_with_repair(
                        endpoint, system, text, binding, model_override,
                        capabilities=capabilities, context_block=context_block,
                        role=role)
                    # Coerce provider/prompt shape drift into the canonical keys
                    # the callers read, so a valid-but-differently-shaped
                    # response no longer logs success while producing empty
                    # content (LLM_ISSUE2). Section-aware for the profile roles.
                    #
                    # Whether this actually CHANGED anything is a diagnostic in
                    # its own right: a rising normalisation rate means the model
                    # or prompt is drifting off-contract, and it is invisible in
                    # cost and latency.
                    from fundos.llm.response_normalizer import normalize
                    _pre_norm = dict(data) if isinstance(data, dict) else data
                    data = normalize(role, data,
                                     section_key=(section_context or {}).get(
                                         "section_key", ""))
                    _normalized = (data != _pre_norm)
                    data = schemas.validate(role, data)
                # Layers 3 and 4: record what actually ran and feed the
                # breaker. On a native-enforcement provider this is a
                # no-op assertion; on an advisory one it is the only
                # feedback loop there is.
                _searches = usage.get("search_count")
                # A role that cannot be answered without searching, which
                # did not search, has answered from memory. Previously this
                # produced `searches=""` in a line marked OK — indisputably
                # the most expensive silent failure in the pipeline.
                if role in SEARCH_EXPECTED_ROLES and not _searches:
                    logger.warning(
                        "LLM: role %s performed NO web search (%s). Its "
                        "answers came from the supplied context and the "
                        "model's own recollection, and cannot be cited. "
                        "Read `caps_sent` and `thinking_budget` on the "
                        "llm.call trace line for this role FIRST — they "
                        "separate 'the tool was never attached' from 'the "
                        "tool was attached and the model declined', which "
                        "need opposite fixes. If web_search is absent from "
                        "caps_sent the fault is configuration: check the "
                        "resolved profile %s is bound to the advanced tier "
                        "and that the search breaker is not open. If it is "
                        "present, the model declined: check thinking_budget "
                        "is non-zero (google_search is model-decided and a "
                        "model with no deliberation budget cannot choose to "
                        "call it), then run tools/diagnose_gemini_search.py "
                        "to test whether responseSchema is suppressing the "
                        "tool on this model.",
                        role, "count unavailable" if _searches is None
                        else "0 searches", config_profile)
                if response_kind == "text":
                    # Attach what the caller needs to judge the answer: how
                    # many searches actually ran, and what they grounded on.
                    # A prose answer with searches=0 is a recollection, and
                    # only the caller can decide whether that is acceptable
                    # for its purpose — so it is reported, not hidden.
                    data["searches"] = _searches or 0
                    data["grounding"] = [
                        f"[{s.get('title') or s.get('uri')}]({s.get('uri')})"
                        for s in (usage.get("grounding_sources") or [])
                        if s.get("uri")
                    ]
                record_search_outcome(
                    config_profile,
                    (capabilities.get("web_search") or {}).get("max_uses"),
                    _searches)
                _log_call(role=role, endpoint_code=endpoint.code,
                          model=model_override or endpoint.default_model,
                          deal_id=deal_id, user=user,
                          calling_context=calling_context, latency_ms=latency,
                          status="success" if label == "primary" else "fallback",
                          prompt_tokens=usage.get("prompt_tokens", 0),
                          # v27.5 — a call truncated at the ceiling reports
                          # completion_tokens=0 on Gemini while still
                          # returning text, and is billed for it.
                          completion_tokens=_completion_from(usage, text),
                          thinking_tokens=usage.get("thinking_tokens", 0),
                          cache_read_tokens=usage.get("cache_read_tokens", 0),
                          cache_write_tokens=usage.get("cache_write_tokens", 0),
                          search_count=_searches,
                          tier=tier or "", config_profile=config_profile or "",
                          prompt_fingerprint=prompt_fingerprint(system),
                          context_chars=len(context_block or ""),
                          was_repaired=repaired, was_normalized=_normalized,
                          # v27.4 — carried so `run.economics` can report how
                          # many calls hit the output ceiling. A truncated
                          # response logs as a success and costs full price.
                          finish_reason=usage.get("finish_reason") or "",
                          output_signals=_output_signals(data))
                # A schema-valid response with every field blank logs as a
                # success and costs full price. Counting populated leaves is
                # the only way that failure mode is visible.
                _filled, _total = _count_populated(data)
                # A response cut off at the output ceiling is the single most
                # misread outcome here: it logs as a successful call with a
                # thin result. Say so explicitly, and say what to change.
                _finish = usage.get("finish_reason") or ""
                _thoughts = usage.get("thinking_tokens") or 0
                _truncated = str(_finish).upper() in (
                    "MAX_TOKENS", "LENGTH", "MAX_OUTPUT_TOKENS")
                _note = ""
                if _truncated:
                    # The remedy used to lead with "raise max output tokens
                    # on the role binding". A binding value can only LOWER
                    # the role ceiling -- `min(binding, role_cap)` -- so that
                    # was advice an administrator could follow and see no
                    # effect. And the ceiling is usually the model's own: a
                    # profile_synthesis run asked Gemini for 102,400 tokens
                    # against a hard 65,536 and stopped four short of it.
                    _note = ("response was CUT OFF at the output ceiling"
                             + (f" after spending {_thoughts} tokens on "
                                "thinking" if _thoughts else "")
                             + " — this is usually the MODEL's own output "
                               "limit rather than a configured one, and a "
                               "binding can only lower the role ceiling, "
                               "never raise it. Lower the thinking budget on "
                               "the config profile to leave more room for the "
                               "answer, or ask for less in one call. The "
                               "prefix is salvaged and the missing sections "
                               "are re-asked for")
                elif not _filled:
                    _note = ("model returned a valid but entirely EMPTY "
                             "response")
                if _truncated:
                    logger.error(
                        "LLM: %s was truncated at the output ceiling "
                        "(finish_reason=%s, thinking_tokens=%s). The parsed "
                        "result is incomplete.", role, _finish, _thoughts)
                _trace(f"llm.call.{role}",
                       _WARN if (_truncated or not _filled) else _OK,
                       endpoint=endpoint.code,
                       model=model_override or endpoint.default_model,
                       attempt=label, ms=latency,
                       prompt_chars=len(full_prompt or ""),
                       context_chars=len(context_block or ""),
                       prompt_tokens=usage.get("prompt_tokens", 0),
                       completion_tokens=usage.get("completion_tokens", 0),
                       thinking_tokens=_thoughts,
                       finish_reason=_finish,
                       response_chars=len(text or ""),
                       # "unknown" and 0 are different facts and must not
                       # render identically. `searches=""` for both is why a
                       # whole day's log could not answer "did the search fix
                       # work?". None means the provider returned no count;
                       # 0 means it counted none.
                       searches=("unknown" if _searches is None
                                 else _searches),
                       # v27.3 — `searches="unknown"` is compatible with two
                       # completely different faults: the tool was never
                       # attached, or it was attached and the model declined.
                       # They need opposite fixes, and a whole day's log
                       # could not tell them apart. Record what was actually
                       # SENT alongside what came back.
                       caps_sent=(",".join(sorted(capabilities))
                                  if capabilities else "none"),
                       # v27.5 — always a bare integer, or the literal
                       # `unset`. It rendered as 2048 on one line and "4000"
                       # on the next in the 19 Aug log, because the trace
                       # formatter quotes strings and not ints, and the value
                       # arrived as whichever type the admin had typed. A
                       # field that changes shape between lines cannot be
                       # parsed by the tooling that reads these logs.
                       thinking_budget=_budget_str(capabilities),
                       # v27.5 — reports what was actually SENT, not role
                       # membership. On 19 Aug this read `search_expected=false`
                       # on `company_profile_section` — the only role in the
                       # entire system that searched (2 then 6 times) — and
                       # `true` on the two roles that never did. The flag was
                       # measurably backwards, because an admin tier override
                       # had put section on `advanced` and handed it the tool
                       # without adding it to any role list. `role_expects`
                       # keeps the declared intent visible alongside it, so a
                       # disagreement between the two is legible rather than
                       # hidden behind one boolean.
                       search_expected=("web_search" in capabilities),
                       role_expects_search=(role in SEARCH_EXPECTED_ROLES),
                       populated_fields=_filled, total_fields=_total,
                       was_repaired=repaired, was_normalized=_normalized,
                       note=_note)
                return data
            except _RetryableError as e:
                last_error = e
                _trace(f"llm.call.{role}", _WARN, endpoint=endpoint.code,
                       attempt=f"{label}#{attempts_used}", retryable=True,
                       error=f"{type(e).__name__}: {e}")
                logger.warning("LLM: %s attempt %d/%d on %s failed "
                               "(retryable): %s", role, attempts_used,
                               MAX_TOTAL_ATTEMPTS, endpoint.code, e)
                if attempts_used < MAX_TOTAL_ATTEMPTS:
                    # Immediate re-fire on a 5xx just burns another full-price
                    # call into the same overloaded upstream.
                    time.sleep(RETRY_BACKOFF_SECONDS * attempts_used)
                continue
            except Exception as e:
                last_error = e
                latency = int((time.monotonic() - started) * 1000)
                # v27.5 — bill what was actually spent.
                #
                # `usage` is populated if the failure happened AFTER dispatch
                # returned, which is the common case: the provider generated
                # every token and we could not parse the result. Passing zeros
                # there reported 43 seconds of billed inference as free and
                # made every budget figure optimistic in exactly the situation
                # where spend is running away.
                _billed = _completion_from(usage, text)
                _log_call(role=role, endpoint_code=endpoint.code,
                          model=model_override or endpoint.default_model,
                          deal_id=deal_id, user=user,
                          calling_context=calling_context, latency_ms=latency,
                          status="error", error=str(e)[:500],
                          prompt_tokens=usage.get("prompt_tokens", 0),
                          completion_tokens=_billed,
                          thinking_tokens=usage.get("thinking_tokens", 0),
                          cache_read_tokens=usage.get("cache_read_tokens", 0),
                          cache_write_tokens=usage.get("cache_write_tokens", 0),
                          tier=tier or "", config_profile=config_profile or "",
                          context_chars=len(context_block or ""),
                          finish_reason=usage.get("finish_reason") or "")
                _trace(f"llm.call.{role}", _FAIL, endpoint=endpoint.code,
                       model=model_override or endpoint.default_model,
                       attempt=label, ms=latency,
                       prompt_chars=len(full_prompt or ""),
                       # The FAIL line used to carry none of this, so the
                       # most expensive calls in the log were the least
                       # instrumented — and the reader could not tell a
                       # billed parse failure from a free connection refusal.
                       context_chars=len(context_block or ""),
                       prompt_tokens=usage.get("prompt_tokens", 0),
                       completion_tokens=_billed,
                       thinking_tokens=usage.get("thinking_tokens", 0),
                       finish_reason=usage.get("finish_reason") or "",
                       response_chars=len(text or ""),
                       billed=bool(usage),
                       caps_sent=(",".join(sorted(capabilities))
                                  if capabilities else "none"),
                       thinking_budget=str(
                           (capabilities.get("thinking") or {})
                           .get("budget_tokens", "unset")),
                       error=f"{type(e).__name__}: {e}")
                logger.error("LLM: %s failed on %s: %s", role, endpoint.code, e)
                break  # non-retryable → next endpoint
        if attempts_used >= MAX_TOTAL_ATTEMPTS:
            logger.error("LLM: %s exhausted the %d-attempt budget; not "
                         "trying further endpoints.", role, MAX_TOTAL_ATTEMPTS)
            break

    # Enhanced error reporting with categorization and remediation
    error_category = _categorize_error(last_error) if last_error else "OTHER"
    remediation = _remediation_for_error(error_category, role)
    
    error_msg = (
        f"LLM role '{role}' unavailable after {attempts_used} attempt(s). "
        f"Error category: {error_category}. "
        f"Last error: {str(last_error)[:200]}. "
        f"Remediation: {remediation}"
    )
    
    # Try to log with trace context if available
    try:
        from fundos.profile.trace import current_trace
        trace = current_trace()
        run_id = trace.run_id if trace else "?"
    except Exception:
        run_id = "?"
    
    logger.critical("GEN[%s] FATAL LLM: %s", run_id, error_msg)
    raise LLMUnavailable(error_msg)


def _categorize_error(error_msg):
    """
    Classify error into diagnostic category for admin remediation.
    
    Returns one of: AUTH, NETWORK, RATE_LIMIT, MODEL_ERROR, OTHER
    """
    msg = str(error_msg).lower()
    
    # Authentication failures
    if any(x in msg for x in ["api key", "unauthorized", "403", 
                               "invalid key", "invalid api key",
                               "authentication", "credentials"]):
        return "AUTH"
    
    # Network/connectivity issues
    elif any(x in msg for x in ["connection", "timeout", "refused", 
                                "unreachable", "dns", "network", 
                                "connection reset"]):
        return "NETWORK"
    
    # Rate limiting / quota issues
    elif any(x in msg for x in ["rate limit", "429", "quota", 
                                "too many requests", "throttle"]):
        return "RATE_LIMIT"
    
    # Model not found / availability issues
    elif any(x in msg for x in ["not found", "404", "model", 
                                "unavailable", "does not exist"]):
        return "MODEL_ERROR"
    
    return "OTHER"


def _remediation_for_error(error_category, role, endpoint_code=None):
    """
    Suggest actionable fix based on error category.
    
    Returns a remediation string the admin can immediately act on.
    """
    base_remediation = {
        "AUTH": (
            "Check GEMINI_API_KEYS or OPENAI_API_KEY in /etc/fundos/fundos.env. "
            "Verify the keys are valid and active with the provider."
        ),
        "NETWORK": (
            "Verify outbound connectivity to API provider. Check DNS resolution, "
            "firewall rules, and proxy settings if in use."
        ),
        "RATE_LIMIT": (
            f"API rate limit or quota exceeded for role '{role}'. "
            "Retry in 60 seconds or upgrade API quota with the provider."
        ),
        "MODEL_ERROR": (
            f"Model binding issue for role '{role}'. Verify the model name "
            "bound in Admin UI exists and is available with the provider."
        ),
        "OTHER": (
            f"Unclassified error for role '{role}'. Check admin logs for "
            "full error message and contact support if issue persists."
        ),
    }
    return base_remediation.get(error_category, base_remediation["OTHER"])


class _RetryableError(Exception):
    pass


def _redact(text):
    """Strip credentials from anything on its way to a log or a trace."""
    import re
    text = re.sub(r"([?&](?:key|api_key|access_token)=)[^&\s\"']+",
                  r"\1REDACTED", str(text))
    return re.sub(r"\b(AIza[0-9A-Za-z_\-]{10,}|sk-[0-9A-Za-z_\-]{10,})",
                  "REDACTED", text)


def _raise_for_status(resp, provider, model):
    """Fail with the provider's OWN explanation, not just the status line.

    `requests.raise_for_status()` produces "400 Client Error: Bad Request for
    url: …" and discards the response body — but for a 4xx the body is the
    only part that says WHY. A rejected schema, an unknown model, a bad
    capability combination and an exhausted quota all arrive as the same
    sentence, so a real defect reads as a billing problem and the operator is
    sent to the wrong place entirely.
    """
    if resp.status_code < 400:
        return
    detail = ""
    try:
        payload = resp.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("status") or "")
        if not detail:
            detail = str(payload)
    except Exception:
        detail = (resp.text or "")
    detail = _redact(detail).strip().replace("\n", " ")[:400]
    raise requests.HTTPError(
        f"{provider} {resp.status_code} for model {model}"
        + (f": {detail}" if detail else ""), response=resp)


_CONTEXT_HEADER = ("CONTEXT (authoritative facts — use these values verbatim; "
                   "never invent numbers):")

# Hard ceiling on the serialised context. Kept as a safety net only — the
# real reduction happens in _filter_context and in the source compression
# done upstream in profile.services.collect_sources.
# v27.3 — RAISED from 24,000 and no longer silent.
#
# On 17 Aug MediBuddy's sources.summary reported 31,001 characters of
# website and the deep extract received 24,081: 22% of the ONLY live source
# discarded with no log line, no trace event and no marker in the text. Its
# assessment coverage came out at 43.8% against ClearDekho's 75.0%, and
# ClearDekho happened to fit under the cap.
#
# 24k chars is roughly 6k tokens — a small fraction of what any current
# model accepts, and this constant predates them. The ceiling stays as a
# genuine safety net against a runaway payload, set where it can no longer
# amputate an ordinary company website.
MAX_CONTEXT_CHARS = 600000


def _filter_context(role, context):
    """Drop context keys the role did not declare it needs.

    Every entry in default_prompts declares `context_keys`. Before this the
    declaration was inert and each caller's full payload bundle (website
    extract + every document summary + market research + founder bios) went
    to every role, so a 15-section profile run paid for the same corpus 15
    times. Roles with no declaration are left untouched, so this can never
    starve a call site that was never audited.
    """
    if not context:
        return context
    try:
        from fundos.llm.default_prompts import get_default
        declared = (get_default(role) or {}).get("context_keys")
    except Exception:
        return context
    if not declared:
        return context
    allowed = set(declared)
    filtered = {k: v for k, v in context.items() if k in allowed}
    # Never hand back an empty context because of a stale declaration — that
    # would silently degrade output quality to save a rounding error.
    return _trim_bulk_sources(role, filtered or context)


# The raw source corpus, in the order it is worth keeping when trimming.
_BULK_KEYS = ("website_extract", "document_extracts", "research")

# Roles that read the corpus WHOLE. The deep extract is the call the corpus
# exists for; everything else runs after it and gets the same material a
# second time.
_FULL_CORPUS_ROLES = {"company_profile_deep_extract"}

# What a fan-out role may see of each bulk value. A live run sent 24,081
# characters of context to each of ten follow-up calls — about 12,000 prompt
# tokens apiece — re-reading a corpus the deep extract had already turned
# into structured fields. The budget keeps enough for a section to quote and
# check itself against the source without paying for the whole corpus each
# time.
_BULK_CHAR_BUDGET = 6000


def _trim_bulk_sources(role, context):
    """Cap the raw corpus for roles that run AFTER the deep extract.

    v27.4 — REPORTS what it cuts.

    The in-text marker told the MODEL the corpus was incomplete but told the
    operator nothing: no warning, no trace event, no numbers. Two of the three
    truncation points in this pipeline were invisible in a whole day of logs,
    which is why the 22% loss on MediBuddy had to be inferred by subtracting
    two unrelated trace lines. A cut that nothing records cannot be tuned, and
    cannot be ruled out as the cause of a coverage difference.

    Emitted once per call, and only when material was actually removed, so a
    healthy run adds no lines.
    """
    if not context or role in _FULL_CORPUS_ROLES:
        return context
    out = dict(context)
    cuts = []
    for key in _BULK_KEYS:
        value = out.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, default=str)
        if len(text) <= _BULK_CHAR_BUDGET:
            continue
        dropped = len(text) - _BULK_CHAR_BUDGET
        cuts.append((key, len(text), dropped))
        out[key] = (text[:_BULK_CHAR_BUDGET]
                    + f"\n\n[... {dropped} more "
                      f"characters of source omitted — the full corpus was "
                      f"read by the deep extract, whose output is in this "
                      f"context]")
    if cuts:
        from fundos.profile.trace import trace_event as _trace, OK as _OK
        total_in = sum(c[1] for c in cuts)
        total_dropped = sum(c[2] for c in cuts)
        _trace("llm.context.fanout_trim", _OK, role=role,
               keys=",".join(c[0] for c in cuts),
               chars_in=total_in,
               chars_sent=total_in - total_dropped,
               chars_dropped=total_dropped,
               pct_dropped=round(100.0 * total_dropped / total_in, 1),
               budget_per_key=_BULK_CHAR_BUDGET,
               note="intended: follow-up roles read the deep extract's OUTPUT, "
                    "not the corpus again. Raise _BULK_CHAR_BUDGET only if a "
                    "section demonstrably needs to quote the raw source.")
    return out


def _render_context(context):
    """Serialise context once, for use as a standalone (cacheable) block."""
    if not context:
        return ""
    body = json.dumps(context, default=str, indent=2, sort_keys=True)
    if len(body) > MAX_CONTEXT_CHARS:
        dropped = len(body) - MAX_CONTEXT_CHARS
        logger.warning(
            "LLM: context truncated — %d of %d characters were NOT sent "
            "(%.0f%% of the supplied material). A tail-slice removes whatever "
            "sits at the end of the corpus, which on a scraped site is "
            "typically the About / Team / Investors pages. Compress upstream "
            "in profile.services.collect_sources rather than relying on this "
            "ceiling.", dropped, len(body), 100.0 * dropped / len(body))
        body = body[:MAX_CONTEXT_CHARS] + (
            f"\n\n[... {dropped} characters of source were TRUNCATED and not "
            f"supplied. Do not treat the material above as complete.]")
    return f"{_CONTEXT_HEADER}\n{body}"


def _compose_prompt(prompt, context):
    """Legacy inline composition — used when caching is off or unsupported."""
    if not context:
        return prompt
    return f"{prompt}\n\n---\n{_render_context(context)}"


def _cache_enabled(profile):
    """Whether to split the context into its own cacheable block.

    Admin-controllable per config profile; defaults ON because the profile
    generation path re-sends an identical bundle across every section call.
    """
    if profile is not None:
        return bool(getattr(profile, "enable_prompt_cache", True))
    return True


def _cacheable(text):
    """A cache WRITE costs 1.25x input — only worth it above a size floor."""
    return bool(text) and len(text) >= MIN_CACHEABLE_CHARS


def prompt_fingerprint(system):
    """Short stable hash of the system prompt actually sent.

    The point is A/B-ability: when an admin edits a prompt, every call after
    the edit gets a different fingerprint, so "did that change help?" becomes
    a GROUP BY rather than a guess.
    """
    if not system:
        return ""
    return hashlib.sha256(system.encode("utf-8", "replace")).hexdigest()[:16]


def _output_signals(data):
    """Quality signals from a parsed response.

    Cost tells you what a call spent; these tell you what it was worth. A
    prompt change that raises `missing_count` is usually an IMPROVEMENT — the
    model admitting gaps instead of inventing — and no cost metric would ever
    show that.
    """
    if not isinstance(data, dict):
        return {}
    sig = {}
    if "needsInput" in data:
        sig["needs_input"] = bool(data.get("needsInput"))
    for key, name in (("missing", "missing_count"),
                      ("conflicts", "conflicts_count"),
                      ("records", "records_count"),
                      ("conflict_resolution", "resolutions_count"),
                      ("citations", "citations_count")):
        val = data.get(key)
        if isinstance(val, list):
            sig[name] = len(val)
    content = data.get("content")
    if isinstance(content, str):
        sig["content_chars"] = len(content)
    structured = data.get("structured")
    if isinstance(structured, dict) and isinstance(
            structured.get("items"), list):
        sig["items_count"] = len(structured["items"])
    # Top-level key set — the cheapest way to spot a model returning a
    # plausible-looking but wrongly-shaped object.
    sig["keys"] = sorted(k for k in data.keys())[:12]
    return sig


def context_fingerprint(context):
    """Stable hash of a context dict — used by callers for cache/skip logic."""
    try:
        blob = json.dumps(context, default=str, sort_keys=True)
    except Exception:
        blob = repr(context)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _usage(prompt_tokens=0, completion_tokens=0, cache_read=0, cache_write=0):
    """One usage shape for every provider.

    Each vendor reports cached tokens differently — Anthropic splits them into
    creation/read fields, OpenAI nests `cached_tokens` under
    prompt_tokens_details, Gemini reports cachedContentTokenCount. Normalising
    here means the call log, the price book and the admin dashboard show the
    SAME four numbers no matter which LLM a tenant is configured on. Without
    this, "how much did caching save us" would be an unanswerable question the
    moment a tenant switched from Claude to Gemini.
    """
    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "cache_read_tokens": int(cache_read or 0),
        "cache_write_tokens": int(cache_write or 0),
    }


def _openai_cached_tokens(usage):
    """OpenAI reports automatic-cache hits under prompt_tokens_details."""
    if usage is None:
        return 0
    details = getattr(usage, "prompt_tokens_details", None) or getattr(
        usage, "input_tokens_details", None)
    if details is None:
        return 0
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return int(cached or 0)


def cache_strategy_for(provider_kind):
    """'explicit' | 'implicit' | None for a provider kind."""
    from fundos.llm.models import CACHE_STRATEGY, ENDPOINT_KIND_TO_PROVIDER
    family = ENDPOINT_KIND_TO_PROVIDER.get(provider_kind)
    return CACHE_STRATEGY.get(family)


def _enforce_budget(*, role, calling_context="", reserve_tokens=0):
    """Stop before dispatch when the tenant is over its monthly ceiling.

    Runaway loops, not single expensive calls, are how these bills blow up.
    A missing/inactive budget row means "no cap", so this is opt-in per
    tenant and cannot break an existing deployment.

    `reserve_tokens` is the WORST-CASE additional input a web-search call
    could accumulate. On a provider that does not enforce max_uses this is
    the only hard protection available: a call whose worst case would breach
    the ceiling is refused before it starts, rather than discovered after.
    """
    try:
        from fundos.llm.models import TenantLLMBudget
        from fundos.core.scoping import get_current_tenant
        tenant = get_current_tenant()
        allowed, spent, cap = TenantLLMBudget.check_budget(tenant)
        if allowed and reserve_tokens and cap:
            from decimal import Decimal
            headroom = cap - spent
            # Deliberately crude and pessimistic — this is a guard rail, not
            # an invoice. Under-estimating here is what lets a budget blow.
            est = (Decimal(reserve_tokens) / 1000) * Decimal("0.50")
            if est > headroom:
                allowed = False
                logger.error(
                    "LLM: refusing %s — worst-case search cost (~Rs %.2f) "
                    "exceeds remaining budget (Rs %.2f). The provider for "
                    "this tier does not enforce max_uses, so the worst case "
                    "is what must be affordable.", role, est, headroom)
    except Exception as e:
        logger.debug("LLM: budget check skipped: %s", e)
        return
    if not allowed:
        raise LLMUnavailable(
            f"Monthly AI budget exhausted for this tenant "
            f"(spent ₹{spent:.2f} of ₹{cap:.2f}). Role '{role}' "
            f"({calling_context}) was not dispatched. Raise the cap in "
            f"admin → LLM budgets, or wait for the next billing month.")


def _with_admin_guidance(system):
    try:
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        extra = (getattr(cfg, "ai_guidance_data", "") or "").strip()
        if extra:
            return f"{system}\n\nADMIN GUIDANCE:\n{extra}"
    except Exception:
        pass
    return system


def _parse_json_with_repair(endpoint, system, text, binding, model_override,
                            *, capabilities=None, context_block="", role=None):
    """Parse JSON; salvage, then one repair attempt, before failing.

    Cost note: the repair is a SECOND full-price call that re-sends the whole
    bad response. When structured output was forced, the provider guarantees
    schema-shaped JSON, so a parse failure there is a real error rather than
    formatting drift and re-asking just doubles the bill for nothing.

    v29 — TWO DEFECTS FIXED HERE, BOTH VISIBLE IN THE 19 AUGUST RUNS.

    1. `role` was not passed to `_dispatch`, so the repair resolved
       `ROLE_MAX_OUTPUT_TOKENS.get("")` and fell to DEFAULT_MAX_OUTPUT_TOKENS
       (2048). The roles that actually need repairing are the SEARCH_REQUIRED
       ones -- they get no responseSchema, so their JSON is obtained by
       instruction -- and those are precisely the roles designed for 8192.
       A repair of a large truncated response was therefore itself truncated
       at a quarter of the budget, and failed for the same reason as the call
       it was repairing. That is run 1:

           llm.call.assessment_inputs  FAIL
           JSONDecodeError: Expecting ',' delimiter: line 64 col 6 (char 4492)

       4,492 characters is about 1,100 tokens of the ~2,048 a repair could
       emit once the prompt was counted. The repair never had room to finish.

    2. No salvage was attempted before paying for a repair. A response cut
       off at the ceiling is not malformed in an interesting way -- it is
       valid JSON with the tail missing -- and closing it costs nothing. We
       now try that first, and only dispatch a repair when salvage fails.
    """
    capabilities = capabilities or {}
    try:
        return _extract_json(text), False
    except Exception:
        pass

    # Salvage before spending money. A truncated-but-well-formed prefix is
    # the single most common shape here, and it needs no model to fix.
    salvaged = _salvage_truncated_json(text)
    if salvaged is not None:
        # Which shape it was decides the fix, so say it rather than guessing.
        # Cut off at the ceiling -> raise the ceiling. Broken in the middle of
        # a response that finished -> the model emitted a bad string (an
        # unescaped quote in a verbatim extract is the usual one) and a bigger
        # ceiling would change nothing.
        kept = len(json.dumps(salvaged)) if salvaged is not None else 0
        logger.warning(
            "LLM: %s returned JSON that would not parse; recovered the good "
            "prefix locally (%d of %d characters kept) without paying for a "
            "repair call. The result is INCOMPLETE by definition. If the "
            "response was CUT OFF, raise the role's output ceiling; if it "
            "finished (finish_reason=STOP) the fault is a malformed value "
            "mid-response and the ceiling is not the problem.",
            role or endpoint.code, kept, len(text or ""))
        return salvaged, True

    if (capabilities.get("structured_output") or {}).get("schema"):
        logger.error("LLM: structured output failed to parse on %s — not "
                     "paying for a repair call.", endpoint.code)
        raise ValueError(
            f"structured output failed to parse on {endpoint.code}")
    repair_prompt = (
        "The following response was not valid JSON. Return the SAME "
        "content as valid JSON only, with no preamble or markdown fences:\n\n"
        + text[:12000])
    # The repair re-states the payload; it does NOT need the context block
    # again (that is what made repairs so expensive). It DOES need the role,
    # or it inherits the 2048 default ceiling regardless of what the role
    # was designed for.
    repaired_text, _ = _dispatch(endpoint, system, repair_prompt,
                                 binding, model_override,
                                 context_block="", role=role)
    try:
        return _extract_json(repaired_text), True
    except Exception:
        salvaged = _salvage_truncated_json(repaired_text)
        if salvaged is not None:
            return salvaged, True
        raise


#: How many times salvage may cut back to the reported fault and retry. Each
#: pass discards everything from the fault onward, so a handful covers a
#: response with several bad strings and still terminates.
_SALVAGE_ATTEMPTS = 6


def _salvage_truncated_json(text):
    """Recover the good prefix of a bad JSON response. None if there is none.

    v29. Both 19 August assessment failures were truncation, not corruption:
    the model emitted correct JSON and stopped when the ceiling arrived. The
    prefix up to the last complete element is perfectly good data, and
    discarding it to raise JSONDecodeError threw away 30 extracted fields to
    punish a missing brace.

    v30 — TRUNCATION WAS NOT THE ONLY SHAPE. A response can also break in the
    MIDDLE and run on correctly for another 190,000 characters, which is what
    a 10 September Tyreplex synthesis did:

        response_chars=205388  finish_reason="STOP"
        JSONDecodeError: Expecting ',' delimiter: line 239 column 7 (char 16630)

    "Expecting ',' delimiter" is the signature of a string that ended early --
    an unescaped `"` inside a value, and a `quote` field carrying a verbatim
    extract is where that happens. `finish_reason=STOP` says the model
    finished; nothing was cut off. Closing the brackets at the end therefore
    changed nothing, because the fault was 188,000 characters earlier, and a
    run that had completed sixteen sections and cost 7m18s was thrown away
    whole.

    So salvage is now error-directed: when the closed text still will not
    parse, cut at the position the parser named and try again. Everything
    before the fault survives; everything after it is discarded, which is the
    honest trade -- the sections that did land are real, and synthesis already
    knows how to re-ask for the ones that did not (its continuation rounds
    exist for exactly this).

    Strategy per pass: walk the text tracking string state and bracket depth,
    remember the last position at which a value was complete at depth>0,
    truncate there, and close the open brackets. Deliberately conservative --
    it never invents a value, only ends the structure early -- so the worst
    case is fewer fields, never wrong ones.
    """
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    if start < 0:
        start = raw.find("[")
    if start < 0:
        return None
    raw = raw[start:]

    for _ in range(_SALVAGE_ATTEMPTS):
        parsed, fault = _close_at_last_safe(raw)
        if parsed is not None:
            return parsed
        if fault is None:
            return None
        # Cut back to the fault and retry on what came before it. The cut must
        # shorten the text or this loops on the same failure.
        cut = min(fault, len(raw) - 1)
        if cut <= 0:
            return None
        raw = raw[:cut]
    return None


def _close_at_last_safe(raw):
    """One salvage pass over `raw`.

    :returns: ``(parsed, fault)`` -- the parsed value and None on success, or
        None and the character offset the parser objected to (itself None when
        there is nothing to cut back to).
    """
    stack = []
    in_string = False
    escaped = False
    last_safe = None          # index AFTER a completed element, depth > 0
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if stack:
                last_safe = i + 1
        elif ch == "," and stack:
            last_safe = i          # cut BEFORE the comma
    if last_safe is None:
        return None, None

    # Recompute the bracket stack at the cut point: the depth there is what
    # has to be closed, not the depth at the end of the truncated text.
    head = raw[:last_safe]
    stack = []
    in_string = False
    escaped = False
    for ch in head:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_string or not stack:
        return None, None
    try:
        return json.loads(head + "".join(reversed(stack))), None
    except json.JSONDecodeError as exc:
        # The fault is somewhere in `head`; name it so the caller can cut back
        # to it. A position inside the closers we appended is not a real
        # offset in the text, so clamp it.
        return None, min(int(getattr(exc, "pos", 0) or 0), len(head))
    except Exception:
        return None, None


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Provider wire formats — one generic dispatcher on provider_kind
# (Doc 6 Appendix C). Temperature policy: deterministic paths pass 0;
# default callers omit the parameter to preserve provider defaults.
# ---------------------------------------------------------------------------

def _resolve_config_profile(code):
    """Load an active LLMConfigProfile by code, or None. Never raises."""
    if not code:
        return None
    try:
        from fundos.llm.models import LLMConfigProfile
        return LLMConfigProfile.objects.filter(
            code=code, is_active=True).select_related("endpoint").first()
    except Exception:
        return None


def resolve_timeout(endpoint_timeout, role):
    """The read timeout for one call: the endpoint's value, floored by the
    role's. See :data:`ROLE_TIMEOUT_SECONDS` for why the floor exists."""
    return max(endpoint_timeout or 60, ROLE_TIMEOUT_SECONDS.get(role or "", 0))


def _dispatch(endpoint, system, prompt, binding, model_override=None,
              *, capabilities=None, context_block="", role=None,
              attachments=None):
    kind = endpoint.provider_kind
    if attachments and kind != "gemini":
        # Refused BEFORE anything else, and before any provider branch can
        # return. A provider that cannot read the document would answer from
        # the prompt alone and hand back a confident summary of nothing,
        # which is indistinguishable from a working call — so this has to
        # fail loudly rather than quietly drop the file.
        raise RuntimeError(
            f"provider_kind {kind!r} cannot accept file attachments; bind "
            f"this role to a Gemini endpoint to read documents")
    model = model_override or endpoint.default_model
    timeout = resolve_timeout(endpoint.timeout_seconds, role)
    temperature = binding.temperature if binding else None
    max_tokens = binding.max_output_tokens if binding else None
    # The role's DESIGNED budget is the default. A binding value is an
    # administrator capping this role BELOW that, so it can only ever lower
    # the figure — which is why an unstated binding must not look like a
    # deliberate 2048: that silently quartered the dossier role's output.
    role_cap = ROLE_MAX_OUTPUT_TOKENS.get(role or "") or DEFAULT_MAX_OUTPUT_TOKENS
    max_tokens = min(max_tokens, role_cap) if max_tokens else role_cap
    capabilities = capabilities or {}

    from fundos.llm.models import KINDS_REQUIRING_KEY
    if kind in KINDS_REQUIRING_KEY and not endpoint.has_api_key_in_env():
        raise RuntimeError(
            f"endpoint {endpoint.code}: env var {endpoint.api_key_env_var} unset")

    if kind == "anthropic":
        # EXPLICIT cache strategy: the caller marks breakpoints.
        return _run_anthropic(endpoint, system, prompt, model, temperature,
                              max_tokens, timeout, capabilities=capabilities,
                              context_block=context_block)

    # IMPLICIT cache strategy (OpenAI, Gemini): the provider caches a repeated
    # prefix on its own. There is no wire flag to set — the caller's only job
    # is to keep that prefix byte-stable AND FIRST. So the context block goes
    # ahead of the volatile per-call instruction, not appended after it as
    # before. Appending it (the previous behaviour) put the varying text first
    # and made the repeated part unreachable to any prefix cache.
    if context_block:
        if cache_strategy_for(kind) == "implicit":
            prompt = f"{context_block}\n\n---\n{prompt}"
            # v27.3 — the search instruction lives in `system`, which on this
            # path is emitted BEFORE the context. On the 17 Aug run that put
            # it 24,081 characters ahead of the task: the model read "search
            # first", then a wall of material that looked entirely
            # sufficient, then a 611-character instruction. It answered from
            # context on every call, in every run.
            #
            # Restating it in the volatile TAIL keeps the cached prefix
            # byte-stable — the context block is unchanged and still first —
            # so this costs a few dozen tokens and no cache hits.
            # v27.5 — same correction as the directive above: if the tool
            # was attached, remind the model to use it. The role list is
            # about what we EXPECT; the capability is what we PAID FOR.
            if "web_search" in capabilities:
                prompt = f"{prompt}{_search_reminder()}"
        else:
            prompt = f"{prompt}\n\n---\n{context_block}"

    if kind == "openai_chat":
        return _run_openai_chat(endpoint, system, prompt, model, temperature,
                                max_tokens, timeout, capabilities=capabilities)
    if kind == "gemini":
        return _run_gemini(endpoint, system, prompt, model, temperature,
                           max_tokens, timeout, capabilities=capabilities,
                           attachments=attachments)
    if kind == "deepinfra":
        return _run_deepinfra(endpoint, system, prompt, model, temperature,
                              max_tokens, timeout)
    if kind == "custom_rest":
        return _run_custom_rest(endpoint, system, prompt, model, timeout)
    raise RuntimeError(f"unknown provider_kind {kind}")


def _run_openai_chat(endpoint, system, prompt, model, temperature, max_tokens,
                     timeout, *, capabilities=None):
    """OpenAI call with Phase-4 capability translation.

    Capability mapping:
      * structured_output → response_format json_schema (Chat Completions)
      * reasoning_effort  → reasoning_effort param (reasoning models)
      * web_search        → hosted web_search tool via the RESPONSES API
                            (that's where the tool lives); this routes to a
                            separate path only when search is requested.
      * function_calling / code_execution → left to future need; not sent here.
    A call with no capabilities is exactly the prior Chat Completions request.
    """
    from openai import OpenAI
    capabilities = capabilities or {}
    client = OpenAI(api_key=endpoint.api_key,
                    base_url=endpoint.base_url or None, timeout=timeout)

    # Web search is a hosted tool on the Responses API — route there.
    if "web_search" in capabilities:
        return _run_openai_responses(client, system, prompt, model,
                                     temperature, max_tokens, capabilities)

    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature

    # Reasoning effort (reasoning models).
    effort = capabilities.get("reasoning_effort")
    if effort:
        kwargs["reasoning_effort"] = effort

    # Structured output → json_schema response format.
    so = capabilities.get("structured_output")
    if so and so.get("schema"):
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "structured_output", "strict": False,
                            "schema": so["schema"]},
        }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            max_tokens=max_tokens, **kwargs)
    except Exception as e:
        if "timeout" in str(e).lower() or "5xx" in str(e) or "50" in str(getattr(e, 'status_code', '')):
            raise _RetryableError(str(e))
        raise
    usage = getattr(resp, "usage", None)
    # OpenAI caches automatically; cached_tokens are INCLUDED in prompt_tokens,
    # so subtract them out to keep the four numbers comparable with Anthropic's.
    cached = _openai_cached_tokens(usage)
    total_in = getattr(usage, "prompt_tokens", 0) if usage else 0
    return resp.choices[0].message.content, _usage(
        prompt_tokens=max(total_in - cached, 0),
        completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        cache_read=cached)


def _run_openai_responses(client, system, prompt, model, temperature,
                          max_tokens, capabilities):
    """OpenAI Responses API path — used when the hosted web_search tool is
    requested. Also carries reasoning effort and structured output."""
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    effort = capabilities.get("reasoning_effort")
    if effort:
        kwargs["reasoning"] = {"effort": effort}

    tools = [{"type": "web_search"}]
    kwargs["tools"] = tools

    so = capabilities.get("structured_output")
    if so and so.get("schema"):
        kwargs["text"] = {
            "format": {"type": "json_schema", "name": "structured_output",
                       "schema": so["schema"], "strict": False},
        }

    try:
        resp = client.responses.create(
            model=model,
            input=[{"role": "system", "content": system},
                   {"role": "user", "content": prompt}],
            max_output_tokens=max_tokens, **kwargs)
    except Exception as e:
        if "timeout" in str(e).lower() or "5xx" in str(e) or "50" in str(getattr(e, 'status_code', '')):
            raise _RetryableError(str(e))
        raise

    text, sources = _extract_openai_responses_output(resp)
    usage = getattr(resp, "usage", None)
    cached = _openai_cached_tokens(usage)
    total_in = getattr(usage, "input_tokens", 0) if usage else 0
    out = _usage(prompt_tokens=max(total_in - cached, 0),
                 completion_tokens=getattr(usage, "output_tokens", 0)
                 if usage else 0,
                 cache_read=cached)
    # Layer 3: OpenAI does not enforce max_uses, so counting is the only way
    # to know whether the prompt directive was respected.
    out["search_count"] = sum(
        1 for item in (getattr(resp, "output", None) or [])
        if getattr(item, "type", "") == "web_search_call")
    if sources:
        out["grounding_sources"] = sources
    return text, out


def _extract_openai_responses_output(resp):
    """Pull answer text + web-search citations from a Responses API result.

    Prefers the SDK convenience `output_text`; otherwise walks the output
    blocks. Citations come from message-content annotations of type
    url_citation.
    """
    sources = []
    # Collect url citations from annotations if present.
    for item in (getattr(resp, "output", None) or []):
        for content in (getattr(item, "content", None) or []):
            for ann in (getattr(content, "annotations", None) or []):
                url = getattr(ann, "url", None) or (
                    ann.get("url") if isinstance(ann, dict) else None)
                if url:
                    title = getattr(ann, "title", "") or (
                        ann.get("title", "") if isinstance(ann, dict) else "")
                    sources.append({"uri": url, "title": title})

    text = getattr(resp, "output_text", None)
    if text:
        return text, sources

    # Fallback: concatenate text from output message blocks.
    parts = []
    for item in (getattr(resp, "output", None) or []):
        for content in (getattr(item, "content", None) or []):
            t = getattr(content, "text", None)
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts), sources


def _build_anthropic_system(system, context_block):
    """Two cache breakpoints, ordered longest-lived first.

    Block 1 — role instructions + JSON schema. Byte-identical for EVERY
              company, so once warm it is read at 0.1x forever.
    Block 2 — this run's source bundle. Identical across the ~15 section
              calls of one profile generation.

    Anthropic requires an exact prefix match, so the stable block must come
    first; putting the per-company bundle ahead of it would invalidate the
    schema cache on every company.
    """
    if not context_block:
        # Nothing to split — cache the instructions alone when big enough.
        if _cacheable(system):
            return [{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}]
        return system

    blocks = []
    if _cacheable(system):
        blocks.append({"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}})
    else:
        blocks.append({"type": "text", "text": system})

    ctx = {"type": "text", "text": context_block}
    if _cacheable(context_block):
        ctx["cache_control"] = {"type": "ephemeral"}
    blocks.append(ctx)
    return blocks


def _run_anthropic(endpoint, system, prompt, model, temperature, max_tokens,
                   timeout, *, capabilities=None, context_block=""):
    """Anthropic messages call, with Phase-3 capability translation.

    Translates the normalized capability payload into real Anthropic params:
      * web_search        → web_search tool, with max_uses / allowed_domains /
                            blocked_domains / user_location sub-controls
      * url_fetch         → web_fetch tool
      * code_execution    → code_execution tool (also enables Claude's
                            search-result dynamic filtering when requested)
      * thinking(budget)  → thinking={type:"enabled", budget_tokens:N}
      * structured_output → a forced tool whose input_schema is the response
                            schema; the tool input IS the structured JSON
    Absent/unsupported capabilities are simply not sent, so a plain call is
    identical to before.
    """
    import anthropic
    capabilities = capabilities or {}
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature

    tools = []
    ws = capabilities.get("web_search")
    if ws is not None:
        tool = {"type": "web_search_20250305", "name": "web_search"}
        if ws.get("max_uses"):
            tool["max_uses"] = ws["max_uses"]
        if ws.get("allowed_domains"):
            tool["allowed_domains"] = ws["allowed_domains"]
        if ws.get("blocked_domains"):
            tool["blocked_domains"] = ws["blocked_domains"]
        if ws.get("user_location"):
            tool["user_location"] = ws["user_location"]
        tools.append(tool)
    if capabilities.get("url_fetch"):
        tools.append({"type": "web_fetch_20250910", "name": "web_fetch"})
    if capabilities.get("code_execution"):
        tools.append({"type": "code_execution_20250522",
                      "name": "code_execution"})

    # Extended thinking.
    thinking = capabilities.get("thinking")
    if thinking and thinking.get("budget_tokens"):
        try:
            budget = int(thinking["budget_tokens"])
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Thinking requires max_tokens > budget; bump if needed.
            if max_tokens <= budget:
                max_tokens = budget + 1024
        except (TypeError, ValueError):
            pass

    # Structured output via a forced tool whose schema is the response schema.
    so = capabilities.get("structured_output")
    forced_schema = None
    if so and so.get("schema"):
        forced_schema = so["schema"]
        tools.append({
            "name": "emit_structured_output",
            "description": "Return the answer as structured JSON.",
            "input_schema": forced_schema,
        })
        # Can't force a specific tool while thinking is enabled; only set
        # tool_choice when not thinking (Anthropic constraint).
        if "thinking" not in kwargs:
            kwargs["tool_choice"] = {"type": "tool",
                                     "name": "emit_structured_output"}

    if tools:
        kwargs["tools"] = tools

    client = anthropic.Anthropic(api_key=endpoint.api_key, timeout=timeout)
    try:
        # `system` is a top-level param, not a message (Doc 6 Appendix C).
        # It may now be a LIST of blocks so the stable prefix can be cached.
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=_build_anthropic_system(system, context_block),
            messages=[{"role": "user", "content": prompt}], **kwargs)
    except anthropic.APITimeoutError as e:
        raise _RetryableError(str(e))
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise _RetryableError(str(e))
        raise

    text, sources = _extract_anthropic_output(resp, forced_schema)
    usage = getattr(resp, "usage", None)
    # Cached reads and writes are billed at DIFFERENT rates (0.1x and 1.25x),
    # and neither is included in input_tokens. Logging only input_tokens once
    # caching is on would make the spend dashboard quietly wrong.
    out = _usage(
        prompt_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
        completion_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        cache_read=(getattr(usage, "cache_read_input_tokens", 0) or 0)
        if usage else 0,
        cache_write=(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        if usage else 0)
    # Layer 3: count what actually ran. Anthropic enforces max_uses, so this
    # should always be <= the cap — logging it proves that rather than
    # assuming it.
    stu = getattr(usage, "server_tool_use", None) if usage else None
    if stu is not None:
        out["search_count"] = getattr(stu, "web_search_requests", None)
    if sources:
        out["grounding_sources"] = sources
    return text, out


def _extract_anthropic_output(resp, forced_schema):
    """Pull the answer text (and any web-search sources) from a Claude
    response that may contain thinking, tool-use and search-result blocks.

    * If structured output was forced, the answer is the tool_use input JSON —
      return it as a JSON string so the normal parse path handles it.
    * Otherwise, concatenate all text blocks.
    """
    import json as _json

    text_parts = []
    sources = []
    structured = None
    for block in getattr(resp, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            name = getattr(block, "name", "")
            if name == "emit_structured_output":
                structured = getattr(block, "input", None)
        elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
            for item in (getattr(block, "content", None) or []):
                url = getattr(item, "url", None) or (
                    item.get("url") if isinstance(item, dict) else None)
                title = getattr(item, "title", None) or (
                    item.get("title", "") if isinstance(item, dict) else "")
                if url:
                    sources.append({"uri": url, "title": title or ""})

    if forced_schema is not None and structured is not None:
        return _json.dumps(structured), sources
    return "".join(text_parts), sources


def _run_gemini(endpoint, system, prompt, model, temperature, max_tokens,
                timeout, *, capabilities=None, attachments=None):
    """Gemini generateContent, with Phase-2 capability translation.

    Translates the normalized capability payload into real Gemini API params:
      * web_search        → tools:[{google_search:{}}]      (Search grounding)
      * url_fetch         → tools:[{url_context:{}}]        (URL context)
      * maps_grounding    → tools:[{google_maps:{}}]
      * function_calling  → (NOT combined with search — enforced upstream)
      * code_execution    → tools:[{code_execution:{}}]
      * structured_output → responseMimeType + responseSchema
    Unsupported/absent capabilities are simply not sent.
    """
    capabilities = capabilities or {}
    base = endpoint.base_url or "https://generativelanguage.googleapis.com"
    # The key goes in a HEADER, not the query string. As a query parameter it
    # ends up inside requests' HTTPError message ("... for url: <full url>"),
    # and from there into the generation log, the stored run diagnostics, and
    # any support bundle built from them. Gemini accepts x-goog-api-key for
    # every endpoint, so there is no reason to put it in the URL.
    url = f"{base}/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": endpoint.api_key,
               "Content-Type": "application/json"}

    generation_config = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        generation_config["temperature"] = temperature

    # Thinking. Gemini 2.5+ reasons by default and those thought tokens are
    # deducted from maxOutputTokens, so a silent default can consume the
    # entire allowance and return a truncated body with candidatesTokenCount
    # of zero — which reads downstream as "the model returned nothing" with
    # no indication why. The budget is therefore always stated: 0 to switch
    # thinking off, a positive number to allow it.
    thinking = capabilities.get("thinking")
    thinking_budget = None
    if isinstance(thinking, dict) and thinking.get("budget_tokens") is not None:
        try:
            thinking_budget = int(thinking["budget_tokens"])
        except (TypeError, ValueError):
            thinking_budget = None
    if thinking_budget is None:
        # SAFE DEFAULT. Absence of a capability means "nobody said" — which
        # happens whenever a call runs without a config profile at all (an
        # unresolved tier, an explicit model_override, a direct adapter call).
        # Leaving it unsaid hands the decision to Gemini, and Gemini's answer
        # is "think", with the thoughts billed against maxOutputTokens and
        # excluded from candidatesTokenCount. That is the failure this whole
        # section exists to prevent, so it cannot depend on configuration
        # being present and correct.
        #
        # Absence therefore means OFF here, exactly as it already does on
        # Anthropic and OpenAI. Thinking is opt-in: state a positive budget on
        # the config profile to enable it, or -1 for Gemini's dynamic budget.
        thinking_budget = 0
    if thinking_budget >= 0:
        # The budget comes OUT of maxOutputTokens, so the ceiling must carry
        # the thoughts AND a full answer allowance — not merely be larger
        # than the budget.
        #
        # The earlier guard only fired when max_tokens <= thinking_budget. A
        # judgement call with a 4000-token budget and a 4096 ceiling passed
        # that test and was left with 96 tokens to answer in: the model spent
        # 3,932 thinking, was cut off mid-JSON, and every adjudication came
        # back as a repaired fragment while still reporting a healthy call.
        if thinking_budget > 0:
            generation_config["maxOutputTokens"] = thinking_budget + max_tokens
        # ASK FOR NO MORE THAN THE MODEL CAN RETURN.
        #
        # `profile_synthesis` carries a role ceiling of 98,304, and with a
        # 4,096 thinking budget the request asked Gemini for 102,400 output
        # tokens. gemini-2.5-flash stops at 65,536. A live run produced
        # 61,436 completion + 4,095 thinking = 65,531 and came back
        # MAX_TOKENS: four tokens short of the hard limit, having been
        # promised half as much again.
        #
        # Clamping does not lengthen the answer -- the model was always going
        # to stop there. It makes the REQUEST honest, so the trace reports a
        # ceiling that exists and the remedy it suggests is one an
        # administrator can actually act on.
        requested = generation_config.get("maxOutputTokens")
        cap = _provider_output_cap(model)
        if requested and cap and requested > cap:
            logger.warning(
                "LLM: asked %s for %s output tokens; it returns at most %s. "
                "Clamping. A response near that ceiling is TRUNCATED, not "
                "complete — the salvage recovers the prefix and synthesis "
                "re-asks for what is missing.",
                model, requested, cap)
            generation_config["maxOutputTokens"] = cap
        generation_config["thinkingConfig"] = {
            "thinkingBudget": thinking_budget}
    else:
        # -1 is Gemini's "decide for yourself" sentinel. An administrator who
        # sets it has asked for the provider default deliberately, so it is
        # sent as given rather than being turned into a zero.
        generation_config["thinkingConfig"] = {"thinkingBudget": -1}

    # Structured output → JSON schema mode.
    so = capabilities.get("structured_output")
    if so and so.get("schema"):
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = so["schema"]

    # Tools. Gemini requires search-type tools NOT be mixed with function
    # calling in one request; the config layer already blocks that combo, so
    # here we just assemble whatever was requested.
    tools = []
    if "web_search" in capabilities:
        tools.append({"google_search": {}})
    if capabilities.get("url_fetch"):
        tools.append({"url_context": {}})
    if capabilities.get("maps_grounding"):
        tools.append({"google_maps": {}})
    if capabilities.get("code_execution"):
        tools.append({"code_execution": {}})

    body = {
        "contents": [{"parts": _gemini_parts(system, prompt, attachments)}],
        "generationConfig": generation_config,
    }
    if tools:
        body["tools"] = tools

    # The payload actually on the wire. The two 400 fallbacks below build a
    # reduced body and send that, leaving `body` as the original — which the
    # call log holds a reference to and must keep. Anything retrying AFTER
    # those fallbacks has to resend what was last sent, or a 429 retry would
    # silently reinstate the very field the provider just rejected.
    sent_body = body
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    # Not every Gemini model accepts a thinking budget, and the frontier Pro
    # models cannot have it set to zero at all. That is a property of the
    # model, not a misconfiguration, so retry once without the field rather
    # than failing the call — the operator's intent ("do not spend the output
    # budget on thoughts") simply cannot be honoured on that model.
    # v29 — a rejected responseSchema must not cost the whole call.
    #
    # Until v29 no responseSchema was ever sent, so this path could not fire
    # and no fallback existed. Now that a derived schema can go on the wire, a
    # provider that dislikes it (an unsupported type, a model that does not do
    # JSON mode) would turn a working call into a hard failure — trading an
    # inert tick for an outage. Retry once without it: the response is
    # validated and repaired downstream regardless, so the schema is an
    # optimisation, never a requirement.
    if resp.status_code == 400 and "responseSchema" in generation_config:
        body_text = (resp.text or "").lower()
        if "schema" in body_text or "json" in body_text:
            logger.warning(
                "LLM: %s rejected the derived responseSchema — retrying "
                "without it. The response will be validated and repaired "
                "downstream instead. Schema was: %s",
                model, list((generation_config.get("responseSchema")
                             or {}).get("properties") or {}))
            retry_config = {k: v for k, v in generation_config.items()
                            if k not in ("responseSchema", "responseMimeType")}
            retry_body = dict(body)
            retry_body["generationConfig"] = retry_config
            sent_body = retry_body
            resp = requests.post(url, json=retry_body, headers=headers,
                                 timeout=timeout)
            generation_config = retry_config

    if resp.status_code == 400 and "thinkingConfig" in generation_config:
        body_text = (resp.text or "").lower()
        if "thinking" in body_text or "thinkingbudget" in body_text:
            logger.warning(
                "LLM: %s rejected thinkingBudget=%s — retrying without it. "
                "This model reasons by default and its thoughts count "
                "against maxOutputTokens; raise the output ceiling if "
                "responses come back truncated.", model, thinking_budget)
            # Build a fresh payload rather than mutating the one already
            # sent — the original is what the call log and any request
            # recorder hold a reference to.
            retry_config = {k: v for k, v in generation_config.items()
                            if k != "thinkingConfig"}
            retry_body = dict(body, generationConfig=retry_config)
            sent_body = retry_body
            resp = requests.post(url, json=retry_body, headers=headers,
                                 timeout=timeout)
    # Quota exhaustion is a property of the KEY, not of the request.
    #
    # A 429 is not >= 500, so it fell straight through to `_raise_for_status`
    # and failed the batch. That is the wrong response when several keys are
    # configured: Gemini's quota windows are minute-scale, so retrying the same
    # key immediately fails again, while the next key in the pool is very
    # likely fine. Rotate, cool the exhausted key, and retry — bounded by the
    # pool size, so a genuinely exhausted account fails fast instead of walking
    # every key twice.
    attempts = 0
    max_rotations = max(0, min(endpoint.api_key_pool_size - 1, 4))
    while resp.status_code == 429 and attempts < max_rotations:
        attempts += 1
        endpoint.penalise_api_key(headers.get("x-goog-api-key"),
                                  reason=f"HTTP 429 on {model}")
        rotated = endpoint.api_key
        if not rotated or rotated == headers.get("x-goog-api-key"):
            break
        headers = dict(headers, **{"x-goog-api-key": rotated})
        logger.warning("LLM: gemini returned 429 for %s — retrying on another "
                       "key (%d of %d rotations).",
                       model, attempts, max_rotations)
        resp = requests.post(url, json=sent_body, headers=headers,
                             timeout=timeout)

    if resp.status_code == 429:
        # Out of keys to try. Retryable rather than fatal so the adapter's own
        # backoff and circuit breaker get to see it — this is a wait, not a
        # misconfiguration, and the two have opposite remedies.
        raise _RetryableError(
            f"gemini 429: every configured key is rate-limited "
            f"({endpoint.api_key_pool_size} in the pool)")
    if resp.status_code >= 500:
        raise _RetryableError(f"gemini {resp.status_code}")
    _raise_for_status(resp, "gemini", model)
    data = resp.json()
    text = _extract_gemini_text(data)
    meta = data.get("usageMetadata", {})
    # Gemini implicit caching: cachedContentTokenCount is INCLUDED in
    # promptTokenCount, so subtract it out for a like-for-like figure.
    cached = int(meta.get("cachedContentTokenCount", 0) or 0)
    usage = _usage(
        prompt_tokens=max(int(meta.get("promptTokenCount", 0) or 0) - cached, 0),
        completion_tokens=meta.get("candidatesTokenCount", 0),
        cache_read=cached)
    # thoughtsTokenCount is billed at the output rate but is NOT included in
    # candidatesTokenCount, so a call that spent everything on thinking
    # reports zero completion tokens. Recording it is what makes "the model
    # returned nothing" explicable rather than mysterious.
    thoughts = int(meta.get("thoughtsTokenCount", 0) or 0)
    if thoughts:
        usage["thinking_tokens"] = thoughts
    # finishReason distinguishes "the model had nothing to say" from "the
    # model was cut off mid-sentence" — the latter is the cause of every
    # JSONDecodeError of the form "Unterminated string".
    try:
        finish = (data.get("candidates") or [{}])[0].get("finishReason")
        if finish:
            usage["finish_reason"] = finish
    except (AttributeError, IndexError):
        pass
    # Surface grounding metadata (sources) for observability (Phase 5 will log
    # it); harmless to callers that ignore it.
    grounding = _extract_gemini_grounding(data)
    if grounding:
        usage["grounding_sources"] = grounding
    # Layer 3: webSearchQueries is the list of queries actually ISSUED.
    # groundingChunks counts SOURCES and over-reports — one query can return
    # many chunks — so it must not be used as a search count.
    try:
        gm = data["candidates"][0].get("groundingMetadata") or {}
        queries = gm.get("webSearchQueries")
        if queries is not None:
            usage["search_count"] = len(queries)
        elif capabilities and "web_search" in capabilities:
            # v27.5 — the tool WAS attached and no queries came back, which on
            # Gemini means the model declined. Measured 19 Aug: every call
            # that searched returned this field, so its absence is evidence of
            # zero rather than absence of evidence. Reporting it as "unknown"
            # made run.economics read `searches=6 searches_unknown=8` when the
            # truth was six searches and eight refusals.
            usage["search_count"] = 0
    except (KeyError, IndexError):
        pass
    return text, usage


def _gemini_parts(system, prompt, attachments):
    """The request parts, with any files FIRST.

    Order is deliberate: Gemini reads a document better when the bytes
    precede the instruction about them, and putting the file first also keeps
    the instruction — the volatile half — at the tail, where it cannot
    disturb a cached prefix.
    """
    parts = []
    for item in attachments or []:
        data = item.get("data")
        if not data:
            continue
        parts.append({"inline_data": {
            "mime_type": item.get("mime_type") or "application/octet-stream",
            "data": base64.b64encode(data).decode("ascii")}})
    parts.append({"text": f"{system}\n\n{prompt}"})
    return parts


def _extract_gemini_text(data):
    """Concatenate all text parts of the first candidate. With grounding /
    tools enabled the model may return multiple parts, some non-text."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        return ""
    texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "".join(t for t in texts if t)


def _extract_gemini_grounding(data):
    """Return the list of grounded source URLs/titles, if any."""
    try:
        gm = data["candidates"][0].get("groundingMetadata") or {}
    except (KeyError, IndexError):
        return []
    chunks = gm.get("groundingChunks") or []
    sources = []
    for c in chunks:
        web = c.get("web") or {}
        if web.get("uri"):
            sources.append({"uri": web.get("uri"),
                            "title": web.get("title", "")})
    return sources


def _run_deepinfra(endpoint, system, prompt, model, temperature, max_tokens,
                   timeout):
    url = f"{endpoint.base_url.rstrip('/')}/v1/openai/chat/completions"
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    resp = requests.post(url, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {endpoint.api_key}"})
    if resp.status_code >= 500:
        raise _RetryableError(f"deepinfra {resp.status_code}")
    _raise_for_status(resp, "deepinfra", model)
    data = resp.json()
    usage = data.get("usage", {})
    cached = int((usage.get("prompt_tokens_details") or {}).get(
        "cached_tokens", 0) or 0)
    return data["choices"][0]["message"]["content"], _usage(
        prompt_tokens=max(usage.get("prompt_tokens", 0) - cached, 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cache_read=cached)


def _run_custom_rest(endpoint, system, prompt, model, timeout):
    url = endpoint.build_url(model)
    resp = requests.post(url, json={"prompt": prompt, "context": system},
                         timeout=timeout)
    if resp.status_code >= 500:
        raise _RetryableError(f"custom_rest {resp.status_code}")
    _raise_for_status(resp, "custom_rest", model)
    try:
        data = resp.json()
        text = data.get("text") or data.get("response") or json.dumps(data)
    except ValueError:
        text = resp.text
    return text, _usage()


# ---------------------------------------------------------------------------
# Call tracking (Doc 6 §3.2/D) — never let tracking failure break the call.
# ---------------------------------------------------------------------------

def _price(endpoint_code, model, prompt_tokens, completion_tokens,
           thinking_tokens, cache_read_tokens, cache_write_tokens):
    """Cost of one call in INR. Never raises — pricing must not break a call.

    Thinking tokens are added to the OUTPUT side because every provider bills
    reasoning at the output rate and none of them include it in the completion
    count. Omitting that under-reported the 17 Aug runs by 39%.
    """
    from decimal import Decimal
    try:
        from fundos.llm.models import LLMModelCost
        return LLMModelCost.cost_inr(
            endpoint_code, model or "", prompt_tokens,
            (completion_tokens or 0) + (thinking_tokens or 0),
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens)
    except Exception as e:
        logger.warning("LLM: pricing lookup failed for %s/%s (%s) — this call "
                       "is recorded at zero cost.", endpoint_code, model, e)
        return Decimal("0")


def _budget_str(capabilities):
    """Thinking budget as a bare int, or "unset". Never a quoted number.

    v27.5. The 19 Aug log carried `thinking_budget=2048` on one line and
    `thinking_budget="4000"` on the next, because the value came straight
    from a JSON config field where an administrator had typed a string. The
    trace formatter quotes strings, so the same field changed shape between
    lines and broke every parser reading it.
    """
    raw = (capabilities.get("thinking") or {}).get("budget_tokens", None)
    if raw is None:
        return "unset"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return "unset"


def _completion_from(usage, text):
    """Completion tokens, estimated from the text when the provider says zero.

    v27.5. On 19 Aug a truncated Gemini call reported
    `candidatesTokenCount=0` while returning 485 characters of JSON, and the
    ledger duly recorded zero output tokens for a call that was billed for
    every one of them. Gemini stops counting candidate tokens once a response
    is cut off at the ceiling, but it charges for what it produced.

    Only fires when the provider's own number is missing or zero AND text came
    back, so a genuinely empty response still reports zero. ~4 characters per
    token is the usual English/JSON ratio; it is an ESTIMATE and is marked as
    one on the trace line via `billed`, because a rough number in the right
    order of magnitude beats a precise zero that is wrong.
    """
    reported = int(usage.get("completion_tokens") or 0)
    if reported > 0 or not text:
        return reported
    return max(len(text) // 4, 1)


def _log_call(*, role, endpoint_code, model, deal_id, user, calling_context,
              latency_ms, status, prompt_tokens, completion_tokens, error="",
              thinking_tokens=0,
              cache_read_tokens=0, cache_write_tokens=0, search_count=None,
              tier="", config_profile="", prompt_fingerprint="",
              context_chars=0, was_repaired=False, was_normalized=False,
              output_signals=None, finish_reason=""):
    # v27.4 — the run ledger is written FIRST, and outside the try below.
    #
    # `_log_call` deliberately swallows its own exceptions so that call
    # tracking can never break a generation. That is correct, and it means the
    # database row is not guaranteed. The `run.economics` trace line must still
    # be right when the row is missing — a run whose DB writes failed is
    # precisely when someone needs to read the accounting — so the in-memory
    # book does not depend on the write succeeding.
    #
    # This is the only place a ledger entry is created. Every dispatch path
    # (success, fallback, mocked, hard failure) funnels through here, so the
    # ledger cannot disagree with LLMCallLog about how many calls were made.
    _cost = _price(endpoint_code, model, prompt_tokens, completion_tokens,
                   thinking_tokens, cache_read_tokens, cache_write_tokens)
    ledger.record(
        role=role, endpoint_code=endpoint_code, model=model or "",
        status=status, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, thinking_tokens=thinking_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens, cost_inr=_cost,
        latency_ms=latency_ms, context_chars=context_chars,
        search_count=search_count, tier=tier, config_profile=config_profile,
        was_repaired=was_repaired, was_normalized=was_normalized,
        finish_reason=finish_reason)

    try:
        from fundos.core.scoping import get_current_tenant
        from fundos.llm.models import LLMCallLog
        LLMCallLog.objects.create(
            tenant_id=get_current_tenant(),
            provider=endpoint_code, model_name=model or "",
            function_name=role, calling_context=calling_context[:128],
            deal_id=deal_id,
            req_user=user if getattr(user, "pk", None) else None,
            is_user_initiated=bool(user),
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens or 0,
            cache_write_tokens=cache_write_tokens or 0,
            search_count=search_count if search_count is not None else -1,
            tier=tier or "", config_profile=config_profile or "",
            prompt_fingerprint=prompt_fingerprint or "",
            context_chars=context_chars or 0,
            was_repaired=bool(was_repaired),
            was_normalized=bool(was_normalized),
            output_signals=output_signals or {},
            total_tokens=((prompt_tokens or 0) + (completion_tokens or 0)
                          + (thinking_tokens or 0)
                          + (cache_read_tokens or 0) + (cache_write_tokens or 0)),
            response_time_ms=latency_ms,
            success=status in ("success", "fallback", "mocked"),
            status=status, error_message=error,
            # v27.3 — thinking tokens BILL AT THE OUTPUT RATE and are not
            # included in completion_tokens by any provider. Omitting them
            # under-reported the 17 Aug runs by 39% (10,448 of 26,864 billed
            # output tokens), and _enforce_budget reads this column, so a
            # tenant ceiling could be overshot by that margin before it
            # tripped. That mattered less while thinking was off almost
            # everywhere; the v27.3 tier contract turns it ON for the
            # advanced and judgment tiers, so it must be counted now.
            #
            # Folded into the output figure rather than stored separately:
            # a dedicated column is the better end state but needs a
            # migration, and a correct number today beats a tidy schema
            # next release. The raw count remains visible on the trace line.
            #
            # v27.4 — computed ONCE above and shared with the ledger, so the
            # `run.economics` line and the LLMCallLog rows can never report
            # different money for the same call.
            cost_inr=_cost,
        )
    except Exception as e:
        logger.warning("LLM: call-tracking failed (never breaks the call): %s", e)


def registry_consistency_check():
    """Startup check (Doc 6 §3.1): every bound role points at an ACTIVE
    endpoint whose env key is present. Logged loudly, never crashes boot."""
    problems = []
    try:
        from fundos.llm.models import KINDS_REQUIRING_KEY, LLMRoleBinding
        for binding in LLMRoleBinding.objects.select_related("primary_endpoint"):
            ep = binding.primary_endpoint
            if not ep or not ep.is_active:
                problems.append(f"{binding.role}: primary endpoint inactive/missing")
            elif ep.provider_kind in KINDS_REQUIRING_KEY and not ep.has_api_key_in_env():
                problems.append(
                    f"{binding.role}: env var {ep.api_key_env_var} not set")
    except Exception as e:
        logger.warning("LLM: registry check skipped: %s", e)
        return problems
    for p in problems:
        logger.error("LLM REGISTRY CHECK: %s", p)
    return problems
