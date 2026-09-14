"""One invalid key in a pool must not fail a call.

A live run with nineteen Gemini keys lost its whole "Founders & Leadership"
research batch: the call landed on a key Google rejected ("API key not
valid"), which was treated as a fatal AUTH error instead of a broken key to
rotate past. A rate-limited key already rotated; an invalid one now does too,
and stays out of rotation.
"""
import os
from unittest import mock

from django.test import SimpleTestCase

from fundos.llm import adapter, keyring

VAR = "TEST_ROTATION_KEYS"
GOOD_A, BAD, GOOD_B = "key-good-aaaa", "key-bad-bbbb", "key-good-cccc"


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        if not self._payload and self.text.startswith("{"):
            import json
            return json.loads(self.text)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


INVALID = _Resp(400, '{"error": {"message": "API key not valid. Please pass '
                     'a valid API key.", "status": "INVALID_ARGUMENT", '
                     '"details": [{"reason": "API_KEY_INVALID"}]}}')
OK = _Resp(200, "", {"candidates": [{"content": {"parts": [{"text": "hi"}]},
                                     "finishReason": "STOP"}],
                     "usageMetadata": {"promptTokenCount": 1,
                                       "candidatesTokenCount": 1}})


class _Endpoint:
    base_url = ""
    api_key_env_var = VAR

    @property
    def api_key(self):
        return keyring.next_key(VAR)

    @property
    def api_key_pool_size(self):
        return keyring.pool_size(VAR)

    def penalise_api_key(self, key, *, reason=""):
        keyring.penalise(VAR, key, reason=reason)

    def disable_api_key(self, key, *, reason=""):
        keyring.disable(VAR, key, reason=reason)


class _Pool(SimpleTestCase):
    keys = (BAD, GOOD_A, GOOD_B)

    def setUp(self):
        keyring.reset(VAR)
        patcher = mock.patch.dict(os.environ, {VAR: ",".join(self.keys)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(keyring.reset, VAR)


class ARejectedKeyLeavesThePool(_Pool):

    def test_it_is_never_handed_out_again(self):
        keyring.disable(VAR, BAD, reason="invalid")
        handed = {keyring.next_key(VAR) for _ in range(12)}
        self.assertEqual(handed, {GOOD_A, GOOD_B})

    def test_describe_reports_it_without_the_key(self):
        keyring.disable(VAR, BAD, reason="invalid")
        info = keyring.describe(VAR)
        self.assertEqual((info["rejected"], info["available"]), (1, 2))
        self.assertEqual(info["rejected_tails"], ["…bbbb"])
        self.assertNotIn(BAD, str(info))

    def test_the_log_line_does_not_carry_the_key(self):
        with self.assertLogs("fundos.llm", "ERROR") as logs:
            keyring.disable(VAR, BAD, reason="invalid")
        self.assertNotIn(BAD, "\n".join(logs.output))
        self.assertIn("…bbbb", "\n".join(logs.output))

    def test_all_rejected_still_returns_a_key_so_auth_error_surfaces(self):
        for key in self.keys:
            keyring.disable(VAR, key)
        self.assertIn(keyring.next_key(VAR), self.keys)

    def test_changing_the_variable_restores_the_pool(self):
        keyring.disable(VAR, BAD)
        with mock.patch.dict(os.environ, {VAR: f"{BAD},{GOOD_A}"}):
            self.assertIn(BAD, {keyring.next_key(VAR) for _ in range(4)})


class ASingleKeyIsNeverDisabled(_Pool):
    keys = (BAD,)

    def test_the_only_key_stays(self):
        keyring.disable(VAR, BAD)
        self.assertEqual(keyring.next_key(VAR), BAD)


class TheDetectorIsSpecific(SimpleTestCase):

    def test_invalid_key(self):
        self.assertTrue(adapter._gemini_key_rejected(INVALID))

    def test_suspended_project(self):
        self.assertTrue(adapter._gemini_key_rejected(
            _Resp(403, "Permission denied: Consumer 'api_key:x' has been suspended.")))

    def test_a_request_fault_is_not_a_key_fault(self):
        for resp in (_Resp(400, "Invalid JSON payload: unknown field schema"),
                     _Resp(403, "User location is not supported"),
                     _Resp(429, "API key not valid"),
                     _Resp(500, "API key not valid"), OK):
            self.assertFalse(adapter._gemini_key_rejected(resp), resp.text)


class TheCallRotatesPastAnInvalidKey(_Pool):

    def _run(self, responses):
        sent = []

        def post(url, json=None, headers=None, timeout=None):
            sent.append(headers["x-goog-api-key"])
            return responses.pop(0)

        with mock.patch.object(adapter.requests, "post", side_effect=post):
            result = adapter._run_gemini(_Endpoint(), "", "hello",
                                         "gemini-2.5-flash", None, 100, 5)
        return result, sent

    def test_an_invalid_key_is_retried_on_another_key_and_succeeds(self):
        keyring.reset(VAR)
        with mock.patch.object(keyring, "next_key",
                               side_effect=[BAD, GOOD_A]):
            result, sent = self._run([INVALID, OK])
        self.assertEqual(sent, [BAD, GOOD_A])
        self.assertIn("hi", str(result))

    def test_the_invalid_key_is_skipped_afterwards(self):
        with mock.patch.object(keyring, "next_key",
                               side_effect=[BAD, GOOD_A]):
            self._run([INVALID, OK])
        self.assertNotIn(BAD, {keyring.next_key(VAR) for _ in range(8)})

    def test_a_single_invalid_key_still_fails_as_before(self):
        with mock.patch.dict(os.environ, {VAR: BAD}):
            keyring.reset(VAR)
            with self.assertRaises(Exception) as caught:
                self._run([INVALID])
        self.assertIn("API key not valid", str(caught.exception))
