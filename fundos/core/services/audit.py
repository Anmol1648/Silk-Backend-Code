"""Audit helper — one append-only row per sensitive action (M0-§7)."""
import logging

from django.db import transaction

from fundos.core.scoping import get_current_tenant

logger = logging.getLogger(__name__)


def audit(action, *, actor=None, deal_id=None, entity="", entity_id=None,
          meta=None, tenant_id=None):
    """Write an audit_log row. Never raises into the caller.

    "Never raises" needs two things on PostgreSQL, and this used to have only
    one of them. Catching the exception stops it reaching the caller, but a
    statement that failed inside an open atomic block leaves the transaction
    aborted, so the caller's NEXT query dies instead — the error surfaces far
    from its cause, wearing someone else's traceback. The write therefore runs
    in its own savepoint: a failure rolls back that savepoint only, and the
    caller's transaction survives intact.

    `tenant_id` is NOT NULL on the table. A missing tenant is a programming
    error at the call site, not something to paper over with a NULL insert
    that is certain to fail — so it is logged loudly and the row is skipped.
    An audit row is a record of what happened; losing one is bad, and taking
    the user's request down with it is worse.
    """
    from fundos.core.models import AuditLog

    tenant = tenant_id or get_current_tenant()
    if not tenant:
        logger.error(
            "AUDIT: '%s' has no tenant (entity=%s id=%s). The row is NOT "
            "written. Pass tenant_id= at the call site, or run inside "
            "tenant_context().", action, entity, entity_id)
        return

    try:
        with transaction.atomic():
            AuditLog.objects.create(
                tenant_id=tenant,
                deal_id=deal_id,
                actor=actor if getattr(actor, "pk", None) else None,
                action=action,
                entity=entity,
                entity_id=entity_id,
                meta=meta or {},
            )
    except Exception as e:
        logger.error("AUDIT: write failed for '%s': %s", action, e)
