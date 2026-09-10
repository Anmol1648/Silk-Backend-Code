"""
M2 API (Doc 4 /strategy/*): objectives, peers, raise, valuation,
instruments, dilution simulator, blueprint, strategy-profile approval.

Generators run async (202 + poll GET); dev runs them eagerly.
"""
import logging

from rest_framework import status
from rest_framework.response import Response

from fundos.core.api.base import DealScopedAPIView
from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.strategy import services

logger = logging.getLogger(__name__)


def _money(value, ccy="USD", basis=""):
    """Gap G13: stored engine amounts are USD; the presentation edge
    converts to the configured presentation currency (INR for the India
    market) and keeps the underlying USD value visible."""
    from fundos.core.services.currency import present_money
    if ccy and ccy.upper() != "USD" and value is not None:
        # Already presentation-native (rare) — pass through honestly.
        return {"value": float(value), "ccy": ccy.upper(),
                **({"basis": basis} if basis else {})}
    return present_money(value, basis=basis)


def _fx():
    from fundos.core.services.currency import fx_block
    return fx_block()


def _queue_generation(view, request, kind, task):
    """Gap G6: every generator returns a pollable job handle."""
    from fundos.core.services import jobs
    job = jobs.create_job(view.deal, kind, request.user)
    task.delay(str(view.deal.tenant_id), str(view.deal.id),
               str(request.user.id), job_id=str(job.id))
    job.refresh_from_db()   # dev runs eagerly — reflect the final status
    return Response({"status": "queued", "jobId": str(job.id),
                     "jobStatus": job.status,
                     "poll": f"/api/v1/deals/{view.deal.id}/jobs/{job.id}"},
                    status=status.HTTP_202_ACCEPTED)



class _GenerateThrottledMixin:
    def get_throttles(self):
        from fundos.core.throttling import GenerateThrottle
        return [GenerateThrottle()]

class ObjectivesView(DealScopedAPIView):
    required_action = "strategy.edit"

    def get(self, request, deal_id):
        from fundos.strategy.models import FounderObjectives
        obj = FounderObjectives.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not obj:
            raise NotFoundInDeal("Objectives not captured yet.")
        return Response({
            "versionNo": obj.version_no, "purposes": obj.purposes,
            "timeline": obj.timeline, "runway": obj.runway,
            "preferredInvestorTypes": obj.preferred_investor_types,
            "geographies": obj.geographies,
            "maxDilutionPct": float(obj.max_dilution_pct)
            if obj.max_dilution_pct is not None else None})

    def put(self, request, deal_id):
        obj = services.set_objectives(
            self.deal, purposes=request.data.get("purposes") or [],
            timeline=request.data.get("timeline", ""),
            runway=request.data.get("runway", ""),
            preferred_investor_types=request.data.get("preferredInvestorTypes"),
            geographies=request.data.get("geographies"),
            max_dilution_pct=request.data.get("maxDilutionPct"),
            user=request.user)
        return Response({"versionNo": obj.version_no},
                        status=status.HTTP_201_CREATED)

    # Gap G9: POST accepted as an alias for the PUT upsert — several
    # frontends guessed POST and got a confusing 405.
    def post(self, request, deal_id):
        return self.put(request, deal_id)


class PeersGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        from fundos.strategy.tasks import generate_peers_task
        return _queue_generation(self, request, "peers", generate_peers_task)


class PeersView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.strategy.models import PeerUniverse
        universe = PeerUniverse.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not universe:
            raise NotFoundInDeal("No peer universe yet.")
        return Response({
            "versionNo": universe.version_no, "status": universe.status,
            "peers": [{
                # `peerId` mirrors `id` — several frontend builds read
                # `peerId` and ended up issuing `/peers/undefined` calls
                # (QA BUG-012 / BUG-013); serve both names permanently.
                "id": str(p.id), "peerId": str(p.id),
                "group": p.group_code, "groupCode": p.group_code,
                "name": p.name,
                "sector": p.sector, "subsector": p.subsector,
                "stage": p.stage, "geography": p.geography,
                "currentStatus": p.current_status,
                "similarityScore": float(p.similarity_score or 0),
                "similarityBreakdown": p.similarity_breakdown,
                "rationale": p.selection_rationale,
                "selectionRationale": p.selection_rationale,
                "origin": p.origin,
                "milestones": [{
                    "round": m.round,
                    "amountRaised": _money(m.amount_raised_value,
                                           m.amount_raised_ccy),
                    "valuation": _money(m.valuation_value, m.valuation_ccy,
                                        m.valuation_basis),
                    "arr": float(m.arr) if m.arr is not None else None,
                    "employees": m.employees,
                } for m in p.milestones.all()],
            } for p in universe.peers.all().order_by("-similarity_score")],
            "insights": [{
                "text": i.text, "kind": i.kind, "evidence": i.evidence,
                "label": "AI Generated Insights",
            } for i in universe.insights.all()]})


class PeerCurateView(DealScopedAPIView):
    """Founder add/remove/regroup peers (BR-M2-011 curation)."""
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        from fundos.core.services import recalc
        from fundos.strategy.models import PeerCompany, PeerUniverse
        universe = PeerUniverse.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not universe:
            raise NotFoundInDeal("No peer universe yet.")
        action = request.data.get("action")
        if action == "add":
            peer = PeerCompany.objects.create(
                tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
                peer_universe=universe, name=request.data.get("name", ""),
                group_code=request.data.get("group", "B"),
                sector=request.data.get("sector", ""),
                origin="founder_added",
                selection_rationale="Added by founder",
                created_by=request.user)
            recalc.propagate_change(self.deal.id, "peer_universe",
                                    reason="peer added")
            # Echo the full row so the client can render it immediately
            # without a page reload (QA BUG-011).
            return Response({"id": str(peer.id), "peerId": str(peer.id),
                             "name": peer.name, "group": peer.group_code,
                             "groupCode": peer.group_code,
                             "sector": peer.sector, "origin": peer.origin,
                             "selectionRationale": peer.selection_rationale,
                             "similarityScore": None},
                            status=status.HTTP_201_CREATED)
        if action == "remove":
            peer = PeerCompany.objects.filter(
                id=request.data.get("peerId"),
                peer_universe=universe).first()
            if not peer:
                raise NotFoundInDeal("Peer not found.")
            # BR-S2-013: never drop below 3 comparables.
            if PeerCompany.objects.filter(
                    peer_universe=universe).count() <= 3:
                from fundos.core.exceptions import ConflictError
                raise ConflictError(
                    "At least 3 comparable peers are required (BR-S2-013) "
                    "— add a replacement before removing this one.")
            peer.delete()
            recalc.propagate_change(self.deal.id, "peer_universe",
                                    reason="peer removed")
            return Response(status=status.HTTP_204_NO_CONTENT)
        if action == "approve":
            universe.status = "approved"
            universe.save(update_fields=["status", "updated_at"])
            return Response({"status": "approved"})
        raise DomainValidationError("action must be add|remove|approve.",
                                    fields={"action": "invalid"})


class RaiseGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        from fundos.strategy.models import FounderObjectives
        if not FounderObjectives.objects.filter(deal_id=self.deal.id,
                                                is_active=True).exists():
            raise DomainValidationError(
                "Capture founder objectives before generating the raise.",
                fields={"objectives": "missing"})
        from fundos.strategy.tasks import generate_raise_task
        return _queue_generation(self, request, "raise", generate_raise_task)


class RaiseView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.strategy.models import RaiseRecommendation
        rec = RaiseRecommendation.objects.filter(deal_id=self.deal.id,
                                                 is_active=True).first()
        if not rec:
            raise NotFoundInDeal("No raise recommendation yet.")
        return Response({
            "versionNo": rec.version_no,
            "recommended": _money(rec.recommended_value, rec.recommended_ccy,
                                  rec.recommended_basis),
            "range": {"low": _money(rec.low_value, rec.low_ccy),
                      "high": _money(rec.high_value, rec.high_ccy)},
            "confidence": rec.confidence,
            "dilutionRangePct": [float(rec.dilution_low_pct or 0),
                                 float(rec.dilution_high_pct or 0)],
            "recommendedTimeline": rec.recommended_timeline,
            "fundingBand": rec.funding_band,
            "dimensionScores": rec.dimension_scores,
            "reasoning": rec.reasoning, "risks": rec.risks,
            "narrative": rec.narrative, "label": "AI Generated Insights",
            "fx": _fx(),
            "scenarios": [{
                "id": str(s.id), "label": s.label,
                "raise": _money(s.raise_value, s.raise_ccy),
                "dilutionPct": float(s.dilution_pct or 0),
                "runwayMonths": s.runway_months,
                "assessment": s.assessment, "isSelected": s.is_selected,
            } for s in rec.scenarios.all()]})


class ValuationGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        # Gap G5: cheap synchronous pre-checks so the API answers 409/422
        # immediately instead of silently queueing a failure.
        from fundos.core.exceptions import ConflictError
        from fundos.core.services import ckb_service
        from fundos.strategy.models import RaiseRecommendation
        if not RaiseRecommendation.objects.filter(deal_id=self.deal.id,
                                                  is_active=True).exists():
            raise ConflictError(
                "Generate the ideal raise before the valuation "
                "(objectives → peers → raise → valuation).")
        flat = {}
        for group in ckb_service.snapshot(self.deal.id).values():
            flat.update({k: v.get("value") for k, v in group.items()})
        if not (flat.get("arr") or flat.get("revenue")):
            raise DomainValidationError(
                "Insufficient financial inputs for a valuation — complete "
                "ARR/revenue in the Company Knowledge Base first.",
                fields={"ckb.arr": "required",
                        "valuation": "needs_financials"})
        from fundos.strategy.tasks import generate_valuation_task
        return _queue_generation(self, request, "valuation",
                                 generate_valuation_task)


class ValuationView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.llm.guardrail import SEBI_DISCLAIMER
        from fundos.strategy.models import Valuation
        v = Valuation.objects.filter(deal_id=self.deal.id,
                                     is_active=True).first()
        if not v:
            raise NotFoundInDeal("No valuation yet.")

        # QA BUG-015: football-field per-method values are persisted in raw
        # engine USD, but the headline range is converted at this edge —
        # rendering the two side by side produced figures ~fx-rate apart.
        # Convert method values the same way the range is converted, and
        # keep the underlying USD figures visible (lowUsd/midUsd/highUsd).
        def _ff_money(x):
            m = _money(x)
            return (m.get("value"), m.get("usdValue", x))
        football_field = []
        for m in (v.football_field or []):
            low, low_usd = _ff_money(m.get("low"))
            mid, mid_usd = _ff_money(m.get("mid"))
            high, high_usd = _ff_money(m.get("high"))
            football_field.append({**m, "low": low, "mid": mid, "high": high,
                                   "lowUsd": low_usd, "midUsd": mid_usd,
                                   "highUsd": high_usd})

        return Response({
            "versionNo": v.version_no,
            "range": {"low": _money(v.range_low_value, v.range_low_ccy,
                                    v.range_low_basis),
                      "high": _money(v.range_high_value, v.range_high_ccy,
                                     v.range_high_basis)},
            "confidence": v.confidence,
            "footballField": football_field,
            "assumptions": v.assumptions, "limitations": v.limitations,
            "positioningStatement": v.positioning_statement,
            "negotiation": [{
                "kind": n.kind, "text": n.text, "evidence": n.evidence,
            } for n in v.negotiation_items.all()],
            "dilutionMatrix": [{
                # Same conversion gap as the football field — the grid is
                # persisted in USD; present converted + explicit USD.
                "raise": _money(float(c.raise_value)).get("value"),
                "raiseUsd": float(c.raise_value),
                "valuation": _money(float(c.valuation_value)).get("value"),
                "valuationUsd": float(c.valuation_value),
                "dilutionPct": float(c.dilution_pct),
                "founderOwnershipPct": float(c.founder_ownership_pct),
                "investorOwnershipPct": float(c.investor_ownership_pct),
                "esopImpactPct": float(c.esop_impact_pct),
                "existingInvestorDilutionPct":
                    float(c.existing_investor_dilution_pct),
            } for c in v.dilution_grid.all()],
            "label": "AI Generated Insights",
            "fx": _fx(),
            "disclaimer": SEBI_DISCLAIMER,   # inseparable from the range
        })


class InstrumentsView(DealScopedAPIView):
    required_action = "strategy.edit"

    def get(self, request, deal_id):
        from fundos.llm.guardrail import OPTIONS_NOT_ADVICE
        from fundos.strategy.models import InstrumentOption
        return Response({
            "items": [{
                "id": str(o.id), "instrument": o.instrument, "pros": o.pros,
                "cons": o.cons, "mechanicsExplainer": o.mechanics_explainer,
                "marketContext": o.market_context,
                "applicabilityNote": o.applicability_note,
                "isSelected": o.is_selected,
            } for o in InstrumentOption.objects.filter(deal_id=self.deal.id)],
            "disclaimer": OPTIONS_NOT_ADVICE})

    def post(self, request, deal_id):
        options, disclaimer = services.generate_instruments(self.deal,
                                                            user=request.user)
        return Response({"generated": len(options),
                         "disclaimer": disclaimer},
                        status=status.HTTP_201_CREATED)


class DilutionSimulateView(DealScopedAPIView):
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        try:
            raise_usd = float(request.data.get("raiseUsd") or 0)
            valuation_low = float(request.data.get("valuationLowUsd") or 0)
            valuation_high = float(request.data.get("valuationHighUsd") or 0)
        except (TypeError, ValueError):
            raise DomainValidationError("Numeric inputs required.",
                                        fields={"raiseUsd": "numeric"})
        scenario = services.simulate_dilution(
            self.deal, instrument=request.data.get("instrument", "equity"),
            raise_usd=raise_usd, valuation_low_usd=valuation_low,
            valuation_high_usd=valuation_high, user=request.user)
        return Response({
            "id": str(scenario.id), "instrument": scenario.instrument,
            "preMoney": scenario.pre_money,
            "postMoney": scenario.post_money,
            "ownershipBands": scenario.ownership_bands,   # ranges, always
            "conversionAssumptions": scenario.conversion_assumptions,
        }, status=status.HTTP_201_CREATED)


class BlueprintGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "strategy.edit"

    def post(self, request, deal_id):
        from fundos.strategy.tasks import generate_blueprint_task
        return _queue_generation(self, request, "blueprint",
                                 generate_blueprint_task)


class BlueprintView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.strategy.models import FundraisingStrategy
        s = FundraisingStrategy.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not s:
            raise NotFoundInDeal("No strategy blueprint yet.")

        # QA BUG-018: `current_position` is stored as {"summary": "..."} and
        # was previously returned as-is; rendering the object directly
        # crashed the entire Approve page. Flatten narrative fields to
        # strings at the edge so no client can trip on the shape.
        def _narrative(x):
            if isinstance(x, dict):
                return x.get("summary") or x.get("text") or \
                    "; ".join(str(v) for v in x.values() if v)
            return x if isinstance(x, str) else (x or "")

        raise_json = s.recommended_raise or {}
        val_json = s.recommended_valuation or {}
        return Response({
            "versionNo": s.version_no, "status": s.status,
            "executiveSummary": _narrative(s.executive_summary),
            "currentPosition": _narrative(s.current_position),
            # Stored engine values are USD — convert at the presentation
            # edge like every other money-bearing endpoint (Gap G13).
            "recommendedRaise": _money(raise_json.get("value"))
            if raise_json.get("value") is not None else None,
            "recommendedValuation": {
                "low": _money(val_json.get("low")),
                "high": _money(val_json.get("high")),
            } if val_json.get("high") is not None else None,
            "fx": _fx(),
            "whyThisStrategy": s.why_this_strategy,
            "risks": s.risks, "successFactors": s.success_factors,
            "nextMilestones": s.next_milestones,
            "label": "AI Generated Insights",
            "alternatives": [{
                "id": str(a.id), "label": a.label,
                "description": a.description,
                "raise": _money(a.raise_value, a.raise_ccy),
                "dilutionPct": float(a.dilution_pct or 0),
                "runwayMonths": a.runway_months,
                "advantages": a.advantages,
                "disadvantages": a.disadvantages,
                "executionComplexity": a.execution_complexity,
                "probabilityOfSuccess": a.probability_of_success,
                "isSelected": a.is_selected,
            } for a in s.alternatives.all()]})


class StrategyApproveView(DealScopedAPIView):
    required_action = "strategy.approve"

    def post(self, request, deal_id):
        profile = services.approve_strategy_profile(
            self.deal, user=request.user,
            selected_alternative_id=request.data.get("selectedAlternativeId"),
            selected_instrument=request.data.get("selectedInstrument", ""),
            key_positioning=request.data.get("keyPositioning", ""),
            principal_investment_thesis=request.data.get(
                "principalInvestmentThesis", ""))
        return Response(_serialise_profile(profile),
                        status=status.HTTP_201_CREATED)


class StrategyProfileView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.strategy.models import StrategyProfile
        profile = StrategyProfile.objects.filter(
            deal_id=self.deal.id, is_active=True, status="approved").first()
        if not profile:
            raise NotFoundInDeal("No approved strategy profile.")
        return Response(_serialise_profile(profile))


def _serialise_profile(p):
    return {
        "id": str(p.id), "versionNo": p.version_no, "status": p.status,
        "approvedRaise": _money(p.approved_raise_value, p.approved_raise_ccy,
                                p.approved_raise_basis),
        "valuationRange": {
            "low": _money(p.val_range_low_value, p.val_range_low_ccy),
            "high": _money(p.val_range_high_value, p.val_range_high_ccy)},
        "preferredInvestorCategories": p.preferred_investor_categories,
        "fundraisingTimeline": p.fundraising_timeline,
        "keyPositioning": p.key_positioning,
        "principalInvestmentThesis": p.principal_investment_thesis,
        "risksMitigations": p.risks_mitigations,
        "selectedInstrument": p.selected_instrument,
        "approvedAt": p.approval_timestamp.isoformat()
        if p.approval_timestamp else None,
        "fx": _fx(),
    }


class RaiseScenarioSelectView(DealScopedAPIView):
    """PATCH /deals/{id}/strategy/raise/scenario {scenarioId, selected}
    (Doc 4 M2 — missing surface found in the sweep)."""

    required_action = "strategy.edit"

    def patch(self, request, deal_id):
        from fundos.strategy.models import RaiseRecommendation, RaiseScenario
        rec = RaiseRecommendation.objects.filter(deal_id=self.deal.id,
                                                 is_active=True).first()
        if not rec:
            raise NotFoundInDeal("No raise recommendation yet.")
        scenario = RaiseScenario.objects.filter(
            id=request.data.get("scenarioId"),
            raise_recommendation=rec).first()
        if not scenario:
            raise NotFoundInDeal("Scenario not found.")
        RaiseScenario.objects.filter(raise_recommendation=rec) \
                             .update(is_selected=False)
        scenario.is_selected = True
        scenario.save(update_fields=["is_selected", "updated_at"])
        return Response({"scenarioId": str(scenario.id), "selected": True})


class InstrumentSelectView(DealScopedAPIView):
    """PATCH /deals/{id}/strategy/instruments/{instrument_id}
    {selected:true} — informational preference only (never advice)."""

    required_action = "strategy.edit"

    def patch(self, request, deal_id, instrument_id):
        from fundos.strategy.models import InstrumentOption
        option = InstrumentOption.objects.filter(
            id=instrument_id, deal_id=self.deal.id).first()
        if not option:
            raise NotFoundInDeal("Instrument option not found.")
        InstrumentOption.objects.filter(deal_id=self.deal.id) \
                                .update(is_selected=False)
        option.is_selected = bool(request.data.get("selected", True))
        option.save(update_fields=["is_selected", "updated_at"])
        return Response({"id": str(option.id),
                         "isSelected": option.is_selected})


class PeerRestView(DealScopedAPIView):
    """Doc 4 REST shape: POST /strategy/peers adds a founder peer;
    complements the existing /peers/curate action endpoint."""

    required_action = "strategy.edit"

    def post(self, request, deal_id):
        request.data["action"] = "add"
        proxy = PeerCurateView()
        proxy.deal = self.deal
        return proxy.post(request, deal_id)


class PeerDetailView(DealScopedAPIView):
    """DELETE /strategy/peers/{peerId} — enforces ≥3 comparables
    (BR-S2-013) before removal; GET returns the single peer."""

    required_action = "strategy.edit"

    def get(self, request, deal_id, peer_id):
        from fundos.strategy.models import PeerCompany
        p = PeerCompany.objects.filter(id=peer_id,
                                       deal_id=self.deal.id).first()
        if not p:
            raise NotFoundInDeal("Peer not found.")
        head = {
            "id": str(p.id), "peerId": str(p.id),
            "group": p.group_code, "name": p.name,
            "sector": p.sector, "subsector": p.subsector, "stage": p.stage,
            "geography": p.geography,
            "similarityScore": float(p.similarity_score or 0),
            "similarityBreakdown": p.similarity_breakdown,
            "rationale": p.selection_rationale,
            "selectionRationale": p.selection_rationale, "origin": p.origin,
        }
        milestones = [{
            "round": m.round,
            "amountRaised": _money(m.amount_raised_value,
                                   m.amount_raised_ccy),
            # `capitalRaised` alias: the journey drill-down UI reads it.
            "capitalRaised": _money(m.amount_raised_value,
                                    m.amount_raised_ccy),
            "valuation": _money(m.valuation_value, m.valuation_ccy,
                                m.valuation_basis),
            "arr": float(m.arr) if m.arr is not None else None,
            "employees": m.employees,
        } for m in p.milestones.all()]
        return Response({**head, "peer": head, "milestones": milestones})

    def delete(self, request, deal_id, peer_id):
        from fundos.core.exceptions import ConflictError
        from fundos.core.services import recalc
        from fundos.strategy.models import PeerCompany, PeerUniverse
        universe = PeerUniverse.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        peer = PeerCompany.objects.filter(id=peer_id,
                                          peer_universe=universe).first() \
            if universe else None
        if not peer:
            raise NotFoundInDeal("Peer not found.")
        # BR-S2-013: never drop below 3 comparables.
        if PeerCompany.objects.filter(peer_universe=universe).count() <= 3:
            raise ConflictError(
                "At least 3 comparable peers are required (BR-S2-013) — "
                "add a replacement before removing this one.")
        peer.delete()
        recalc.propagate_change(self.deal.id, "peer_universe",
                                reason="peer removed")
        return Response(status=status.HTTP_204_NO_CONTENT)
