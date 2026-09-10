"""
Celery health signals (Doc 6 §6.5) — ported from Gyain
`notifications/celery_signals.py`.

Wires task lifecycle signals to emit_alert so EVENT alerts fire on
operational conditions:
  celery.task.slow    — runtime > CELERY_SLOW_TASK_ALERT_SECONDS (default 300,
                        0 disables)
  celery.task.failed  — when CELERY_TASK_FAILURE_ALERT=True

Directly covers "LLM generation is slow/stuck" monitoring for the
Stage 1–3 generators.
"""
import logging
import time

from celery.signals import task_failure, task_postrun, task_prerun

logger = logging.getLogger(__name__)

# Task start times keyed by task id (worker-local, in-memory).
_task_started_at = {}

# Never alert on the alerting machinery itself — prevents feedback loops.
_EXCLUDED_TASK_PREFIXES = (
    "fundos.core.alerting.tasks.process_event_alert",
    "fundos.core.alerting.tasks.evaluate_alerts",
    "fundos.core.services.email_service.send_email_task",
    "fundos.core.services.whatsapp_service.send_whatsapp_task",
)


def _slow_threshold():
    try:
        from django.conf import settings
        return int(getattr(settings, "CELERY_SLOW_TASK_ALERT_SECONDS", 300) or 0)
    except Exception:
        return 300


def _failure_alerts_enabled():
    try:
        from django.conf import settings
        return bool(getattr(settings, "CELERY_TASK_FAILURE_ALERT", False))
    except Exception:
        return False


def _is_excluded(task_name):
    name = task_name or ""
    return any(name.startswith(p) for p in _EXCLUDED_TASK_PREFIXES)


@task_prerun.connect
def _on_task_prerun(task_id=None, task=None, **kwargs):
    try:
        _task_started_at[task_id] = time.monotonic()
    except Exception:
        pass


@task_postrun.connect
def _on_task_postrun(task_id=None, task=None, **kwargs):
    started = _task_started_at.pop(task_id, None)
    if started is None:
        return
    task_name = getattr(task, "name", "") or ""
    if _is_excluded(task_name):
        return
    threshold = _slow_threshold()
    if threshold <= 0:
        return
    runtime = time.monotonic() - started
    if runtime < threshold:
        return
    try:
        import socket
        hostname = socket.gethostname()
    except Exception:
        hostname = "?"
    logger.warning("[ALERT_ENGINE] slow task '%s' ran %.0fs (limit %ss)",
                   task_name, runtime, threshold)
    try:
        from fundos.core.alerting.tasks import emit_alert
        emit_alert("celery.task.slow", {
            "task": task_name,
            "task_id": str(task_id),
            "runtime": round(runtime),
            "limit": threshold,
            "hostname": hostname,
        })
    except Exception as e:
        logger.error("[ALERT_ENGINE] failed to emit slow-task alert: %s", e)


@task_failure.connect
def _on_task_failure(task_id=None, exception=None, sender=None, **kwargs):
    if not _failure_alerts_enabled():
        return
    task_name = getattr(sender, "name", "") or ""
    if _is_excluded(task_name):
        return
    try:
        from fundos.core.alerting.tasks import emit_alert
        emit_alert("celery.task.failed", {
            "task": task_name,
            "task_id": str(task_id),
            "error": str(exception)[:300],
        })
    except Exception as e:
        logger.error("[ALERT_ENGINE] failed to emit task-failure alert: %s", e)
