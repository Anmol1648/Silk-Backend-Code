"""Stage clocks and the activity log.

The pipeline routinely runs for tens of minutes — ten search-grounded LLM
calls, then OCR over every uploaded page, then one large synthesis call.
Without this module the only thing a caller polling the profile can see is a
coarse ``status`` string, and a slow run is indistinguishable from a wedged
one.

Two things are recorded, both onto :class:`~fundos.profile.models.ProfileGenerationRun`:

* **stage timings** — ``stage_timings[<stage>]`` gets ``started_at``,
  ``finished_at``, ``duration_seconds`` and a state, so the API can say *which*
  stage is eating the wall clock.
* **activity events** — an append-only, bounded list of one-line notes ("OCR
  triggered on page 12 of 40"), which is what makes a long stage legible from
  outside.

Neither is load-bearing. Every function here swallows its own errors: losing a
progress note must never fail a run that is otherwise fine. Events are also
mirrored into the :class:`~fundos.profile.trace.GenerationTrace` and the
``fundos.generation`` log, so the narration survives even when the database
write does not.
"""
import logging
import time
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("fundos.generation")


@contextmanager
def _savepoint(what):
    """Run a best-effort ORM write in its own savepoint and swallow failure.

    Every write in this module is deliberately non-load-bearing: losing a
    progress note must never fail a run. But swallowing a database error is
    only safe inside a savepoint.

    Django sets ``connection.needs_rollback`` when a statement fails inside an
    atomic block. Catching that exception without rolling back to a savepoint
    leaves the flag set, and Django then refuses EVERY subsequent query in the
    surrounding transaction with "An error occurred in the current
    transaction. You can't execute queries until the end of the 'atomic'
    block" — attributed to whatever innocent statement runs next.

    That is what turned a run whose ``ProfileGenerationRun`` row had gone
    missing into a cascade: ``run.save()`` raised "Save with update_fields did
    not affect any rows", the handler swallowed it, and the next
    ``ProfileRunEvent.objects.create()`` died on a foreign key it could no
    longer even reach. Rolling back to a savepoint clears the flag, so the
    failure stays local to the write that caused it.
    """
    try:
        with transaction.atomic():
            yield
    except Exception:
        logger.debug("could not %s", what, exc_info=True)

# Bound on stored events per run. A chatty run (a 300-page scan notes a line
# per OCR'd page) would otherwise grow the row without limit. The tail is what
# matters when diagnosing where a run stopped, so the HEAD is dropped and a
# marker is left in its place rather than silently losing the count.
MAX_EVENTS = 500


# --- stage vocabulary -------------------------------------------------------
# Ordered to match execution: RESEARCHING and PROCESSING_DOCUMENTS run
# concurrently, then CONSOLIDATING merges their output, then SYNTHESIZING
# produces the structured profile. A UI renders them in this order regardless
# of which of the two concurrent stages finishes first.
QUEUED = "queued"
RESEARCHING = "researching"
PROCESSING_DOCUMENTS = "processing_documents"
CONSOLIDATING = "consolidating"
SYNTHESIZING = "synthesizing"
WRITING = "writing"
COMPLETED = "completed"

STAGE_ORDER = (QUEUED, RESEARCHING, PROCESSING_DOCUMENTS, CONSOLIDATING,
               SYNTHESIZING, WRITING, COMPLETED)

STAGE_LABELS = {
    QUEUED: "Queued",
    RESEARCHING: "Researching the company on the web",
    PROCESSING_DOCUMENTS: "Reading the uploaded documents",
    CONSOLIDATING: "Merging the sources into one dossier",
    SYNTHESIZING: "Building the structured profile from the dossier",
    WRITING: "Saving the profile",
    COMPLETED: "Completed",
}


class Elapsed:
    """A monotonic stopwatch for timing work inside a stage.

    ``time.monotonic`` rather than wall clock: this measures durations, which
    must not jump if the system clock is adjusted mid-run.
    """

    def __init__(self):
        self._start = time.monotonic()

    @property
    def seconds(self):
        """Elapsed time since construction or the last :meth:`reset`, rounded
        to hundredths — enough for a human-facing duration without implying
        accuracy the measurement does not have."""
        return round(time.monotonic() - self._start, 2)

    def reset(self):
        self._start = time.monotonic()


def humanize_duration(seconds):
    """A duration a human reads without counting digits: '4.2s', '3m 07s'."""
    if seconds is None:
        return ""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class RunTracker:
    """Records progress for one run. Every method is best-effort.

    Bound to a :class:`~fundos.profile.models.ProfileGenerationRun` row and
    shared across the concurrently-running sources, so all mutation goes
    through a lock and every write is a targeted ``update_fields`` save rather
    than a whole-row write — two threads narrating at once must not clobber
    each other's stage timings.
    """

    def __init__(self, run, trace=None):
        """:param run: The ProfileGenerationRun row to record onto.
        :param trace: The active GenerationTrace, so events land in the
            diagnostic log alongside the pipeline's own step records.
        """
        import threading

        self.run = run
        self.trace = trace
        self._lock = threading.Lock()
        self._started = time.monotonic()

    # -- activity log ------------------------------------------------------
    def event(self, message, **fields):
        """Record a one-line activity note, and log it.

        The single entry point every stage calls to narrate what it is doing
        right now. Always logged server-side regardless of whether the database
        write below succeeds, so an operator tailing ``silk_generation.log``
        never loses a progress note to a transient DB problem.
        """
        logger.info("PIPELINE[%s] %s", self.run_id, message)
        if self.trace is not None:
            try:
                self.trace.event("pipeline.activity", "OK", note=message,
                                 **fields)
            except Exception:
                pass
        with _savepoint("append activity event"):
            self._append_event(message, **fields)

    def _append_event(self, message, **fields):
        from fundos.profile.models import ProfileRunEvent

        with self._lock:
            ProfileRunEvent.objects.create(
                run=self.run,
                tenant_id=self.run.tenant_id,
                stage=str(fields.get("stage") or self.run.stage or ""),
                message=str(message)[:1000],
                t_plus_seconds=round(time.monotonic() - self._started, 2),
                detail={k: v for k, v in fields.items() if k != "stage"},
            )
            # Trim the head, keeping the tail — the end of the log is what
            # explains where a run stopped.
            excess = (ProfileRunEvent.objects.filter(run=self.run).count()
                      - MAX_EVENTS)
            if excess > 0:
                stale = list(ProfileRunEvent.objects
                             .filter(run=self.run).order_by("at", "id")
                             .values_list("id", flat=True)[:excess])
                ProfileRunEvent.objects.filter(id__in=stale).delete()

    @property
    def run_id(self):
        return str(getattr(self.run, "id", ""))[:8]

    # -- stage timings -----------------------------------------------------
    def stage_started(self, stage, note=""):
        """Mark ``stage`` running and narrate it.

        Called at the moment the stage begins real work, not when it is
        scheduled — a queued run's RESEARCHING stage starts when the worker
        picks it up, not when the request came in.
        """
        now = timezone.now()
        self._touch_stage(stage, {"state": "running",
                                  "started_at": now.isoformat()})
        with self._lock:
            self.run.stage = stage
            try:
                self.run.save(update_fields=["stage"])
            except Exception:
                logger.debug("could not record stage start", exc_info=True)
        self.event(note or f"{STAGE_LABELS.get(stage, stage)} — started",
                   stage=stage)

    def stage_finished(self, stage, *, ok=True, note=""):
        """Mark ``stage`` completed/failed and narrate it with its duration.

        Duration is derived from the stage's own recorded ``started_at`` rather
        than the caller tracking elapsed time, so "when did this stage start"
        has exactly one source of truth — :meth:`stage_started`'s write —
        instead of being duplicated into every call site.
        """
        now = timezone.now()
        duration = None
        try:
            started = (self.run.stage_timings or {}).get(stage, {}) \
                .get("started_at")
            if started:
                from django.utils.dateparse import parse_datetime
                started_dt = parse_datetime(started)
                if started_dt:
                    duration = round((now - started_dt).total_seconds(), 2)
        except Exception:
            pass

        self._touch_stage(stage, {
            "state": "completed" if ok else "failed",
            "finished_at": now.isoformat(),
            "duration_seconds": duration,
            "duration_human": humanize_duration(duration),
        })
        suffix = f" in {humanize_duration(duration)}" if duration else ""
        verb = "finished" if ok else "FAILED"
        self.event(note or f"{STAGE_LABELS.get(stage, stage)} — {verb}{suffix}",
                   stage=stage, duration_seconds=duration)

    def _touch_stage(self, stage, patch):
        """Merge ``patch`` into one stage's timing block.

        Merge, not replace: `stage_finished` must not erase the `started_at`
        that `stage_started` wrote, and the two calls come from different
        points in the run (sometimes different threads).
        """
        with self._lock:
            with _savepoint("record stage timing"):
                timings = dict(self.run.stage_timings or {})
                block = dict(timings.get(stage) or {})
                block.update(patch)
                timings[stage] = block
                self.run.stage_timings = timings
                self.run.save(update_fields=["stage_timings"])

    # -- progress counters -------------------------------------------------
    def update(self, **fields):
        """Patch scalar progress fields on the run row (batch counts, sizes).

        Only fields that actually exist on the model are written, so adding a
        counter to a caller before its migration lands degrades to a no-op
        rather than an AttributeError inside a worker thread.
        """
        with self._lock:
            with _savepoint("update run progress"):
                writable = [name for name in fields
                            if hasattr(self.run, name)]
                for name in writable:
                    setattr(self.run, name, fields[name])
                if writable:
                    self.run.save(update_fields=writable)
