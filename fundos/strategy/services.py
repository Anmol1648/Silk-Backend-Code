"""
Stage-2 services (Doc 1 M2 / Doc 8 §5): objectives → peers → raise →
valuation → instruments/dilution → blueprint → approve Strategy Profile.

Every generator: [DET] engine computes numbers, [AI] role explains them,
guardrails stamp disclaimers, artefacts persist the scoring_config version,
propagation marks dependents, everything audited.
"""
import logging

from django.db import transaction
from django.utils import timezone

from fundos.core.exceptions import ConflictError, DomainValidationError, PendingRecalc
from fundos.core.services import ckb_service, recalc
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)

def _usd_inr():
    """Indicative conversion for engine normalisation (settings-driven —
    Gap G13; the rate is surfaced to the UI as an explicit assumption)."""
    from fundos.core.services.currency import usd_inr_rate
    return usd_inr_rate()


def _flat_ckb(deal):
    flat = {}
    for group in ckb_service.snapshot(deal.id).values():
        for key, meta in group.items():
            flat[key] = meta["value"]
    return flat


def _active(model, deal_id):
    return model.objects.filter(deal_id=deal_id, is_active=True).first()


def _supersede(model, deal_id):
    prior = _active(model, deal_id)
    if prior:
        prior.is_active = False
        prior.save(update_fields=["is_active", "updated_at"])
        return prior.version_no + 1
    return 1


# ---------------------------------------------------------------------------
# M2.1 Objectives
# ---------------------------------------------------------------------------

@transaction.atomic
def set_objectives(deal, *, purposes, timeline, runway,
                   preferred_investor_types=None, geographies=None,
                   max_dilution_pct=None, user=None):
    """BR-M2-001/-002: versioned; a material change marks all downstream
    Stage-2 artefacts pending_recalc (never silently regenerates)."""
    from fundos.strategy.models import FounderObjectives

    if not purposes:
        raise DomainValidationError("At least one raise purpose is required.",
                                    fields={"purposes": "required"})
    if not timeline or not runway:
        raise DomainValidationError("Timeline and runway are required.",
                                    fields={"timeline": "required",
                                            "runway": "required"})

    version = _supersede(FounderObjectives, deal.id)
    objectives = FounderObjectives.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        purposes=purposes, timeline=timeline, runway=runway,
        preferred_investor_types=preferred_investor_types or [],
        geographies=geographies or [],
        max_dilution_pct=max_dilution_pct,
        created_by=user if getattr(user, "pk", None) else None)

    if version > 1:
        recalc.propagate_change(deal.id, "founder_objectives",
                                reason="objectives updated")
    audit("strategy.objectives.set", actor=user, deal_id=deal.id,
          entity="founder_objectives", entity_id=objectives.id,
          meta={"version": version})
    return objectives


# ---------------------------------------------------------------------------
# M2.2 Peer universe
# ---------------------------------------------------------------------------

# Fixture peer catalogue used until the market feed is ratified (Doc 7
# critical path). Replaced transparently by market_deal-driven candidates.
_FIXTURE_PEERS = [
    {"name": "Peer Alpha", "sector": "SaaS", "subsector": "Vertical SaaS",
     "business_model": "b2b_saas", "revenue": 180_000_000, "growth_rate": 90,
     "geography": "India", "funding_stage": "series_a", "team_size": 60,
     "stage": "Series A", "current_status": "operating",
     "milestones": [
         {"round": "Seed", "amount_usd": 1_200_000, "valuation_usd": 6_000_000,
          "arr": 40_000_000, "employees": 15},
         {"round": "Series A", "amount_usd": 7_000_000,
          "valuation_usd": 32_000_000, "arr": 160_000_000, "employees": 55}]},
    {"name": "Peer Beta", "sector": "SaaS", "subsector": "Horizontal SaaS",
     "business_model": "b2b_saas", "revenue": 90_000_000, "growth_rate": 130,
     "geography": "India", "funding_stage": "seed", "team_size": 30,
     "stage": "Seed", "current_status": "operating",
     "milestones": [
         {"round": "Seed", "amount_usd": 2_500_000, "valuation_usd": 11_000_000,
          "arr": 70_000_000, "employees": 25}]},
    {"name": "Peer Gamma", "sector": "Fintech", "subsector": "Payments",
     "business_model": "b2b_saas", "revenue": 300_000_000, "growth_rate": 60,
     "geography": "SEA", "funding_stage": "series_a", "team_size": 90,
     "stage": "Series A", "current_status": "operating",
     "milestones": [
         {"round": "Series A", "amount_usd": 9_000_000,
          "valuation_usd": 45_000_000, "arr": 250_000_000, "employees": 80}]},
    {"name": "Peer Delta", "sector": "SaaS", "subsector": "Vertical SaaS",
     "business_model": "b2b_saas", "revenue": 40_000_000, "growth_rate": 200,
     "geography": "India", "funding_stage": "pre_seed", "team_size": 12,
     "stage": "Pre-seed", "current_status": "operating", "milestones": []},
    {"name": "Peer Epsilon", "sector": "SaaS", "subsector": "DevTools",
     "business_model": "plg_saas", "revenue": 500_000_000, "growth_rate": 45,
     "geography": "US", "funding_stage": "series_b", "team_size": 200,
     "stage": "Series B", "current_status": "operating", "milestones": []},
]


# Group A is "Closest Comparables" — the peers a founder is invited to read
# as their reference set. Below this score a candidate is not a comparable,
# it is the least-unrelated row in the table, and saying otherwise is worse
# than saying nothing.
#
# SET THIS TO 0 to restore the pre-v29 behaviour, in which the top scorer was
# always promoted regardless of score. See `group_peers_by_score` for why the
# default changed and what it overrides.
PEER_PROMOTION_FLOOR = 45.0
PEER_GROUP_A = 60.0
PEER_GROUP_B = 35.0


def group_peers_by_score(scores, *, floor=None):
    """Assign A/B/C bands. Returns (groups, indices_promoted_into_A).

    v29 — THE UNCONDITIONAL PROMOTION IS GONE. THIS REVERSES AN ACCEPTED TEST.

    The previous implementation ended with `promoted = [0]`: when no candidate
    cleared the floor, the single top scorer was promoted into Group A
    regardless of its score. `_FIXTURE_PEERS` is four SaaS companies and one
    Fintech, so for a construction, electronics, logistics or agritech company
    that promoted an unrelated peer into "Closest Comparables" — and attached
    a rationale line asserting it was the closest available comparable.

    That is exactly the outcome the assessment path refuses to produce.
    `resolve_sub_sector` declines to fuzzy-match below a high floor on the
    stated grounds that putting a construction firm on a D2C benchmark group
    "produces a confident number against the wrong comparators, which is
    worse than a blank". Two halves of one product held opposite
    philosophies; only one of them had been audited.

    WHAT THIS OVERRIDES, AND WHY IT IS A DECISION RATHER THAN A FIX
    --------------------------------------------------------------
    `tests/api/test_issue_report_2076_fixes.py::PeerGroupA` asserted
    "Group A must never be empty" (Tester Issue 15 / TC-049). Its scenario is
    an **agritech** company against the SaaS fixture universe — precisely the
    out-of-sector case — so the old assertion could only be satisfied by
    fabricating a comparable. The two contracts cannot both hold.

    The complaint behind Issue 15 is real: an absolute >=60 bar leaves Group A
    empty for genuine near-misses, and an unexplained empty section is a bad
    experience. So the RELATIVE promotion survives — anything at or above the
    floor is still promoted. What does not survive is promotion at ANY score.
    An empty Group A with a logged reason is an honest answer; a Vertical SaaS
    company presented to an agritech founder as their closest comparable is a
    confident wrong one, and it is the kind of wrong that gets quoted in an
    investor conversation.

    An operator who disagrees can set `PEER_PROMOTION_FLOOR = 0`, which
    restores the old behaviour exactly. The default is the safe reading.

    Pure and dependency-free so the banding rule can be tested directly,
    rather than asserted against the source text of a 90-line function.
    """
    cutoff = PEER_PROMOTION_FLOOR if floor is None else floor
    groups = ["A" if s >= PEER_GROUP_A else "B" if s >= PEER_GROUP_B else "C"
              for s in scores]
    if not scores or "A" in groups:
        return groups, []
    promoted = [i for i, s in enumerate(scores) if s >= cutoff]
    for i in promoted[:3]:
        groups[i] = "A"
    return groups, promoted[:3]


@transaction.atomic
def generate_peers(deal, *, user=None):
    """[DET] similarity engine over candidates + [AI] insights.
    BR-S2-010: explainable scores; founder curation via add/remove APIs."""
    from fundos.engines.scoring import active_scoring_config
    from fundos.engines.similarity_engine import compute_similarity
    from fundos.config.models import SectorTaxonomyMap
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.strategy.models import (
        PeerCompany, PeerInsight, PeerMilestone, PeerUniverse,
    )

    cfg, cfg_id = active_scoring_config()
    flat = _flat_ckb(deal)
    company = {
        "sector": flat.get("sector"), "subsector": flat.get("subsector"),
        "business_model": flat.get("business_model"),
        "revenue": flat.get("arr") or flat.get("revenue"),
        "growth_rate": flat.get("growth_rate"),
        "geography": flat.get("geography") or "India",
        "funding_stage": deal.round_type,
        "team_size": flat.get("employees"),
    }
    adjacency = {row.founder_sector.lower(): row.adjacent_sectors
                 for row in SectorTaxonomyMap.objects.all()}

    version = _supersede(PeerUniverse, deal.id)
    universe = PeerUniverse.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        is_active=True, status="draft", generated_at=timezone.now(),
        created_by=user if getattr(user, "pk", None) else None)

    scored = []
    for candidate in _FIXTURE_PEERS:
        result = compute_similarity(company, candidate,
                                    cfg["similarity_weights"], adjacency)
        scored.append((result, candidate))
    scored.sort(key=lambda pair: -pair[0].score)

    # Tester Issue 15 (Round-1 TC-049): with a real founder profile the
    # absolute A-threshold (>=60) often leaves Group A empty — every peer
    # lands in B/C and "Closest Comparables" shows nothing. Grouping is now
    # relative as well as absolute: if nothing clears the A bar, the
    # closest peers (score >= 45, else the single top scorer) are promoted
    # to A with the promotion noted in their rationale.
    groups, promoted_ranks = group_peers_by_score([r.score for r, _ in scored])
    for i in promoted_ranks:
        scored[i][0].rationale += (" Promoted to Group A as the closest "
                                   "available comparable.")
    if scored and "A" not in groups:
        logger.info(
            "PEERS: no candidate reached the promotion floor (best %.1f of "
            "%.0f) — leaving Group A empty rather than presenting an "
            "unrelated company as the closest comparable. For a company "
            "outside the peer universe this is the correct result; load "
            "peers covering its sector to populate it.",
            scored[0][0].score, PEER_PROMOTION_FLOOR)

    for rank, (result, candidate) in enumerate(scored):
        group = groups[rank]
        peer = PeerCompany.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, peer_universe=universe,
            group_code=group, name=candidate["name"],
            sector=candidate.get("sector", ""),
            subsector=candidate.get("subsector", ""),
            stage=candidate.get("stage", ""),
            geography=candidate.get("geography", ""),
            current_status=candidate.get("current_status", ""),
            similarity_score=result.score,
            similarity_breakdown=result.breakdown,
            selection_rationale=result.rationale,
            origin="ai_recommended")
        for i, m in enumerate(candidate.get("milestones", [])):
            PeerMilestone.objects.create(
                tenant_id=deal.tenant_id, deal_id=deal.id, peer_company=peer,
                round=m.get("round", ""),
                amount_raised_value=m.get("amount_usd"),
                valuation_value=m.get("valuation_usd"),
                arr=m.get("arr"), employees=m.get("employees"), sort_order=i)

    # [AI] peer insights — fact/inference separated, evidence-backed.
    try:
        ctx = llm_context.build("peer_insight", deal)
        ctx["candidate_peers"] = [{"name": c["name"],
                                   "similarity": float(r.score)}
                                  for r, c in scored[:8]]
        ai = llm_generate(
            role="peer_insight",
            system=("You are a venture analyst. Produce evidence-backed peer "
                    "insights. Mark each insight kind as 'fact' (from given "
                    "data) or 'inference'. Return JSON: {\"insights\": "
                    "[{text, kind, evidence}]}. Never invent funding numbers."),
            prompt="Generate peer landscape insights.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="strategy.peers")
        ai = guardrail.apply("peer_insight", ai)
        for item in ai.get("insights", []):
            PeerInsight.objects.create(
                tenant_id=deal.tenant_id, deal_id=deal.id,
                peer_universe=universe, text=item.get("text", ""),
                kind=item.get("kind", "inference"),
                evidence=item.get("evidence", []))
    except Exception as e:
        logger.error("PEERS: insight generation failed: %s", e)

    recalc.clear(deal.id, "peer_universe")
    audit("strategy.peers.generated", actor=user, deal_id=deal.id,
          entity="peer_universe", entity_id=universe.id,
          meta={"version": version, "count": len(scored)})
    _recompute(deal)
    return universe


def peer_stats(deal_id) -> dict:
    """Derive engine inputs from the active Group-A peer set."""
    from statistics import median
    from fundos.strategy.models import PeerCompany, PeerMilestone, PeerUniverse

    universe = PeerUniverse.objects.filter(deal_id=deal_id,
                                           is_active=True).first()
    if not universe:
        return {}
    peers = PeerCompany.objects.filter(peer_universe=universe, group_code="A")
    milestones = PeerMilestone.objects.filter(
        peer_company__in=peers, amount_raised_value__isnull=False)
    raises = [float(m.amount_raised_value) for m in milestones]
    valuations = [float(m.valuation_value) for m in milestones
                  if m.valuation_value]
    stats = {}
    if raises:
        stats["median_raise_usd"] = median(raises)
    if valuations:
        stats["stage_postmoney_median_usd"] = median(valuations)
        stats["trading_valuations_usd"] = valuations
        stats["transaction_valuations_usd"] = valuations
    return stats


# ---------------------------------------------------------------------------
# M2.3 Ideal raise
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_raise(deal, *, user=None):
    from fundos.engines.raise_engine import compute_raise
    from fundos.engines.scoring import active_scoring_config
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.strategy.models import (
        FounderObjectives, RaiseRecommendation, RaiseScenario,
    )

    objectives = _active(FounderObjectives, deal.id)
    if not objectives:
        raise DomainValidationError(
            "Capture founder objectives before generating the raise.",
            fields={"objectives": "missing"})

    cfg, cfg_id = active_scoring_config()
    result = compute_raise(
        _flat_ckb(deal),
        {"purposes": objectives.purposes, "timeline": objectives.timeline,
         "runway": objectives.runway,
         "maxDilutionPct": float(objectives.max_dilution_pct or 0) or None},
        peer_stats(deal.id), cfg, usd_inr=_usd_inr())

    narrative = ""
    try:
        ctx = llm_context.build("raise_narrative", deal)
        ctx["deterministic_result"] = {
            "recommendedUsd": result.recommended_usd,
            "range": [result.low_usd, result.high_usd],
            "dilutionRangePct": [result.dilution_low_pct,
                                 result.dilution_high_pct],
            "reasoning": result.reasoning,
        }
        ai = llm_generate(
            role="raise_narrative",
            system=("You are an investment banker. Explain the deterministic "
                    "raise recommendation in plain language. Use the given "
                    "numbers verbatim — never new amounts. Separate facts, "
                    "inferences, assumptions. Return JSON per the "
                    "raise_narrative schema."),
            prompt="Explain the raise recommendation.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="strategy.raise")
        ai = guardrail.apply("raise_narrative", ai)
        narrative = ai.get("narrative", "")
    except Exception as e:
        logger.error("RAISE: narrative failed: %s", e)

    version = _supersede(RaiseRecommendation, deal.id)
    recommendation = RaiseRecommendation.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        recommended_value=result.recommended_usd,
        low_value=result.low_usd, high_value=result.high_usd,
        confidence=result.confidence,
        dilution_low_pct=result.dilution_low_pct,
        dilution_high_pct=result.dilution_high_pct,
        recommended_timeline=result.recommended_timeline,
        funding_band=result.funding_band,
        dimension_scores=result.dimension_scores,
        reasoning=result.reasoning, risks=result.risks, narrative=narrative,
        scoring_config_version_id=cfg_id,
        created_by=user if getattr(user, "pk", None) else None)
    for s in result.scenarios:
        RaiseScenario.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id,
            raise_recommendation=recommendation, label=s.label,
            raise_value=s.raise_usd, dilution_pct=s.dilution_pct,
            runway_months=s.runway_months, assessment=s.assessment,
            is_selected=(s.label == "Recommended"))

    recalc.clear(deal.id, "raise_recommendation")
    audit("strategy.raise.generated", actor=user, deal_id=deal.id,
          entity="raise_recommendation", entity_id=recommendation.id,
          meta={"version": version})
    _recompute(deal)
    return recommendation


# ---------------------------------------------------------------------------
# M2.4 Valuation
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_valuation(deal, *, user=None):
    from fundos.engines.dilution_engine import dilution_matrix
    from fundos.engines.scoring import active_scoring_config
    from fundos.engines.valuation_engine import compute_valuation
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.strategy.models import (
        RaiseRecommendation, Valuation, ValuationDilutionMatrix,
        ValuationMethodResult, ValuationNegotiation,
    )

    # Gap G5 (ordering): enforce the documented generation order — the
    # valuation is anchored to the raise; block until a current raise exists.
    if not _active(RaiseRecommendation, deal.id):
        raise ConflictError(
            "Generate the ideal raise before the valuation "
            "(objectives → peers → raise → valuation).")
    recalc.assert_not_pending(deal.id, "raise_recommendation")
    cfg, cfg_id = active_scoring_config()
    result = compute_valuation(_flat_ckb(deal), peer_stats(deal.id), cfg,
                               usd_inr=_usd_inr())

    # Gap G5 (degenerate result): never persist an all-methods-inapplicable
    # zero range — the API previously returned 0/0 happily and the Strategy
    # Profile then froze a zero valuation (G7).
    if not any(m.applicable for m in result.football_field) \
            or not result.range_high or float(result.range_high) <= 0:
        raise DomainValidationError(
            "Insufficient financial inputs for a valuation — complete "
            "ARR/revenue in the Company Knowledge Base, then regenerate.",
            fields={"ckb.arr": "required", "valuation": "needs_financials"})

    version = _supersede(Valuation, deal.id)
    valuation = Valuation.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        range_low_value=result.range_low, range_high_value=result.range_high,
        confidence=result.confidence,
        football_field=[{
            "method": m.method, "low": m.low, "mid": m.mid, "high": m.high,
            "weight": m.weight, "applicable": m.applicable,
            "isOutlier": m.is_outlier, "outlierReason": m.outlier_reason,
        } for m in result.football_field],
        assumptions=(list(result.assumptions or [])
                     + [f"USD/INR conversion rate assumed: {_usd_inr():g} "
                        f"(engines compute in USD; amounts presented in INR)"]),
        limitations=result.limitations,
        scoring_config_version_id=cfg_id,
        created_by=user if getattr(user, "pk", None) else None)

    for m in result.football_field:
        ValuationMethodResult.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, valuation=valuation,
            method=m.method, low_value=m.low, mid_value=m.mid,
            high_value=m.high, weight=m.weight, is_outlier=m.is_outlier,
            outlier_reason=m.outlier_reason, assumptions=m.assumptions,
            applicable=m.applicable)

    # Valuation-vs-dilution grid around the recommended raise.
    recommendation = _active(RaiseRecommendation, deal.id)
    if recommendation and result.range_high:
        raise_points = [float(recommendation.low_value or 0),
                        float(recommendation.recommended_value or 0),
                        float(recommendation.high_value or 0)]
        raise_points = [p for p in raise_points if p > 0]
        span = result.range_high - result.range_low
        valuation_points = [result.range_low,
                            result.range_low + span / 2,
                            result.range_high] if span > 0 else [result.range_high]
        cap_table = _cap_table(deal)
        for cell in dilution_matrix(raise_points, valuation_points, cap_table,
                                    cfg["dilution"]["esop_topup_default_pct"]):
            ValuationDilutionMatrix.objects.create(
                tenant_id=deal.tenant_id, deal_id=deal.id, valuation=valuation,
                raise_value=cell["raise"], valuation_value=cell["valuation"],
                dilution_pct=cell["dilutionPct"],
                founder_ownership_pct=cell["founderOwnershipPct"],
                investor_ownership_pct=cell["investorOwnershipPct"],
                esop_impact_pct=cell["esopImpactPct"],
                existing_investor_dilution_pct=cell["existingInvestorDilutionPct"])

    # [AI] negotiation preparation — SEBI-guardrailed.
    try:
        ctx = llm_context.build("valuation_negotiation", deal)
        ctx["deterministic_result"] = {
            "rangeUsd": [result.range_low, result.range_high],
            "methods": [m.method for m in result.football_field if m.applicable],
        }
        ai = llm_generate(
            role="valuation_negotiation",
            system=("You are a negotiation coach for founders. Given the "
                    "deterministic valuation range, produce supporting "
                    "arguments, likely investor objections and suggested "
                    "responses, plus a peer-anchored positioning statement. "
                    "Use given numbers verbatim. Return JSON per the "
                    "valuation_negotiation schema."),
            prompt="Prepare valuation negotiation guidance.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="strategy.valuation")
        ai = guardrail.apply("valuation_negotiation", ai)
        valuation.positioning_statement = ai.get("positioning_statement", "")
        valuation.save(update_fields=["positioning_statement", "updated_at"])
        for kind, key in (("supporting_argument", "supporting_arguments"),
                          ("investor_objection", "investor_objections"),
                          ("suggested_response", "suggested_responses")):
            for i, item in enumerate(ai.get(key, [])):
                ValuationNegotiation.objects.create(
                    tenant_id=deal.tenant_id, deal_id=deal.id,
                    valuation=valuation, kind=kind,
                    text=item.get("text", "") if isinstance(item, dict) else str(item),
                    evidence=item.get("evidence", []) if isinstance(item, dict) else [],
                    sort_order=i)
    except Exception as e:
        logger.error("VALUATION: negotiation guidance failed: %s", e)

    recalc.clear(deal.id, "valuation")
    audit("strategy.valuation.generated", actor=user, deal_id=deal.id,
          entity="valuation", entity_id=valuation.id, meta={"version": version})
    _recompute(deal)
    return valuation


def _cap_table(deal):
    flat = _flat_ckb(deal)
    raw = flat.get("cap_table")
    if isinstance(raw, list):
        table = []
        for h in raw:
            if isinstance(h, dict) and "pct" in h:
                table.append({"holder": h.get("holder", "?"),
                              "pct": float(h["pct"]),
                              "kind": h.get("kind", "founder")})
        if table:
            return table
    return [{"holder": "Founders", "pct": 100.0, "kind": "founder"}]


# ---------------------------------------------------------------------------
# M2.5 Instruments + dilution simulator
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_instruments(deal, *, user=None):
    """BR-M2-050: options with pros/cons, never a prescription."""
    from fundos.config.feature_flags import enable_instrument_selection
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.strategy.models import InstrumentOption

    if not enable_instrument_selection():
        raise DomainValidationError("Instrument selection is disabled.")

    ctx = llm_context.build("instrument_explainer", deal)
    ai = llm_generate(
        role="instrument_explainer",
        system=("You are a startup-financing educator. Compare instruments "
                "(equity, SAFE, convertible note, CCD as applicable to the "
                "company's jurisdiction) with pros, cons, mechanics, and "
                "market context. NEVER recommend one — present options only. "
                "Return JSON per the instrument_explainer schema."),
        prompt="Explain the instrument options for this raise.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="strategy.instruments")
    ai = guardrail.apply("instrument_explainer", ai)

    InstrumentOption.objects.filter(deal_id=deal.id).delete()
    options = []
    for item in ai.get("instruments", []):
        options.append(InstrumentOption.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id,
            instrument=item.get("instrument", "")[:32],
            pros=item.get("pros", []), cons=item.get("cons", []),
            mechanics_explainer=item.get("mechanics_explainer", ""),
            market_context=item.get("market_context", ""),
            applicability_note=item.get("applicability_note", ""),
            created_by=user if getattr(user, "pk", None) else None))
    audit("strategy.instruments.generated", actor=user, deal_id=deal.id,
          entity="instrument_option", meta={"count": len(options)})
    _recompute(deal)
    return options, ai.get("disclaimer", "")


@transaction.atomic
def simulate_dilution(deal, *, instrument, raise_usd, valuation_low_usd,
                      valuation_high_usd, user=None):
    """[DET] simulator — persists a DilutionScenario row (ranges only)."""
    from fundos.config.feature_flags import enable_dilution_simulator
    from fundos.engines.dilution_engine import simulate_dilution as engine_sim
    from fundos.engines.scoring import active_scoring_config
    from fundos.strategy.models import DilutionScenario

    if not enable_dilution_simulator():
        raise DomainValidationError("The dilution simulator is disabled.")
    if raise_usd <= 0 or valuation_low_usd <= 0 or valuation_high_usd <= 0:
        raise DomainValidationError("Raise and valuation must be positive.",
                                    fields={"raise": "positive"})
    if valuation_low_usd > valuation_high_usd:
        raise DomainValidationError(
            "Valuation low must not exceed valuation high.",
            fields={"valuation": "range_order"})

    cfg, _ = active_scoring_config()
    result = engine_sim(raise_usd, valuation_low_usd, valuation_high_usd,
                        instrument, _cap_table(deal), cfg)
    scenario = DilutionScenario.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, instrument=instrument,
        raise_value=raise_usd, valuation_low_value=valuation_low_usd,
        valuation_high_value=valuation_high_usd,
        pre_money=result.pre_money, post_money=result.post_money,
        ownership_bands=result.ownership_bands,
        conversion_assumptions=result.conversion_assumptions,
        created_by=user if getattr(user, "pk", None) else None)
    _recompute(deal)
    return scenario


# ---------------------------------------------------------------------------
# M2.6 Blueprint
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_blueprint(deal, *, user=None):
    """Assemble the strategy blueprint: [DET] numbers + [AI] narrative +
    ≥3 alternatives (Doc 2 M2.6)."""
    from fundos.engines.scoring import active_scoring_config
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.strategy.models import (
        FundraisingStrategy, RaiseRecommendation, RaiseScenario,
        StrategyAlternative, Valuation,
    )

    recommendation = _active(RaiseRecommendation, deal.id)
    valuation = _active(Valuation, deal.id)
    if not recommendation or not valuation:
        raise DomainValidationError(
            "Generate the raise and valuation before the blueprint.",
            fields={"raise": "missing" if not recommendation else "ok",
                    "valuation": "missing" if not valuation else "ok"})
    recalc.assert_not_pending(deal.id, "raise_recommendation")
    recalc.assert_not_pending(deal.id, "valuation")

    cfg, cfg_id = active_scoring_config()

    narrative = {"executive_summary": "", "why_this_strategy": "",
                 "risks": [], "success_factors": [], "next_milestones": [],
                 "current_position": {}}
    try:
        ctx = llm_context.build("strategy_blueprint", deal)
        ctx["deterministic_inputs"] = {
            "raise": {"recommendedUsd": float(recommendation.recommended_value or 0),
                      "band": recommendation.funding_band},
            "valuationRangeUsd": [float(valuation.range_low_value or 0),
                                  float(valuation.range_high_value or 0)],
        }
        ai = llm_generate(
            role="strategy_blueprint",
            system=("You are a fundraising strategist. Assemble a strategy "
                    "blueprint narrative from the deterministic inputs — use "
                    "the given numbers verbatim. Return JSON per the "
                    "strategy_blueprint schema."),
            prompt="Assemble the fundraising strategy blueprint.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="strategy.blueprint")
        narrative = guardrail.apply("strategy_blueprint", ai)
    except Exception as e:
        logger.error("BLUEPRINT: narrative failed: %s", e)

    version = _supersede(FundraisingStrategy, deal.id)
    strategy = FundraisingStrategy.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        executive_summary=narrative.get("executive_summary", ""),
        current_position=narrative.get("current_position", {}),
        recommended_raise={
            "value": float(recommendation.recommended_value or 0),
            "ccy": recommendation.recommended_ccy,
            "range": [float(recommendation.low_value or 0),
                      float(recommendation.high_value or 0)]},
        recommended_valuation={
            "low": float(valuation.range_low_value or 0),
            "high": float(valuation.range_high_value or 0),
            "ccy": valuation.range_low_ccy},
        why_this_strategy=narrative.get("why_this_strategy", ""),
        risks=narrative.get("risks", []),
        success_factors=narrative.get("success_factors", []),
        next_milestones=narrative.get("next_milestones", []),
        scoring_config_version_id=cfg_id, status="draft",
        created_by=user if getattr(user, "pk", None) else None)

    # ≥3 alternatives from the raise scenarios.
    scenarios = RaiseScenario.objects.filter(
        raise_recommendation=recommendation).order_by("raise_value")
    for s in scenarios:
        StrategyAlternative.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id,
            fundraising_strategy=strategy, label=s.label,
            description=s.assessment, raise_value=s.raise_value,
            dilution_pct=s.dilution_pct, runway_months=s.runway_months,
            advantages=["Less dilution"] if s.label == "Conservative"
            else ["Longer runway"] if s.label == "Aggressive"
            else ["Balanced milestone coverage"],
            disadvantages=["Shorter runway"] if s.label == "Conservative"
            else ["More dilution"] if s.label == "Aggressive" else [],
            execution_complexity={"Conservative": "low", "Recommended": "medium",
                                  "Aggressive": "high"}.get(s.label, "medium"),
            probability_of_success={"Conservative": "high",
                                    "Recommended": "high",
                                    "Aggressive": "medium"}.get(s.label, "medium"),
            is_selected=(s.label == "Recommended"))

    recalc.clear(deal.id, "fundraising_strategy")
    audit("strategy.blueprint.generated", actor=user, deal_id=deal.id,
          entity="fundraising_strategy", entity_id=strategy.id,
          meta={"version": version})
    _recompute(deal)
    return strategy


# ---------------------------------------------------------------------------
# M2.7 Strategy Profile approval — the hard-gate key
# ---------------------------------------------------------------------------

@transaction.atomic
def approve_strategy_profile(deal, *, user, selected_alternative_id=None,
                             selected_instrument="", key_positioning="",
                             principal_investment_thesis=""):
    """BR-M2-070/-071 + BR-M0-020: creates the immutable approved profile,
    derives investor categories [DET], opens the Stage-3 hard gate, emits
    fundos.strategy.approved."""
    from fundos.config.models import InvestorCategoryMap
    from fundos.engines.dilution_engine import derive_investor_categories
    from fundos.strategy.models import (
        FundraisingStrategy, PeerUniverse, RaiseRecommendation,
        StrategyAlternative, StrategyProfile, Valuation,
    )

    strategy = _active(FundraisingStrategy, deal.id)
    recommendation = _active(RaiseRecommendation, deal.id)
    valuation = _active(Valuation, deal.id)
    if not (strategy and recommendation and valuation):
        raise ConflictError(
            "Blueprint, raise and valuation must exist before approval.")
    for artefact in ("raise_recommendation", "valuation",
                     "fundraising_strategy"):
        recalc.assert_not_pending(deal.id, artefact)

    # Gap G7: pre-approval invariant — a profile must never freeze a
    # zero/degenerate valuation range (a direct consequence of G5), which
    # would poison every Stage-3 document referencing it.
    if float(valuation.range_high_value or 0) <= 0 \
            or float(valuation.range_low_value or 0) < 0:
        raise ConflictError(
            "Cannot approve the strategy: the valuation range is zero or "
            "not meaningful. Complete ARR/revenue in the CKB and regenerate "
            "the valuation before approving.")

    alternative = None
    if selected_alternative_id:
        alternative = StrategyAlternative.objects.filter(
            id=selected_alternative_id,
            fundraising_strategy=strategy).first()
        if not alternative:
            raise DomainValidationError("Unknown strategy alternative.",
                                        fields={"selectedAlternativeId": "unknown"})
    else:
        alternative = StrategyAlternative.objects.filter(
            fundraising_strategy=strategy, is_selected=True).first()

    approved_raise = float((alternative.raise_value if alternative else None)
                           or recommendation.recommended_value or 0)
    if approved_raise <= 0:
        raise ConflictError(
            "Cannot approve the strategy: the approved raise amount is "
            "zero. Regenerate the raise recommendation first.")

    flat = _flat_ckb(deal)
    category_rows = [{
        "round_type": r.round_type, "band_low_usd": float(r.band_low_usd),
        "band_high_usd": float(r.band_high_usd), "primary": r.primary,
        "secondary": r.secondary, "optional": r.optional,
    } for r in InvestorCategoryMap.objects.all()]
    categories = derive_investor_categories(
        deal.round_type, approved_raise, category_rows,
        growth_rate=flat.get("growth_rate"))

    version = _supersede(StrategyProfile, deal.id)
    # Prior active version becomes superseded (kept for history).
    StrategyProfile.all_objects.filter(
        deal_id=deal.id, is_deleted=False, status="approved",
        is_active=False).update(status="superseded")

    profile = StrategyProfile.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version,
        is_active=True, status="approved",
        approved_raise_value=approved_raise,
        val_range_low_value=valuation.range_low_value,
        val_range_high_value=valuation.range_high_value,
        peer_universe=_active(PeerUniverse, deal.id),
        preferred_investor_categories=categories,
        fundraising_timeline=recommendation.recommended_timeline,
        key_positioning=key_positioning or valuation.positioning_statement,
        principal_investment_thesis=principal_investment_thesis,
        risks_mitigations=strategy.risks,
        approved_assumptions=valuation.assumptions,
        selected_alternative=alternative,
        selected_instrument=selected_instrument,
        approval_timestamp=timezone.now(), approved_by=user,
        created_by=user)

    strategy.status = "approved"
    strategy.save(update_fields=["status", "updated_at"])

    audit("strategy.profile.approved", actor=user, deal_id=deal.id,
          entity="strategy_profile", entity_id=profile.id,
          meta={"version": version, "approvedRaiseUsd": approved_raise})

    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.strategy.approved", {
        "deal_id": str(deal.id), "company": deal.company.name,
        "raise_usd": approved_raise, "tenant_id": str(deal.tenant_id)})

    _recompute(deal)   # opens the Stage-3 hard gate
    return profile


def _recompute(deal):
    from fundos.core.services.stage_state import recompute_stage_completion
    recompute_stage_completion(deal.id)
