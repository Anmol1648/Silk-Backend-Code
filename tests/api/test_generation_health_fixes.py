"""Regression tests for the generation-health fixes.

Every test here corresponds to something visible in silk_generation_1.log that
was either misreported or silently lost. Each names the log evidence, because a
test whose motivation is not written down is the first one deleted when it
becomes inconvenient.
"""
from unittest import mock

from django.db import transaction
from django.test import TestCase

from fundos.core.models import Company, Deal, Tenant, User
from fundos.core.scoping import tenant_context


def _world(domain="", website_url=""):
    """One tenant, company, deal and profile. Returns the pieces."""
    from fundos.profile.models import CompanyProfile

    tenant = Tenant.objects.create(name="T-health")
    with tenant_context(tenant.id):
        user = User.objects.create_user(
            email="f@health.io", tenant_id=tenant.id, name="F")
        company = Company.objects.create(
            tenant_id=tenant.id, name="Healthco", domain=domain)
        deal = Deal.objects.create(
            tenant_id=tenant.id, company=company, name="D1",
            round_type="series_a", primary_owner=user, created_by=user)
        profile = CompanyProfile.objects.create(
            tenant_id=tenant.id, company=company, website_url=website_url)
    return tenant, company, deal, profile, user


class DocumentReadinessTests(TestCase):
    """`analysed=0` fired on runs whose documents had been read perfectly.

    Log evidence: the 1 Sep Zyla run recovered 552,541 characters from
    `Project Orah_Financial Model_vf.xlsx` and 22,882 from the teaser, and the
    same run logged `sources.documents status="FAIL" analysed=0`, which raised
    a CRITICAL diagnosis telling the operator to start Celery.

    The cause is that `document_readiness` counted only `summary` and
    `extracted_metrics` — written by the background AI critique — and ignored
    `extraction_status` / `native_chars`, which is what the pipeline's own
    inline reader writes.
    """

    def setUp(self):
        self.tenant, self.company, self.deal, self.profile, _ = _world()

    def _asset(self, **kw):
        from fundos.readiness.models import MaterialAsset
        with tenant_context(self.tenant.id):
            return MaterialAsset.objects.create(
                tenant_id=self.tenant.id, deal_id=self.deal.id, **kw)

    def _readiness(self):
        from fundos.profile.services import document_readiness
        with tenant_context(self.tenant.id):
            return document_readiness(self.profile)

    def test_pipeline_extraction_counts_as_analysed(self):
        """extraction_status='extracted' is a document that WAS read."""
        self._asset(extraction_status="extracted", native_chars=552541)
        self.assertEqual(self._readiness()["analysed"], 1)
        self.assertEqual(self._readiness()["pending"], 0)

    def test_native_chars_alone_counts_as_analysed(self):
        """Text was recovered, so the file is not unread, whatever the
        status column happens to say."""
        self._asset(extraction_status="pending", native_chars=22882)
        self.assertEqual(self._readiness()["analysed"], 1)

    def test_critique_fields_still_count(self):
        """The original signal must keep working — this is additive."""
        self._asset(extraction_status="pending", summary="a summary")
        self.assertEqual(self._readiness()["analysed"], 1)

    def test_genuinely_unread_document_is_still_pending(self):
        """The fix must not make every document look read."""
        self._asset(extraction_status="pending", native_chars=0)
        readiness = self._readiness()
        self.assertEqual(readiness["analysed"], 0)
        self.assertEqual(readiness["pending"], 1)

    def test_failed_extraction_is_not_analysed_and_not_pending(self):
        """A file that was tried and failed is neither read nor waiting."""
        self._asset(extraction_status="failed", native_chars=0)
        readiness = self._readiness()
        self.assertEqual(readiness["analysed"], 0)
        self.assertEqual(readiness["pending"], 0)


class WebsiteAdapterFallbackTests(TestCase):
    """"no valid website URL on the company record" with a URL in the log.

    Log evidence:
        sources.website status="FAIL" url="https://zyla.in"
        error="ValueError: no valid website URL on the company record"

    `_collect_sources` resolves the address as
    `profile.website_url or company.domain` and logs that, but the adapter
    read only `company.domain`. The message sent the operator to a field that
    was legitimately empty.
    """

    def test_falls_back_to_profile_website_url(self):
        from fundos.research.adapters.website import WebsiteAdapter

        tenant, _c, deal, _p, _u = _world(domain="",
                                          website_url="https://zyla.in")
        adapter = WebsiteAdapter(deal=deal, ckb_snapshot={})
        with tenant_context(tenant.id):
            self.assertEqual(adapter._candidate_domain(), "https://zyla.in")

    def test_company_domain_still_wins(self):
        """A curated company domain must not be overridden by the profile."""
        from fundos.research.adapters.website import WebsiteAdapter

        tenant, _c, deal, _p, _u = _world(domain="curated.example.com",
                                          website_url="https://other.example")
        adapter = WebsiteAdapter(deal=deal, ckb_snapshot={})
        with tenant_context(tenant.id):
            self.assertEqual(adapter._candidate_domain(),
                             "curated.example.com")

    def test_both_empty_raises_a_message_naming_both_fields(self):
        """The old message named only the company record."""
        from fundos.research.adapters.website import WebsiteAdapter

        tenant, _c, deal, _p, _u = _world(domain="", website_url="")
        adapter = WebsiteAdapter(deal=deal, ckb_snapshot={})
        with tenant_context(tenant.id):
            with self.assertRaises(ValueError) as caught:
                adapter.fetch()
        message = str(caught.exception)
        self.assertIn("company.domain", message)
        self.assertIn("website_url", message)


class TrackerSavepointTests(TestCase):
    """A swallowed tracker write must not poison the caller's transaction.

    Log evidence, 1 Sep, in order:
        DatabaseError: Save with update_fields did not affect any rows.
        ...
        psycopg2.errors.ForeignKeyViolation: insert or update on table
        "profile_run_event" violates foreign key constraint

    The run row had gone missing. `run.save()` failed, the handler swallowed
    it WITHOUT a savepoint, `connection.needs_rollback` stayed set, and every
    subsequent query in the surrounding atomic block died — including ones
    that had nothing wrong with them.
    """

    def _tracker(self):
        from fundos.profile.pipeline.tracker import RunTracker

        class _DeadRun:
            """Stands in for a run row that is no longer in the database."""
            id = "00000000-0000-0000-0000-000000000000"
            tenant_id = None
            stage = "researching"
            stage_timings = {}

            def save(self, *a, **kw):
                from django.db import DatabaseError
                raise DatabaseError(
                    "Save with update_fields did not affect any rows.")

        return RunTracker(_DeadRun())

    def test_failed_stage_write_leaves_the_transaction_usable(self):
        tracker_ = self._tracker()
        with transaction.atomic():
            tracker_._touch_stage("researching", {"state": "running"})
            # The real assertion: the transaction still works afterwards.
            self.assertTrue(Tenant.objects.filter(name="nope").count() == 0)

    def test_failed_progress_write_leaves_the_transaction_usable(self):
        tracker_ = self._tracker()
        with transaction.atomic():
            tracker_.update(stage_timings={"x": 1})
            self.assertTrue(Tenant.objects.filter(name="nope").count() == 0)

    def test_failed_event_write_leaves_the_transaction_usable(self):
        tracker_ = self._tracker()
        with transaction.atomic():
            tracker_.event("a note that cannot be stored")
            self.assertTrue(Tenant.objects.filter(name="nope").count() == 0)


class DocumentsDiagnosisTests(TestCase):
    """The documents diagnosis named infrastructure that is not in the path.

    The pipeline reads documents inline (`pipeline.source2_documents`), so
    "start Celery and Redis" was advice about a component that never
    participates in reading a profile's documents.
    """

    def _diagnose(self, steps):
        from fundos.profile.trace import GenerationTrace

        trace = GenerationTrace.__new__(GenerationTrace)
        trace.steps = steps
        return trace.diagnose()

    def test_unread_documents_is_informational_not_critical(self):
        findings = self._diagnose([
            {"step": "preflight.config", "status": "OK", "ai_mocked": False},
            {"step": "sources.documents", "status": "FAIL",
             "uploaded": 2, "analysed": 0, "pending": 2},
            {"step": "sections.summary", "status": "OK",
             "sections_written": 14},
        ])
        docs = [f for f in findings if "Documents" in f["cause"]]
        self.assertTrue(docs, "expected a finding about documents")
        self.assertEqual(docs[0]["severity"], "INFO")
        self.assertNotIn("Celery", docs[0]["remedy"])
        self.assertNotIn("Redis", docs[0]["remedy"])

    def test_informational_note_does_not_suppress_the_no_fault_verdict(self):
        """The gate used to be `if not findings`, so ANY note hid this."""
        findings = self._diagnose([
            {"step": "preflight.config", "status": "OK", "ai_mocked": False},
            {"step": "sources.documents", "status": "FAIL",
             "uploaded": 2, "analysed": 0, "pending": 2},
            {"step": "sections.summary", "status": "OK",
             "sections_written": 14},
        ])
        self.assertTrue(
            any("No fault detected" in f["cause"] for f in findings),
            "a healthy run must still report no fault")

    def test_a_real_fault_still_suppresses_the_no_fault_verdict(self):
        findings = self._diagnose([
            {"step": "preflight.config", "status": "WARN", "ai_mocked": True},
            {"step": "sections.summary", "status": "OK",
             "sections_written": 14},
        ])
        self.assertFalse(
            any("No fault detected" in f["cause"] for f in findings))


class AssessmentConfigPreflightTests(TestCase):
    """`error="no_config"` was the largest failure signature in the log.

    44 occurrences, every one an unseeded environment, and every one reported
    only AFTER ten research calls and a synthesis call had been paid for.
    """

    def test_preflight_reports_the_parameter_count(self):
        from fundos.profile.services import preflight_report

        _t, _c, _d, profile, _u = _world()
        fields = preflight_report(profile)
        self.assertIn("assessment_parameters", fields)

    def test_unseeded_config_names_the_seed_command(self):
        from fundos.profile.services import preflight_report

        _t, _c, _d, profile, _u = _world()
        with mock.patch(
                "fundos.profile.assessment_extraction.question_set",
                return_value=([], [])):
            fields = preflight_report(profile)
        self.assertEqual(fields["assessment_parameters"], 0)
        self.assertIn("seed_assessment_config", fields["assessment_config"])

    def test_seeded_config_adds_no_warning_key(self):
        from fundos.profile.services import preflight_report

        _t, _c, _d, profile, _u = _world()
        with mock.patch(
                "fundos.profile.assessment_extraction.question_set",
                return_value=([mock.Mock()], [mock.Mock()])):
            fields = preflight_report(profile)
        self.assertEqual(fields["assessment_parameters"], 2)
        self.assertNotIn("assessment_config", fields)


class AssessmentInputsPromptTests(TestCase):
    """The prompt offered no way to cite an uploaded document.

    `assessment_extraction._citation` accepts `sourceDoc` precisely because
    the company's own financial model has no URL — its docstring records that
    a run "threw away TWENTY-TWO" values for want of one. But the prompt asked
    only for `sourceUrl` and the JSON skeleton it showed the model listed only
    `sourceUrl`, so the model had no field to name the document in. The 1 Sep
    run reported `dossier_sourced=0`.
    """

    def test_prompt_offers_sourcedoc(self):
        from fundos.llm.default_prompts import DEFAULT_PROMPTS
        system = DEFAULT_PROMPTS["assessment_inputs"]["system"]
        self.assertIn("sourceDoc", system)

    def test_prompt_skeleton_includes_sourcedoc_for_values_and_bands(self):
        from fundos.llm.default_prompts import DEFAULT_PROMPTS
        system = DEFAULT_PROMPTS["assessment_inputs"]["system"]
        # Both arrays must carry it; bands were the half that lost every
        # qualitative anchor.
        self.assertEqual(system.count('"sourceDoc":""'), 2)

    def test_citation_accepts_a_document_reference(self):
        """The prompt change is only useful if the parser honours it."""
        from fundos.profile.assessment_extraction import _citation
        url, detail, ok = _citation(
            {"sourceDoc": "Financial Model.xlsx, P&L tab"}, dossier=False)
        self.assertTrue(ok)
        self.assertEqual(url, "")
        self.assertIn("P&L tab", detail)

    def test_citation_still_rejects_a_value_with_no_source(self):
        from fundos.profile.assessment_extraction import _citation
        _url, _detail, ok = _citation({"value": "12"}, dossier=False)
        self.assertFalse(ok)


class SourcesDocumentsStatusTests(TestCase):
    """Not-yet-read documents must not count as a run failure.

    `sources.documents` runs BEFORE the pipeline reads anything, so on a first
    generation "uploaded but nothing analysed" is the normal opening state. It
    was reported FAIL, which put a permanent +1 on every such run's failure
    count — the log's dominant pattern was 56 runs sitting on exactly two
    failures, and this was one of them.
    """

    def setUp(self):
        self.tenant, self.company, self.deal, self.profile, self.user = _world(
            domain="example.com")

    def _document(self):
        """One ProfileDocument with a MaterialAsset that has not been read."""
        from fundos.profile.models import ProfileDocument
        from fundos.readiness.models import MaterialAsset
        with tenant_context(self.tenant.id):
            asset = MaterialAsset.objects.create(
                tenant_id=self.tenant.id, deal_id=self.deal.id,
                extraction_status="pending", native_chars=0)
            ProfileDocument.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                filename="deck.pdf", category="company_presentation",
                material_asset_id=asset.id)

    def _documents_step(self):
        from fundos.profile.services import collect_sources
        from fundos.profile.trace import GenerationTrace

        # The website adapter is stubbed out: this test is about the DOCUMENTS
        # step, and letting it reach the network makes the result depend on
        # whether the build host has outbound access.
        with tenant_context(self.tenant.id), \
                mock.patch("fundos.research.adapters.website.WebsiteAdapter"
                           ".fetch", return_value={"facts": {}, "raw": {},
                                                   "confidence": 0.5}):
            with GenerationTrace(profile=self.profile, company=self.company,
                                 mode="pipeline", trigger="test") as trace:
                collect_sources(self.profile, user=self.user)
                steps = [s for s in trace.steps
                         if s["step"] == "sources.documents"]
        return steps[0] if steps else None

    def test_unread_document_is_skip_not_fail(self):
        self._document()
        step = self._documents_step()
        self.assertIsNotNone(step)
        self.assertEqual(step["status"], "SKIP")

    def test_no_documents_at_all_is_ok(self):
        step = self._documents_step()
        self.assertIsNotNone(step)
        self.assertEqual(step["status"], "OK")


class DiagnosisLogLevelTests(TestCase):
    """An INFO diagnosis must not be logged as a WARNING.

    Operators grep WARNING. A note that needs no action, logged at WARNING on
    every healthy run with documents, is how the lines that do need action
    stop being read.
    """

    def test_info_diagnosis_logs_at_info_level(self):
        from fundos.profile.trace import GenerationTrace

        trace = GenerationTrace.__new__(GenerationTrace)
        trace.steps = [
            {"step": "preflight.config", "status": "OK", "ai_mocked": False},
            {"step": "sources.documents", "status": "SKIP",
             "uploaded": 2, "analysed": 0, "pending": 2},
            {"step": "sections.summary", "status": "OK",
             "sections_written": 14},
        ]
        trace.started = None
        trace.run_id = "abc123"

        with mock.patch.object(GenerationTrace, "elapsed_ms", return_value=1), \
                mock.patch.object(GenerationTrace, "failures",
                                  return_value=[]), \
                self.assertLogs("fundos.generation", level="INFO") as logs:
            trace.log_diagnosis()

        info_lines = [r for r in logs.records
                      if "DIAGNOSIS" in r.getMessage()]
        self.assertTrue(info_lines)
        for record in info_lines:
            self.assertNotEqual(
                record.levelname, "WARNING",
                f"informational diagnosis logged as WARNING: "
                f"{record.getMessage()}")
