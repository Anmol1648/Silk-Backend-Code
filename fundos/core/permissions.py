"""
RBAC + deal access (Doc 1 M0-§1 / Doc 4 Appendix 4-B / Doc 8 §3).

Deal visibility = membership(user, deal) + RBAC. No RLS, no SQL rewriting.
Effective role on a deal = max(company_role, deal_role) (GC-07).
"""
from fundos.core.exceptions import AuthzError

# Role precedence (for max()); higher index = wider capability set.
ROLE_ORDER = [
    "team", "advisor", "co_founder", "founder",
    "banker", "lead_banker", "fundos_analyst", "fundos_admin", "data_steward",
]

# Doc 4 Appendix 4-B — RBAC per action (Stage 1–3 subset).
ACTION_MATRIX = {
    "research.run":     {"founder", "co_founder", "team", "banker", "lead_banker",
                         "fundos_admin", "fundos_analyst"},
    "strategy.run":     {"founder", "co_founder", "team", "banker", "lead_banker",
                         "fundos_admin", "fundos_analyst"},
    "materials.upload": {"founder", "co_founder", "team", "banker", "lead_banker",
                         "fundos_admin"},
    "strategy.approve": {"founder", "co_founder", "banker", "lead_banker", "fundos_admin"},
    "package.approve":  {"founder", "co_founder", "banker", "lead_banker", "fundos_admin"},
    "document.edit":    {"founder", "co_founder", "team", "banker", "lead_banker",
                         "fundos_admin"},
    "document.comment": {"founder", "co_founder", "team", "advisor", "banker",
                         "lead_banker", "fundos_admin"},
    "members.manage":   set(),   # owner-only (checked separately)
    "stage.override":   set(),   # owner-only
    "alerts.manage":    {"fundos_admin", "fundos_analyst"},
    "config.manage":    {"fundos_admin", "data_steward"},
    # View-layer aliases used by the M0–M3 endpoints.
    "ckb.edit":            {"founder", "co_founder", "team", "banker",
                            "lead_banker", "fundos_admin"},
    "ckb.assign":          {"founder", "co_founder", "banker", "lead_banker",
                            "fundos_admin"},
    "readiness.generate":  {"founder", "co_founder", "team", "banker",
                            "lead_banker", "fundos_admin", "fundos_analyst"},
    "strategy.edit":       {"founder", "co_founder", "team", "banker",
                            "lead_banker", "fundos_admin"},
    "materials.generate":  {"founder", "co_founder", "team", "banker",
                            "lead_banker", "fundos_admin"},
    "materials.approve":   {"founder", "co_founder", "banker", "lead_banker",
                            "fundos_admin"},
}

OWNER_ONLY_ACTIONS = {"members.manage", "stage.override"}


def get_deal(deal_id):
    from django.core.exceptions import ValidationError as DjangoValidationError

    from fundos.core.models import Deal
    try:
        # Tester Issue 3 (cross-tenant collaboration): the caller's JWT sets
        # the thread-local tenant to THEIR home tenant, but an invited
        # member's deal lives in the inviter's tenant — the tenant-scoped
        # manager would filter it out and turn a legitimate member into a
        # 403. Identity/authorization lookups therefore bypass the tenant
        # filter; authority comes from the membership check that follows,
        # and the request is re-scoped to the deal's tenant only AFTER that
        # check passes (require_membership).
        return Deal.all_objects.get(id=deal_id, is_deleted=False)
    except (Deal.DoesNotExist, DjangoValidationError, ValueError, TypeError):
        # Generic forbidden — never reveal existence (VAL-M0-001).
        # A malformed id (e.g. the literal "undefined" sent by a client with a
        # missing deal id) raises Django's ValidationError from the UUID field;
        # it must not surface as a 500.
        raise AuthzError()


def effective_roles(user, deal):
    """All active roles the user holds on the deal (deal + company scoped).

    Membership rows live in the DEAL's tenant; the requesting user may be
    homed in a different tenant (cross-tenant invite, Issue 3), so this
    lookup must not be filtered by the caller's tenant.
    """
    from fundos.core.models import Membership
    roles = set(
        Membership.all_objects.filter(
            user=user, status="active", is_deleted=False,
        ).filter(
            models_q_scope(deal)
        ).values_list("role", flat=True)
    )
    if user.is_internal:
        roles.add("fundos_analyst")
    return roles


def models_q_scope(deal):
    from django.db.models import Q
    return (Q(scope_type="deal", scope_id=deal.id) |
            Q(scope_type="company", scope_id=deal.company_id))


def is_deal_owner(user, deal):
    from fundos.core.models import Membership
    if deal.primary_owner_id == user.id:
        return True
    return Membership.all_objects.filter(
        user=user, status="active", is_secondary_owner=True,
        is_deleted=False,
    ).filter(models_q_scope(deal)).exists()


def require_membership(user, deal_id):
    """Assert membership(user, deal) else E-AUTHZ-403. Returns the deal.

    On success the request's tenant context is switched to the DEAL's
    tenant (Issue 3): authorization is by membership, and data scoping
    follows the resource, not the caller's home tenant. This is what lets
    an invited external collaborator actually load the deal they were
    invited to.
    """
    deal = get_deal(deal_id)
    roles = effective_roles(user, deal)
    if not roles and deal.primary_owner_id != user.id:
        raise AuthzError()
    from fundos.core import scoping
    scoping.set_current_tenant(deal.tenant_id)
    return deal


def require_action(user, deal, action):
    """Assert the RBAC matrix allows `action` for the user's effective roles."""
    if action in OWNER_ONLY_ACTIONS:
        if not is_deal_owner(user, deal):
            raise AuthzError()
        return
    allowed = ACTION_MATRIX.get(action, set())
    roles = effective_roles(user, deal)
    if deal.primary_owner_id == user.id:
        roles.add("founder")
    if roles & allowed:
        return
    raise AuthzError()
