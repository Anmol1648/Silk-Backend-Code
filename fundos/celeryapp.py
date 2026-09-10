"""
Celery wiring — exact pattern from Doc 6 Appendix B.

Gyain bug-class to avoid: tasks silently never registered because they lived
outside tasks.py and weren't in conf.imports — hence the explicit list.
"""
import logging
import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fundos.settings.dev")

app = Celery("fundos")
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.imports = (
    # REQUIRED: autodiscover only finds tasks.py — list every non-tasks.py
    # module holding @shared_task
    "fundos.research.tasks",
    "fundos.readiness.tasks",
    "fundos.strategy.tasks",
    "fundos.materials.tasks",
    "fundos.llm.tasks",
    "fundos.core.alerting.tasks",
    "fundos.core.alerting.celery_signals",
    "fundos.core.services.email_service",
    "fundos.core.services.whatsapp_service",
)

app.autodiscover_tasks()

# ---------------------------------------------------------------------------
# Worker log correlation.
#
# The `request_context` filter (fundos.core.logging.RequestContextFilter)
# reads request_id / tenant_id / deal_id from fundos.core.scoping, which is
# backed by a threading.local. In a web request that context is set by
# middleware; a Celery task runs outside any request, so without help every
# worker line logs request_id="-" and one run cannot be told from another in
# silk_generation.log.
#
# Celery prefork workers run each task synchronously in a single thread, so
# scoping's thread-local is the correct place to set the context. We set:
#   * request_id <- the Celery task id (unique per run),
#   * tenant_id / deal_id <- from task kwargs when present,
# and clear it in postrun so ids never leak between tasks on the same worker.
# ---------------------------------------------------------------------------
from fundos.core import scoping  # noqa: E402


@task_prerun.connect
def _bind_task_log_context(task_id=None, task=None, args=None, kwargs=None, **_):
    kwargs = kwargs or {}
    try:
        scoping.set_request_id(str(task_id or "-"))
        tenant = kwargs.get("tenant_id")
        if tenant:
            scoping.set_current_tenant(str(tenant))
        deal = kwargs.get("deal_id")
        if deal:
            scoping.set_current_deal(str(deal))
    except Exception:
        # Never let logging-context setup fail a task.
        pass


@task_postrun.connect
def _unbind_task_log_context(**_):
    try:
        scoping.set_request_id("-")
        scoping.set_current_tenant(None)
        scoping.set_current_deal(None)
    except Exception:
        pass


# Beat jobs (Doc 6 §7): alert evaluator, nightly reconciliation, sweeps.
app.conf.beat_schedule = {
    "evaluate-alerts": {
        "task": "fundos.core.alerting.tasks.evaluate_alerts",
        "schedule": 300.0,  # every 5 min
    },
    "stage-completion-reconciliation": {
        "task": "fundos.core.tasks.reconcile_stage_completion",
        "schedule": crontab(hour=1, minute=30),
    },
    "upload-session-sweep": {
        "task": "fundos.readiness.tasks.sweep_expired_upload_sessions",
        "schedule": crontab(hour=2, minute=0),
    },
    "retention-purge-sweep": {
        "task": "fundos.core.tasks.retention_purge_sweep",
        "schedule": crontab(hour=3, minute=0),
    },
}
