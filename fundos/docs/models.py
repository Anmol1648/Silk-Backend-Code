"""
Document framework (Doc 2 M0.7 / Doc 6 §9) — versioning, approval workflow,
access log. Backbone for Stage-1 uploads and every Stage-3 artefact.

Rules: versions are append-only; "restore" creates a NEW version copying an
old one; approved versions are never edited in place (edits fork).
"""
import uuid

from django.conf import settings
from django.db import models

from fundos.core.models.base import DealScopedModel

DOC_STATUSES = ("Not Started", "AI Draft Ready", "In Review", "Founder Review",
                "Advisor Review", "Approved", "Locked")

DOC_STATUS_CHOICES = [(s, s) for s in DOC_STATUSES]

DOC_TYPES = (("upload", "upload"), ("teaser", "teaser"), ("deck", "deck"),
             ("model", "model"), ("im", "im"), ("story", "story"))


class Document(DealScopedModel):
    """`document` header (Doc 2 M0.7)."""

    ORIGIN = (("founder_uploaded", "founder_uploaded"),
              ("fundos_generated", "fundos_generated"))

    workspace_id = models.UUIDField(null=True, blank=True, db_index=True)
    doc_type = models.CharField(max_length=16, choices=DOC_TYPES)
    title = models.CharField(max_length=255, blank=True, default="")
    origin = models.CharField(max_length=24, choices=ORIGIN,
                              default="fundos_generated")
    current_version_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=DOC_STATUS_CHOICES,
                              default="Not Started")
    assigned_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")
    ai_review_status = models.CharField(max_length=24, blank=True, default="")

    class Meta:
        db_table = "document"

    def set_status(self, status, actor=None):
        """BR-S3-010: one status only; changes audited."""
        from fundos.core.services.audit import audit
        old = self.status
        self.status = status
        self.save(update_fields=["status", "updated_at"])
        audit("document.status_changed", actor=actor, deal_id=self.deal_id,
              entity="document", entity_id=self.id,
              meta={"from": old, "to": status})

    def new_version(self, *, storage_uri="", content_json=None,
                    change_summary="", ai_model_version="",
                    source_prompt_ref="", scoring_config_version_id=None,
                    created_by=None):
        """Append-only version creation; updates the current pointer."""
        last = (DocumentVersion.all_objects
                .filter(document=self, is_deleted=False)
                .order_by("-version_no").first())
        version = DocumentVersion.objects.create(
            tenant_id=self.tenant_id, deal_id=self.deal_id, document=self,
            version_no=(last.version_no + 1) if last else 1,
            storage_uri=storage_uri, content_json=content_json,
            change_summary=change_summary, ai_model_version=ai_model_version,
            source_prompt_ref=source_prompt_ref,
            scoring_config_version_id=scoring_config_version_id,
            created_by=created_by,
        )
        self.current_version_id = version.id
        self.save(update_fields=["current_version_id", "updated_at"])
        return version

    def restore_version(self, version_no, actor=None):
        """BR-S3-021: any historical version restorable — as a NEW version."""
        source = DocumentVersion.objects.filter(
            document=self, version_no=version_no).first()
        if not source:
            from fundos.core.exceptions import NotFoundInDeal
            raise NotFoundInDeal("Version not found.")
        return self.new_version(
            storage_uri=source.storage_uri, content_json=source.content_json,
            change_summary=f"Restored from v{version_no}", created_by=actor)


class DocumentVersion(DealScopedModel):
    """`document_version` — immutable rows (Doc 2 M0.7)."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="versions")
    version_no = models.IntegerField(default=1)
    storage_uri = models.CharField(max_length=512, blank=True, default="")
    content_json = models.JSONField(null=True, blank=True)
    change_summary = models.TextField(blank=True, default="")
    ai_model_version = models.CharField(max_length=128, blank=True, default="")
    source_prompt_ref = models.CharField(max_length=255, blank=True, default="")
    scoring_config_version_id = models.UUIDField(null=True, blank=True)
    approval_status = models.CharField(max_length=24, blank=True, default="")

    class Meta:
        db_table = "document_version"
        unique_together = [("document", "version_no")]


class DocumentPermission(DealScopedModel):
    """Collaboration — roles × permissions (Doc 2 M0.7 / Doc 1 §7.13)."""

    PERMS = (("view", "view"), ("comment", "comment"), ("suggest", "suggest"),
             ("approve", "approve"), ("edit", "edit"), ("resolve", "resolve"))

    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="permissions")
    grantee = models.ForeignKey(settings.AUTH_USER_MODEL,
                                on_delete=models.CASCADE, related_name="+")
    perm = models.CharField(max_length=12, choices=PERMS)

    class Meta:
        db_table = "document_permission"


class DocumentComment(DealScopedModel):
    """Shared vs private comments (Doc 6 §9.2)."""

    VISIBILITY = (("shared", "shared"), ("private", "private"))

    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL,
                               on_delete=models.CASCADE, related_name="+")
    body = models.TextField()
    visibility = models.CharField(max_length=8, choices=VISIBILITY,
                                  default="shared")
    anchor = models.JSONField(null=True, blank=True)  # section/slide ref
    resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "document_comment"


class AccessLog(models.Model):
    """One row per view/download/share — "who saw what" (Doc 6 §9.3)."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name="access_logs")
    version_no = models.IntegerField(null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                             on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=12)  # view | download | share
    at = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "access_log"
