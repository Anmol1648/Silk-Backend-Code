"""
M3 API (Doc 4 Stage-3 surface): workspace, story, teaser, deck (two-step),
financial model + assumptions + statements, IM, package review, package
approval. Every route is behind the Stage-3 hard gate (E-GATE-423) via
services.get_or_create_workspace / _approved_profile.
"""
import logging

from rest_framework import status
from rest_framework.response import Response

from fundos.core.api.base import DealScopedAPIView, queue_generation
from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.materials import services

logger = logging.getLogger(__name__)



class _GenerateThrottledMixin:
    def get_throttles(self):
        from fundos.core.throttling import GenerateThrottle
        return [GenerateThrottle()]

class WorkspaceView(DealScopedAPIView):
    # Gap G9: POST is accepted as an idempotent create-or-return alias.
    def post(self, request, deal_id):
        return self.get(request, deal_id)

    def get(self, request, deal_id):
        # CR-02 / C6: navigation is never blocked. Reading the workspace
        # before a strategy is approved used to return E-GATE-423, which
        # made the whole page render as a locked panel. The read now
        # succeeds and reports the outstanding prerequisite instead; the
        # generation actions below remain guarded server-side, which is
        # where the integrity constraint actually belongs.
        from fundos.core.exceptions import GateLocked
        from fundos.core.services.stage_state import prerequisite_status

        try:
            workspace = services.get_or_create_workspace(self.deal,
                                                         user=request.user)
        except GateLocked as exc:
            return Response({
                "available": False,
                "prerequisites": prerequisite_status(self.deal.id, 3),
                "explanation": str(exc.detail),
                "documents": [],
            })

        from fundos.docs.models import Document
        documents = Document.objects.filter(deal_id=self.deal.id,
                                            workspace_id=workspace.id)
        return Response({
            "available": True,
            "prerequisites": prerequisite_status(self.deal.id, 3),
            "id": str(workspace.id),
            "strategyProfileId": str(workspace.strategy_profile_id),
            "status": workspace.status,
            "documents": [{
                "id": str(d.id), "docType": d.doc_type, "title": d.title,
                "status": d.status,
                "currentVersionId": str(d.current_version_id)
                if d.current_version_id else None,
            } for d in documents]})


# --- Story ---

class StoryGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.tasks import generate_story_task
        return queue_generation(self, request, "story",
                                generate_story_task)


class StoryView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.materials.models import InvestmentStory
        story = InvestmentStory.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not story:
            raise NotFoundInDeal("No investment story yet.")
        return Response({
            "id": str(story.id), "versionNo": story.version_no,
            "status": story.status, "thesisPrimary": story.thesis_primary,
            "selectedPositioning": story.selected_positioning,
            "label": "AI Generated Insights",
            "components": [{
                "component": c.component, "content": c.content,
                "evidence": c.evidence,
                "editedByFounder": c.edited_by_founder,
            } for c in story.components.all()],
            "positioningOptions": [{
                "id": str(o.id), "text": o.text, "rationale": o.rationale,
                "isSelected": o.is_selected,
            } for o in story.positioning_options.all()],
            "messageBlocks": [{
                "blockType": b.block_type, "content": b.content,
            } for b in story.message_blocks.all()]})


class StoryApproveView(DealScopedAPIView):
    required_action = "materials.approve"

    def post(self, request, deal_id):
        from fundos.materials.models import InvestmentStory
        story = InvestmentStory.objects.filter(deal_id=self.deal.id,
                                               is_active=True).first()
        if not story:
            raise NotFoundInDeal("No investment story yet.")
        story = services.approve_story(
            self.deal, story, user=request.user,
            selected_positioning_id=request.data.get("selectedPositioningId"))
        return Response({"id": str(story.id), "status": story.status})


# --- Teaser ---

class TeaserGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.tasks import generate_teaser_task
        return queue_generation(self, request, "teaser",
                                generate_teaser_task)


class TeaserView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.materials.models import Teaser
        teaser = Teaser.objects.filter(deal_id=self.deal.id).first()
        if not teaser:
            raise NotFoundInDeal("No teaser yet.")
        return Response({
            "id": str(teaser.id),
            "documentId": str(teaser.document_id) if teaser.document_id else None,
            "headline": teaser.headline, "thesis": teaser.thesis,
            "tagline": teaser.tagline, "label": "AI Generated Insights",
            "sections": [{
                "sectionKey": s.section_key, "title": s.title,
                "body": s.body, "isMandatory": s.is_mandatory,
                "included": s.included,
            } for s in teaser.sections.all()]})


# --- Pitch deck (two-step, story-first) ---

class DeckOutlineView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        slide_count = request.data.get("length") or \
            request.data.get("slideCount")
        deck = services.generate_deck_outline(
            self.deal, user=request.user,
            template_code=request.data.get("templateCode", ""),
            slide_count=int(slide_count) if slide_count else None)
        return Response({"id": str(deck.id),
                         "narrativeOutline": deck.narrative_outline,
                         "outlineApproved": deck.outline_approved,
                         "label": "AI Generated Insights"},
                        status=status.HTTP_201_CREATED)

    def get(self, request, deal_id):
        from fundos.materials.models import PitchDeck
        deck = PitchDeck.objects.filter(deal_id=self.deal.id).first()
        if not deck:
            raise NotFoundInDeal("No deck yet.")
        # QA BUG-024: this was the one AI-generated surface missing the
        # mandatory "AI Generated Insights" label (TC-097).
        return Response({"id": str(deck.id),
                         "narrativeOutline": deck.narrative_outline,
                         "outlineApproved": deck.outline_approved,
                         "label": "AI Generated Insights"})


class DeckApproveOutlineView(DealScopedAPIView):
    required_action = "materials.approve"

    def post(self, request, deal_id):
        from fundos.materials.models import PitchDeck
        deck = PitchDeck.objects.filter(deal_id=self.deal.id).first()
        if not deck:
            raise NotFoundInDeal("No deck yet.")
        deck.outline_approved = True
        deck.save(update_fields=["outline_approved", "updated_at"])
        return Response({"id": str(deck.id), "outlineApproved": True})


class DeckSlidesView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.models import PitchDeck
        deck = PitchDeck.objects.filter(deal_id=self.deal.id).first()
        if not deck:
            raise NotFoundInDeal("No deck yet — generate the outline first.")
        deck = services.generate_deck_slides(self.deal, deck,
                                             user=request.user)
        return self.get(request, deal_id)

    def get(self, request, deal_id):
        from fundos.materials.models import PitchDeck
        deck = PitchDeck.objects.filter(deal_id=self.deal.id).first()
        if not deck:
            raise NotFoundInDeal("No deck yet.")
        return Response({
            "id": str(deck.id),
            "documentId": str(deck.document_id) if deck.document_id else None,
            "outlineApproved": deck.outline_approved,
            "label": "AI Generated Insights",
            "slides": [{
                "slideType": s.slide_type, "headline": s.headline,
                "narrative": s.narrative, "keyMetrics": s.key_metrics,
                "chartSpec": s.chart_spec, "icons": s.icons,
                "speakerNotes": s.speaker_notes,
                "investorTakeaway": s.investor_takeaway,
            } for s in deck.slides.all()]})


# --- Financial model ---

class ModelGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.tasks import generate_model_task
        return queue_generation(
            self, request, "model", generate_model_task,
            horizon_months=int(request.data.get("horizonMonths", 36)))


def _current_model(deal):
    from fundos.materials.models import FinancialModel
    model = FinancialModel.objects.filter(deal_id=deal.id).first()
    if not model:
        raise NotFoundInDeal("No financial model yet.")
    return model


class ModelAssumptionsView(DealScopedAPIView):
    def get(self, request, deal_id):
        model = _current_model(self.deal)
        return Response({
            "modelId": str(model.id),
            "horizonMonths": model.horizon_months,
            "items": [{
                "group": a.group, "key": a.key,
                "value": float(a.value_num) if a.value_num is not None else None,
                "unit": a.unit, "source": a.source,
            } for a in model.assumptions.all()]})

    def patch(self, request, deal_id):
        """PATCH {group, key, value} — edit → [DET] recompute → propagate."""
        from fundos.core.permissions import require_action
        require_action(request.user, self.deal, "materials.generate")
        model = _current_model(self.deal)
        assumption = services.update_assumption(
            self.deal, model, group=request.data.get("group", ""),
            key=request.data.get("key", ""),
            value_num=request.data.get("value"), user=request.user)
        return Response({"group": assumption.group, "key": assumption.key,
                         "value": float(assumption.value_num),
                         "source": assumption.source})


class ModelStatementsView(DealScopedAPIView):
    def get(self, request, deal_id):
        model = _current_model(self.deal)
        scenario = request.query_params.get("scenario", "base")
        statement = request.query_params.get("statement")   # optional
        lines = model.statement_lines.filter(scenario=scenario)
        if statement:
            lines = lines.filter(statement=statement)
        by_line = {}
        for line in lines.order_by("line_key", "month_index"):
            by_line.setdefault(f"{line.statement}.{line.line_key}",
                               []).append(float(line.value))
        kpis = {}
        for kpi in model.kpis.filter(scenario=scenario).order_by(
                "kpi_key", "month_index"):
            kpis.setdefault(kpi.kpi_key, []).append(float(kpi.value))
        return Response({"modelId": str(model.id), "scenario": scenario,
                         "horizonMonths": model.horizon_months,
                         "lines": by_line, "kpis": kpis})


# --- Investment memorandum ---

class ImGenerateView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.tasks import generate_im_task
        return queue_generation(self, request, "im", generate_im_task)


class ImView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.materials.models import InvestmentMemorandum
        im = InvestmentMemorandum.objects.filter(deal_id=self.deal.id).first()
        if not im:
            raise NotFoundInDeal("No investment memorandum yet.")
        # TC-087 cosmetic gap: the header rendered "IM v" with no number —
        # no version field was serialised. IMs are superseded on regenerate,
        # so the count for the deal is the honest version number.
        version_no = getattr(im, "version_no", None)
        if not version_no:
            manager = getattr(InvestmentMemorandum, "all_objects",
                              InvestmentMemorandum.objects)
            version_no = manager.filter(deal_id=self.deal.id).count() or 1
        return Response({
            "id": str(im.id),
            "versionNo": version_no,
            "documentId": str(im.document_id) if im.document_id else None,
            "label": "AI Generated Insights",
            "sections": [{
                "sectionKey": s.section_key, "title": s.title,
                "body": s.body, "isMandatory": s.is_mandatory,
                "consistencyFlags": s.consistency_flags,
            } for s in im.sections.all()]})


# --- Package review + approval ---

class ReviewRunView(_GenerateThrottledMixin, DealScopedAPIView):
    required_action = "materials.generate"

    def post(self, request, deal_id):
        from fundos.materials.tasks import run_package_review_task
        return queue_generation(self, request, "review",
                                run_package_review_task)


class ReviewView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.materials.models import PackageReview
        review = (PackageReview.objects.filter(deal_id=self.deal.id)
                  .order_by("-created_at").first())
        if not review:
            raise NotFoundInDeal("No package review yet.")

        # Tester Issue 6: surface which package documents are currently out
        # of date with their inputs (live check, not review-time), so the
        # UI can say "regenerate the Financial Model" instead of a generic
        # "fix the source facts".
        from fundos.core.services import recalc
        stale_documents = [label for artefact, label in (
            ("financial_model", "Financial Model"),
            ("investment_memorandum", "Investment Memorandum"),
            ("teaser", "Teaser"), ("pitch_deck", "Pitch Deck"),
            ("investment_story", "Investment Story"))
            if recalc.status_of(self.deal.id, artefact) in (
                "pending_recalc", "needs_review")]

        findings = [{
            "id": str(f.id), "priority": f.priority,
            "category": f.category, "reason": f.reason,
            "businessImpact": f.business_impact,
            "affectedDocuments": f.affected_documents,
            "estimatedImprovement": f.estimated_improvement,
            "status": f.status,
        } for f in review.findings.all()]
        inconsistencies = [{
            "id": str(i.id),
            "fieldKey": i.field_key, "values": i.values,
            "recommendedCorrection": i.recommended_correction,
            "resolved": i.resolved,
            "priority": "high",   # deterministic cross-doc gaps block approval
        } for i in review.inconsistencies.all()]

        # QA BUG-021: the UI expects a numeric quality score and per-
        # category scores, but no such field existed anywhere in the
        # response (it rendered a permanent 0/100). Compute both
        # deterministically from the review contents so the number is
        # reproducible and moves when issues are fixed.
        weights = {"high": 15, "med": 7, "medium": 7, "low": 3}
        deduction = sum(weights.get(str(f["priority"]).lower(), 5)
                        for f in findings if f["status"] == "open")
        deduction += 12 * sum(1 for i in inconsistencies if not i["resolved"])
        quality_score = max(5, min(98, 100 - deduction))

        categories = ("story", "commercial", "financial", "presentation",
                      "investment", "completeness")
        category_scores = {}
        for cat in categories:
            cat_ded = sum(weights.get(str(f["priority"]).lower(), 5)
                          for f in findings
                          if f["status"] == "open" and f["category"] == cat)
            category_scores[cat] = max(5, min(98, 100 - cat_ded))

        return Response({
            "id": str(review.id), "categoryNotes": review.category_notes,
            "riskSummary": review.risk_summary,
            "outstandingActions": review.outstanding_actions,
            "label": "AI Generated Insights",
            "staleDocuments": stale_documents,
            "qualityScore": quality_score,
            "categoryScores": category_scores,
            "scoringBasis": "Deterministic: 100 minus weighted open "
                            "findings (high 15 / medium 7 / low 3) and 12 "
                            "per unresolved cross-document inconsistency.",
            "findings": findings,
            # `recommendations` mirrors `findings` — the review UI reads
            # this name (QA BUG-021).
            "recommendations": findings,
            "crossDocInconsistencies": inconsistencies,
            "inconsistencies": inconsistencies,
            "objections": [{
                "investorType": o.investor_type, "question": o.question,
                "reason": o.reason, "suggestedResponse": o.suggested_response,
                "evidence": o.evidence, "confidence": o.confidence,
                "priority": o.priority,
            } for o in review.objections.all()]})


class PackageApproveView(DealScopedAPIView):
    required_action = "materials.approve"

    def post(self, request, deal_id):
        package = services.approve_investor_package(self.deal,
                                                    user=request.user)
        return Response(_serialise_package(package),
                        status=status.HTTP_201_CREATED)


class PackageView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.materials.models import InvestorPackage
        package = InvestorPackage.objects.filter(deal_id=self.deal.id,
                                                 is_active=True).first()
        if not package:
            raise NotFoundInDeal("No investor package yet.")
        return Response(_serialise_package(package))


def _serialise_package(p):
    return {
        "id": str(p.id), "versionNo": p.version_no, "status": p.status,
        "documentVersionIds": p.document_version_ids,
        "approvedAt": p.approved_at.isoformat() if p.approved_at else None,
    }
