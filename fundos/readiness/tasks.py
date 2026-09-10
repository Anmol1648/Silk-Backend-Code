"""
Readiness/materials Celery tasks (Doc 1 M1-§3 step 3.2 — assess-then-act).
"""
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from fundos.core.scoping import tenant_context
from fundos.core.task_locks import single_instance

logger = logging.getLogger(__name__)

# Words a model uses when asked for a confidence, and what they are worth.
# Mapped rather than rejected: "high" is a real answer to the question asked,
# and discarding it loses a metric over its notation.
_CONFIDENCE_WORDS = {
    "high": 0.9, "very high": 0.95, "certain": 0.95, "confident": 0.9,
    "medium": 0.6, "moderate": 0.6, "med": 0.6,
    "low": 0.3, "very low": 0.15, "unsure": 0.3, "uncertain": 0.3,
}


def _string_list(raw, limit=20):
    """A model's list of findings as clean strings.

    Tolerant on the way in, strict on the way out: a model asked for a list of
    problems answers with strings most of the time and with
    ``{"issue": "..."}`` the rest, and one dict in the list must not cost the
    other nine. Anything unreadable is dropped rather than rendered as
    `{'issue': ...}` in front of a reader.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("issue") or item.get("description")
                       or item.get("text") or "").strip()
        else:
            text = ""
        if text and text not in out:
            out.append(text[:500])
        if len(out) >= limit:
            break
    return out


def _confidence(raw):
    """A model's confidence as a number in 0..1, or None.

    Accepts 0.9, "0.9", "90%", and "high". Returns None for anything else —
    a null confidence is honest, whereas a guessed one is a number nobody
    asserted. Never raises: this feeds a DecimalField, and a ValidationError
    here breaks the enclosing transaction and takes the whole critique with
    it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().lower()
        if not text:
            return None
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        try:
            value = float(text.rstrip("%"))
        except ValueError:
            return None
        if text.endswith("%"):
            value /= 100.0
    # A model that reads "confidence" as a percentage returns 90, not 0.9.
    if 1 < value <= 100:
        value /= 100.0
    return round(value, 2) if 0 <= value <= 1 else None


@shared_task(name="fundos.readiness.tasks.scan_and_analyse_material", bind=True,
             max_retries=2, default_retry_delay=120)
def scan_and_analyse_material(self, tenant_id, deal_id, material_id,
                              user_id=None):
    """Virus scan + text extraction/OCR + AI classification & critique
    (LLM-M1-021), then CKB linkage. Idempotent — safe to re-run."""
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.llm import context as llm_context
        from fundos.llm.adapter import llm_generate
        from fundos.readiness.models import MaterialAsset, MaterialExtractionLink

        material = MaterialAsset.objects.filter(id=material_id).first()
        if not material:
            return "gone"
        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None

        # 1) Virus scan (ClamAV in uat/prod; the hook degrades to 'clean'
        #    with a logged warning when the scanner is absent — dev only).
        material.scan_status = _virus_scan(material)
        material.save(update_fields=["scan_status", "updated_at"])
        if material.scan_status == "infected":
            logger.error("MATERIALS: %s infected — analysis skipped",
                         material_id)
            return "infected"
        if material.scan_status != "clean":
            # Fail closed (Gap G8): a scan error blocks availability —
            # the file is never analysed or exposed until it scans clean.
            logger.error("MATERIALS: %s not scanned clean (%s) — analysis "
                         "blocked.", material_id, material.scan_status)
            return "scan_blocked"

        # 2) Text extraction / OCR (LibreOffice+openpyxl locally per Doc 6 F;
        #    kept behind a helper so the worker degrades gracefully).
        text, ocr_applied = _extract_text(material)
        material.ocr_applied = ocr_applied
        material.extraction_status = "extracted" if text else "failed"

        # 3) AI classification / summary / metrics / critique (assess-then-act,
        #    BR-M1-021 — recommendations only; founder triggers actions).
        try:
            ctx = llm_context.build("material_critique", deal,
                                    include_materials=False)
            ctx["document_text"] = (text or "")[:20000]
            ctx["category"] = material.category
            data = llm_generate(
                role="material_critique",
                system=("You are an investment-materials analyst. Classify "
                        "the document type, summarise it, extract financial "
                        "metrics (fieldKey/value/ccy/confidence), list "
                        "inconsistencies and missing information, and produce "
                        "improvement recommendations as "
                        "{target_section, issue, suggested_change, "
                        "estimated_effort_min}. Never fabricate metrics "
                        "absent from the text.\n\n"
                        # `summary` is the ONLY route by which this document
                        # reaches the Deal Scorecard: the assessment
                        # extraction reads MaterialAsset.summary and
                        # .extracted_metrics, never the file. A summary that
                        # describes the document rather than reporting its
                        # contents therefore deletes the document from the
                        # scoring pipeline while looking like a success.
                        "`summary` must report WHAT THE DOCUMENT SAYS, not "
                        "what kind of document it is. Include the concrete "
                        "figures it states — revenue and its period, growth, "
                        "burn, runway, headcount, customers, the raise "
                        "sought, market size — because this text is read "
                        "downstream as evidence about the company.\n\n"
                        'Return JSON: {"detected_type": str, "summary": str, '
                        '"extracted_metrics": [{"fieldKey","value","ccy",'
                        '"confidence"}], "inconsistencies": [str], '
                        '"missing_information": [str], "recommendations": '
                        '[{"target_section","issue","suggested_change",'
                        '"estimated_effort_min"}]}'),
                prompt="Assess this uploaded investor material.",
                context=ctx, deal_id=deal.id, user=user,
                calling_context="materials.critique")
            material.detected_type = (data.get("detected_type") or "")[:48]
            material.summary = data.get("summary", "")
            material.extracted_metrics = data.get("extracted_metrics", [])
            material.recommendations = data.get("recommendations", [])
            # Asked for in the role's schema, returned by the model, and until
            # now discarded here — the two halves of the critique a reader
            # most wants: what this document contradicts, and what it omits.
            material.inconsistencies = _string_list(
                data.get("inconsistencies"))
            material.missing_information = _string_list(
                data.get("missing_information"))

            # 4) Extraction → CKB linkage rows (founder accepts explicitly).
            #
            # `confidence` went to a DecimalField unconverted. A model asked
            # for a confidence answers "high" about as often as 0.9, and the
            # resulting ValidationError does not merely lose the metric: it
            # poisons the surrounding transaction, so every later query in
            # this task — including `material.save()` below — fails with
            # TransactionManagementError and the whole critique is discarded
            # after the model was paid for.
            #
            # Each row is written independently for the same reason: one
            # unparseable metric out of twelve must cost that metric, not the
            # other eleven and the summary with them.
            for metric in material.extracted_metrics:
                if not isinstance(metric, dict):
                    continue
                try:
                    with transaction.atomic():
                        MaterialExtractionLink.objects.create(
                            tenant_id=deal.tenant_id, deal_id=deal.id,
                            material_asset=material,
                            extracted_value_json=metric,
                            confidence=_confidence(metric.get("confidence")))
                except Exception:
                    logger.warning(
                        "MATERIALS: could not store extracted metric %r from "
                        "%s.", str(metric)[:120], material_id, exc_info=True)
        except Exception as e:
            logger.error("MATERIALS: critique failed for %s: %s",
                         material_id, e)
            from fundos.core.alerting.tasks import emit_alert
            emit_alert("fundos.generation.failed",
                       {"deal_id": str(deal_id), "what": "material_critique",
                        "error": str(e)[:200], "tenant_id": str(tenant_id)})
        material.save()

        # Materials change → readiness pending recalc (BR-M1-032).
        from fundos.core.services import recalc
        recalc.mark(deal.id, "readiness_assessment",
                    reason="material analysed")
        from fundos.core.services.stage_state import recompute_stage_completion
        recompute_stage_completion(deal.id)
        return "ok"


def _virus_scan(material):
    """Gap G8: stream the stored object through clamd (ClamAV) before
    marking it clean/available.

    * FUNDOS_CLAMAV_HOST/PORT configured → INSTREAM scan; hit ⇒ 'infected'
      (quarantined: analysis never runs on it); scanner error ⇒ fail closed
      ('pending' — the file stays unavailable) when scanning is required.
    * Scanner absent and FUNDOS_VIRUS_SCAN_REQUIRED=False (dev only) →
      degrade to 'clean' with a logged warning.
    * Scanner absent and scanning REQUIRED (uat/prod) → fail closed.
    """
    from django.conf import settings as dj_settings

    host = getattr(dj_settings, "FUNDOS_CLAMAV_HOST", "")
    port = int(getattr(dj_settings, "FUNDOS_CLAMAV_PORT", 3310))
    required = bool(getattr(dj_settings, "FUNDOS_VIRUS_SCAN_REQUIRED", False))

    if not host:
        if required:
            logger.error("MATERIALS: virus scanning required but "
                         "FUNDOS_CLAMAV_HOST is not set — failing closed.")
            return "pending"
        logger.warning("MATERIALS: virus scanner not configured — marking "
                       "clean (dev only; uat/prod must run ClamAV).")
        return "clean"

    try:
        import io

        import clamd

        data = _material_bytes(material)
        if data is None:
            logger.error("MATERIALS: could not read %s for scanning — "
                         "failing closed.", material.id)
            return "pending" if required else "clean"
        client = clamd.ClamdNetworkSocket(host=host, port=port, timeout=60)
        result = client.instream(io.BytesIO(data)) or {}
        status_, name = (result.get("stream") or ("ERROR", ""))[:2]
        if status_ == "OK":
            return "clean"
        if status_ == "FOUND":
            logger.error("MATERIALS: %s INFECTED (%s) — quarantined.",
                         material.id, name)
            from fundos.core.alerting.tasks import emit_alert
            emit_alert("fundos.material.infected", {
                "deal_id": str(material.deal_id),
                "material_id": str(material.id), "signature": str(name),
                "tenant_id": str(material.tenant_id)})
            return "infected"
        logger.error("MATERIALS: scanner returned %s for %s — failing "
                     "closed.", status_, material.id)
        return "pending" if required else "clean"
    except Exception as e:
        logger.error("MATERIALS: virus scan error for %s: %s", material.id, e)
        return "pending" if required else "clean"


def _material_bytes(material):
    """Fetch the stored object's bytes for scanning."""
    try:
        import tempfile

        from fundos.docs.models import DocumentVersion
        from fundos.docs.storage import get_storage

        version = DocumentVersion.objects.filter(
            document_id=material.document_id).order_by("-version_no").first()
        if not version or not version.storage_uri:
            return None
        with tempfile.NamedTemporaryFile(delete=False) as f:
            local = f.name
        if not get_storage().download_file(version.storage_uri, local):
            return None
        with open(local, "rb") as fh:
            return fh.read()
    except Exception as e:
        logger.error("MATERIALS: byte fetch failed: %s", e)
        return None


def _extract_text(material):
    """Extract a material's text, and record HOW it was extracted.

    Uses the company-profile pipeline's Source-2 extractor
    (:mod:`fundos.profile.pipeline.source2_documents`), so upload-time
    extraction and generation-time extraction cannot disagree about what a
    file contains: MarkItDown for native text and tables across PDF, DOCX,
    XLSX, PPTX, CSV and HTML, plus selective OCR for scanned PDF pages and
    images embedded in slides.

    This replaces a helper that read `.txt`, `.csv` and a PDF's text layer and
    returned an empty string for everything else — with a comment promising
    "LibreOffice headless + openpyxl in deployed envs", machinery that was
    never built. A founder who uploaded a pitch deck and a financial model
    contributed nothing to their profile and had no way to know, because the
    file still appeared in the Document Center marked as uploaded.

    :returns: ``(text, ocr_applied)``. The per-file detail (handler, OCR
        status and reason, character counts) is written onto the material row
        as a side effect, so a support question about one file is answerable
        from that row.
    """
    import os
    import tempfile

    try:
        from fundos.docs.models import DocumentVersion
        from fundos.docs.storage import get_storage
        from fundos.profile.pipeline import source2_documents as source2

        version = DocumentVersion.objects.filter(
            document_id=material.document_id).order_by("-version_no").first()
        if not version or not version.storage_uri:
            return "", False

        # The suffix matters: both MarkItDown and the OCR dispatch key off the
        # extension, so a temp file without one is handled as "unknown" and
        # silently skips OCR on a scanned PDF.
        filename = os.path.basename(version.storage_uri)
        suffix = os.path.splitext(filename)[1].lower()
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        handle.close()
        local = handle.name
        try:
            if not get_storage().download_file(version.storage_uri, local):
                return "", False

            policy = source2.policy_for(filename)
            try:
                native = source2._convert_with_markitdown(local)
            except Exception as exc:
                logger.warning("MATERIALS: native extraction failed for %s: "
                               "%s", filename, exc)
                native = ""

            # `_model_stage` replaced `_ocr_stage` when the OCR engines were
            # dropped for the document model. It reads only `tenant_id` off
            # the last argument, and the material carries one, so the task
            # does not need a profile to hand it.
            outcome = source2._model_stage(local, filename, policy,
                                           lambda _m: None, [], material)
            text = "\n\n".join(part for part in (native, outcome.text) if part)

            _record_extraction_detail(material, filename, suffix, policy,
                                      outcome, len(native))
            return text, outcome.status == "triggered"
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
    except Exception as e:
        logger.warning("MATERIALS: extraction failed: %s", e)
        return "", False


def _record_extraction_detail(material, filename, suffix, policy, outcome,
                              native_chars):
    """Stamp how this file was read onto the material row. Never raises.

    Field-tolerant so an install that has the code but not yet the
    extraction-detail migration degrades to a partial update rather than
    failing the whole analysis task over bookkeeping.
    """
    try:
        fields = {"file_type": suffix, "handler": policy.handler,
                  "native_chars": native_chars}
        fields.update(outcome.as_document_fields(None))
        writable = [name for name in fields if hasattr(material, name)]
        for name in writable:
            setattr(material, name, fields[name])
        if writable:
            material.save(update_fields=writable)
    except Exception:
        logger.debug("MATERIALS: could not record extraction detail for %s",
                     filename, exc_info=True)


@shared_task(name="fundos.readiness.tasks.assemble_upload_session", bind=True)
def assemble_upload_session(self, tenant_id, deal_id, session_id, user_id=None):
    """Assemble chunks → register material (Doc 4 chunked complete)."""
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.docs.storage import get_storage
        from fundos.readiness.models import UploadSession
        from fundos.readiness.services import register_material

        session = UploadSession.objects.filter(id=session_id).first()
        if not session or session.status not in ("assembling", "open"):
            return "skipped"
        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None
        storage = get_storage()

        try:
            session.status = "scanning"
            session.save(update_fields=["status", "updated_at"])
            parts = []
            import tempfile
            for n in sorted(session.received_chunk_numbers or []):
                with tempfile.NamedTemporaryFile(delete=False) as f:
                    local = f.name
                storage.download_file(
                    f"{session.storage_tmp_uri}/chunk_{n:06d}", local)
                with open(local, "rb") as fh:
                    parts.append(fh.read())
            content = b"".join(parts)

            material, _ = register_material(
                deal, filename=session.filename, content=content,
                mime=session.mime, category=session.category,
                batch_id=session.batch_id, user=user)
            session.material_asset = material
            session.status = "complete"
            session.save(update_fields=["material_asset", "status", "updated_at"])
            for n in sorted(session.received_chunk_numbers or []):
                storage.delete_file(f"{session.storage_tmp_uri}/chunk_{n:06d}")
            return "ok"
        except Exception as e:
            logger.error("UPLOAD: assembly failed for %s: %s", session_id, e)
            session.status = "failed"
            session.save(update_fields=["status", "updated_at"])
            raise


@shared_task(name="fundos.readiness.tasks.sweep_expired_upload_sessions")
@single_instance(name="sweep_expired_upload_sessions", expire=1800)
def sweep_expired_upload_sessions():
    """Nightly sweep — expire incomplete sessions past 24h (Doc 2 M1.3)."""
    from fundos.readiness.models import UploadSession
    expired = UploadSession.all_objects.filter(
        is_deleted=False, status="open", expires_at__lt=timezone.now())
    count = expired.update(status="aborted")
    logger.info("UPLOAD: swept %d expired sessions", count)
    return count


@shared_task(name="fundos.readiness.tasks.run_readiness", bind=True)
def run_readiness(self, tenant_id, deal_id, user_id=None, job_id=None):
    """Job-tracked readiness generation (Tester Issue 12: the generate
    endpoint must behave like every other generator — 202 + pollable job —
    instead of a synchronous 201)."""
    from fundos.core.services import jobs
    return jobs.run_tracked(job_id, _run_readiness, tenant_id, deal_id,
                            user_id)


def _run_readiness(tenant_id, deal_id, user_id=None):
    with tenant_context(tenant_id):
        from fundos.core.models import Deal, User
        from fundos.readiness.services import generate_readiness

        deal = Deal.objects.get(id=deal_id)
        user = User.objects.filter(id=user_id).first() if user_id else None
        assessment, _show_numeric = generate_readiness(deal, user=user)
        return assessment
