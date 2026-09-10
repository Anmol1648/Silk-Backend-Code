"""Tests for the generation diagnostic trace.

The trace exists so that an empty profile explains itself. These tests assert
the DIAGNOSIS, not just that lines were emitted — a trace that records
faithfully but cannot say what to fix has not solved the problem it was built
for.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile.services import get_or_create_profile
from fundos.profile.trace import (FAIL, GenerationTrace, OK, WARN,
                                  count_populated, current_trace, trace_event)


def _bootstrap():
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Trace User")
    company = Company.objects.create(
        tenant_id=tenant.id, name="Trace Co", created_by=user,
        domain=f"t-{uuid.uuid4().hex[:6]}.test")
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    return tenant, user, company, get_or_create_profile(company, user=user)


class TraceMechanicsTests(TestCase):
    def test_step_records_timing_and_success(self):
        t = GenerationTrace()
        with t.step("demo.step") as out:
            out["chars"] = 10
        rec = t.first("demo.step")
        self.assertEqual(rec["status"], OK)
        self.assertEqual(rec["chars"], 10)
        self.assertIn("ms", rec)

    def test_step_records_failure_and_reraises(self):
        t = GenerationTrace()
        with self.assertRaises(ValueError):
            with t.step("demo.boom"):
                raise ValueError("nope")
        rec = t.first("demo.boom")
        self.assertEqual(rec["status"], FAIL)
        self.assertIn("nope", rec["error"])

    def test_trace_event_is_safe_with_no_active_trace(self):
        """Instrumented code runs outside generation too. Tracing must never
        be the thing that breaks it."""
        self.assertIsNone(current_trace())
        trace_event("orphan.step", OK)      # must not raise

    def test_nested_code_reaches_the_active_trace(self):
        t = GenerationTrace()
        with t:
            trace_event("deep.step", OK, detail="from nested code")
        self.assertIsNotNone(t.first("deep.step"))

    def test_count_populated_detects_an_empty_response(self):
        filled, total = count_populated(
            {"a": "", "b": None, "c": [], "d": {"e": ""}})
        self.assertEqual(filled, 0)
        self.assertGreater(total, 0)
        filled, _ = count_populated({"a": "value", "b": ""})
        self.assertEqual(filled, 1)


class DiagnosisTests(TestCase):
    """Each signature must produce the right cause AND the right remedy."""

    def _diagnose(self, steps):
        t = GenerationTrace()
        for name, status, fields in steps:
            t.event(name, status, **fields)
        return t.diagnose()

    def test_mocked_ai_is_the_top_finding(self):
        f = self._diagnose([
            ("preflight.config", WARN, {"ai_mocked": True, "llm_bindings": 2,
                                        "llm_endpoints_active": 1}),
        ])[0]
        self.assertEqual(f["severity"], "CRITICAL")
        self.assertIn("mocked", f["cause"].lower())
        self.assertIn("force", f["remedy"].lower(),
                      "the remedy must mention forcing, or the admin will fix "
                      "the setting and see the cached empty result")

    def test_missing_binding_is_reported(self):
        causes = " ".join(x["cause"] for x in self._diagnose([
            ("preflight.config", WARN, {"ai_mocked": False,
                                        "llm_bindings": 0,
                                        "llm_endpoints_active": 1}),
        ]))
        self.assertIn("no ai endpoint is bound", causes.lower())

    def test_bad_api_key_is_named_specifically(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("llm.call.deep", FAIL, {"error": "HTTP 401 invalid api key"}),
        ])
        text = " ".join(x["cause"] + x["remedy"] for x in f).lower()
        self.assertIn("credential", text)
        self.assertIn("api key", text)

    def test_rate_limit_is_distinguished_from_a_bad_key(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("llm.call.deep", FAIL, {"error": "HTTP 429 quota exceeded"}),
        ])
        text = " ".join(x["cause"] for x in f).lower()
        self.assertIn("quota", text)
        self.assertNotIn("credential", text)

    def test_unread_documents_are_called_out(self):
        """Called out, but as context rather than as a fault.

        This used to assert that the remedy mentioned a "background worker".
        That advice was wrong: the profile pipeline reads documents inline
        (`pipeline.source2_documents`) and Celery is not in that path. The
        step runs before the pipeline, so nothing analysed yet is the normal
        starting state of a first run.
        """
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("sources.documents", FAIL, {"uploaded": 3, "analysed": 0,
                                         "pending": 3}),
        ])
        docs = [x for x in f if "documents" in x["cause"].lower()]
        self.assertTrue(docs, "the unread state must still be reported")
        self.assertEqual(docs[0]["severity"], "INFO")
        self.assertIn("inline", docs[0]["cause"].lower())

    def test_no_sources_at_all_is_reported(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("sources.summary", OK, {"website_chars": 0, "documents_chars": 0,
                                     "research_chars": 0}),
        ])
        self.assertIn("no research material",
                      " ".join(x["cause"] for x in f).lower())

    def test_valid_but_empty_model_response_is_reported(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("llm.call.deep", OK, {"populated_fields": 0}),
        ])
        self.assertIn("empty", " ".join(x["cause"] for x in f).lower())

    def test_unseeded_registry_is_reported(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 1,
                                      "llm_endpoints_active": 1}),
            ("sections.write.company_story", FAIL,
             {"error": "SectionUpdateError: Unknown section 'company_story'"}),
        ])
        text = " ".join(x["cause"] + x["remedy"] for x in f).lower()
        self.assertIn("seed_platform_config", text)

    def test_healthy_run_reports_no_fault(self):
        f = self._diagnose([
            ("preflight.config", OK, {"ai_mocked": False, "llm_bindings": 2,
                                      "llm_endpoints_active": 1}),
            ("sources.summary", OK, {"website_chars": 4000,
                                     "documents_chars": 900,
                                     "research_chars": 2200}),
            ("llm.call.deep", OK, {"populated_fields": 42}),
            ("sections.summary", OK, {"sections_written": 9}),
        ])
        self.assertEqual(f[0]["severity"], "NONE")


class PreflightTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def test_preflight_records_the_settings_that_matter(self):
        from fundos.profile.services import preflight_report

        _, _, _, profile = _bootstrap()
        t = GenerationTrace()
        with t:
            fields = preflight_report(profile)
        for key in ("ai_mocked", "llm_bindings", "llm_endpoints_active",
                    "registered_sections"):
            self.assertIn(key, fields)
        self.assertIsNotNone(t.first("preflight.config"))

    def test_preflight_names_the_model_the_run_will_dispatch_on(self):
        """It reported the endpoint's `default_model`, which is only the
        fallback for a call that pins nothing. Both profile roles pin a config
        profile, and the pin's `model_string` is what the adapter uses — so
        every preflight line of a live run named gemini-3.6-flash while the
        research batches ran on gemini-2.5-flash."""
        from fundos.llm.models import LLMConfigProfile
        from fundos.profile.services import preflight_report

        pinned = LLMConfigProfile.objects.filter(
            code="profile.research", is_active=True).first()
        self.assertIsNotNone(pinned)

        _, _, _, profile = _bootstrap()
        with GenerationTrace():
            fields = preflight_report(profile)

        self.assertEqual(fields["research_model"], pinned.model_string)
        self.assertEqual(fields["research_profile"], pinned.code)

    def test_preflight_says_so_when_a_pin_cannot_be_honoured(self):
        """An absent or deactivated pin does not raise — the adapter falls
        back to tier resolution. Before the fact, this line is the only place
        that shows it."""
        from fundos.llm.models import LLMConfigProfile
        from fundos.profile.services import preflight_report

        LLMConfigProfile.objects.filter(code="profile.research").update(
            is_active=False)

        _, _, _, profile = _bootstrap()
        with GenerationTrace():
            fields = preflight_report(profile)

        self.assertIn("UNRESOLVED", fields["research_profile"])


class SourceTracingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def test_every_source_reports_an_outcome(self):
        from fundos.profile.services import collect_sources

        _, user, _, profile = _bootstrap()
        t = GenerationTrace()
        with t:
            collect_sources(profile, user=user)

        for step in ("sources.deal", "sources.documents", "sources.founders",
                     "sources.summary"):
            self.assertIsNotNone(t.first(step), f"{step} was not recorded")

    def test_missing_website_is_recorded_as_skipped_not_silence(self):
        from fundos.profile.services import collect_sources

        _, user, company, profile = _bootstrap()
        company.domain = ""
        company.save()
        profile.website_url = ""
        profile.save()

        t = GenerationTrace()
        with t:
            collect_sources(profile, user=user)
        rec = t.first("sources.website")
        self.assertIsNotNone(rec, "an absent website must still be recorded")
        self.assertEqual(rec["status"], "SKIP")


class ReportTests(TestCase):
    def test_report_contains_steps_and_diagnosis(self):
        t = GenerationTrace(mode="test")
        t.event("preflight.config", WARN, ai_mocked=True)
        text = t.report()
        self.assertIn("PROFILE GENERATION TRACE", text)
        self.assertIn("DIAGNOSIS", text)
        self.assertIn("preflight.config", text)

    def test_to_dict_is_json_serialisable(self):
        import json

        t = GenerationTrace(mode="test")
        t.event("preflight.config", OK, ai_mocked=False)
        json.dumps(t.to_dict(), default=str)
