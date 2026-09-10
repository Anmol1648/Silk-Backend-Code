"""
Tenancy & scoping spine (Doc 2 §0.2 / Doc 8 §3).

Two distinct keys, never conflated:
  * tenant_id — customer-organisation boundary, resolved AT LOGIN (JWT claim),
    never from a URL/subdomain. Single-DB with disciplined tenant_id filtering.
  * deal_id   — an ordinary entity scope inside the tenant. Deal visibility =
    membership(user, deal) + RBAC. NO row-level security, no SQL rewriting.

The authenticated request sets the thread-local tenant (IdpJWTAuthentication);
Celery code passes it explicitly via `with tenant_context(tenant_id):`.
"""
import threading
import uuid
from contextlib import contextmanager

from django.db import models

_local = threading.local()


def set_current_tenant(tenant_id):
    _local.tenant_id = str(tenant_id) if tenant_id else None


def get_current_tenant():
    return getattr(_local, "tenant_id", None)


def set_request_id(request_id):
    _local.request_id = request_id


def get_request_id():
    return getattr(_local, "request_id", None)


def set_current_deal(deal_id):
    _local.deal_id = str(deal_id) if deal_id else None


def get_current_deal():
    return getattr(_local, "deal_id", None)


@contextmanager
def tenant_context(tenant_id):
    """Explicit tenant pinning for Celery workers / management commands.

    Worker context rule (Doc 6 §7): long jobs iterate one deal at a time
    inside this context — never a cross-tenant bulk query.
    """
    prev = get_current_tenant()
    set_current_tenant(tenant_id)
    try:
        yield
    finally:
        set_current_tenant(prev)


class TenantQuerySet(models.QuerySet):
    def delete(self):
        """Soft delete is global (Doc 2 GC-03): .delete() sets deleted_at."""
        from django.utils import timezone
        return super().update(is_deleted=True, deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()


class TenantManager(models.Manager):
    """Filters is_deleted=False + tenant from the request/worker context.

    `all_objects` (plain Manager) is for steward/admin/import paths ONLY
    (lint-checked, Doc 8 §3).
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db).filter(is_deleted=False)
        tenant_id = get_current_tenant()
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


def new_uuid():
    return uuid.uuid4()
