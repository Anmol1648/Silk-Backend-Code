"""
M1 API (Doc 4): research run/sources, materials CRUD + chunked uploads +
critique + link-metric, readiness generate/get/gaps.
"""
import logging
import uuid as uuid_lib

from rest_framework import status
from rest_framework.parsers import BaseParser, FileUploadParser, MultiPartParser
from rest_framework.response import Response

from fundos.core.api.base import DealScopedAPIView, queue_generation
from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.core.services import ckb_service
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pillar 1 — research
# ---------------------------------------------------------------------------

class ResearchRunView(DealScopedAPIView):
    required_action = "research.run"

    def get_throttles(self):
        from fundos.core.throttling import GenerateThrottle
        return [GenerateThrottle()]

    def post(self, request, deal_id):
        from fundos.core.services import jobs
        from fundos.research.adapters.registry import enabled_sources
        from fundos.research.tasks import run_research

        sources = request.data.get("sources")   # optional subset
        runnable = enabled_sources(sources)
        job = jobs.create_job(self.deal, "research", request.user)
        run_research.delay(str(self.deal.tenant_id), str(self.deal.id),
                           sources, str(request.user.id),
                           job_id=str(job.id))
        job.refresh_from_db()
        return Response({"status": "queued", "sources": runnable,
                         "jobId": str(job.id), "jobStatus": job.status,
                         "poll": f"/api/v1/deals/{self.deal.id}"
                                 f"/jobs/{job.id}"},
                        status=status.HTTP_202_ACCEPTED)


class ResearchSourcesView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.research.models import CompanyResearchSource
        rows = [{
            "sourceType": s.source_type, "status": s.status,
            "confidence": float(s.confidence) if s.confidence is not None else None,
            "fetchedAt": s.fetched_at.isoformat() if s.fetched_at else None,
            "error": s.error_message or None,
        } for s in CompanyResearchSource.objects.filter(
            deal_id=self.deal.id)]
        # `sources` alias: the UI read this key and rendered nothing
        # (QA BUG-002) — serve both names permanently.
        return Response({"items": rows, "sources": rows})


# ---------------------------------------------------------------------------
# Pillar 3 — materials
# ---------------------------------------------------------------------------

def _serialise_material(m, filename=""):
    quarantined = m.scan_status not in ("clean", "pending")
    return {
        # `materialId` mirrors `id` — the UI reads `materialId` and was
        # issuing `/materials/undefined` calls (QA BUG-006).
        "id": str(m.id), "materialId": str(m.id),
        "documentId": str(m.document_id) if m.document_id else None,
        "filename": filename or None,
        "category": m.category, "detectedType": m.detected_type,
        "aiStatus": m.ai_status, "scanStatus": m.scan_status,
        "extractionStatus": m.extraction_status,
        # QA BUG-007: a file that did not scan clean is quarantined — never
        # expose analysis output or allow downstream actions on it.
        "quarantined": quarantined,
        "summary": "" if quarantined else m.summary,
        "extractedMetrics": [] if quarantined else m.extracted_metrics,
        "recommendations": [] if quarantined else m.recommendations,
        "batchId": str(m.batch_id) if m.batch_id else None,
        "sizeBytes": m.size_bytes,
        "createdAt": m.created_at.isoformat(),
    }


def _material_filenames(deal_id, materials):
    """Bulk-resolve upload filenames (Document titles) for a material list."""
    from fundos.docs.models import Document
    doc_ids = [m.document_id for m in materials if m.document_id]
    titles = {str(d.id): d.title for d in
              Document.objects.filter(id__in=doc_ids)}
    return {str(m.id): titles.get(str(m.document_id), "")
            for m in materials}


class MaterialsView(DealScopedAPIView):
    parser_classes = [MultiPartParser]
    required_action = "materials.upload"

    def get(self, request, deal_id):
        from fundos.readiness.models import MaterialAsset
        materials = list(MaterialAsset.objects.filter(deal_id=self.deal.id)
                         .order_by("-created_at"))
        names = _material_filenames(self.deal.id, materials)
        return Response({"items": [
            _serialise_material(m, filename=names.get(str(m.id), ""))
            for m in materials]})

    def post(self, request, deal_id):
        """Direct multipart upload ≤50MB; batch via repeated calls with a
        shared batchId (folder upload, BR-M1-022)."""
        from fundos.readiness.services import register_material

        upload = request.FILES.get("file")
        if not upload:
            raise DomainValidationError("file is required.",
                                        fields={"file": "required"})
        batch_id = request.data.get("batchId") or None
        material, document = register_material(
            self.deal, filename=upload.name, content=upload.read(),
            mime=upload.content_type or "", 
            category=request.data.get("category", "other"),
            batch_id=uuid_lib.UUID(batch_id) if batch_id else None,
            user=request.user)
        return Response(_serialise_material(material, filename=upload.name),
                        status=status.HTTP_201_CREATED)


class MaterialDetailView(DealScopedAPIView):
    def _get_material(self, material_id):
        from fundos.readiness.models import MaterialAsset
        m = MaterialAsset.objects.filter(id=material_id,
                                         deal_id=self.deal.id).first()
        if not m:
            raise NotFoundInDeal("Material not found.")
        return m

    def get(self, request, deal_id, material_id):
        m = self._get_material(material_id)
        names = _material_filenames(self.deal.id, [m])
        return Response(_serialise_material(
            m, filename=names.get(str(m.id), "")))

    def patch(self, request, deal_id, material_id):
        """PATCH {aiStatus: reuse|improve|replace|generate} — the founder's
        decision on the critique (BR-M1-021: assess-then-act). This verb
        was documented but missing, which left every critique action dead
        in the UI (related to QA BUG-006)."""
        from fundos.core.permissions import require_action
        require_action(request.user, self.deal, "materials.upload")
        m = self._get_material(material_id)
        # QA BUG-007: infected / unscanned files are quarantined — no
        # downstream action may be taken on them.
        if m.scan_status != "clean":
            from fundos.core.exceptions import ConflictError
            raise ConflictError(
                "This file did not scan clean and is quarantined — it "
                "cannot be used downstream. Upload a clean copy instead.",
                fields={"scanStatus": m.scan_status})
        ai_status = request.data.get("aiStatus", "")
        allowed = ("reuse", "improve", "replace", "generate")
        if ai_status not in allowed:
            raise DomainValidationError(
                f"aiStatus must be one of {', '.join(allowed)}.",
                fields={"aiStatus": "invalid"})
        m.ai_status = ai_status
        m.save(update_fields=["ai_status", "updated_at"])
        audit("material.action", actor=request.user, deal_id=self.deal.id,
              entity="material_asset", entity_id=m.id,
              meta={"aiStatus": ai_status})
        names = _material_filenames(self.deal.id, [m])
        return Response(_serialise_material(
            m, filename=names.get(str(m.id), "")))

    def delete(self, request, deal_id, material_id):
        from fundos.readiness.models import MaterialAsset
        m = MaterialAsset.objects.filter(id=material_id,
                                         deal_id=self.deal.id).first()
        if not m:
            raise NotFoundInDeal("Material not found.")
        m.delete()   # soft delete
        audit("material.deleted", actor=request.user, deal_id=self.deal.id,
              entity="material_asset", entity_id=m.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MaterialLinkMetricView(DealScopedAPIView):
    """Founder accepts an extracted metric into the CKB (Doc 4 M1)."""
    required_action = "ckb.edit"

    def post(self, request, deal_id, material_id, link_id):
        from fundos.readiness.models import MaterialExtractionLink
        link = MaterialExtractionLink.objects.filter(
            id=link_id, material_asset_id=material_id,
            deal_id=self.deal.id).first()
        if not link:
            raise NotFoundInDeal("Extraction link not found.")
        metric = link.extracted_value_json or {}
        field_key = metric.get("fieldKey")
        if not field_key:
            raise DomainValidationError("The extraction has no fieldKey.")
        field, warnings = ckb_service.set_field(
            self.deal, field_key, metric.get("value"), user=request.user,
            source="document_extracted", confidence=metric.get("confidence"),
            ccy=metric.get("ccy", ""),
            source_ref=f"material:{material_id}",
            verify=True, reason="accepted extracted metric")
        link.accepted = True
        link.ckb_field_id = field.id
        link.save(update_fields=["accepted", "ckb_field_id", "updated_at"])
        return Response({"fieldKey": field_key, "accepted": True,
                         "warnings": warnings})


# --- Chunked uploads (BR-M1-023) ---

class UploadSessionView(DealScopedAPIView):
    required_action = "materials.upload"

    def post(self, request, deal_id):
        from fundos.readiness.services import open_upload_session
        session = open_upload_session(
            self.deal, filename=request.data.get("filename", ""),
            size=int(request.data.get("size", 0)),
            mime=request.data.get("mime", ""),
            category=request.data.get("category", "other"),
            user=request.user)
        # Tester Issue 7: the UI read `uploadId` but only `sessionId` was
        # served — every chunk PUT went to /sessions/undefined/... which
        # matches no route and surfaced as a bare "HTTP 404". Both names
        # are served permanently.
        return Response({
            "sessionId": str(session.id), "uploadId": str(session.id),
            "chunkSize": session.chunk_size,
            "expiresAt": session.expires_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class _OctetStreamParser(BaseParser):
    """Raw application/octet-stream chunk bodies (Issue 7): the UI PUTs the
    chunk blob directly; DRF's FileUploadParser demands a Content-Disposition
    filename which a raw blob PUT doesn't carry."""
    media_type = "application/octet-stream"

    def parse(self, stream, media_type=None, parser_context=None):
        return stream.read()


class UploadChunkView(DealScopedAPIView):
    # Issue 7: raw octet-stream first (what the UI sends), multipart kept
    # for API clients that prefer form uploads.
    parser_classes = [_OctetStreamParser, MultiPartParser]
    required_action = "materials.upload"

    def put(self, request, deal_id, session_id, chunk_no):
        from fundos.readiness.models import UploadSession
        from fundos.readiness.services import receive_chunk
        session = UploadSession.objects.filter(id=session_id,
                                               deal_id=self.deal.id).first()
        if not session:
            raise NotFoundInDeal("Upload session not found.")
        if isinstance(request.data, (bytes, bytearray)):
            data = bytes(request.data)                     # raw chunk body
        else:
            upload = request.FILES.get("file") or request.data.get("file")
            data = upload.read() if hasattr(upload, "read") else (upload or b"")
        received = receive_chunk(session, int(chunk_no), data)
        return Response({"chunksReceived": received})


class UploadCompleteView(DealScopedAPIView):
    required_action = "materials.upload"

    def post(self, request, deal_id, session_id):
        from fundos.readiness.models import UploadSession
        from fundos.readiness.services import complete_upload_session
        session = UploadSession.objects.filter(id=session_id,
                                               deal_id=self.deal.id).first()
        if not session:
            raise NotFoundInDeal("Upload session not found.")
        session = complete_upload_session(session, user=request.user)
        return Response({"sessionId": str(session.id),
                         "status": session.status},
                        status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Pillar 4 — readiness
# ---------------------------------------------------------------------------

class ReadinessGenerateView(DealScopedAPIView):
    required_action = "readiness.generate"

    def get_throttles(self):
        from fundos.core.throttling import GenerateThrottle
        return [GenerateThrottle()]

    def post(self, request, deal_id):
        # Tester Issue 12: this endpoint returned a synchronous 201 while
        # every other generator returns 202 {jobId, poll}. It now follows
        # the same async contract (dev Celery runs eagerly, so the job is
        # typically terminal by the time the 202 lands).
        from fundos.readiness.tasks import run_readiness
        return queue_generation(self, request, "readiness_assessment",
                                run_readiness)


class ReadinessView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.readiness.models import ReadinessAssessment
        assessment = ReadinessAssessment.objects.filter(
            deal_id=self.deal.id, is_active=True).first()
        if not assessment:
            # Tester Issue 8: a 404 before first generation made the page
            # look broken. Serve an explicit, well-formed "not generated
            # yet" default so the screen can render its empty state.
            return Response({
                "generated": False, "versionNo": 0,
                "overallBand": None, "overallScore": None,
                "summary": "", "status": "not_generated",
                "label": "AI Generated Insights",
                "dimensions": [], "createdAt": None,
            })
        return Response(_serialise_assessment(assessment, True))


def _serialise_assessment(a, show_numeric=True):
    # Tester Issue 14: the numeric score existed in the DB but was hidden
    # behind a default-off feature flag — the band alone made versions
    # incomparable. Scores are now always surfaced.
    payload = {
        "generated": True,
        "id": str(a.id), "versionNo": a.version_no,
        "overallBand": a.overall_band,
        "summary": a.summary, "label": "AI Generated Insights",
        "status": a.status,
        "dimensions": [{
            "dimension": d.dimension, "status": d.status,
            "evidence": d.evidence, "explanation": d.explanation,
            **({"subScore": float(d.sub_score)}
               if d.sub_score is not None else {}),
        } for d in a.dimensions.all()],
        "createdAt": a.created_at.isoformat(),
    }
    if a.overall_score is not None:
        payload["overallScore"] = float(a.overall_score)
    return payload


class ReadinessGapsView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.readiness.models import ReadinessAssessment
        assessment = ReadinessAssessment.objects.filter(
            deal_id=self.deal.id, is_active=True).first()
        if not assessment:
            raise NotFoundInDeal("No readiness assessment yet.")
        return Response({"items": [{
            "id": str(g.id), "kind": g.kind, "dimension": g.dimension,
            "text": g.text, "impact": g.impact,
            # Tester Issue 5 (CP-081): the model default is the STRING
            # "none", which is truthy in JS — the UI showed a "none" chip
            # instead of the Accept/Acknowledge/Defer controls. No action
            # yet serialises as null.
            "founderAction": (g.founder_action
                              if g.founder_action and
                              g.founder_action != "none" else None),
        } for g in assessment.gaps.all()]})


class ReadinessGapActionView(DealScopedAPIView):
    """Founder accepts / acknowledges / defers a recommendation (M1-§4)."""
    required_action = "readiness.generate"

    def post(self, request, deal_id, gap_id):
        from fundos.readiness.models import ReadinessGap
        gap = ReadinessGap.objects.filter(id=gap_id,
                                          deal_id=self.deal.id).first()
        if not gap:
            raise NotFoundInDeal("Gap not found.")
        action = request.data.get("action")
        if action not in ("accepted", "acknowledged", "deferred"):
            raise DomainValidationError(
                "action must be accepted|acknowledged|deferred.",
                fields={"action": "invalid"})
        gap.founder_action = action
        gap.save(update_fields=["founder_action", "updated_at"])
        audit("readiness.gap.actioned", actor=request.user,
              deal_id=self.deal.id, entity="readiness_gap", entity_id=gap.id,
              meta={"action": action})
        return Response({"id": str(gap.id), "founderAction": action})
