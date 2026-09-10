"""
Base models & mixins — write once, inherit everywhere (Doc 8 §3).

Global conventions implemented (Doc 2 §0):
  1. UUID primary keys on all domain tables.
  2. tenant_id on every table; deal_id on every per-deal row.
  3. Global soft delete (is_deleted, deleted_at, deleted_by).
  4. created_at / updated_at.
  6. Money is a triple {value, ccy, basis} — see money_kwargs()/MoneyMixin
     factories below; canonical reporting currency USD.
  8. Versioning columns via VersionedMixin.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from fundos.core.scoping import TenantManager, get_current_tenant


class BaseModel(models.Model):
    """Every FundOS table (Doc 8 §3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    objects = TenantManager()
    all_objects = models.Manager()  # steward/admin/import paths ONLY

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        # Auto-stamp the tenant from context when unset (request paths).
        if not self.tenant_id:
            tenant = get_current_tenant()
            if tenant:
                self.tenant_id = tenant
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False, deleted_by=None):
        """Soft delete (Doc 2 GC-03). Hard purge only via retention sweep."""
        self.is_deleted = True
        self.deleted_at = timezone.now()
        if deleted_by is not None:
            self.deleted_by = deleted_by
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "updated_at"])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "updated_at"])


class DealScopedModel(BaseModel):
    """Every per-deal domain table (Doc 8 §3)."""

    deal_id = models.UUIDField(db_index=True)

    class Meta:
        abstract = True


class VersionedMixin(models.Model):
    """Versioning columns (Doc 2 GC-08): version tables store these."""

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    change_summary = models.TextField(blank=True, default="")
    ai_model_version = models.CharField(max_length=128, blank=True, default="")
    source_prompt_ref = models.CharField(max_length=255, blank=True, default="")
    approval_status = models.CharField(max_length=24, blank=True, default="")

    class Meta:
        abstract = True


def money_fields(prefix, **extra):
    """Return the three columns of the money triple (Doc 2 GC-06).

    Usage inside a model body:
        locals().update(money_fields("recommended"))
    is NOT used — instead call money_contribute in a class decorator, or
    declare explicitly. Kept as a helper for programmatic model building.
    """
    return {
        f"{prefix}_value": models.DecimalField(
            max_digits=20, decimal_places=2, null=True, blank=True, **extra),
        f"{prefix}_ccy": models.CharField(max_length=3, blank=True, default="USD"),
        f"{prefix}_basis": models.CharField(max_length=32, blank=True, default="actual"),
    }


def money(instance, prefix):
    """Read a money triple off an instance as a dict (API serialisation)."""
    value = getattr(instance, f"{prefix}_value", None)
    if value is None:
        return None
    return {
        "value": float(value),
        "ccy": getattr(instance, f"{prefix}_ccy", "USD"),
        "basis": getattr(instance, f"{prefix}_basis", "actual"),
    }


class ProvenanceMixin(models.Model):
    """Provenance on knowledge fields (Doc 2 GC-07 / BR-M0-010)."""

    source = models.CharField(
        max_length=32, blank=True, default="",
        help_text="ai_research | founder | document_extracted | stage0",
    )
    confidence = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    source_ref = models.CharField(max_length=255, blank=True, default="")
    verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
