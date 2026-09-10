"""Diagnostics: run records and correlated events.

THE ACCEPTANCE CRITERION THIS APP EXISTS TO MEET
------------------------------------------------
Given only a company ID and a complaint that a number looks wrong, an engineer
must be able to produce the full derivation — source document, extracted fact,
rule applied, config version, score, and every weight in the roll-up — without
re-running anything.

That is not achievable with log files. The supplied stack called
`logging.basicConfig(FileHandler(app.log, mode='w'))`, truncating the log at
the start of every run. Perfect for a desktop tool with a live console tail;
useless for answering "what happened last Saturday", because last Saturday's
log was deleted by this Saturday's run.

TWO TABLES
----------
  RunRecord   One row per job of any kind — refresh, extraction, scoring,
              matching. Inputs, config version, counts, timings, outcome.
              Queryable, so "it worked last week" is answerable.

  DiagEvent   Structured events under a correlation ID. One identifier follows
              a request through every task it spawns, so tracing an issue is a
              filter rather than a reconstruction.
"""
import uuid

from django.conf import settings
from django.db import models


class RunRecord(models.Model):
    """One execution of anything worth being able to ask about later."""

    KINDS = [
        ("refresh", "Data refresh"),
        ("extraction", "Evidence extraction"),
        ("scoring", "Assessment scoring"),
        ("rubric_review", "AI rubric review"),
        ("matching", "Investor matching"),
        ("import", "Bulk import"),
        ("classification", "Investor classification"),
        ("other", "Other"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correlation_id = models.UUIDField(db_index=True, default=uuid.uuid4)
    kind = models.CharField(max_length=24, choices=KINDS, db_index=True)

    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    company_id = models.UUIDField(null=True, blank=True, db_index=True)
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)
    subject = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Human-readable subject — company name, source name, query.")

    inputs = models.JSONField(default=dict, blank=True)
    config_version = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Config generation in force. Without it a historical result "
                  "cannot be reproduced after a threshold changes.")
    counts = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")

    status = models.CharField(
        max_length=16, default="running", db_index=True,
        choices=[("running", "Running"), ("ok", "Completed"),
                 ("warning", "Completed with warnings"), ("failed", "Failed")])
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "diag_run_record"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["kind", "-started_at"]),
            models.Index(fields=["deal_id", "-started_at"]),
            models.Index(fields=["correlation_id"]),
        ]

    def __str__(self):
        return f"{self.kind} · {self.subject} · {self.status}"


class DiagEvent(models.Model):
    """A structured event. Correlated, filterable, and kept.

    `component` and `stage` are separate from `message` deliberately: filtering
    by component is how someone finds the twelve lines that matter among the
    thousand that do not.
    """

    LEVELS = [("debug", "Debug"), ("info", "Info"), ("warning", "Warning"),
              ("error", "Error"), ("critical", "Critical")]

    id = models.BigAutoField(primary_key=True)
    correlation_id = models.UUIDField(db_index=True)
    run = models.ForeignKey(RunRecord, null=True, blank=True,
                            on_delete=models.CASCADE, related_name="events")

    level = models.CharField(max_length=10, choices=LEVELS, default="info",
                             db_index=True)
    component = models.CharField(max_length=64, blank=True, default="",
                                 db_index=True)
    stage = models.CharField(max_length=64, blank=True, default="")
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True)

    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)
    source_location = models.CharField(
        max_length=255, blank=True, default="",
        help_text="file:line — traceable to origin without a debugger, the "
                  "one genuinely useful habit from the original log format.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "diag_event"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["correlation_id", "created_at"])]

    def __str__(self):
        return f"[{self.level}] {self.component}: {self.message[:60]}"
