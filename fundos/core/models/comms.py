"""
M0.7 Notifications, alerting & audit (Doc 2 M0.7, Doc 6 §6/§6A).

`AlertDefinition` is the Gyain `DashboardAlert` (alerting enhancement),
renamed per Doc 6 §6.1, with the three latent bugs fixed upstream respected:
Decimal threshold comparison, owner field is `owner`, no phantom row filter.
"""
import uuid

from django.conf import settings
from django.db import models

from fundos.core.models.base import BaseModel


class Notification(models.Model):
    """One generic in-app notification table (Doc 6 §6A consolidation)."""

    CHANNEL = (("in_app", "in_app"), ("email", "email"), ("both", "both"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)  # nullable for platform
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="notifications")
    type = models.CharField(max_length=64)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    channel = models.CharField(max_length=8, choices=CHANNEL, default="in_app")
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "notification"
        ordering = ("-created_at",)


class NotificationTemplate(models.Model):
    """Editable DB rows (GC-16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, unique=True)
    channel = models.CharField(max_length=16, default="email")
    subject_tmpl = models.CharField(max_length=255, blank=True, default="")
    body_tmpl = models.TextField(blank=True, default="")
    variables = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notification_template"


class AlertDefinition(BaseModel):
    """Multi-channel alert definition — Gyain DashboardAlert port (Doc 6 §6.1).

    THRESHOLD — scheduled evaluator, metric vs condition/threshold.
    INSIGHT   — scheduled LLM narrative (phase 2 for FundOS).
    EVENT     — pushed by code via emit_alert(event_key, context);
                the PRIMARY FundOS mechanism for domain events.
    """

    class SourceType(models.TextChoices):
        THRESHOLD = "THRESHOLD"
        INSIGHT = "INSIGHT"
        EVENT = "EVENT"

    class Interval(models.TextChoices):
        MINUTES_5 = "5MIN"
        HOURLY = "HOURLY"
        DAILY = "DAILY"
        WEEKLY = "WEEKLY"
        MONTHLY = "MONTHLY"

    # Platform-scope alerts (seeded event definitions) have tenant_id NULL;
    # tenant-scoped alerts set it. Overrides BaseModel's non-null field.
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)

    name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=12, choices=SourceType.choices,
                                   default=SourceType.EVENT)
    heading = models.CharField(max_length=255, blank=True, default="")
    alert_image = models.FileField(upload_to="alert_images/", null=True, blank=True)

    # --- threshold config ---
    dashboard_ref = models.CharField(max_length=128, blank=True, default="")
    slot_number = models.IntegerField(null=True, blank=True)
    metric_column = models.CharField(max_length=128, blank=True, default="")
    condition = models.CharField(
        max_length=8, blank=True, default="",
        help_text="gt | gte | lt | lte | eq | ne")
    threshold_value = models.DecimalField(max_digits=24, decimal_places=6,
                                          null=True, blank=True)

    # --- insight config ---
    insight_user_query = models.TextField(
        blank=True, default="",
        help_text="For EVENT alerts this doubles as the message template; "
                  "may reference {placeholders} from emit_alert context.")
    insight_keywords = models.CharField(max_length=255, blank=True, default="")
    insight_focus_entity = models.CharField(max_length=255, blank=True, default="")
    insight_depth = models.CharField(max_length=32, blank=True, default="")

    # --- event config ---
    event_key = models.CharField(
        max_length=128, blank=True, default="", db_index=True,
        help_text="e.g. fundos.strategy.approved, celery.task.slow")

    # --- channels ---
    notify_email = models.BooleanField(default=True)
    notify_in_app = models.BooleanField(default=True)
    notify_whatsapp = models.BooleanField(default=False)

    # --- recipients ---
    recipients_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="alert_subscriptions")
    recipients_groups = models.ManyToManyField("auth.Group", blank=True)
    recipients_phone_numbers = models.TextField(
        blank=True, default="",
        help_text="Comma-separated explicit WhatsApp numbers (optional; "
                  "primary resolution is deal membership phone_msisdn).")

    # --- schedule (THRESHOLD/INSIGHT) ---
    interval = models.CharField(max_length=8, choices=Interval.choices,
                                blank=True, default="")
    scheduled_time = models.TimeField(null=True, blank=True)
    day_of_week = models.SmallIntegerField(null=True, blank=True)
    day_of_month = models.SmallIntegerField(null=True, blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="owned_alerts")
    is_active = models.BooleanField(default=True, db_index=True)

    # --- run bookkeeping ---
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_status = models.CharField(max_length=32, blank=True, default="")
    last_metric_value = models.DecimalField(max_digits=24, decimal_places=6,
                                            null=True, blank=True)

    class Meta:
        db_table = "alert_definition"

    def __str__(self):
        return f"[{self.source_type}] {self.name}"


class AuditLog(models.Model):
    """Append-only audit (Doc 2 M0.7 / GC-18); partition by month in PG.

    No UPDATE/DELETE via app (even admin); export-only.
    """

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=64)
    entity = models.CharField(max_length=64, blank=True, default="")
    entity_id = models.UUIDField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "audit_log"

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.pop("_force", False):
            raise RuntimeError("audit_log is append-only")
        super().save(*args, **kwargs)


class IdempotencyRecord(models.Model):
    """DB-backed idempotency store (used when Redis cache is unavailable and
    for durable audit of replays). Key = (tenant, user, path, key)."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    user_id = models.UUIDField(null=True, blank=True)
    path = models.CharField(max_length=255)
    key = models.CharField(max_length=128)
    body_sha256 = models.CharField(max_length=64)
    response_status = models.IntegerField(default=200)
    response_body = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "idempotency_record"
        unique_together = [("tenant_id", "user_id", "path", "key")]
