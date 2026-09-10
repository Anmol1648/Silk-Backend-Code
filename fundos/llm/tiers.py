"""
Tier resolution: which LLM (Simple / Advanced / Judgement) a role runs on.

Design (per the enhancement brief):
  * The TIER of a role is admin-configurable on the prompt itself
    (platformcfg.PromptTemplate.tier). If no admin row exists, the shipped
    default in llm/default_prompts.py (the role's "tier" key) applies, else
    "simple" as the safe fallback.
  * The concrete model + capabilities for each tier are chosen PER TENANT via
    llm.models.TenantLLMTier (with a global NULL-tenant default).

So the full chain is:
    role -> tier (prompt row / code default) -> tenant's config-profile code
"""
from fundos.core.scoping import get_current_tenant

SIMPLE = "simple"
ADVANCED = "advanced"
JUDGMENT = "judgment"
_VALID = {SIMPLE, ADVANCED, JUDGMENT}


def tier_for_role(role: str) -> str:
    """Resolve a role's tier: admin PromptTemplate.tier > code default > simple."""
    # 1) Admin override on the prompt row.
    try:
        from fundos.platformcfg.models import PromptTemplate
        row = PromptTemplate.resolve(role)
        if row is not None:
            t = (getattr(row, "tier", "") or "").strip().lower()
            if t in _VALID:
                return t
    except Exception:
        pass
    # 2) Shipped default carried on the prompt definition.
    try:
        from fundos.llm.default_prompts import get_tier
        t = (get_tier(role) or "").strip().lower()
        if t in _VALID:
            return t
    except Exception:
        pass
    # 3) Fallback.
    #
    # v27.3: this used to return SIMPLE unconditionally, reasoning "never
    # silently spend the advanced (web-search) budget". That instinct is
    # right for most roles and exactly backwards for a handful of them. For
    # `company_profile_deep_extract` the cost of a silent downgrade is not a
    # saved search — it is a published dossier assembled from the model's
    # recollection, which is the failure the whole grounding gate exists to
    # prevent. A role that cannot answer without retrieval gets the tier that
    # retrieves, and if that tier does not resolve the run fails loudly.
    from fundos.llm.tier_contract import SEARCH_REQUIRED_ROLES
    if role in SEARCH_REQUIRED_ROLES:
        return ADVANCED
    return SIMPLE


def resolve_tier_profile_code(role: str = None, tier: str = None,
                              tenant_id=None) -> str | None:
    """The LLMConfigProfile code to use for a call, or None.

    Precedence: explicit tier arg > tier derived from the role's prompt.
    Tenant: the argument, else the current request/worker tenant context.
    """
    from fundos.llm.models import TenantLLMTier
    from fundos.llm.tier_contract import assert_role_tier_coherent
    if tier is None:
        tier = tier_for_role(role or "")
    # A search-required role sitting on a non-searching tier is an admin
    # misconfiguration that produces confident, uncitable output. Refuse it
    # here, where the reason is still legible, rather than three warnings
    # and 90 seconds later.
    if role:
        assert_role_tier_coherent(role, tier)
    if tenant_id is None:
        tenant_id = get_current_tenant()
    return TenantLLMTier.resolve_code(tier, tenant_id)
