"""The generation log must survive being held open by another process.

`RotatingFileHandler` rotates by renaming the file it writes. On Windows that
fails whenever any other process has a handle on it — and more than one always
does, because `runserver` runs an autoreloader parent plus a serving child and
a deployed stack runs a worker per core.

When that rename failed, the stdlib handler raised inside `emit`, so logging
printed a full traceback for every subsequent line. A profile run that was
succeeding became indistinguishable from one that was crashing.

These tests reproduce the blocked rename directly rather than by holding a
real Windows file handle, so they assert the same behaviour on every platform
the suite runs on.
"""
import logging
import os
import tempfile
from unittest import mock

from django.test import TestCase

from fundos.core.logging import SharedRotatingFileHandler


class RotationUnderContentionTests(TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "generation.log")
        # A tiny cap, so a couple of records force a rollover.
        self.handler = SharedRotatingFileHandler(
            self.path, maxBytes=200, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger(f"test.rotation.{id(self)}")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.handler.close()

    def _blocked(self):
        """os.rename refusing the way Windows refuses a held file."""
        return mock.patch(
            "os.rename",
            side_effect=PermissionError(
                32, "The process cannot access the file because it is being "
                    "used by another process"))

    def emit(self, n, prefix="line"):
        for i in range(n):
            self.logger.info("%s %d %s", prefix, i, "x" * 60)

    def test_a_blocked_rotation_does_not_raise(self):
        with self._blocked():
            self.emit(20)   # far past the 200-byte cap

    def test_a_blocked_rotation_does_not_print_a_traceback(self):
        """The traceback flood is the actual defect being fixed."""
        with self._blocked(), mock.patch(
                "logging.Handler.handleError") as reported:
            self.emit(20)
        self.assertFalse(
            reported.called,
            "a blocked rotation must not report itself on every line")

    def test_no_line_is_lost_while_rotation_is_blocked(self):
        with self._blocked():
            self.emit(20, prefix="held")
        self.handler.flush()
        with open(self.path, encoding="utf-8") as fh:
            written = fh.read()
        for i in range(20):
            self.assertIn(f"held {i} ", written)

    def test_the_handler_still_has_a_stream_after_a_blocked_rollover(self):
        """The base class closes the stream before renaming.

        A rename that raised would otherwise leave the handler with no stream,
        turning every later line into a second, different error.
        """
        with self._blocked():
            self.emit(20)
        self.assertIsNotNone(self.handler.stream)
        self.assertFalse(self.handler.stream.closed)

    def test_rotation_resumes_once_the_other_process_lets_go(self):
        with self._blocked():
            self.emit(20, prefix="blocked")
        # Contention over: the next oversized write rolls the file for real.
        self.emit(20, prefix="after")
        self.assertTrue(os.path.exists(self.path + ".1"),
                        "rotation must recover on its own, with no restart")

    def test_normal_rotation_is_unchanged(self):
        self.emit(40)
        self.assertTrue(os.path.exists(self.path + ".1"))
        # backupCount is still honoured — this is a rotating handler, not an
        # append-forever one.
        self.assertFalse(os.path.exists(self.path + ".4"))

    def test_errors_are_still_reportable_when_asked_for(self):
        """Silence is the default, not a wall."""
        with self._blocked(), mock.patch.dict(
                os.environ, {"FUNDOS_LOG_DEBUG": "1"}), mock.patch(
                    "logging.Handler.handleError") as reported:
            self.handler.handleError(
                logging.LogRecord("t", logging.INFO, __file__, 1, "m", (),
                                  None))
        self.assertTrue(reported.called)


class ConfiguredHandlerTests(TestCase):
    """The setting has to actually point at the subclass."""

    def test_the_generation_log_uses_the_shared_handler(self):
        from django.conf import settings

        handler = settings.LOGGING["handlers"]["generation_file"]
        self.assertEqual(handler["class"],
                         "fundos.core.logging.SharedRotatingFileHandler")

    def test_the_generation_log_opens_only_when_written_to(self):
        """A process that never logs must not block one that does."""
        from django.conf import settings

        handler = settings.LOGGING["handlers"]["generation_file"]
        self.assertTrue(
            handler.get("delay"),
            "without delay the autoreloader parent holds the file open and "
            "the serving child can never rotate it")

    def test_the_live_generation_logger_is_wired_to_it(self):
        logger = logging.getLogger("fundos.generation")
        rotating = [h for h in logger.handlers
                    if isinstance(h, SharedRotatingFileHandler)]
        self.assertTrue(
            rotating,
            "fundos.generation must write through the contention-safe handler")
