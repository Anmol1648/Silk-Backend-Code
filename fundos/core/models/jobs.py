"""
GenerationJob (Gap G6) — a queryable handle for every async generator.

Every /…/generate endpoint now returns `{status:"queued", jobId}` and the
frontend polls GET /deals/{id}/jobs/{jobId} until the job reaches
succeeded/failed, instead of guessing from an ambiguous 404 on the artefact
GET. Completion also emits `fundos.generation.completed|failed` through the
alerting layer and an in-app notification to the requester.
"""
import uuid

from django.db import models


class GenerationJob(models.Model):
    STATUS = (("queued", "queued"), ("running", "running"),
              ("succeeded", "succeeded"), ("failed", "failed"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    kind = models.CharField(max_length=32)          # peers|raise|valuation|…
    status = models.CharField(max_length=12, choices=STATUS, default="queued")
    error = models.TextField(blank=True, default="")
    artifact_id = models.UUIDField(null=True, blank=True)
    requested_by_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "generation_job"
        indexes = [models.Index(fields=["deal_id", "kind", "-created_at"])]

    def __str__(self):
        return f"{self.kind}:{self.status}"
