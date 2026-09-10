"""
Alerting engine (Doc 6 §6) — ported from the Gyain alerting enhancement
(notifications/tasks.py + gyainapp/notification_service.py) and adapted to
FundOS single-DB tenancy.

  emit_alert(event_key, context)   — the domain-event hook (use everywhere);
                                     non-blocking, never raises to the caller.
  dispatch_alert(...)              — single fan-out to email + WhatsApp;
                                     in-app rows are written by the caller.
  evaluate_alerts()                — beat evaluator for THRESHOLD/INSIGHT,
                                     under a single_instance lock.

Upstream fixes respected (Doc 6 §6.6): threshold comparisons use Decimal;
the owner field is `owner`; no phantom row filter.
"""
import logging
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from celery import shared_task
from django.utils import timezone

from fundos.core.scoping import tenant_context
from fundos.core.services.email_service import send_email_async
from fundos.core.services.whatsapp_service import normalize_msisdn, send_whatsapp_async
from fundos.core.task_locks import single_instance

logger = logging.getLogger(__name__)

# Standard FundOS event keys (Doc 6 §6.3)
STANDARD_EVENT_KEYS = (
    "fundos.readiness.ready",
    "fundos.strategy.approved",
    "fundos.package.approved",
    "fundos.member.invited",
    "fundos.doc.generated",
    "fundos.review.complete",
    "fundos.generation.failed",
    "fundos.email.failed",
    "celery.task.slow",
    "celery.task.failed",
)


# ---------------------------------------------------------------------------
# dispatch_alert — single fan-out (exact signature per Doc 6 §6.4)
# ---------------------------------------------------------------------------

def dispatch_alert(heading: str, body_text: str, *,
                   emails: Optional[List[str]] = None,
                   phone_numbers: Optional[List[str]] = None,
                   html_body: Optional[str] = None,
                   image_bytes: Optional[bytes] = None,
                   image_mime: str = "image/png",
                   image_filename: str = "alert.png",
                   channels: Optional[dict] = None) -> dict:
    """Fan a rendered alert out to the external channels (email + WhatsApp).

    channels like {'email': True, 'whatsapp': False}; missing keys default
    False; None ⇒ email only. In-app notifications are written by the caller.
    Channel failures are independent: WhatsApp down never blocks email.
    """
    channels = channels or {"email": True}
    result = {"email": False, "whatsapp": False}

    # ---- Email ----
    if channels.get("email") and emails:
        final_html = html_body
        attachments = None
        if image_bytes:
            cid = "alert_image"
            if not final_html:
                safe_text = (body_text or "").replace("\n", "<br>")
                final_html = (
                    f'<div style="font-family:Arial,sans-serif;max-width:640px">'
                    f'<img src="cid:{cid}" alt="{heading}" '
                    f'style="max-width:100%;border-radius:8px;margin-bottom:16px"/>'
                    f'<h2 style="margin:0 0 12px">{heading}</h2>'
                    f'<div style="font-size:14px;line-height:1.6">{safe_text}</div>'
                    f'</div>'
                )
            else:
                final_html = (
                    f'<img src="cid:{cid}" alt="{heading}" '
                    f'style="max-width:100%;border-radius:8px;margin-bottom:16px"/>'
                    + final_html
                )
            attachments = [(image_filename, image_bytes, image_mime, cid)]
        try:
            send_email_async(subject=heading, body=body_text or heading,
                             to=list(emails), html_body=final_html,
                             attachments=attachments)
            result["email"] = True
        except Exception as e:
            logger.error("NOTIFY: dispatch_alert email failed: %s", e)

    # ---- WhatsApp ----
    if channels.get("whatsapp") and phone_numbers:
        try:
            send_whatsapp_async(to_numbers=list(phone_numbers), heading=heading,
                                body=body_text or heading,
                                image_bytes=image_bytes, image_mime=image_mime)
            result["whatsapp"] = True
        except Exception as e:
            logger.error("NOTIFY: dispatch_alert whatsapp failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# emit_alert — domain-event hook (exact signature per Doc 6 §6.3)
# ---------------------------------------------------------------------------

def emit_alert(event_key: str, context: dict | None = None) -> None:
    """Raise an application event that active EVENT alerts subscribe to.

    Non-blocking (Celery hand-off; inline fallback). NEVER raises into the
    caller. The alert body may reference {placeholders} from `context`.
    """
    context = context or {}
    try:
        from fundos.core.scoping import get_current_tenant
        tenant_id = context.get("tenant_id") or get_current_tenant()
        try:
            process_event_alert.delay(event_key, context, str(tenant_id) if tenant_id else None)
        except Exception:
            process_event_alert(event_key, context, str(tenant_id) if tenant_id else None)
    except Exception as e:
        logger.error("[ALERT_ENGINE] emit_alert('%s') failed: %s", event_key, e)


@shared_task(name="fundos.core.alerting.tasks.process_event_alert")
def process_event_alert(event_key, context=None, tenant_id=None):
    """Resolve every active EVENT alert matching `event_key` and deliver."""
    context = context or {}
    from fundos.core.models import AlertDefinition

    qs = AlertDefinition.all_objects.filter(
        is_deleted=False, is_active=True,
        source_type=AlertDefinition.SourceType.EVENT,
        event_key=event_key,
    )
    if tenant_id:
        from django.db.models import Q
        qs = qs.filter(Q(tenant_id=tenant_id) | Q(tenant_id__isnull=True))

    total = 0
    for alert in qs:
        try:
            template_src = alert.insight_user_query or alert.heading or alert.name
            try:
                body_text = template_src.format(**context)
            except Exception:
                body_text = template_src  # missing placeholder — send raw
            heading = (alert.heading or alert.name).format(**{
                k: v for k, v in context.items()}) if alert.heading else alert.name
            _deliver_alert(alert, heading, body_text, context)
            total += 1
        except Exception as e:
            logger.error("[ALERT_ENGINE] event alert %s delivery failed: %s",
                         alert.id, e)
    logger.info("[ALERT_ENGINE] event '%s' matched %d alert(s)", event_key, total)
    return total


def _resolve_recipients(alert):
    """Emails from users/groups; WhatsApp numbers from users' verified
    phone_msisdn (Doc 6 §5 recipient resolution) + explicit numbers."""
    emails, phones = set(), set()
    for user in alert.recipients_users.filter(is_active=True, is_deleted=False):
        if user.email:
            emails.add(user.email)
        if alert.notify_whatsapp and user.phone_msisdn and user.phone_verified:
            phones.add(normalize_msisdn(user.phone_msisdn))
    for group in alert.recipients_groups.all():
        for user in group.user_set.filter(is_active=True):
            if user.email:
                emails.add(user.email)
            if alert.notify_whatsapp and getattr(user, "phone_msisdn", "") and \
                    getattr(user, "phone_verified", False):
                phones.add(normalize_msisdn(user.phone_msisdn))
    for raw in (alert.recipients_phone_numbers or "").split(","):
        n = normalize_msisdn(raw)
        if n:
            phones.add(n)
    return sorted(emails), sorted(p for p in phones if p)


def _deliver_alert(alert, heading, body_text, context=None):
    """Fan out one rendered alert: in-app rows here, external channels via
    dispatch_alert (Doc 6 §6.4)."""
    from fundos.core.models import Notification

    emails, phones = _resolve_recipients(alert)

    # In-app rows — written by the caller side of dispatch_alert.
    if alert.notify_in_app:
        deal_id = (context or {}).get("deal_id")
        for user in alert.recipients_users.filter(is_active=True, is_deleted=False):
            try:
                Notification.objects.create(
                    tenant_id=alert.tenant_id, deal_id=deal_id or None,
                    recipient=user, type=alert.event_key or alert.source_type,
                    title=heading, body=body_text,
                    payload=context or {}, channel="in_app",
                )
            except Exception as e:
                logger.error("[ALERT_ENGINE] in-app write failed: %s", e)

    image_bytes = None
    if alert.alert_image:
        try:
            alert.alert_image.open("rb")
            image_bytes = alert.alert_image.read()
            alert.alert_image.close()
        except Exception:
            image_bytes = None

    dispatch_alert(
        heading, body_text,
        emails=emails, phone_numbers=phones,
        image_bytes=image_bytes,
        channels={"email": alert.notify_email, "whatsapp": alert.notify_whatsapp},
    )

    alert.last_run_at = timezone.now()
    alert.last_run_status = "sent"
    alert.save(update_fields=["last_run_at", "last_run_status", "updated_at"])


# ---------------------------------------------------------------------------
# Scheduled evaluator (THRESHOLD / INSIGHT) — beat every 5 min
# ---------------------------------------------------------------------------

_CONDITIONS = {
    "gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
}


@shared_task(name="fundos.core.alerting.tasks.evaluate_alerts")
@single_instance(name="evaluate_alerts", expire=600)
def evaluate_alerts():
    """Evaluate due THRESHOLD/INSIGHT alerts (next_run_at <= now)."""
    from fundos.core.models import AlertDefinition

    now = timezone.now()
    due = AlertDefinition.all_objects.filter(
        is_deleted=False, is_active=True,
        source_type__in=[AlertDefinition.SourceType.THRESHOLD,
                         AlertDefinition.SourceType.INSIGHT],
    ).filter(next_run_at__lte=now) | AlertDefinition.all_objects.filter(
        is_deleted=False, is_active=True,
        source_type__in=[AlertDefinition.SourceType.THRESHOLD,
                         AlertDefinition.SourceType.INSIGHT],
        next_run_at__isnull=True,
    )

    count = 0
    for alert in due.distinct():
        try:
            with tenant_context(alert.tenant_id):
                if alert.source_type == AlertDefinition.SourceType.THRESHOLD:
                    _evaluate_threshold(alert)
                else:
                    _evaluate_insight(alert)
            count += 1
        except Exception as e:
            logger.error("[ALERT_ENGINE] evaluation failed for %s: %s", alert.id, e)
            alert.last_run_status = f"error: {str(e)[:100]}"
            alert.save(update_fields=["last_run_status", "updated_at"])
        finally:
            alert.next_run_at = _compute_next_run(alert, now)
            alert.save(update_fields=["next_run_at", "updated_at"])
    return count


def _evaluate_threshold(alert):
    """Metric vs condition/threshold. Comparison uses Decimal (Doc 6 §6.6)."""
    metric_value = _read_metric(alert)
    alert.last_metric_value = metric_value
    alert.last_run_at = timezone.now()
    if metric_value is None or alert.threshold_value is None:
        alert.last_run_status = "no_data"
        alert.save(update_fields=["last_metric_value", "last_run_at",
                                  "last_run_status", "updated_at"])
        return
    try:
        current = Decimal(str(metric_value))
        threshold = Decimal(str(alert.threshold_value))
    except (InvalidOperation, TypeError):
        alert.last_run_status = "bad_metric"
        alert.save(update_fields=["last_run_status", "updated_at"])
        return
    cond = _CONDITIONS.get(alert.condition or "gt")
    if cond and cond(current, threshold):
        body = (alert.insight_user_query or
                f"{alert.metric_column or 'metric'} is {current} "
                f"({alert.condition} {threshold}).")
        _deliver_alert(alert, alert.heading or alert.name, body,
                       {"value": str(current), "threshold": str(threshold)})
    else:
        alert.last_run_status = "not_triggered"
        alert.save(update_fields=["last_metric_value", "last_run_at",
                                  "last_run_status", "updated_at"])


def _read_metric(alert):
    """Threshold metric source. FundOS phase-1 supports operational metrics
    keyed by metric_column; per-deal KPI watches are phase 2 (Doc 6 §6.2)."""
    key = (alert.metric_column or "").strip()
    try:
        if key == "llm_error_rate_24h":
            from fundos.llm.models import LLMCallLog
            since = timezone.now() - timezone.timedelta(hours=24)
            total = LLMCallLog.objects.filter(created_at__gte=since).count()
            if not total:
                return Decimal("0")
            errors = LLMCallLog.objects.filter(created_at__gte=since,
                                               success=False).count()
            return Decimal(errors * 100) / Decimal(total)
        if key == "llm_cost_inr_24h":
            from django.db.models import Sum
            from fundos.llm.models import LLMCallLog
            since = timezone.now() - timezone.timedelta(hours=24)
            agg = LLMCallLog.objects.filter(created_at__gte=since).aggregate(
                s=Sum("cost_inr"))
            return agg["s"] or Decimal("0")
    except Exception as e:
        logger.error("[ALERT_ENGINE] metric read '%s' failed: %s", key, e)
    return None


def _evaluate_insight(alert):
    """INSIGHT alerts — LLM narrative from a pinned query/keywords.
    Phase 2 for FundOS (market-feed nudges); safe stub delivers the
    configured question through the peer_insight role when AI is live."""
    try:
        from fundos.llm.adapter import llm_generate
        question = alert.insight_user_query or alert.insight_keywords
        if not question:
            alert.last_run_status = "no_query"
            alert.save(update_fields=["last_run_status", "updated_at"])
            return
        result = llm_generate(
            role="peer_insight",
            system="You produce a short, factual 'did you know' style insight.",
            prompt=question, context={"focus": alert.insight_focus_entity},
        )
        text = result.get("text") or result.get("insight") or str(result)[:900]
        _deliver_alert(alert, alert.heading or alert.name, text)
    except Exception as e:
        logger.error("[ALERT_ENGINE] insight alert %s failed: %s", alert.id, e)
        alert.last_run_status = f"error: {str(e)[:100]}"
        alert.save(update_fields=["last_run_status", "updated_at"])


def _compute_next_run(alert, now):
    interval = alert.interval or "5MIN"
    delta = {
        "5MIN": timezone.timedelta(minutes=5),
        "HOURLY": timezone.timedelta(hours=1),
        "DAILY": timezone.timedelta(days=1),
        "WEEKLY": timezone.timedelta(weeks=1),
        "MONTHLY": timezone.timedelta(days=30),
    }.get(interval, timezone.timedelta(minutes=5))
    return now + delta


def evaluate_single_alert(alert_id):
    """Admin 'evaluate-now' (Doc 6 §6.6) — synchronous single evaluation."""
    from fundos.core.models import AlertDefinition

    alert = AlertDefinition.objects.filter(id=alert_id).first()
    if not alert:
        return "not_found"
    with tenant_context(alert.tenant_id):
        if alert.source_type == AlertDefinition.SourceType.THRESHOLD:
            _evaluate_threshold(alert)
        elif alert.source_type == AlertDefinition.SourceType.INSIGHT:
            _evaluate_insight(alert)
        else:
            # EVENT alerts fire from emit_alert; evaluate-now sends a probe.
            _deliver_alert(alert, alert.heading or alert.name,
                           "[TEST] Manual evaluation of this event alert.",
                           {"manual": True})
    alert.refresh_from_db()
    return alert.last_run_status or "evaluated"
