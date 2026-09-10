"""A lock that SQLite refuses to wait on must not be a terminal failure.

Assessment generation died twice with `database is locked`, 0.14 seconds into
a run, against a 30-second busy timeout. The timeout was not the problem and
raising it would have changed nothing: a transaction that BEGINs deferred
starts as a reader, and when it later needs to write after someone else has
committed, SQLite returns SQLITE_BUSY *immediately without calling the busy
handler* — waiting could only deadlock.

The retry is the fix, because the failed transaction is rolled back and a
fresh attempt takes a new snapshot. Django 5.1 solves it with
`transaction_mode="IMMEDIATE"`; this project is on 4.2.
"""
from unittest import mock

from django.db import OperationalError
from django.test import TestCase

from fundos.core.services import jobs


class LockClassificationTests(TestCase):

    def test_a_lock_error_is_recognised(self):
        self.assertTrue(jobs._is_locked(
            OperationalError("database is locked")))
        self.assertTrue(jobs._is_locked(
            OperationalError("database table is locked: assess_assessment")))

    def test_other_database_errors_are_not(self):
        """Retrying a real error just repeats it more expensively."""
        self.assertFalse(jobs._is_locked(
            OperationalError("no such column: foo")))
        self.assertFalse(jobs._is_locked(ValueError("database is locked")))


class LockRetryTests(TestCase):

    def setUp(self):
        # The real backoff would make this suite sleep for seconds.
        patcher = mock.patch.object(jobs.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_succeeds_on_a_later_attempt(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OperationalError("database is locked")
            return "scored"

        self.assertEqual(jobs._run_with_lock_retry(flaky), "scored")
        self.assertEqual(calls["n"], 3)

    def test_a_first_attempt_success_does_not_sleep(self):
        self.assertEqual(jobs._run_with_lock_retry(lambda: "ok"), "ok")
        self.sleep.assert_not_called()

    def test_it_gives_up_and_re_raises(self):
        def always_locked():
            raise OperationalError("database is locked")

        with self.assertRaises(OperationalError):
            jobs._run_with_lock_retry(always_locked)

    def test_it_stops_at_the_configured_ceiling(self):
        calls = {"n": 0}

        def always_locked():
            calls["n"] += 1
            raise OperationalError("database is locked")

        with self.assertRaises(OperationalError):
            jobs._run_with_lock_retry(always_locked)
        self.assertEqual(calls["n"], jobs.LOCK_RETRIES)

    def test_a_non_lock_error_is_not_retried(self):
        """Retrying a schema error burns time to produce the same failure."""
        calls = {"n": 0}

        def broken():
            calls["n"] += 1
            raise ValueError("bad rubric")

        with self.assertRaises(ValueError):
            jobs._run_with_lock_retry(broken)
        self.assertEqual(calls["n"], 1)

    def test_arguments_are_passed_through(self):
        self.assertEqual(
            jobs._run_with_lock_retry(lambda a, b=0: a + b, 2, b=3), 5)

    def test_the_backoff_grows(self):
        def always_locked():
            raise OperationalError("database is locked")

        with self.assertRaises(OperationalError):
            jobs._run_with_lock_retry(always_locked)
        delays = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(delays), jobs.LOCK_RETRIES - 1)
        self.assertLess(delays[0], delays[-1],
                        "a constant retry interval re-collides with whatever "
                        "is holding the lock")


class Phase1RetryPolicyTests(TestCase):
    """A transient failure should self-heal; a real one should be shown."""

    @property
    def view(self):
        from fundos.assessment.phase1_views import FundraisingPhase1View
        return FundraisingPhase1View

    def _job(self, error, status="failed"):
        return mock.Mock(error=error, status=status, deal_id=None)

    def test_a_lock_failure_is_retryable(self):
        with mock.patch("fundos.core.models.GenerationJob.objects") as qs:
            qs.filter.return_value.order_by.return_value = []
            self.assertTrue(
                self.view._retryable(self._job("database is locked")))

    def test_a_provider_error_is_NOT_retried_here(self):
        """The adapter already retried it across the key pool.

        Retrying at job level duplicates that at a real cost per poll, and
        replaces the reason the user needs to read — "every key is
        rate-limited" — with a spinner that never resolves.
        """
        self.assertFalse(self.view._retryable(self._job("gemini 503")))
        self.assertFalse(self.view._retryable(
            self._job("gemini 429: every key is rate-limited")))

    def test_a_schema_error_is_not_retryable(self):
        """This would fail identically every poll and cost a call each time."""
        self.assertFalse(self.view._retryable(
            self._job("missing required key 'detected_type'")))

    def test_an_empty_error_is_not_retryable(self):
        self.assertFalse(self.view._retryable(self._job("")))

    def test_repeated_transient_failures_stop_retrying(self):
        """Without a ceiling, every poll starts another run."""
        failed = [mock.Mock(status="failed")
                  for _ in range(self.view.MAX_TRANSIENT_RETRIES)]
        with mock.patch("fundos.core.models.GenerationJob.objects") as qs:
            qs.filter.return_value.order_by.return_value = failed
            self.assertFalse(
                self.view._retryable(self._job("database is locked")))

    def test_a_recent_success_re_enables_retrying(self):
        history = [mock.Mock(status="failed"), mock.Mock(status="succeeded"),
                   mock.Mock(status="failed")]
        with mock.patch("fundos.core.models.GenerationJob.objects") as qs:
            qs.filter.return_value.order_by.return_value = history
            self.assertTrue(
                self.view._retryable(self._job("database is locked")))
