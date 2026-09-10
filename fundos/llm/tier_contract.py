"""
Tier capability contracts.

WHY THIS EXISTS
---------------
`LLMConfigProfile.tier` was a LABEL and `web_search` was an independent
boolean. A row saying `tier="advanced", web_search=False` was valid, saved
without complaint, and produced a dossier generated from the model's memory
while every layer of the stack reported healthy.

The invariant "advanced means it searches" was written down in three places
-- TIER_DEFINITIONS prose, the diagnose_config check, and a post-hoc adapter
warning -- and enforced in none of them. Three detectors, zero constraints.

A tier is now a CONTRACT. The tier states what a call at that tier MUST and
MUST NOT be able to do; the profile tunes everything the contract leaves
open. An administrator cannot tick the advanced tier's search off, because
there is no longer a tick to reach: the capability follows from the tier.

WHAT THE CONTRACT COVERS, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------------
It covers TOOLS and the reasoning needed to drive them, because those are
what TIER_DEFINITIONS actually defines a tier by:

    simple    "Tools: None. Cannot browse."
    advanced  "Tools: Web search (and URL fetch where supported)."
    judgment  "Tools: None. Deliberately cannot browse."

It does NOT cover model choice, temperature, output ceilings, schemas, or
how much an administrator wants a given profile to think beyond the minimum
its tier requires. The same endpoint and model string may back all three
tiers -- and today does, all on gemini-3.5-flash. Tiers differ by what a call
is ALLOWED AND REQUIRED TO DO, not by how strong the model is;
TIER_DEFINITIONS says as much for Advanced, where search coverage rather
than model strength is named as the binding constraint.

Thinking is a FLOOR on the advanced tier and a DEFAULT elsewhere, never a
ceiling. An administrator who states a budget has made a deliberate choice
and keeps it. The contract only intervenes where silence would produce a
tier that cannot do its job.
"""
import logging

# Imported rather than redeclared: three copies of these strings is how
# "judgement" vs "judgment" becomes a silent tier miss.
from fundos.llm.tiers import ADVANCED, JUDGMENT, SIMPLE

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The contract.
#
#   web_search        True  = the tier is defined by it; forced on
#                     False = the tier is defined by its absence; forced off
#   thinking_unstated the budget used when the profile states none. 0 means
#                     "state zero explicitly", which on Gemini is NOT the
#                     same as saying nothing (it reasons by default and bills
#                     the thoughts against maxOutputTokens).
#   thinking_positive True = a zero budget is incoherent at this tier and is
#                     raised to thinking_unstated.
# --------------------------------------------------------------------------
TIER_CONTRACT = {
    SIMPLE: {
        "web_search": False,
        "thinking_unstated": 0,
        "thinking_positive": False,
        "rationale": "Extract from text the system already holds.",
    },
    ADVANCED: {
        "web_search": True,
        # ------------------------------------------------------------------
        # MEASURED 19 Aug 2026 — gemini-3.5-flash
        # tools/diagnose_gemini_search.py, 8 cells, groundingMetadata.
        #
        #   schema  thinking  directive   searches
        #   True    0         before      unknown
        #   True    0         after       unknown
        #   True    2048      before      unknown
        #   True    2048      after       unknown
        #   False   0         before      4
        #   False   0         after       4
        #   False   2048      before      3
        #   False   2048      after       6
        #
        #   H1 responseSchema : DECISIVE. with = 0% searched, without = 100%.
        #   H2 thinking       : NO EFFECT.
        #   H3 directive pos  : NO EFFECT.
        #
        # This settles a question two halves of the repo answered oppositely
        # for six releases, both from reasoning and neither from measurement.
        # It is written here, with the date and the model string, so it is
        # never re-derived by argument again. Re-measure when the model
        # changes; do not assume it carries forward.
        # ------------------------------------------------------------------
        #
        # A responseSchema and a search tool cannot coexist on this model, and
        # of the two, retrieval is the one this tier exists for. The advanced
        # tier therefore forbids structured output outright: JSON is obtained
        # by instruction and the existing repair/normalise path, which the
        # 17-19 Aug logs show handling it (was_repaired fired once across 22
        # calls and recovered cleanly).
        "structured_output": False,
        # H2 measured NO EFFECT on whether the model searches. The floor is
        # kept only because it is what the JUDGMENT-class reasoning inside a
        # research call uses; it is NOT a search enabler, and v27.5 stops
        # treating it as one. See adapter.NO_THINKING_ROLES for the guard
        # that was narrowed as a result.
        "thinking_unstated": 2048,
        "thinking_positive": True,
        "rationale": "Discover facts that are NOT in the supplied context.",
    },
    JUDGMENT: {
        "web_search": False,
        # Left unstated, Gemini picks its own budget per call: two
        # structurally identical runs on 17 Aug spent 4,182 and 6,266 tokens
        # thinking, so neither cost nor latency was predictable. An explicit
        # default makes the tier deterministic without capping an
        # administrator who wants more.
        "thinking_unstated": 4096,
        "thinking_positive": False,
        "rationale": "Reason over data another call already extracted.",
    },
}

# Roles that cannot produce a publishable answer without retrieval. Distinct
# from adapter.SEARCH_EXPECTED_ROLES, which describes what we HOPE a call
# does; this describes what must be TRUE for its output to be worth writing.
SEARCH_REQUIRED_ROLES = {
    "company_profile_deep_extract",
    "assessment_inputs",
    "peer_insight",
    "research_synthesis",
    # The company-profile pipeline's retrieval half. Its entire job is to
    # reach the open web; an answer it produced without searching is
    # recollection wearing a citation format.
    #
    # It normally pins an explicit config profile, which bypasses tier
    # resolution and therefore `assert_role_tier_coherent` — so declaring it
    # here changes nothing on the happy path. It matters in the two cases
    # where something has gone wrong: if an administrator deletes the config
    # profile the role falls back to a tier, and this is what makes that
    # fallback the SEARCHING tier rather than a silent downgrade; and if
    # search is switched off on the profile, the wire-level contract check
    # refuses the call instead of billing for an uncitable answer.
    #
    # The supported way to run a cheaper pass is to deactivate research
    # batches in admin — each batch is one call — not to strip the tool from
    # a retrieval role and hope.
    "profile_research_batch",
}


class TierContractViolation(Exception):
    """A profile cannot honour the contract of the tier it is bound to.

    Raised at VALIDATION time (admin save, seeding, tier resolution) rather
    than at call time, so the bad state never reaches a generation run.
    """


def contract_for(tier):
    return TIER_CONTRACT.get(tier or SIMPLE, TIER_CONTRACT[SIMPLE])


def validate_profile_against_tier(profile, tier, provider_caps):
    """Raise if `profile` cannot serve `tier`. Called from clean().

    provider_caps is PROVIDER_CAPABILITIES[profile.provider] -- what the
    provider exposes at all.
    """
    contract = contract_for(tier)

    if contract["web_search"] and "web_search" not in provider_caps:
        raise TierContractViolation(
            f"Provider '{profile.provider}' does not support web search, so "
            f"it cannot serve the {tier} tier. The advanced tier exists to "
            f"retrieve from the internet; a profile that cannot search is "
            f"not an advanced profile with a limitation, it is a simple "
            f"profile with the wrong label. Choose a searching provider, or "
            f"set this profile's tier to 'simple'.")

    if contract["thinking_positive"] and not (
            {"thinking_budget", "reasoning_effort"} & set(provider_caps)):
        raise TierContractViolation(
            f"Provider '{profile.provider}' exposes no thinking or reasoning "
            f"control, which the {tier} tier needs in order to decide to "
            f"search.")

    return True


def apply_contract(capabilities, tier, *, provider, provider_caps,
                   profile=None):
    """Force `capabilities` to honour the tier contract. Returns a new dict.

    Called at the END of LLMConfigProfile.capabilities_payload(), so the
    profile's own toggles assemble first and the contract has the last word.
    """
    contract = contract_for(tier)
    out = dict(capabilities)

    _apply_search(out, contract, tier, provider, provider_caps, profile)
    _apply_thinking(out, contract, provider, provider_caps)
    _apply_structured_output(out, contract, tier, provider, profile)
    return out


def _apply_structured_output(out, contract, tier, provider, profile):
    """Drop responseSchema where it would suppress the search tool.

    MEASURED 19 Aug 2026 on gemini-3.5-flash: a request carrying a
    responseSchema searched in 0 of 4 configurations; the same request without
    one searched in 4 of 4. See the ADVANCED block above for the full grid.

    The two features are mutually exclusive on this model, so a tier has to
    choose. The advanced tier exists to retrieve facts that are not in the
    supplied context; a schema is a convenience for parsing what comes back.
    Convenience loses.

    Scoped to Gemini because that is where it was measured. Anthropic and
    OpenAI have no equivalent interaction and there is no evidence to justify
    degrading their output shape on a Gemini finding.
    """
    if contract.get("structured_output") is not False:
        return
    if provider != "gemini":
        return
    if "structured_output" not in out:
        return
    logger.warning(
        "LLM: profile %s requests structured output on the '%s' tier. On "
        "Gemini a responseSchema suppresses google_search entirely (measured "
        "19 Aug 2026, gemini-3.5-flash: 0/4 searched with a schema, 4/4 "
        "without), and this tier is defined by retrieval. The schema is being "
        "dropped; JSON is obtained by instruction and the repair path. Set "
        "this profile's tier to 'simple' if it needs a guaranteed shape more "
        "than it needs facts.",
        getattr(profile, "code", "?"), tier)
    out.pop("structured_output", None)


def _apply_search(out, contract, tier, provider, provider_caps, profile):
    if contract["web_search"]:
        if "web_search" not in provider_caps:
            # Reaching here means validation was bypassed -- a fixture, a raw
            # DB write, a migration. Fail loudly rather than silently
            # returning a non-searching "advanced" call: silent degradation
            # is the exact failure this module exists to end.
            raise TierContractViolation(
                f"The {tier} tier resolved to provider '{provider}', which "
                f"cannot search. Refusing to run an ungrounded call under a "
                f"tier whose contract is retrieval.")
        ws = dict(out.get("web_search") or {})
        # ALWAYS carry a cap. A blank one used to produce `web_search: {}` --
        # falsy everywhere, present everywhere -- which sent Gemini the tool
        # with no instruction to use it.
        ws.setdefault("max_uses", _default_max_uses())
        out["web_search"] = ws
        return

    if "web_search" in out:
        # Never silent: an administrator ticked this and it is not happening.
        logger.warning(
            "LLM: profile %s has web search enabled but is bound to the '%s' "
            "tier, which is defined by NOT browsing (%s). Search is being "
            "dropped from this call. If this profile is meant to research, "
            "set its tier to 'advanced'.",
            getattr(profile, "code", "?"), tier, contract["rationale"])
    out.pop("web_search", None)


def _apply_thinking(out, contract, provider, provider_caps):
    # An effort-based control (OpenAI) is a stated intent in its own right
    # and is never second-guessed here -- it cannot be zero, so it cannot
    # produce the failure this contract guards against.
    if out.get("reasoning_effort"):
        return

    stated = _stated_budget(out)
    if stated is not None:
        # The administrator said a number. Honour it, unless the tier cannot
        # function at zero.
        if stated <= 0 and contract["thinking_positive"]:
            out["thinking"] = {"budget_tokens": contract["thinking_unstated"]}
        return

    if out.get("thinking") is not None and not contract["thinking_positive"]:
        # A stated-but-unparseable budget on a tier with no floor: leave the
        # profile's payload untouched rather than inventing a number.
        return

    budget = contract["thinking_unstated"]
    if budget:
        out["thinking"] = {"budget_tokens": budget}
    elif provider == "gemini" and "thinking_budget" in provider_caps:
        # Gemini 2.5+ reasons BY DEFAULT and bills thoughts against
        # maxOutputTokens without reporting them in candidatesTokenCount.
        # Silence means "provider default", not "off", so zero must be said.
        out["thinking"] = {"budget_tokens": 0}
    # Anthropic and OpenAI: absence genuinely means off. Say nothing.


def _stated_budget(capabilities):
    """The profile's own budget as an int, or None if it stated none.

    LLMConfigProfile.thinking_budget is a CharField, so "" and "medium" both
    reach here and neither is a token count.
    """
    thinking = capabilities.get("thinking")
    if not isinstance(thinking, dict):
        return None
    try:
        return int(thinking.get("budget_tokens"))
    except (TypeError, ValueError):
        return None


def _default_max_uses():
    from fundos.llm.models import DEFAULT_SEARCH_MAX_USES
    return DEFAULT_SEARCH_MAX_USES


def assert_role_tier_coherent(role, tier):
    """A role must land on a tier whose contract matches the work it does.

    v28 — covers ALL THREE tiers. It previously guarded only the search
    direction, which left the other two failure modes silent:

      * a JUDGEMENT role on `simple` gets thinking_unstated=0 -- reasoning
        with the reasoning turned off, which reads as a cheap, fast, confident
        and shallow answer, and nothing in the log says why
      * an EXTRACTION role on `advanced` gets a search tool it does not need,
        a thinking budget it does not use, and -- since v27.5 -- LOSES its
        responseSchema, because a schema suppresses google_search on Gemini
        (measured 19 Aug 2026: 0/4 searched with a schema, 4/4 without)

    That last one is not hypothetical. On 19 Aug an admin override had put
    `company_profile_section` on `advanced`; it cost a thinking budget on
    every call, and after v27.5 it would silently have lost the structured
    output the section writer depends on.

    Direction matters for severity. A search role that cannot search publishes
    recollection as research, so that RAISES. The other two produce degraded
    output rather than false output, and an administrator may have a reason we
    do not know about, so they WARN loudly and proceed. Blocking an admin's
    deliberate choice on a guess is how a diagnostic becomes something people
    route around.
    """
    from fundos.llm.default_prompts import get_tier

    if role in SEARCH_REQUIRED_ROLES and not contract_for(tier)["web_search"]:
        raise TierContractViolation(
            f"Role '{role}' cannot answer from supplied context -- it needs "
            f"retrieval -- but resolved to the '{tier}' tier, which does not "
            f"search. Set this prompt's tier to 'advanced' (Admin -> AI "
            f"Prompts), or remove the role from the pipeline. Running it as "
            f"it stands publishes recollection as research.")

    shipped = get_tier(role)
    if shipped == tier:
        return

    if shipped == JUDGMENT and tier == SIMPLE:
        logger.warning(
            "LLM TIER MISMATCH: role '%s' is a JUDGEMENT role -- it weighs "
            "evidence and reaches a conclusion -- but resolved to 'simple', "
            "whose contract sets the thinking budget to 0. It will answer "
            "quickly, cheaply and shallowly, and nothing downstream will "
            "indicate that it was not allowed to reason. Set this prompt's "
            "tier back to 'judgment' in Admin -> AI Prompts.", role)

    elif shipped == SIMPLE and tier == ADVANCED:
        logger.warning(
            "LLM TIER MISMATCH: role '%s' is an EXTRACTION role -- it answers "
            "from material already supplied -- but resolved to 'advanced'. "
            "That tier attaches a web-search tool it does not need, funds a "
            "thinking budget it does not use, and DROPS its responseSchema, "
            "because a schema suppresses google_search on Gemini. If this "
            "role relies on structured output, the override is actively "
            "harming it. Set the tier back to 'simple' in Admin -> AI "
            "Prompts unless you specifically want this role searching.", role)


def profiles_losing_search(queryset=None):
    """Profiles whose ticked web_search the contract will now drop.

    UPGRADE AID. The contract is a deliberate behaviour change, and an
    operator upgrading to v27.3 deserves the list BEFORE the first run rather
    than a warning line afterwards. Used by `manage.py diagnose_config` and
    safe to call from a shell.
    """
    from fundos.llm.models import LLMConfigProfile
    qs = queryset if queryset is not None else LLMConfigProfile.objects.all()
    return [p for p in qs
            if p.web_search and not contract_for(p.tier)["web_search"]]
