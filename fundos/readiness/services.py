"""
Readiness services (Doc 1 M1-§3/§4, Doc 8 §5 conventions: one public
function per use case; transactions; audit + emit_alert; Celery for >1s).
"""
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from fundos.core.exceptions import (
    DomainValidationError, UploadSizeError, UploadTypeError,
)
from fundos.core.services import ckb_service, recalc
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Materials — upload + assess-then-act
# ---------------------------------------------------------------------------

def validate_upload(filename: str, size: int, mime: str, *, chunked=False):
    """VAL-M1-020 — allow-list types, block executables, size caps."""
    # QA BUG-008: a genuinely empty file was accepted, scanned "clean" and
    # given a fabricated AI classification. There is nothing to analyse in
    # zero bytes — reject at the door.
    if not size or size <= 0:
        raise DomainValidationError(
            "The file is empty (0 bytes) — upload a file with content.",
            fields={"file": "empty"})
    limit = (settings.FUNDOS_UPLOAD_CHUNKED_MAX_BYTES if chunked
             else settings.FUNDOS_UPLOAD_DIRECT_MAX_BYTES)
    if size and size > limit:
        raise UploadSizeError(
            f"File exceeds the {'2 GB' if chunked else '50 MB'} limit.")
    lowered = (filename or "").lower()
    if lowered.endswith((".exe", ".bat", ".cmd", ".sh", ".msi", ".dll", ".js")):
        raise UploadTypeError("Executable files are not allowed.")
    mime = mime or ""
    allowed = settings.FUNDOS_UPLOAD_ALLOWED_MIME_PREFIXES
    if mime and not any(mime.startswith(p) for p in allowed):
        raise UploadTypeError(f"File type '{mime}' is not allowed.")


@transaction.atomic
def register_material(deal, *, filename, content: bytes, mime, category,
                      batch_id=None, user=None):
    """Direct multipart path (≤50 MB): store → scan(queued) → analyse(queued).
    BR-M1-020: every uploaded document is version-controlled."""
    from fundos.docs.models import Document
    from fundos.docs.storage import get_storage
    from fundos.readiness.models import MaterialAsset

    validate_upload(filename, len(content or b""), mime)

    remote_path = f"{deal.tenant_id}/{deal.id}/materials/{uuid.uuid4().hex}/{filename}"
    get_storage().upload_bytes(content or b"", remote_path)

    document = Document.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, doc_type="upload",
        title=filename, origin="founder_uploaded", status="Not Started",
        created_by=user if getattr(user, "pk", None) else None)
    document.new_version(storage_uri=remote_path,
                         change_summary="Founder upload", created_by=user)

    material = MaterialAsset.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, document_id=document.id,
        category=category or "other", batch_id=batch_id,
        scan_status="pending", size_bytes=len(content or b""),
        created_by=user if getattr(user, "pk", None) else None)

    audit("material.uploaded", actor=user, deal_id=deal.id,
          entity="material_asset", entity_id=material.id,
          meta={"filename": filename, "category": category})

    from fundos.readiness.tasks import scan_and_analyse_material
    scan_and_analyse_material.delay(str(deal.tenant_id), str(deal.id),
                                    str(material.id),
                                    str(user.id) if user else None)
    return material, document


@transaction.atomic
def open_upload_session(deal, *, filename, size, mime, category,
                        batch_id=None, user=None):
    """BR-M1-023 — chunked/resumable session init (>50 MB up to 2 GB)."""
    from fundos.readiness.models import UploadSession

    validate_upload(filename, size, mime, chunked=True)
    session = UploadSession.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, filename=filename,
        declared_size=size, mime=mime or "", category=category or "other",
        batch_id=batch_id, chunk_size=settings.FUNDOS_UPLOAD_CHUNK_SIZE,
        storage_tmp_uri=f"{deal.tenant_id}/{deal.id}/uploads/{uuid.uuid4().hex}",
        expires_at=timezone.now() + timezone.timedelta(hours=24),
        created_by=user if getattr(user, "pk", None) else None)
    return session


def receive_chunk(session, chunk_no: int, data: bytes):
    """Idempotent per chunk; resumable (Doc 4 M1)."""
    from fundos.core.exceptions import DomainValidationError
    from fundos.docs.storage import get_storage

    if session.status != "open":
        raise DomainValidationError("Upload session is not open.")
    if timezone.now() > session.expires_at:
        session.status = "aborted"
        session.save(update_fields=["status", "updated_at"])
        raise DomainValidationError("Upload session expired.")

    get_storage().upload_bytes(data, f"{session.storage_tmp_uri}/chunk_{chunk_no:06d}")
    received = set(session.received_chunk_numbers or [])
    if chunk_no not in received:
        received.add(chunk_no)
        session.received_chunk_numbers = sorted(received)
        session.chunks_received = len(received)
        session.save(update_fields=["received_chunk_numbers", "chunks_received",
                                    "updated_at"])
    return session.chunks_received


@transaction.atomic
def complete_upload_session(session, *, user=None):
    """Assemble → scan → register (Doc 4: E-CONFLICT-409 on missing chunks)."""
    from fundos.core.exceptions import ConflictError

    # Tester Issue 4 (CP-075): the client numbers chunks 0..N-1 (and the
    # chunk route stores them that way), but this check demanded chunks
    # 1..N — so a fully-uploaded file always "missed" one chunk and every
    # chunked upload died with E-CONFLICT-409. The check is now 0-based,
    # matching the wire contract and the assembler's ordering.
    expected = -(-session.declared_size // session.chunk_size)  # ceil
    received = set(session.received_chunk_numbers or [])
    missing = [n for n in range(expected) if n not in received]
    if missing:
        raise ConflictError(
            f"Missing chunks: {len(missing)} of {expected} chunk(s) were "
            "never received — resume the upload and retry completion.",
            fields={"missingChunks": missing[:50],
                    "expectedChunks": expected})

    session.status = "assembling"
    session.save(update_fields=["status", "updated_at"])

    from fundos.readiness.tasks import assemble_upload_session
    assemble_upload_session.delay(str(session.tenant_id), str(session.deal_id),
                                  str(session.id),
                                  str(user.id) if user else None)
    return session


# ---------------------------------------------------------------------------
# Readiness assessment — the Stage-1 output
# ---------------------------------------------------------------------------

@transaction.atomic
def generate_readiness(deal, *, user=None):
    """[DET] engine + [AI] narrative (Doc 1 M1-§4). Persists the
    scoring_config version used; supersedes the prior active version."""
    from fundos.config.feature_flags import show_numeric_readiness
    from fundos.engines.readiness_engine import compute_readiness
    from fundos.engines.scoring import active_scoring_config
    from fundos.llm import context as llm_context, guardrail
    from fundos.llm.adapter import llm_generate
    from fundos.readiness.models import (
        MaterialAsset, ReadinessAssessment, ReadinessDimension, ReadinessGap,
    )

    cfg, cfg_id = active_scoring_config()

    # Flat CKB values for the pure engine.
    flat = {}
    for group in ckb_service.snapshot(deal.id).values():
        for key, meta in group.items():
            flat[key] = meta["value"]

    materials_count = MaterialAsset.objects.filter(deal_id=deal.id).count()
    result = compute_readiness(flat, materials_count, cfg)

    # [AI] narrative — labelled, human-review-required.
    ctx = llm_context.build("readiness_summary", deal)
    ctx["deterministic_result"] = {
        "overall_band": result.overall_band,
        "dimensions": [{"dimension": d.dimension, "status": d.status,
                        "subScore": d.sub_score} for d in result.dimensions],
    }
    try:
        ai = llm_generate(
            role="readiness_summary",
            system=("You are a senior investment banker. Write an "
                    "executive-friendly readiness narrative and a short "
                    "explanation per dimension. You EXPLAIN the deterministic "
                    "results given — never change or invent scores. Separate "
                    "facts from inferences. Return JSON: {\"summary\": str, "
                    "\"dimension_explanations\": {dimension: str}}."),
            prompt="Explain this readiness assessment for the founder.",
            context=ctx, deal_id=deal.id, user=user,
            calling_context="readiness.generate")
        ai = guardrail.apply("readiness_summary", ai)
        summary = ai.get("summary", "")
        dim_expl = ai.get("dimension_explanations", {})
    except Exception as e:
        logger.error("READINESS: narrative failed (deterministic result "
                     "still saved): %s", e)
        summary, dim_expl = "", {}

    prior = ReadinessAssessment.objects.filter(deal_id=deal.id,
                                               is_active=True).first()
    version_no = (prior.version_no + 1) if prior else 1
    if prior:
        prior.is_active = False
        prior.save(update_fields=["is_active", "updated_at"])

    assessment = ReadinessAssessment.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, version_no=version_no,
        is_active=True,
        overall_score=result.overall_score,   # stored; surfaced per config flag
        overall_band=result.overall_band, summary=summary,
        scoring_config_version_id=cfg_id, status="current",
        created_by=user if getattr(user, "pk", None) else None)

    for d in result.dimensions:
        ReadinessDimension.objects.create(
            tenant_id=deal.tenant_id, assessment=assessment, deal_id=deal.id,
            dimension=d.dimension, status=d.status, sub_score=d.sub_score,
            evidence=d.evidence,
            explanation=dim_expl.get(d.dimension, d.gap))

    for kind, items in (("strength", result.strengths), ("risk", result.risks),
                        ("missing", result.missing)):
        for text in items:
            ReadinessGap.objects.create(
                tenant_id=deal.tenant_id, assessment=assessment,
                deal_id=deal.id, kind=kind, text=text)
    for rec in result.recommendations:
        ReadinessGap.objects.create(
            tenant_id=deal.tenant_id, assessment=assessment, deal_id=deal.id,
            kind="recommendation", text=rec["text"], impact=rec["impact"],
            dimension=rec.get("dimension", ""))

    recalc.clear(deal.id, "readiness_assessment")
    audit("readiness.generated", actor=user, deal_id=deal.id,
          entity="readiness_assessment", entity_id=assessment.id,
          meta={"version": version_no, "band": result.overall_band})

    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.readiness.ready", {
        "deal_id": str(deal.id), "company": deal.company.name,
        "band": result.overall_band, "tenant_id": str(deal.tenant_id)})

    from fundos.core.services.stage_state import recompute_stage_completion
    recompute_stage_completion(deal.id)

    return assessment, show_numeric_readiness()
