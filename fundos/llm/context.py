"""
AI Context Engine (Doc 1 LLM-M0-002 / Doc 3 §6).

Every generation request is assembled automatically from the CKB + Strategy
Profile + Peer Universe + prior materials — the founder never writes a
prompt. This shared context is what keeps teaser/deck/model/IM/story
mutually consistent. Snapshots are persisted per generation for
transparency/audit (ai_context_snapshot, Doc 2 M3.1).
"""
import logging

logger = logging.getLogger(__name__)


def build(role: str, deal, *, include_materials: bool = True) -> dict:
    """Assemble the generation context for `role` on `deal`."""
    from fundos.core.services import ckb_service

    context = {
        "company": {"name": deal.company.name, "domain": deal.company.domain,
                    "hq_country": deal.company.hq_country},
        "deal": {"name": deal.name, "round_type": deal.round_type},
        "ckb": ckb_service.snapshot(deal.id),
    }

    try:
        from fundos.strategy.models import StrategyProfile
        profile = StrategyProfile.objects.filter(
            deal_id=deal.id, is_active=True, status="approved").first()
        if profile:
            context["strategy_profile"] = profile.as_context()
    except Exception as e:
        logger.debug("CONTEXT: strategy profile unavailable: %s", e)

    try:
        from fundos.strategy.models import PeerCompany, PeerUniverse
        universe = PeerUniverse.objects.filter(
            deal_id=deal.id, is_active=True).first()
        if universe:
            context["peer_universe"] = [
                {"name": p.name, "group": p.group_code, "sector": p.sector,
                 "stage": p.stage, "similarity": float(p.similarity_score or 0)}
                for p in PeerCompany.objects.filter(
                    peer_universe=universe).order_by("-similarity_score")[:20]
            ]
    except Exception as e:
        logger.debug("CONTEXT: peers unavailable: %s", e)

    try:
        from fundos.strategy.models import FounderObjectives
        obj = FounderObjectives.objects.filter(
            deal_id=deal.id, is_active=True).first()
        if obj:
            context["founder_objectives"] = {
                "purposes": obj.purposes, "timeline": obj.timeline,
                "runway": obj.runway, "maxDilutionPct": float(obj.max_dilution_pct or 0),
                "geographies": obj.geographies,
            }
    except Exception as e:
        logger.debug("CONTEXT: objectives unavailable: %s", e)

    try:
        from fundos.readiness.models import ReadinessAssessment
        assessment = ReadinessAssessment.objects.filter(
            deal_id=deal.id, is_active=True).first()
        if assessment:
            context["readiness"] = {"band": assessment.overall_band,
                                    "summary": assessment.summary[:2000]}
    except Exception as e:
        logger.debug("CONTEXT: readiness unavailable: %s", e)

    if include_materials:
        try:
            from fundos.readiness.models import MaterialAsset
            context["existing_materials"] = [
                {"category": m.category, "detectedType": m.detected_type,
                 "summary": (m.summary or "")[:500]}
                for m in MaterialAsset.objects.filter(deal_id=deal.id)[:15]
            ]
        except Exception as e:
            logger.debug("CONTEXT: materials unavailable: %s", e)

    return context


def snapshot(role: str, deal, workspace_id=None) -> dict:
    """Build + persist an ai_context_snapshot row (Doc 2 M3.1)."""
    context = build(role, deal)
    try:
        from fundos.materials.models import AIContextSnapshot
        AIContextSnapshot.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id,
            workspace_id=workspace_id, role=role, context_json=context)
    except Exception as e:
        logger.debug("CONTEXT: snapshot persist skipped: %s", e)
    return context
