"""Structured-logging support: request_id / tenant_id / deal_id on every line."""
import logging
import logging.handlers
import os

from fundos.core import scoping


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = scoping.get_request_id() or "-"
        record.tenant_id = scoping.get_current_tenant() or "-"
        record.deal_id = scoping.get_current_deal() or "-"
        return True


class SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log file that more than one process may hold open.

    WHY THIS EXISTS
    ---------------
    `RotatingFileHandler` rotates by renaming the file it is writing. On POSIX
    that works with the file open; on Windows it does not — `os.rename` raises
    `PermissionError` (WinError 32) if ANY process has a handle on it.

    More than one process always does. `runserver` runs two (the autoreloader
    parent and the child that serves), and a deployed stack runs one per
    worker. So the moment the generation log reached its 20 MB cap, every
    process's every attempt to roll it over failed — and because the failure
    happens inside `emit`, the logging module printed a full traceback for
    EVERY SUBSEQUENT LOG LINE. A working profile run then looked exactly like
    a crashing one: screens of `PermissionError` interleaved with the trace
    lines, with `status="OK"` buried in the middle.

    The old behaviour was the worst of both: the log did not rotate AND the
    noise made the thing it exists to diagnose unreadable.

    WHAT THIS DOES INSTEAD
    ----------------------
    Rotation is attempted normally. When it fails because another process
    holds the file, the handler keeps writing to the file it already has. No
    line is lost, nothing is raised, and the next emit tries again — so the
    roll happens on its own as soon as the other holders let go.

    The file can exceed `maxBytes` while that contention lasts. That is the
    deliberate trade: a diagnostic log slightly over its cap is a
    housekeeping matter, whereas losing the ability to read it at all defeats
    the point of keeping it.
    """

    def rotate(self, source, dest):
        try:
            super().rotate(source, dest)
        except OSError:
            # Another process holds the file. Keep the current stream and let
            # a later emit roll it over; see the class docstring.
            pass

    def shouldRollover(self, record):
        try:
            return super().shouldRollover(record)
        except (OSError, ValueError):
            # A closed or vanished stream must not become an exception on the
            # logging path. Never rolling is survivable; raising is not.
            return False

    def doRollover(self):
        """Roll over, and make sure a usable stream survives a failure.

        The base implementation closes the stream BEFORE renaming, so a
        rename that raises would otherwise leave the handler with no stream
        at all and turn every later line into a second error.
        """
        try:
            super().doRollover()
        except OSError:
            pass
        if self.stream is None and not self.delay:
            try:
                self.stream = self._open()
            except OSError:
                pass

    def handleError(self, record):
        """Swallow logging-path errors instead of printing a traceback.

        A diagnostic handler that reports its own failures to stderr on every
        line is worse than one that quietly drops them: the report is what
        made a healthy run unreadable in the first place.
        """
        if os.environ.get("FUNDOS_LOG_DEBUG"):
            super().handleError(record)
