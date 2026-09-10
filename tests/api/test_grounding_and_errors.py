"""v20 — the defects a live Deepak Nitrite run exposed.

Every test here corresponds to something that happened on the server and was
either invisible in the trace or actively misreported.
"""
from unittest import mock

from django.test import TestCase


class ErrorReportingTests(TestCase):
    """A 400 must read as a 400."""

    def test_gemini_url_carries_no_api_key(self):
        """The key used to sit in the query string, which put it inside
        requests' HTTPError message and from there into the log file, the
        stored run diagnostics, and any support bundle built from them."""
        from fundos.llm import adapter
        from fundos.llm.models import LLMEndpoint

        endpoint = LLMEndpoint(code="G", provider_kind="gemini",
                               default_model="gemini-3.6-flash",
                               api_key_env_var="GEMINI_API_KEY",
                               timeout_seconds=30)
        captured = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                        "usageMetadata": {}}

        def _post(url, json=None, headers=None, timeout=None, **kw):
            captured["url"] = url
            captured["headers"] = headers or {}
            return _Resp()

        with mock.patch.object(adapter, "requests") as rq:
            rq.post = _post
            with mock.patch.object(type(endpoint), "api_key",
                                   mock.PropertyMock(return_value="AIzaSECRET")):
                adapter._run_gemini(endpoint, "sys", "prompt",
                                    "gemini-3.6-flash", 0.2, 1024, 30)

        self.assertNotIn("key=", captured["url"])
        self.assertNotIn("AIzaSECRET", captured["url"])
        self.assertEqual(captured["headers"].get("x-goog-api-key"),
                         "AIzaSECRET")

    def test_error_body_is_surfaced_and_redacted(self):
        """raise_for_status() discards the body, and for a 4xx the body is
        the only part that says WHY the request was rejected."""
        import requests

        from fundos.llm.adapter import _raise_for_status

        class _Resp:
            status_code = 400
            text = ""

            def json(self):
                return {"error": {"message": (
                    "Search tool cannot be used with responseSchema. "
                    "key=AIzaSECRETVALUE123")}}

        with self.assertRaises(requests.HTTPError) as ctx:
            _raise_for_status(_Resp(), "gemini", "gemini-3.6-flash")
        message = str(ctx.exception)
        self.assertIn("400", message)
        self.assertIn("responseSchema", message)
        self.assertNotIn("AIzaSECRETVALUE123", message)


class DiagnosisRoutingTests(TestCase):
    """The rule that sent a payload fault to the billing department."""

    def _diagnose(self, error):
        from fundos.profile.trace import FAIL, GenerationTrace

        trace = GenerationTrace(profile=None, company=None, mode="test",
                                trigger="test")
        trace.steps.append({
            "step": "llm.call.company_profile_deep_extract", "status": FAIL,
            "error": error})
        return trace.diagnose()

    def test_gemini_400_is_not_reported_as_a_billing_problem(self):
        """`"rate" in err` matched "gene-rate-Content" in the Gemini URL, so
        EVERY Gemini failure of every kind was reported as rate-limit or
        billing."""
        findings = self._diagnose(
            "HTTPError: 400 Client Error: Bad Request for url: "
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent")
        top = findings[0]
        self.assertIn("REJECTED THE REQUEST", top["cause"])
        self.assertNotIn("rate-limit", top["cause"])
        self.assertIn("responseSchema" if False else "structured output",
                      top["remedy"])

    def test_a_real_rate_limit_still_routes_to_billing(self):
        findings = self._diagnose("HTTPError: 429 Too Many Requests")
        causes = " ".join(f["cause"] for f in findings)
        self.assertIn("rate-limit", causes)


class UngroundedRunTests(TestCase):
    """The run that looked perfect and was written from memory."""

    def _trace(self, *, searches, total_chars, populated):
        from fundos.profile.trace import OK, GenerationTrace

        trace = GenerationTrace(profile=None, company=None, mode="test",
                                trigger="test")
        trace.steps.append({"step": "preflight.config", "status": OK,
                            "ai_mocked": False, "llm_bindings": 9,
                            "llm_endpoints_active": 1})
        trace.steps.append({"step": "sources.summary", "status": OK,
                            "website_chars": 0, "documents_chars": 0,
                            "research_chars": total_chars,
                            "total_chars": total_chars})
        trace.steps.append({
            "step": "llm.call.company_profile_deep_extract", "status": OK,
            "searches": searches, "populated_fields": populated,
            "finish_reason": "STOP"})
        return trace

    def test_dense_output_from_no_sources_is_critical(self):
        findings = self._trace(searches="", total_chars=367,
                               populated=78).diagnose()
        top = findings[0]
        self.assertEqual(top["severity"], "CRITICAL")
        self.assertIn("MEMORY", top["cause"])

    def test_a_grounded_run_is_not_flagged(self):
        findings = self._trace(searches="6", total_chars=24000,
                               populated=78).diagnose()
        self.assertNotIn("MEMORY", " ".join(f["cause"] for f in findings))


class PartialDateTests(TestCase):
    """A funding round must survive an unknown day."""

    def test_month_precision_is_kept(self):
        import datetime as dt

        from fundos.profile.services import _partial_date

        self.assertEqual(_partial_date("2006-05"), (dt.date(2006, 5, 1),
                                                    "month"))
        self.assertEqual(_partial_date("2010-02"), (dt.date(2010, 2, 1),
                                                    "month"))
        self.assertEqual(_partial_date("2018-03-14"),
                         (dt.date(2018, 3, 14), "day"))
        self.assertEqual(_partial_date("2015"), (dt.date(2015, 1, 1), "year"))
        self.assertEqual(_partial_date(""), (None, ""))
        self.assertEqual(_partial_date("last summer"), (None, ""))
        self.assertEqual(_partial_date("2006-13"), (None, ""))

    def test_precision_is_recorded_on_the_model(self):
        from fundos.profile.models import FundingRound

        field = FundingRound._meta.get_field("date_precision")
        self.assertIn("month", dict(field.choices))


class WebsiteExtractionTests(TestCase):
    """The one live source used to return its <title> and nothing else."""

    HTML = """<html><head><title>Deepak Nitrite Limited</title>
    <meta name="description" content="A leading chemical manufacturer.">
    <style>.a{color:red}</style><script>var x=1;</script></head>
    <body><h1>About us</h1><p>Founded in 1970, the company operates four
    manufacturing plants across Gujarat and Maharashtra.</p>
    <p>Revenue for FY24 stood at Rs 7,761 crore.</p></body></html>"""

    def test_body_text_is_extracted_not_just_the_title(self):
        from fundos.research.adapters.website import _visible_text

        text = _visible_text(self.HTML)
        self.assertIn("Founded in 1970", text)
        self.assertIn("7,761 crore", text)
        # Script and style content must not reach the model as "facts".
        self.assertNotIn("var x=1", text)
        self.assertNotIn("color:red", text)
        self.assertGreater(len(text), 120)

    def test_meta_description_is_read(self):
        from fundos.research.adapters.website import _meta

        self.assertEqual(_meta(self.HTML, "description"),
                         "A leading chemical manufacturer.")


class FixtureSourceTests(TestCase):
    """Nine dead adapters must not look like nine working ones."""

    def test_placeholder_adapters_declare_themselves(self):
        from fundos.research.adapters.extensions import (AwardsAdapter,
                                                         HiringAdapter,
                                                         NewsAdapter,
                                                         PatentsAdapter)
        from fundos.research.adapters.market import (CompetitorAdapter,
                                                     IndustryAdapter,
                                                     MarketAdapter,
                                                     PublicFundingAdapter)
        from fundos.research.adapters.website import WebsiteAdapter

        for cls in (MarketAdapter, CompetitorAdapter, IndustryAdapter,
                    PublicFundingAdapter, PatentsAdapter, NewsAdapter,
                    AwardsAdapter, HiringAdapter):
            self.assertTrue(cls.is_fixture, cls.__name__)
        self.assertFalse(WebsiteAdapter.is_fixture)


class RecalcTenantTests(TestCase):
    """Propagation must not stop outside a request."""

    def test_tenant_comes_from_the_deal(self):
        from tests.conftest_helpers import make_world

        from fundos.core.scoping import set_current_tenant
        from fundos.core.services import recalc

        world = make_world()
        deal = world["a"]["deal"]
        set_current_tenant(None)          # a management command or a task
        status = recalc.mark(deal.id, "investment_memorandum",
                             status="needs_review", reason="ckb.revenue")
        self.assertIsNotNone(status, "propagation was silently skipped")
        self.assertEqual(str(status.tenant_id), str(deal.tenant_id))


class WebsiteVariantTests(TestCase):
    """Which of apex and www serves the site is arbitrary."""

    def _adapter(self, domain):
        from fundos.research.adapters.website import WebsiteAdapter

        deal = mock.Mock()
        deal.company.domain = domain
        return WebsiteAdapter(deal=deal, ckb_snapshot={})

    def test_www_is_tried_when_the_apex_fails(self):
        import requests

        tried = []

        class _Resp:
            status_code = 200
            url = "https://www.example.com"
            headers = {"Content-Type": "text/html"}
            text = ("<html><head><title>Example</title></head><body>"
                    "<p>A company that makes things in three factories and "
                    "was founded in 1970 by two engineers.</p></body></html>")

            def raise_for_status(self):
                return None

        adapter = self._adapter("https://example.com")

        def _get(url, timeout=15):
            tried.append(url)
            if "www." not in url:
                raise requests.exceptions.SSLError("cert mismatch")
            if url.rstrip("/") != "https://www.example.com":
                raise requests.exceptions.ConnectionError("404-ish")
            return _Resp()

        with mock.patch.object(adapter, "_get", side_effect=_get):
            result = adapter.fetch()

        self.assertIn("https://example.com", tried)
        self.assertIn("https://www.example.com", tried)
        self.assertIn("founded in 1970", result["facts"]["website_text"])

    def test_the_original_error_is_raised_when_both_fail(self):
        import requests

        adapter = self._adapter("https://example.com")

        def _get(url, timeout=15):
            raise requests.exceptions.SSLError("certificate verify failed")

        with mock.patch.object(adapter, "_get", side_effect=_get):
            with self.assertRaises(requests.exceptions.SSLError) as ctx:
                adapter.fetch()
        # Re-raised with the operator-facing remedy attached.
        self.assertIn("trust store", str(ctx.exception))
