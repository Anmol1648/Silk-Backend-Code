"""
Generation-job tracking (Gap G6).

Usage — in a view:
    job = jobs.create_job(self.deal, "valuation", request.user)
    generate_valuation_task.delay(tenant, deal, user, job_id=str(job.id))
    return Response({"status": "queued", "jobId": str(job.id)}, 202)

In a task, wrap the service call:
    return jobs.run_tracked(job_id, services.generate_valuation, deal, user=user)

run_tracked marks running → succeeded/failed, stores the produced artefact
id and error text, emits `fundos.generation.completed|failed` through the
alerting layer, and drops an in-app Notification for the requester so the
UI can react without tight polling.
"""
import logging
import random
import time

from django.db import OperationalError, connection, transaction

from django.utils import timezone

logger = logging.getLogger(__name__)


def create_job(deal, kind, user=None):
    from fundos.core.models import GenerationJob
    return GenerationJob.objects.create(
        tenant_id=deal.tenant_id, deal_id=deal.id, kind=kind,
        requested_by_id=getattr(user, "id", None))


def serialise(job):
    return {
        "jobId": str(job.id), "kind": job.kind, "status": job.status,
        "error": job.error or None,
        "artifactId": str(job.artifact_id) if job.artifact_id else None,
        "createdAt": job.created_at.isoformat(),
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
    }


def _get(job_id):
    if not job_id:
        return None
    from fundos.core.models import GenerationJob
    return GenerationJob.objects.filter(id=job_id).first()


def _notify(job, ok):
    """Completion notification: in-app row for the requester + alert event."""
    try:
        from fundos.core.alerting.tasks import emit_alert
        emit_alert(f"fundos.generation.{'completed' if ok else 'failed'}", {
            "deal_id": str(job.deal_id), "what": job.kind,
            "job_id": str(job.id), "tenant_id": str(job.tenant_id),
            **({} if ok else {"error": (job.error or "")[:200]})})
    except Exception as e:                                    # never fail flow
        logger.warning("JOBS: emit_alert failed: %s", e)
    if not job.requested_by_id:
        return
    try:
        from fundos.core.models import Notification
        Notification.objects.create(
            tenant_id=job.tenant_id, deal_id=job.deal_id,
            recipient_id=job.requested_by_id,
            type="generation.completed" if ok else "generation.failed",
            title=(f"{job.kind.replace('_', ' ').title()} "
                   f"{'ready' if ok else 'failed'}"),
            body=("" if ok else (job.error or "")[:500]),
            payload={"jobId": str(job.id), "kind": job.kind,
                     "artifactId": str(job.artifact_id)
                     if job.artifact_id else None})
    except Exception as e:
        logger.warning("JOBS: notification failed: %s", e)


LOCK_RETRIES = 5
LOCK_BACKOFF_BASE = 0.4


def _is_locked(exc):
    """True for the one SQLite failure that a retry actually fixes."""
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def _run_with_lock_retry(fn, *args, **kwargs):
    """Run `fn`, retrying the SQLite lock that no timeout can absorb.

    WHY A TIMEOUT IS NOT ENOUGH
    ---------------------------
    The connection already sets `busy_timeout=30000`, and for two writers
    queueing that is the whole answer. It does nothing for the case that
    actually kills assessment generation.

    A transaction that BEGINs deferred starts as a reader. If it later needs
    to write and another connection has committed in the meantime, SQLite
    returns SQLITE_BUSY **immediately, without calling the busy handler** —
    waiting could only deadlock, so it refuses to wait. That is why the job
    died in 0.14 seconds against a 30-second timeout, twice, and why raising
    the timeout again would have changed nothing.

    The retry is the fix: the transaction is rolled back, so re-running it
    takes a fresh snapshot and can proceed. Django 5.1 solves this properly
    with `transaction_mode="IMMEDIATE"`; this project is on 4.2, which has no
    such option.

    Scoped deliberately narrowly — only OperationalError naming a lock, and
    only where the caller is a whole unit of work that is safe to repeat.
    PostgreSQL has MVCC and never raises this, so in production the wrapper
    calls straight through.
    """
    last = None
    for attempt in range(1, LOCK_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_locked(exc) or attempt == LOCK_RETRIES:
                raise
            last = exc
            # The failed transaction has to be cleared before the retry, or
            # the next query raises TransactionManagementError instead.
            try:
                if connection.in_atomic_block:
                    transaction.set_rollback(True)
                connection.close()
            except Exception:
                pass
            delay = LOCK_BACKOFF_BASE * (2 ** (attempt - 1))
            delay += random.uniform(0, delay / 2)   # spread simultaneous waits
            logger.warning(
                "JOBS: database locked (attempt %d/%d); retrying in %.1fs. %s",
                attempt, LOCK_RETRIES, delay, exc)
            time.sleep(delay)
    raise last                                       # pragma: no cover


def run_tracked(job_id, fn, *args, **kwargs):
    """Execute a generator under job tracking. Re-raises on failure so the
    Celery retry/alerting path still fires."""
    job = _get(job_id)
    if job:
        job.status, job.started_at = "running", timezone.now()
        job.save(update_fields=["status", "started_at"])
    try:
        result = _run_with_lock_retry(fn, *args, **kwargs)
    except Exception as e:
        if job:
            job.status, job.error = "failed", str(e)[:1000]
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error", "finished_at"])
            _notify(job, ok=False)
        raise
    if job:
        job.status, job.finished_at = "succeeded", timezone.now()
        artifact_id = getattr(result, "id", None)
        if artifact_id:
            job.artifact_id = artifact_id
        job.save(update_fields=["status", "finished_at", "artifact_id"])
        _notify(job, ok=True)
    return result
