"""V2 Assessment API Views for Django.

Exposes dedicated V2 Assessment endpoints matching Fundraising Strategy V2.0:
- GET /api/v1/companies/{company_id}/assessment
- POST /api/v1/companies/{company_id}/assessment
- GET /api/v1/companies/{company_id}/assessment/parameters/{ref}
- PATCH /api/v1/companies/{company_id}/assessment/parameters/{ref}
- GET /api/v1/assessment/reference
- GET /api/v1/assessment/cohorts
"""
import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from fundos.core.models import Company
from fundos.assessment.models import Assessment, ParameterValue
from fundos.assessment import phase1_views
from fundos.assessment.v2_serializers import (
    build_v2_assessment_response,
    build_v2_parameter_detail,
    v2_ref
)

logger = logging.getLogger(__name__)


def _active_assessment(company):
    """Get the current active assessment for a company."""
    return (Assessment.objects.filter(company=company)
            .exclude(status="superseded")
            .order_by("-created_at").first())


class V2AssessmentView(APIView):
    """Main assessment endpoint: GET serves V2 payload (auto-starts if missing); POST runs assessment."""

    def get(self, request, company_id):
        company = get_object_or_404(Company, id=company_id)
        assessment = _active_assessment(company)

        if assessment is None or assessment.status in ("queued", "extracting"):
            # Auto-start generation if no assessment exists
            if assessment is None:
                try:
                    p1_view = phase1_views.FundraisingPhase1View()
                    p1_view._start_generation(request, company)
                    assessment = _active_assessment(company)
                except Exception as exc:
                    logger.warning("V2_API: Auto-start generation failed for company %s: %s", company_id, exc)

            return Response({
                "assessment_id": str(assessment.id) if assessment else "",
                "job_id": str(company_id),
                "company_name": company.name,
                "status": "extracting" if assessment else "not_started",
                "message": "Assessment generation in progress. Poll this URL until completed."
            }, status=status.HTTP_200_OK)

        payload = build_v2_assessment_response(assessment, company)
        return Response(payload, status=status.HTTP_200_OK)

    def post(self, request, company_id):
        company = get_object_or_404(Company, id=company_id)
        try:
            p1_view = phase1_views.FundraisingPhase1View()
            p1_view._start_generation(request, company)
            assessment = _active_assessment(company)
            return Response({
                "assessment_id": str(assessment.id) if assessment else "",
                "job_id": str(company_id),
                "company_name": company.name,
                "status": "extracting",
                "message": "Assessment queued successfully."
            }, status=status.HTTP_202_ACCEPTED)
        except Exception as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)


class V2AssessmentSuggestionsView(APIView):
    """GET — what is worth asking about a node of the scorecard.

    COMPANY-SCOPED, because this whole V2 surface is: the panel that shows a
    parameter fetched it from `companies/{id}/assessment/parameters/{ref}`
    and holds a company id, not a deal id. A deal-scoped twin exists for
    callers that work from the deal (findings, coverage, the review log),
    and both answer from the same functions.

    `?ref=` addresses any level -- a category (A), a group (A.3) or the row
    that scores (A.3.b), by ref or by input key. With no ref the six
    categories come back, which is what a panel that has not drilled in yet
    needs.
    """

    def get(self, request, company_id):
        from fundos.assessment import suggestions

        company = get_object_or_404(Company, id=company_id)
        assessment = _active_assessment(company)
        if assessment is None:
            return Response({"assessed": False, "suggestions": {}})

        raw = (request.query_params.get("ref") or "").strip()
        refs = [r.strip() for r in raw.split(",") if r.strip()] or None
        return Response({"assessed": True,
                         "suggestions": suggestions.for_assessment(
                             assessment, refs)})


class V2AssessmentQAView(APIView):
    """POST — a question about this scorecard, and any override it proposes.

    The chat never changes a score. It proposes one, and a person applies it
    through the override this same surface already serves:
    `PATCH companies/{id}/assessment/parameters/{ref}`. The proposal carries
    both addresses -- `ref` for that route, `inputKey` for the deal-scoped
    one -- so either write path can take it.
    """

    def post(self, request, company_id):
        from fundos.assessment.qa import answer

        company = get_object_or_404(Company, id=company_id)
        question = ((request.data or {}).get("question") or "").strip()
        if not question:
            return Response({"detail": "question is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        assessment = _active_assessment(company)
        if assessment is None:
            return Response(
                {"detail": "This company has no assessment yet."},
                status=status.HTTP_404_NOT_FOUND)

        return Response(answer(assessment, question,
                               ref=((request.data or {}).get("ref") or ""),
                               user=request.user))


class V2AssessmentParameterDetailView(APIView):
    """Parameter drill-down (GET) and override (PATCH)."""

    def get(self, request, company_id, ref):
        company = get_object_or_404(Company, id=company_id)
        assessment = _active_assessment(company)
        if not assessment:
            return Response({"detail": f"No assessment found for company: {company_id}"}, status=status.HTTP_404_NOT_FOUND)

        detail_payload = build_v2_parameter_detail(assessment, ref)
        return Response(detail_payload, status=status.HTTP_200_OK)

    def patch(self, request, company_id, ref):
        company = get_object_or_404(Company, id=company_id)
        assessment = _active_assessment(company)
        if not assessment:
            return Response({"detail": "This company has no assessment yet."}, status=status.HTTP_404_NOT_FOUND)

        score = request.data.get("score")
        band = request.data.get("band")
        comment = (request.data.get("comment") or "").strip()

        clearing = score is None and band is None
        if not clearing and not comment:
            return Response({
                "detail": "An override needs a reason. A corrected score with no explanation cannot be audited."
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        pv = assessment.parameter_values.filter(input_key=ref).first()
        if not pv:
            pv = assessment.parameter_values.filter(ref_code=ref).first()

        if not pv:
            return Response({"detail": f"No such parameter: {ref}"}, status=status.HTTP_404_NOT_FOUND)

        # THE WRITE GOES THROUGH THE ONE CASCADE, NOT AROUND IT.
        #
        # This wrote the score straight onto the row and rebuilt the payload.
        # Two things that override is supposed to do therefore did not
        # happen: no ReviewLog entry, so the one action that moves a score
        # against the model's own judgement left no trail; and no re-scoring,
        # so the category and the headline kept the numbers they had before
        # the change -- a panel showing a corrected row above an overall
        # score that still reflects the old one.
        #
        # `apply_parameter_override` is where all three steps live, and the
        # deal-scoped route has always used it. One cascade, one trail.
        from fundos.assessment.services import (apply_parameter_override,
                                                run_scoring)
        from fundos.engines.deal_assessment import score_for_band

        if clearing:
            pv.is_overridden = False
            pv.override_comment = ""
            pv.score = pv.system_score
            pv.band = pv.system_band
            pv.overridden_by = None
            pv.save(update_fields=["is_overridden", "override_comment",
                                   "score", "band", "overridden_by"])
            # Reverting is a change to the score too, so the roll-up has to
            # run again or the headline keeps the overridden figure.
            run_scoring(assessment, user=request.user)
            assessment.refresh_from_db()
        else:
            # A band with no score is still a decision; the scoring key says
            # what it is worth, rather than this view inventing a number.
            value = (float(score) if score is not None
                     else score_for_band(band))
            if value is None:
                return Response(
                    {"detail": f"{band!r} is not a band this model scores."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            if not 0 <= value <= 10:
                return Response(
                    {"detail": "score must be between 0 and 10."},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            assessment = apply_parameter_override(
                assessment, pv, score=value, reason=comment,
                user=request.user)

        payload = build_v2_assessment_response(assessment, company)
        return Response(payload, status=status.HTTP_200_OK)


class V2AssessmentReferenceView(APIView):
    """Static methodology reference endpoint."""

    def get(self, request):
        stage = request.query_params.get("stage")
        if not v2_ref:
            return Response({"error": "V2 reference layer unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        stage_filter = v2_ref.normalize_stage(stage) if stage else None
        rubrics = v2_ref.rubrics()
        if stage_filter:
            rubrics = {
                key: {**rubric, "stages": {stage_filter: rubric["stages"].get(stage_filter, {})}}
                for key, rubric in rubrics.items()
            }

        return Response({
            "stages": list(v2_ref.STAGES),
            "bands": list(v2_ref.BANDS),
            "evidence_tiers": list(v2_ref.EVIDENCE_TIERS),
            "scoring_key": v2_ref.constants()["scoring_key"],
            "rating_ladder": v2_ref.constants()["rating_ladder"],
            "rating_floor": v2_ref.constants()["rating_floor"],
            "constants": v2_ref.constants(),
            "rubrics": rubrics,
            "anchors": v2_ref.anchors(),
            "dictionary": v2_ref.dictionary(),
            "tree": v2_ref.tree(),
            "parameters": v2_ref.parameters()
        }, status=status.HTTP_200_OK)


class V2AssessmentCohortsView(APIView):
    """Cohort database mappings endpoint."""

    def get(self, request):
        if not v2_ref:
            return Response({"error": "V2 reference layer unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        data = v2_ref.sectors()
        return Response({
            "coverage": data["coverage"],
            "sectors": data["sectors"],
            "sub_sectors": data["sub_sectors"],
            "raw_sub_sector_map": data["raw_sub_sector_map"],
            "min_sample_deals": v2_ref.constants()["min_sample_deals"]
        }, status=status.HTTP_200_OK)
