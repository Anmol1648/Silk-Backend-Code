"""Diagnostics services: run tracking, a DB log handler, and health.

Three pieces:

  run_record()   A context manager wrapping any job so its inputs, counts,
                 timing and outcome are recorded whether it succeeds or not.
                 A job that fails without leaving a record is the one you most
                 need a record of.

  DatabaseLogHandler
                 A logging handler that persists WARNING and above with the
                 current correlation ID attached. Files still get everything;
                 the database gets what someone will want to search.

  health()       What the dashboard reads. Every check answers a question an
                 operator actually asks: is the data fresh, is anything stuck,
                 does the config still add up.
"""
import logging
import time
import uuid
from contextlib import contextmanager

from django.utils import timezone

logger = logging.getLogger(__name__)

_current = {}


def set_correlation_id(value):
    _current["correlation_id"] = str(value) if value else None


def get_correlation_id():
    cid = _current.get("correlation_id")
    if not cid:
        cid = str(uuid.uuid4())
        _current["correlation_id"] = cid
    return cid


@contextmanager
def run_record(kind, *, subject="", deal_id=None, company_id=None,
               tenant_id=None, inputs=None, config_version="", user=None):
    """Wrap a job so it leaves a queryable record either way.

    Usage:
        with run_record("scoring", subject=company.name, deal_id=deal.id) as r:
            result = do_the_work()
            r.counts = {"parameters": 76}
    """
    from fundos.diagnostics.models import RunRecord

    started = time.monotonic()
    record = RunRecord.objects.create(
        correlation_id=get_correlation_id(), kind=kind, subject=subject[:255],
        deal_id=deal_id, company_id=company_id, tenant_id=tenant_id,
        inputs=inputs or {}, config_version=str(config_version or ""),
        triggered_by=user, status="running")
    try:
        yield record
        record.status = "warning" if record.warnings else "ok"
    except Exception as e:
        record.status = "failed"
        record.error = f"{type(e).__name__}: {e}"[:4000]
        logger.error("RUN %s (%s) failed: %s", record.id, kind, e,
                     exc_info=True)
        raise
    finally:
        record.finished_at = timezone.now()
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.save()


class DatabaseLogHandler(logging.Handler):
    """Persist WARNING and above, correlated.

    Deliberately narrow. Writing every DEBUG line to the database would make
    the table unusable and slow every request; the file handler keeps those.
    What lands here is what someone will search for after something went wrong.
    """

    def __init__(self, level=logging.WARNING):
        super().__init__(level=level)

    def emit(self, record):
        try:
            from fundos.diagnostics.models import DiagEvent
            DiagEvent.objects.create(
                correlation_id=get_correlation_id(),
                level=record.levelname.lower(),
                component=record.name[:64],
                message=self.format(record)[:8000],
                source_location=f"{record.filename}:{record.lineno}"[:255],
                data={"funcName": record.funcName})
        except Exception:
            # A logging handler must never take down the thing it is logging.
            pass


class CorrelationMiddleware:
    """Attach a correlation ID to every request and echo it in the response.

    The echo matters: a user reporting a problem can quote the header, and the
    whole request chain is one filter away.
    """

    HEADER = "HTTP_X_CORRELATION_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cid = request.META.get(self.HEADER) or str(uuid.uuid4())
        set_correlation_id(cid)
        request.correlation_id = cid
        response = self.get_response(request)
        response["X-Correlation-Id"] = cid
        return response


def health():
    """System health for the admin dashboard and the health endpoint."""
    import datetime as dt

    from fundos.diagnostics.models import RunRecord

    out = {"checks": [], "status": "ok"}

    def check(name, ok, detail, warn_only=False):
        state = "ok" if ok else ("warning" if warn_only else "error")
        out["checks"].append({"name": name, "status": state, "detail": detail})
        if state == "error":
            out["status"] = "error"
        elif state == "warning" and out["status"] == "ok":
            out["status"] = "warning"

    # -- data freshness ----------------------------------------------------
    try:
        from fundos.investors.models import RefreshRun
        last = RefreshRun.objects.filter(status="completed",
                                         run_type__in=["commit", "scheduled"]
                                         ).first()
        if last and last.finished_at:
            age = (timezone.now() - last.finished_at).days
            check("Data refresh", age <= 9,
                  f"Last successful refresh {age} day(s) ago.",
                  warn_only=True)
        else:
            check("Data refresh", False, "No successful refresh recorded.",
                  warn_only=True)
    except Exception as e:
        check("Data refresh", False, f"Could not read refresh history: {e}",
              warn_only=True)

    # -- circuit breakers --------------------------------------------------
    try:
        from fundos.investors.sources import breaker_states
        states = breaker_states()
        open_ones = [n for n, s in states.items() if s == "OPEN"]
        check("Circuit breakers", not open_ones,
              (f"Open: {', '.join(open_ones)}" if open_ones
               else f"{len(states) or 0} breaker(s), all closed."),
              warn_only=True)
    except Exception:
        check("Circuit breakers", True, "No breakers initialised yet.")

    # -- review queues -----------------------------------------------------
    try:
        from fundos.investors.models import (ClassificationReviewItem,
                                             InvestorAliasCandidate,
                                             SectorReviewItem)
        pending = (
            InvestorAliasCandidate.objects.filter(
                status__in=["pending", "llm_done"]).count()
            + ClassificationReviewItem.objects.filter(status="open").count()
            + SectorReviewItem.objects.filter(status="open").count())
        check("Review queues", pending == 0,
              f"{pending} item(s) awaiting a decision.", warn_only=True)
    except Exception as e:
        check("Review queues", True, f"Not available: {e}")

    # -- config integrity --------------------------------------------------
    try:
        from django.db.models import Sum
        from fundos.assessment.models import ConfigWeight
        total = (ConfigWeight.objects
                 .filter(level="category", is_active=True, tenant_id__isnull=True)
                 .aggregate(t=Sum("weight"))["t"] or 0)
        check("Scoring weights", abs(float(total) - 100) < 0.01,
              f"Category weights total {float(total):.2f} (must be 100).")
    except Exception as e:
        check("Scoring weights", False, f"Could not verify: {e}", warn_only=True)

    # -- recent failures ---------------------------------------------------
    since = timezone.now() - dt.timedelta(hours=24)
    failed = RunRecord.objects.filter(status="failed",
                                      started_at__gte=since).count()
    check("Failed jobs (24h)", failed == 0, f"{failed} failure(s).",
          warn_only=True)

    return out
