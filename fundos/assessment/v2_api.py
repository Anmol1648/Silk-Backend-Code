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

        if clearing:
            pv.is_overridden = False
            pv.override_comment = ""
            pv.score = pv.system_score
            pv.band = pv.system_band
        else:
            pv.is_overridden = True
            pv.override_comment = comment
            if score is not None:
                pv.score = float(score)
            if band:
                pv.band = band

        pv.save()
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
