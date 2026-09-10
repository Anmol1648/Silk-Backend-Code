"""
M0.2 Company Knowledge Base + M0.3 stage-state / dependency graph
(Doc 2 M0.2–M0.3, Doc 1 M0-§2/§4/§5).
"""
from django.conf import settings
from django.db import models

from fundos.core.models.base import BaseModel, DealScopedModel, ProvenanceMixin

CKB_GROUPS = (
    ("company", "company"), ("business", "business"), ("financial", "financial"),
    ("customers", "customers"), ("team", "team"),
    ("shareholding", "shareholding"), ("legal", "legal"),
)


class CompanyKnowledgeBase(DealScopedModel):
    """One per deal; the container (Doc 2 M0.2)."""

    completeness_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        db_table = "company_knowledge_base"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"], condition=models.Q(is_deleted=False),
                name="uq_ckb_per_deal_live"),
        ]


class CkbField(DealScopedModel, ProvenanceMixin):
    """EAV-style, provenance-tracked, versioned (Doc 2 M0.2).

    BR-M0-011: AI never silently overwrites verified data — an AI value for
    a verified field is stored in `suggested_value_json` beside it.
    """

    ckb = models.ForeignKey(CompanyKnowledgeBase, on_delete=models.CASCADE,
                            related_name="fields")
    group_key = models.CharField(max_length=32, choices=CKB_GROUPS)
    field_key = models.CharField(max_length=64)
    value_text = models.TextField(null=True, blank=True)
    value_num = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    value_json = models.JSONField(null=True, blank=True)
    ccy = models.CharField(max_length=3, blank=True, default="")
    basis = models.CharField(max_length=32, blank=True, default="")
    fact_or_inference = models.CharField(
        max_length=12, blank=True, default="",
        help_text="fact | inference (LLM-M1-003 separation)")
    suggested_value_json = models.JSONField(
        null=True, blank=True,
        help_text="AI suggestion parked beside a verified value (BR-M0-011)")

    class Meta:
        db_table = "ckb_field"
        indexes = [models.Index(fields=["deal_id", "group_key"])]
        constraints = [
            models.UniqueConstraint(
                fields=["ckb", "field_key"], condition=models.Q(is_deleted=False),
                name="uq_ckb_field_live"),
        ]

    @property
    def value(self):
        if self.value_json is not None:
            return self.value_json
        if self.value_num is not None:
            return float(self.value_num)
        return self.value_text


class CkbFieldHistory(models.Model):
    """Append-only change log per field (Doc 2 M0.2 / BR-M0-013)."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    ckb_field = models.ForeignKey(CkbField, on_delete=models.CASCADE, related_name="history")
    deal_id = models.UUIDField(db_index=True)
    old_value_json = models.JSONField(null=True, blank=True)
    new_value_json = models.JSONField(null=True, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "ckb_field_history"


class CkbSectionAssignment(DealScopedModel):
    """M1.2 collaboration — section assignment (Doc 2 M1.2)."""

    STATUS = (("assigned", "assigned"), ("in_progress", "in_progress"), ("done", "done"))

    group_key = models.CharField(max_length=32, choices=CKB_GROUPS)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                 related_name="ckb_assignments")
    status = models.CharField(max_length=16, choices=STATUS, default="assigned")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                    on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "ckb_section_assignment"


class Masterplan(DealScopedModel):
    """Stage-0 output; the founder's home (Doc 2 M0.3)."""

    stages = models.JSONField(default=list)
    estimated_duration_days = models.IntegerField(default=0)
    stage_count = models.IntegerField(default=0)
    activity_count = models.IntegerField(default=0)
    generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "masterplan"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"], condition=models.Q(is_deleted=False),
                name="uq_masterplan_per_deal_live"),
        ]


class StageState(DealScopedModel):
    """Drives soft-gating & adaptive nav (Doc 2 M0.3 / Doc 1 M0-§4).

    BR-M0-033: completion is a function, not a flag — completion_pct is
    recomputed live by core.services.stage_state.
    """

    STATUS = (("locked", "locked"), ("available", "available"),
              ("in_progress", "in_progress"), ("complete", "complete"))

    stage_no = models.SmallIntegerField()
    status = models.CharField(max_length=12, choices=STATUS, default="locked")
    completion_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    unlocked_by_override = models.BooleanField(default=False)  # audited
    hard_gate_met = models.BooleanField(default=False)

    class Meta:
        db_table = "stage_state"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id", "stage_no"], condition=models.Q(is_deleted=False),
                name="uq_stage_state_live"),
        ]


class DependencyEdge(models.Model):
    """Artefact dependency graph for recalculation propagation (Doc 2 M0.3)."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    from_artifact_type = models.CharField(max_length=48)
    from_artifact_id = models.UUIDField(null=True, blank=True)
    to_artifact_type = models.CharField(max_length=48)
    to_artifact_id = models.UUIDField(null=True, blank=True)
    relation = models.CharField(max_length=24, default="feeds")  # feeds|derives
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "dependency_edge"


class ArtifactStatus(models.Model):
    """Generic Pending-Recalculation / Needs-Review marker (Doc 2 M0.3)."""

    STATUS = (("current", "current"), ("pending_recalc", "pending_recalc"),
              ("needs_review", "needs_review"), ("locked", "locked"))

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    artifact_type = models.CharField(max_length=48)
    artifact_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="current")
    reason = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "artifact_status"
        unique_together = [("deal_id", "artifact_type", "artifact_id")]
