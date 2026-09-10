"""
M1.3 Existing Materials + M1.4 Readiness Assessment models
(Doc 2 M1.3–M1.4).
"""
from django.conf import settings
from django.db import models

from fundos.core.models.base import DealScopedModel


class MaterialAsset(DealScopedModel):
    """Per uploaded artefact, assess-then-act state (Doc 2 M1.3)."""

    CATEGORY = (("investment_material", "investment_material"),
                ("financial", "financial"), ("legal", "legal"),
                ("company", "company"), ("other", "other"))
    AI_STATUS = (("", ""), ("reuse", "reuse"), ("improve", "improve"),
                 ("replace", "replace"), ("generate", "generate"))
    SCAN = (("pending", "pending"), ("clean", "clean"), ("infected", "infected"))
    EXTRACTION = (("pending", "pending"), ("extracted", "extracted"),
                  ("failed", "failed"))

    document_id = models.UUIDField(null=True, blank=True, db_index=True)
    category = models.CharField(max_length=32, choices=CATEGORY, default="other")
    detected_type = models.CharField(max_length=48, blank=True, default="")
    ai_status = models.CharField(max_length=12, choices=AI_STATUS,
                                 blank=True, default="")
    scan_status = models.CharField(max_length=12, choices=SCAN, default="pending")
    summary = models.TextField(blank=True, default="")
    extracted_metrics = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(
        default=list, blank=True,
        help_text="[{target_section, issue, suggested_change, effort_min}] "
                  "(BR-M1-021)")

    # The critique asks the model for four things and used to keep two. These
    # were generated, paid for, and dropped on every upload ever made: the
    # role's schema declares `inconsistencies` and `missing_information`, the
    # model returns them, and `scan_and_analyse_material` assigned only
    # detected_type, summary, extracted_metrics and recommendations.
    #
    # What the reader saw instead was derived from the FILENAME — "filename
    # may not match category" — which is a guess about the name of a file
    # rather than a finding about its contents, and reads identically to one.
    inconsistencies = models.JSONField(
        default=list, blank=True,
        help_text="Contradictions the model found INSIDE this document — a "
                  "figure stated two ways, a date that disagrees with a "
                  "chart. Strings, in the model's own words.")
    missing_information = models.JSONField(
        default=list, blank=True,
        help_text="What a reader of this document would expect and not find. "
                  "Distinct from a low section-coverage count: this is the "
                  "model's reading, not a checklist match.")
    batch_id = models.UUIDField(null=True, blank=True)   # folder uploads
    ocr_applied = models.BooleanField(default=False)
    extraction_status = models.CharField(max_length=12, choices=EXTRACTION,
                                         default="pending")
    size_bytes = models.BigIntegerField(default=0)

    # --- How this file was actually read ----------------------------------
    # "Uploaded but unread" is the most misleading state in the system: the
    # founder sees their file listed and assumes it was used. Before these
    # columns, a DOCX or XLSX contributed nothing at all (the extractor
    # returned "" for every format except PDF and plain text) and there was no
    # way to tell from the outside. `ocr_applied` existed and was always False,
    # because nothing ever ran OCR.
    #
    # Each field records a decision the extractor made, so "was my deck read,
    # and how?" is answerable from the row rather than from logs.
    file_type = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Lowercase extension, e.g. '.pptx'. Decides the handler.")
    handler = models.CharField(
        max_length=32, blank=True, default="",
        help_text="How this type is treated: native+ocr / native only / "
                  "ocr only / native (best effort).")
    native_chars = models.IntegerField(
        default=0,
        help_text="Characters native extraction recovered. Zero on a PDF "
                  "means it is almost certainly a scan.")
    ocr_status = models.CharField(
        max_length=12, blank=True, default="",
        help_text="pending / triggered / skipped / disabled / failed.")
    ocr_reason = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Why OCR did or did not run for this specific file. A "
                  "'skipped' with no reason is indistinguishable from a bug.")
    ocr_engines = models.JSONField(default=list, blank=True)
    ocr_units_total = models.IntegerField(
        default=0, help_text="Pages/slides/images considered for OCR.")
    ocr_units_ocred = models.IntegerField(
        default=0, help_text="Of those, how many went through an engine.")
    ocr_chars = models.IntegerField(default=0)
    ocr_seconds = models.FloatField(null=True, blank=True)
    extraction_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "material_asset"


class UploadSession(DealScopedModel):
    """Chunked/resumable large-file uploads (Doc 2 M1.3 / BR-M1-023).
    Sessions expire after 24h if incomplete (nightly sweep). On complete:
    assemble → virus scan → create material_asset + document/version."""

    STATUS = (("open", "open"), ("assembling", "assembling"),
              ("scanning", "scanning"), ("complete", "complete"),
              ("failed", "failed"), ("aborted", "aborted"))

    filename = models.CharField(max_length=255)
    declared_size = models.BigIntegerField()
    mime = models.CharField(max_length=128, blank=True, default="")
    category = models.CharField(max_length=32, default="other")
    batch_id = models.UUIDField(null=True, blank=True)
    chunk_size = models.IntegerField(default=8 * 1024 * 1024)
    chunks_received = models.IntegerField(default=0)
    received_chunk_numbers = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default="open")
    storage_tmp_uri = models.CharField(max_length=512, blank=True, default="")
    material_asset = models.ForeignKey(MaterialAsset, null=True, blank=True,
                                       on_delete=models.SET_NULL,
                                       related_name="+")
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "upload_session"


class MaterialExtractionLink(DealScopedModel):
    """Extracted value → CKB field linkage (Doc 2 M1.3)."""

    material_asset = models.ForeignKey(MaterialAsset, on_delete=models.CASCADE,
                                       related_name="extraction_links")
    ckb_field_id = models.UUIDField(null=True, blank=True)
    extracted_value_json = models.JSONField(default=dict)
    confidence = models.DecimalField(max_digits=3, decimal_places=2,
                                     null=True, blank=True)
    accepted = models.BooleanField(default=False)

    class Meta:
        db_table = "material_extraction_link"


class ReadinessAssessment(DealScopedModel):
    """Versioned, regenerable readiness output (Doc 2 M1.4)."""

    STATUS = (("current", "current"), ("pending_recalc", "pending_recalc"))

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    overall_score = models.DecimalField(max_digits=5, decimal_places=2,
                                        null=True, blank=True)
    overall_band = models.CharField(max_length=24, blank=True, default="")
    summary = models.TextField(blank=True, default="")   # "AI Generated Insights"
    scoring_config_version_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="current")

    class Meta:
        db_table = "readiness_assessment"


class ReadinessDimension(models.Model):
    """Per-dimension result (Doc 2 M1.4)."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    assessment = models.ForeignKey(ReadinessAssessment, on_delete=models.CASCADE,
                                   related_name="dimensions")
    deal_id = models.UUIDField(db_index=True)
    dimension = models.CharField(max_length=48)
    status = models.CharField(max_length=20)   # ready/needs_attention/missing/not_applicable
    sub_score = models.DecimalField(max_digits=5, decimal_places=2,
                                    null=True, blank=True)
    evidence = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True, default="")

    class Meta:
        db_table = "readiness_dimension"
        indexes = [models.Index(fields=["assessment"])]


class ReadinessGap(DealScopedModel):
    """Missing-Information Register + strengths/risks (Doc 2 M1.4)."""

    KIND = (("strength", "strength"), ("risk", "risk"), ("missing", "missing"),
            ("recommendation", "recommendation"))
    ACTION = (("none", "none"), ("accepted", "accepted"),
              ("acknowledged", "acknowledged"), ("deferred", "deferred"))

    assessment = models.ForeignKey(ReadinessAssessment, on_delete=models.CASCADE,
                                   related_name="gaps")
    kind = models.CharField(max_length=16, choices=KIND)
    dimension = models.CharField(max_length=48, blank=True, default="")
    text = models.TextField()
    impact = models.CharField(max_length=8, blank=True, default="")  # high/med/low
    founder_action = models.CharField(max_length=16, choices=ACTION,
                                      default="none")

    class Meta:
        db_table = "readiness_gap"
