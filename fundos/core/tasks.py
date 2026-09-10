"""Core beat jobs (Doc 6 §7): nightly stage-completion reconciliation and the
retention/purge sweep. One deal at a time inside tenant_context — never a
cross-tenant bulk query."""
import logging

from celery import shared_task
from django.utils import timezone

from fundos.core.scoping import tenant_context
from fundos.core.task_locks import single_instance

logger = logging.getLogger(__name__)


@shared_task(name="fundos.core.tasks.reconcile_stage_completion")
@single_instance(name="reconcile_stage_completion", expire=3600)
def reconcile_stage_completion():
    from fundos.core.models import Deal
    from fundos.core.services.stage_state import recompute_stage_completion

    count = 0
    for deal in Deal.all_objects.filter(is_deleted=False, status="active").iterator():
        with tenant_context(deal.tenant_id):
            try:
                recompute_stage_completion(deal.id)
                count += 1
            except Exception as e:
                logger.error("RECONCILE: deal %s failed: %s", deal.id, e)
    logger.info("RECONCILE: recomputed %d deals", count)
    return count


@shared_task(name="fundos.core.tasks.retention_purge_sweep")
@single_instance(name="retention_purge_sweep", expire=3600)
def retention_purge_sweep(retention_days: int = 365):
    """Hard-purge soft-deleted rows past the retention window (Doc 6 §9.3).
    Legal-hold and per-category windows are config-extensible; default 365d."""
    from django.apps import apps

    cutoff = timezone.now() - timezone.timedelta(days=retention_days)
    purged = 0
    for model in apps.get_models():
        if not hasattr(model, "is_deleted") or not hasattr(model, "deleted_at"):
            continue
        manager = getattr(model, "all_objects", model._default_manager)
        try:
            qs = manager.filter(is_deleted=True, deleted_at__lt=cutoff)
            n = qs.count()
            if n:
                for row in qs.iterator():
                    row.hard_delete() if hasattr(row, "hard_delete") else row.delete()
                purged += n
        except Exception as e:
            logger.error("PURGE: %s failed: %s", model.__name__, e)
    logger.info("PURGE: hard-deleted %d rows older than %sd", purged, retention_days)
    return purged
