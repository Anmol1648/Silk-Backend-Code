"""Running pipeline work in threads without losing Django's ambient state.

Why threads and not a Celery chord
----------------------------------
The research batches are independent and network-bound, so they want to run
concurrently. The obvious Django answer is a chord of ten tasks with a
callback, and it is the wrong one here for four reasons:

1. **The tenant is a ``threading.local``** (:mod:`fundos.core.scoping`), not a
   database column the task could re-derive. Every chord task would have to
   re-establish it anyway, so the chord buys no isolation we do not already
   have to write.
2. **The dossier is shared mutable state.** Ten tasks in ten processes cannot
   share a :class:`~fundos.profile.pipeline.dossier.DossierWriter`; they would
   need a database or Redis rendezvous purely to reassemble a document that one
   process can hold in memory.
3. **Dev runs Celery eagerly** (``CELERY_TASK_ALWAYS_EAGER``), so a chord would
   execute serially there and the concurrent path would go untested exactly
   where it is cheapest to test.
4. **Failure isolation is per batch, not per task.** A failed batch must become
   an error stub inside the dossier, which is a thing the *caller* does — a
   chord's error handling works against that.

What this module guarantees
---------------------------
A worker thread starts with none of the ambient state the code it runs assumes.
``ThreadPoolExecutor`` inherits three things this package depends on:

* the **thread-local tenant/deal** — a worker starts with no tenant, so every
  ``TenantManager`` query in it would silently span tenants;
* the **contextvar trace** — :func:`fundos.profile.trace.current_trace` would
  return ``None``, so every ``trace_event`` from a worker would vanish;
* the **LLM cost ledger**, thread-local by design (one book per run) — a worker
  would start an empty one, append its calls there and let them die with the
  thread, so a run that dispatched eleven calls would report one.

and it introduces one Django-specific hazard:

* **a database connection per thread**, which Django will not close for a
  thread it did not create. Left open, every batch leaks a connection for the
  life of the worker process.

:func:`in_worker_context` handles all four. Nothing in this package may call
``ThreadPoolExecutor.submit`` directly — go through :func:`run_concurrently`.
"""
import contextvars
import functools
import logging
from concurrent.futures import ThreadPoolExecutor

from django.db import connection

from fundos.core.scoping import (get_current_deal, get_current_tenant,
                                 set_current_deal, set_current_tenant)
from fundos.llm import ledger

logger = logging.getLogger("fundos.profile")


def in_worker_context(fn, *, tenant_id, deal_id, context, ledger_book=None):
    """Wrap ``fn`` so it runs with the parent's tenant, deal, trace and ledger.

    :param fn: The callable to wrap.
    :param tenant_id: Tenant to pin for the duration of the call. Captured from
        the parent thread at submit time, since the worker cannot derive it.
    :param deal_id: Deal to pin, for research adapters and trace attribution.
    :param context: A :func:`contextvars.copy_context` snapshot taken on the
        parent thread; carries the active ``GenerationTrace``. Must be this
        job's OWN copy — see :func:`run_concurrently`.
    :param ledger_book: The parent's LLM cost book, so calls dispatched here
        are counted against the run that caused them.
    :returns: A callable safe to hand to a worker thread.
    """
    @functools.wraps(fn)
    def runner(*args, **kwargs):
        set_current_tenant(tenant_id)
        set_current_deal(deal_id)
        ledger.adopt(ledger_book)
        try:
            # `context.run` is what carries the trace contextvar across the
            # thread boundary. It must wrap the call itself, not just the
            # setup, or the trace is only visible for the setup.
            return context.run(fn, *args, **kwargs)
        finally:
            # Clear the thread-locals before releasing the thread back to the
            # pool. A pooled thread is reused, and a stale tenant left on it
            # would leak into whatever runs next — the one bug in this file
            # that would be a security issue rather than a correctness one.
            set_current_tenant(None)
            set_current_deal(None)
            try:
                # Django opens a connection per thread and closes it only for
                # threads it manages. This one is ours.
                connection.close()
            except Exception:
                logger.debug("could not close worker DB connection",
                             exc_info=True)
    return runner


def run_concurrently(jobs, *, max_workers, thread_name_prefix="profile"):
    """Run ``jobs`` in a bounded thread pool, preserving Django context.

    Every job runs to completion regardless of what its siblings do — an
    exception is captured and returned in place, never raised here and never
    allowed to cancel a sibling that has already spent an LLM call. This is the
    threading equivalent of ``asyncio.gather(..., return_exceptions=True)``, and
    it is chosen for the same reason: the caller needs to know *which* job
    failed in order to attribute the failure, and a bare raise loses that.

    :param jobs: An iterable of zero-argument callables.
    :param max_workers: Pool size. Clamped to at least 1, and never more than
        the number of jobs — an idle worker per unused slot is pure overhead.
    :param thread_name_prefix: Shows up in thread dumps; worth setting so a
        stuck worker is identifiable.
    :returns: A list of results positionally aligned with ``jobs``. An entry is
        the job's return value, or the ``Exception`` it raised.
    """
    jobs = list(jobs)
    if not jobs:
        return []

    if int(max_workers or 1) <= 1:
        # Run inline rather than spinning up a pool of one.
        #
        # Not just an optimisation. A single worker thread still gets its own
        # database connection, which on SQLite contends with the caller and
        # inside Django's ``TestCase`` cannot see the test's uncommitted
        # transaction at all. With no concurrency to gain, staying on the
        # calling thread avoids both — and is what makes the pipeline
        # exercisable in dev and CI (see settings.max_parallelism).
        results = []
        for job in jobs:
            try:
                results.append(job())
            except BaseException as exc:  # noqa: BLE001 — attributed below
                results.append(exc)
        return results

    tenant_id = get_current_tenant()
    deal_id = get_current_deal()
    ledger_book = ledger.book()
    workers = max(1, min(int(max_workers or 1), len(jobs)))

    # ONE COPY PER JOB, not one shared snapshot.
    #
    # `Context.run()` refuses re-entry — a Context already entered on one
    # thread raises RuntimeError("cannot enter context: ... is already
    # entered") on the second. Sharing a single snapshot across the pool
    # therefore works only while nothing overlaps, and fails precisely when
    # concurrency actually happens: the first batch to start would run and
    # every batch that overlapped it would die.
    #
    # Each copy is taken here, on the parent thread, so they all carry the
    # parent's trace; they are independent objects, so entering them
    # concurrently is fine.
    wrapped = [in_worker_context(job, tenant_id=tenant_id, deal_id=deal_id,
                                 context=contextvars.copy_context(),
                                 ledger_book=ledger_book)
               for job in jobs]

    results = [None] * len(wrapped)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix=thread_name_prefix) as pool:
        futures = {pool.submit(job): index
                   for index, job in enumerate(wrapped)}
        for future, index in futures.items():
            try:
                results[index] = future.result()
            except BaseException as exc:  # noqa: BLE001 — attributed, not swallowed
                results[index] = exc
    return results
