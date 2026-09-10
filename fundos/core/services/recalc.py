"""
Dependency & recalculation propagation (Doc 1 M0-§5 / Doc 4 Appendix 4-C).

BR-M0-040: a dependency graph links artefacts (CKB field → peer universe →
raise → valuation → strategy → each Stage-3 document). When a node changes
materially, descendants are marked "pending_recalc" (analytical outputs) or
"needs_review" (narrative documents). Nothing is auto-regenerated for
representational content.

BR-M0-041: approved/locked versions are never overwritten by propagation.
"""
import logging

from fundos.core.scoping import get_current_tenant

logger = logging.getLogger(__name__)

# Static downstream map (artefact type → descendants). The dependency_edge
# table augments this with per-instance edges (e.g. a specific document).
DOWNSTREAM = {
    "ckb":                ["readiness_assessment", "peer_universe", "raise_recommendation",
                           "valuation", "fundraising_strategy",
                           "investment_story", "teaser", "pitch_deck",
                           "financial_model", "investment_memorandum"],
    "material_asset":     ["readiness_assessment"],
    "founder_objectives": ["peer_universe", "raise_recommendation", "valuation",
                           "fundraising_strategy"],
    "peer_universe":      ["raise_recommendation", "valuation", "fundraising_strategy",
                           "investment_story"],
    "raise_recommendation": ["valuation", "fundraising_strategy"],
    "valuation":          ["fundraising_strategy"],
    "fundraising_strategy": [],
    "strategy_profile":   ["investment_story", "teaser", "pitch_deck",
                           "financial_model", "investment_memorandum"],
    "investment_story":   ["teaser", "pitch_deck", "investment_memorandum"],
    "financial_model":    ["investment_memorandum", "package_review"],
}

# Narrative artefacts get "needs_review"; analytical get "pending_recalc".
NARRATIVE_TYPES = {"investment_story", "teaser", "pitch_deck",
                   "investment_memorandum"}


def _tenant_for(deal_id):
    """Resolve the tenant from the DEAL, falling back to the request scope.

    `get_current_tenant()` is populated by request middleware, so it is None
    in a management command, a Celery task and a shell — and the write then
    dies on a not-null constraint. That failure is caught upstream and
    logged, which means propagation silently stops: downstream artefacts are
    never flagged stale and the deal team goes on reading a memo built from
    figures that have since changed.

    The deal already carries the tenant, so ask it first and treat the
    request scope as the fallback rather than the source of truth.
    """
    from fundos.core.models import Deal
    tenant = (Deal.objects.filter(id=deal_id)
              .values_list("tenant_id", flat=True).first())
    return tenant or get_current_tenant()


def mark(deal_id, artifact_type, artifact_id=None, status="pending_recalc",
         reason=""):
    from fundos.core.models import ArtifactStatus
    tenant = _tenant_for(deal_id)
    if tenant is None:
        # Better a loud skip than a not-null crash swallowed by a caller.
        logger.error(
            "RECALC: no tenant could be resolved for deal %s — %s was NOT "
            "marked %s. Downstream artefacts will not show as stale.",
            deal_id, artifact_type, status)
        return None
    obj, _ = ArtifactStatus.objects.update_or_create(
        deal_id=deal_id, artifact_type=artifact_type, artifact_id=artifact_id,
        defaults={"status": status, "reason": reason[:255],
                  "tenant_id": tenant},
    )
    return obj


def clear(deal_id, artifact_type, artifact_id=None):
    from fundos.core.models import ArtifactStatus
    ArtifactStatus.objects.filter(
        deal_id=deal_id, artifact_type=artifact_type, artifact_id=artifact_id,
    ).update(status="current", reason="")


def status_of(deal_id, artifact_type, artifact_id=None):
    from fundos.core.models import ArtifactStatus
    row = ArtifactStatus.objects.filter(
        deal_id=deal_id, artifact_type=artifact_type, artifact_id=artifact_id,
    ).first()
    return row.status if row else "current"


def propagate_change(deal_id, changed_type, reason=""):
    """Mark all descendants of `changed_type` pending/needs-review."""
    seen = set()
    frontier = list(DOWNSTREAM.get(changed_type, []))
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        status = "needs_review" if node in NARRATIVE_TYPES else "pending_recalc"
        mark(deal_id, node, status=status,
             reason=reason or f"{changed_type} changed")
        frontier.extend(DOWNSTREAM.get(node, []))
    logger.info("RECALC: %s change on deal %s propagated to %s",
                changed_type, deal_id, sorted(seen))
    return seen


def assert_not_pending(deal_id, artifact_type, artifact_id=None):
    """Raise E-PENDING-409 if the artefact is pending recalculation."""
    from fundos.core.exceptions import PendingRecalc
    if status_of(deal_id, artifact_type, artifact_id) == "pending_recalc":
        raise PendingRecalc(
            f"{artifact_type} is pending recalculation; regenerate first.")
