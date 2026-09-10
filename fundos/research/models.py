"""M1.1 Pillar 1 — AI Company Research models (Doc 2 M1.1)."""
from django.db import models

from fundos.core.models.base import DealScopedModel

SOURCE_TYPES = ("website", "linkedin", "market", "competitor", "industry",
                "public_funding", "patents", "news", "awards", "hiring")

CORE_SOURCES = ("website", "linkedin", "market", "competitor", "industry",
                "public_funding")
EXTENSION_SOURCES = ("patents", "news", "awards", "hiring")


class CompanyResearchSource(DealScopedModel):
    """Raw per-adapter output — the evidence trail behind proposed CKB
    fields (synthesised values live in ckb_field with source='ai_research')."""

    STATUS = (("pending", "pending"), ("success", "success"),
              ("failed", "failed"))

    source_type = models.CharField(max_length=32,
                                   choices=[(s, s) for s in SOURCE_TYPES])
    status = models.CharField(max_length=12, choices=STATUS, default="pending")
    payload = models.JSONField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=3, decimal_places=2,
                                     null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        db_table = "company_research_source"
        indexes = [models.Index(fields=["deal_id", "status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id", "source_type"],
                condition=models.Q(is_deleted=False),
                name="uq_research_source_live"),
        ]
