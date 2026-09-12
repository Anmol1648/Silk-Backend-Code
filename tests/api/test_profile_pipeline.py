"""Company Master Data Pipeline.

The tests are grouped by the property each one protects, and every group names
the failure it exists to catch. Several are ported directly from the
prototype's own docstrings, because those encode observed model behaviour
rather than imagined behaviour — an LLM really does flatten the response
wrapper, omit a section, and return an object where an array was asked for.
"""
import threading
import uuid

from django.test import TestCase, override_settings

from fundos.core.scoping import tenant_context
from tests.conftest_helpers import make_world


# ---------------------------------------------------------------------------
# The question bank
# ---------------------------------------------------------------------------
class QuestionBankTests(TestCase):
    """The 100 questions are the research methodology, and they are editable.

    What must hold: the shipped set is complete, the admin table wins when it
    has rows, and an empty table falls back rather than running a company
    profile with no research at all.
    """

    def test_the_shipped_bank_is_ten_batches_of_ten(self):
        from fundos.profile.pipeline import questions

        self.assertEqual(len(questions.SHIPPED_BATCHES), 10)
        self.assertEqual(questions.SHIPPED_QUESTION_COUNT, 100)
        for code, topic, covers, texts in questions.SHIPPED_BATCHES:
            self.assertEqual(len(texts), 10, f"batch {code} is not ten")
            self.assertTrue(topic and covers,
                            f"batch {code} has no topic or covers")

    def test_placeholders_are_substituted(self):
        from fundos.profile.pipeline import questions

        batches = questions.build_batches("Acme Ltd", "https://acme.example")
        joined = " ".join(q for b in batches for q in b.questions)
        self.assertIn("Acme Ltd", joined)
        self.assertIn("https://acme.example", joined)
        self.assertNotIn("{company_name}", joined)
        self.assertNotIn("{website}", joined)

    def test_batches_are_indexed_from_one_in_configured_order(self):
        from fundos.profile.pipeline import questions

        batches = questions.build_batches("Acme", "https://acme.example")
        self.assertEqual([b.index for b in batches],
                         list(range(1, len(batches) + 1)))

    def test_an_empty_table_falls_back_to_the_shipped_bank(self):
        """An unseeded install must still research the company.

        This is the difference between "the admin turned research off" and
        "nobody has run the seeder yet". Only the first should produce a run
        with no web research.
        """
        from fundos.profile.pipeline import questions

        batches = questions.build_batches("Acme", "https://acme.example")
        self.assertEqual(len(batches), 10)

    def test_the_admin_table_wins_when_it_has_rows(self):
        from fundos.platformcfg.models import (ResearchQuestion,
                                               ResearchQuestionBatch)
        from fundos.profile.pipeline import questions

        batch = ResearchQuestionBatch.objects.create(
            code="custom", topic="Only This", covers="8.1", sort_order=1)
        ResearchQuestion.objects.create(
            batch=batch, text="What does {company_name} do?", sort_order=1)

        batches = questions.build_batches("Acme", "https://acme.example")
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].topic, "Only This")
        self.assertEqual(batches[0].questions, ["What does Acme do?"])

    def test_deactivating_a_batch_removes_its_call(self):
        """Each batch is one billed search-grounded call, so this is the
        cost lever an administrator is told to reach for."""
        from fundos.platformcfg.models import (ResearchQuestion,
                                               ResearchQuestionBatch)
        from fundos.profile.pipeline import questions

        for i, active in enumerate([True, False], start=1):
            batch = ResearchQuestionBatch.objects.create(
                code=f"b{i}", topic=f"T{i}", sort_order=i, is_active=active)
            ResearchQuestion.objects.create(batch=batch, text="q?",
                                            sort_order=1)

        batches = questions.build_batches("Acme", "")
        self.assertEqual([b.topic for b in batches], ["T1"])

    def test_a_batch_with_no_active_questions_is_not_called(self):
        """A batch that can only ask nothing must not be paid for."""
        from fundos.platformcfg.models import (ResearchQuestion,
                                               ResearchQuestionBatch)
        from fundos.profile.pipeline import questions

        empty = ResearchQuestionBatch.objects.create(code="empty", topic="E",
                                                     sort_order=1)
        ResearchQuestion.objects.create(batch=empty, text="q?", sort_order=1,
                                        is_active=False)
        full = ResearchQuestionBatch.objects.create(code="full", topic="F",
                                                    sort_order=2)
        ResearchQuestion.objects.create(batch=full, text="q?", sort_order=1)

        self.assertEqual([b.topic for b in questions.build_batches("A", "")],
                         ["F"])

    def test_an_unsubstitutable_placeholder_does_not_kill_the_run(self):
        """An admin typing a stray brace must not take generation down.

        `str.format` raises on `{foo}`; a question that reads slightly wrong is
        worth far more than a run that never starts.
        """
        from fundos.platformcfg.models import (ResearchQuestion,
                                               ResearchQuestionBatch)
        from fundos.profile.pipeline import questions

        batch = ResearchQuestionBatch.objects.create(code="x", topic="X",
                                                     sort_order=1)
        ResearchQuestion.objects.create(
            batch=batch, text="What about {unknown_key} at {company_name}?",
            sort_order=1)

        batches = questions.build_batches("Acme", "")
        self.assertEqual(len(batches), 1)
        self.assertIn("{unknown_key}", batches[0].questions[0])


# ---------------------------------------------------------------------------
# The output schema
# ---------------------------------------------------------------------------
class SchemaTests(TestCase):
    """The 17-section contract, and the fact that it is admin-configurable."""

    def test_the_shipped_schema_has_seventeen_sections(self):
        from fundos.profile import schema

        self.assertEqual(len(schema.SHIPPED_SECTIONS), 17)
        keys = [s["key"] for s in schema.SHIPPED_SECTIONS]
        self.assertEqual(len(set(keys)), 17, "duplicate section key")
        for section in schema.SHIPPED_SECTIONS:
            self.assertIn(section["kind"], ("object", "array"))
            self.assertTrue(section["fields"], f"{section['key']} has no spec")
            self.assertTrue(section["storage_key"])

    def test_the_document_center_is_never_asked_of_the_model(self):
        """We know exactly what was uploaded. Asking the model to describe it
        would only invite it to invent document metadata."""
        from fundos.profile import schema

        self.assertIn("document_center", schema.PIPELINE_OWNED_SECTIONS)
        self.assertNotIn("document_center", schema.promptable_keys())
        self.assertNotIn("document_center", schema.schema_prompt_block())

    def test_the_prompt_block_renders_every_field(self):
        from fundos.profile import schema

        block = schema.schema_prompt_block()
        self.assertIn('sections["company_profile"]', block)
        self.assertIn("description_of_business", block)
        self.assertIn("data is an ARRAY of objects", block)
        self.assertIn("data is an OBJECT with these fields", block)

    def test_an_admin_edit_to_field_spec_changes_the_prompt(self):
        """The point of putting the schema in a table: adding a field is an
        admin edit, and the next run asks for it."""
        from fundos.platformcfg.models import ProfileSectionConfig
        from fundos.profile import schema

        ProfileSectionConfig.objects.create(
            section_key="company_overview", label="Company Overview",
            kind="structured", sort_order=10, is_active=True,
            container_kind="object", spec_ref="8.1",
            storage_key="company_overview",
            field_spec={"employee_count": "number|null - total headcount"})

        block = schema.schema_prompt_block()
        self.assertIn("employee_count", block)
        self.assertIn("total headcount", block)

    def test_a_section_owned_by_another_subsystem_is_ignored_silently(self):
        """readiness and the knowledge base are real sections with no field
        spec. They are not part of the generated profile and must not warn."""
        from fundos.platformcfg.models import ProfileSectionConfig
        from fundos.profile import schema

        ProfileSectionConfig.objects.create(
            section_key="readiness", label="Investor Readiness",
            kind="indicator", sort_order=170, is_active=True)

        self.assertNotIn("readiness", schema.section_keys())

    def test_storage_keys_round_trip(self):
        from fundos.profile import schema

        self.assertEqual(schema.storage_key_for("news"), "recent_news")
        self.assertEqual(schema.storage_key_for("company_profile"),
                         "company_overview")
        self.assertEqual(schema.storage_key_for("investors_cap_table"),
                         "cap_table")
        self.assertEqual(schema.wire_key_for("market_research"),
                         "industry_research")


class NormalizeProfileTests(TestCase):
    """Ported from the prototype's own docstrings.

    Every case here is a real shape an LLM has been observed to return. The
    contract is that each is COERCED, not rejected: turning a nearly-correct
    response into a total failure over a cosmetic mismatch would discard
    sixteen good sections to punish the seventeenth.
    """

    def test_every_section_is_present_even_when_the_response_is_empty(self):
        from fundos.profile import schema

        profile = schema.normalize_profile({})
        self.assertEqual(set(profile["sections"]), set(schema.section_keys()))
        self.assertTrue(all(not s["isComplete"]
                            for s in profile["sections"].values()))

    def test_a_flattened_wrapper_is_accepted(self):
        """The model returned the section map at the top level."""
        from fundos.profile import schema

        profile = schema.normalize_profile(
            {"company_story": {"usp": "Fastest in market"}})
        self.assertEqual(profile["sections"]["company_story"]["data"]["usp"],
                         "Fastest in market")
        self.assertTrue(profile["sections"]["company_story"]["isComplete"])

    def test_a_missing_section_stays_empty_rather_than_raising(self):
        from fundos.profile import schema

        profile = schema.normalize_profile({"sections": {"news": []}})
        self.assertEqual(profile["sections"]["competitors"]["data"], [])

    def test_an_object_where_an_array_was_asked_for_is_wrapped(self):
        from fundos.profile import schema

        profile = schema.normalize_profile(
            {"sections": {"news": {"data": {"title": "Raised $5m"}}}})
        self.assertEqual(profile["sections"]["news"]["data"],
                         [{"title": "Raised $5m"}])

    def test_an_array_where_an_object_was_asked_for_is_unwrapped(self):
        from fundos.profile import schema

        profile = schema.normalize_profile(
            {"sections": {"business_model": {"data": [{"sales_model": "PLG"}]}}})
        self.assertEqual(profile["sections"]["business_model"]["data"],
                         {"sales_model": "PLG"})

    def test_a_bare_scalar_leaves_the_section_empty(self):
        """Nothing sane to coerce — storing it would break every consumer's
        assumption about the container type."""
        from fundos.profile import schema

        profile = schema.normalize_profile(
            {"sections": {"business_model": {"data": "B2B SaaS"}}})
        self.assertEqual(profile["sections"]["business_model"]["data"], {})

    def test_unknown_keys_are_dropped(self):
        from fundos.profile import schema

        profile = schema.normalize_profile({"sections": {"invented": {"x": 1}}})
        self.assertNotIn("invented", profile["sections"])

    def test_is_complete_is_computed_not_trusted(self):
        """A section with the right shape and every field blank is NOT
        complete, whatever the model claimed. This is what distinguishes 'the
        model looked and found nothing' from 'the model reported something'."""
        from fundos.profile import schema

        profile = schema.normalize_profile({"sections": {
            "company_profile": {"isComplete": True,
                                "data": {"website": "", "country": ""}},
            "company_story": {"isComplete": False, "data": {"usp": "Real"}},
        }})
        self.assertFalse(profile["sections"]["company_profile"]["isComplete"])
        self.assertTrue(profile["sections"]["company_story"]["isComplete"])

    def test_a_non_dict_response_yields_the_empty_skeleton(self):
        from fundos.profile import schema

        profile = schema.normalize_profile(["not", "an", "object"])
        self.assertEqual(set(profile["sections"]), set(schema.section_keys()))

    def test_document_center_is_overwritten_from_our_own_records(self):
        from fundos.profile import schema

        profile = schema.normalize_profile(
            {"sections": {"document_center": {"data": {"documents": [
                {"filename": "invented.pdf"}]}}}})
        profile = schema.apply_document_center(
            profile, [{"filename": "real.pdf", "status": "Processed"}])
        docs = profile["sections"]["document_center"]["data"]["documents"]
        self.assertEqual([d["filename"] for d in docs], ["real.pdf"])
        self.assertTrue(profile["sections"]["document_center"]["isComplete"])


# ---------------------------------------------------------------------------
# The dossier
# ---------------------------------------------------------------------------
class _FakeRun:
    """A run stand-in — DossierWriter only needs two ids for its path."""

    def __init__(self):
        self.id = uuid.uuid4()
        self.profile_id = uuid.uuid4()


class DossierTests(TestCase):
    """The evidence file. Its order must not depend on network timing."""

    def _writer(self):
        from fundos.profile.pipeline.dossier import DossierWriter
        return DossierWriter(_FakeRun(), "Acme", "https://acme.example", [])

    def test_section_order_is_fixed_regardless_of_arrival_order(self):
        """The order is fixed, and it is DOCUMENTS AHEAD OF RESEARCH.

        It used to be research first. The dossier is capped before synthesis
        and the cut takes the tail, so whatever renders last is what gets
        lost — and losing the company's own financial model to keep a search
        result whole is backwards.
        """
        writer = self._writer()
        writer.set_section("source1", "# Research\n\nResearch text.")
        writer.set_section("source2", "# Documents\n\nDoc text.")
        text = writer.text
        self.assertLess(text.index("Doc text."),
                        text.index("Research text."),
                        "uploaded documents must render before web research "
                        "whatever the completion order")

    def test_an_unknown_section_key_fails_loudly(self):
        """A silent no-op would produce a dossier quietly missing a source,
        and the profile would look merely thin rather than broken."""
        writer = self._writer()
        with self.assertRaises(ValueError):
            writer.set_section("source3", "text")

    def test_a_source_that_has_not_reported_says_so(self):
        """'This source found nothing' and 'this source has not run' are very
        different statements about a company."""
        writer = self._writer()
        writer.set_section("source1", "# Research\n\nFound things.")
        self.assertIn("_This source has not reported yet._", writer.text)

    def test_the_last_write_for_a_key_wins(self):
        writer = self._writer()
        writer.set_section("source1", "# R\n\nfirst")
        writer.set_section("source1", "# R\n\nsecond")
        self.assertIn("second", writer.text)
        self.assertNotIn("first", writer.text)

    def test_concurrent_writes_do_not_interleave(self):
        writer = self._writer()

        def write(key, marker):
            for _ in range(40):
                writer.set_section(key, f"# {key}\n\n{marker * 200}")

        threads = [threading.Thread(target=write, args=("source1", "A")),
                   threading.Thread(target=write, args=("source2", "B"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = writer.text
        self.assertIn("A" * 200, text)
        self.assertIn("B" * 200, text)
        self.assertEqual(text.count("# Consolidated research dossier"), 1)

    def test_founders_are_labelled_unverified(self):
        """Not decoration — a prompt-safety measure. Without the label the
        model cannot tell a typed-in name from a researched fact."""
        from fundos.profile.pipeline.dossier import DossierWriter

        writer = DossierWriter(_FakeRun(), "Acme", "https://acme.example",
                               [{"name": "A. Founder", "designation": "CEO"}])
        text = writer.text
        self.assertIn("A. Founder", text)
        self.assertIn("unverified", text.lower())


# ---------------------------------------------------------------------------
# Research: failure isolation and grounding
# ---------------------------------------------------------------------------
class ResearchFailureTests(TestCase):
    """One bad batch must cost that batch and nothing else."""

    def _batch(self):
        from fundos.profile.pipeline.questions import QuestionBatch
        return QuestionBatch(index=3, topic="Funding History",
                             covers="8.10 Funding History",
                             questions=[f"Q{i}?" for i in range(1, 11)])

    def test_a_failed_batch_lists_its_unanswered_questions(self):
        """This is what makes partial failure legible in the dossier itself.
        A heading that silently vanishes could be mistaken for 'nothing to
        report' instead of 'we could not find out'."""
        from fundos.profile.pipeline import source1_research

        markdown = source1_research._failure_markdown(self._batch(),
                                                      "quota exhausted")
        self.assertIn("This batch failed", markdown)
        self.assertIn("quota exhausted", markdown)
        for i in range(1, 11):
            self.assertIn(f"Q{i}?", markdown)

    def test_a_batch_that_did_not_search_is_marked_ungrounded(self):
        """An uncited answer that reads like a researched one is the most
        expensive failure this pipeline can produce, so it is labelled."""
        from fundos.profile.pipeline import source1_research

        grounded = source1_research._batch_markdown(self._batch(), "body",
                                                    searches=3)
        ungrounded = source1_research._batch_markdown(self._batch(), "body",
                                                      searches=0)
        self.assertIn("3 web search(es) performed", grounded)
        self.assertIn("Not grounded", ungrounded)
        self.assertIn("unverified", ungrounded)


class GroundingGateTests(TestCase):
    """The guard that stopped a run publishing 122 invented fields.

    The gate must key off what was actually RETRIEVED, and it must reach the
    same verdict as the scorecard, since the two disagreeing is what let a
    refused-to-score run publish a dossier anyway.
    """

    def test_a_run_that_searched_nothing_and_read_nothing_is_refused(self):
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, reason, mode = _check_grounding(
            {"searches": 0, "text_chars": 0}, {"documents": []})
        self.assertFalse(ok)
        self.assertEqual(mode, "none")
        self.assertIn("nothing was retrieved", reason)

    def test_searches_alone_are_enough(self):
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, _, mode = _check_grounding({"searches": 4, "text_chars": 9000},
                                       {"documents": []})
        self.assertTrue(ok)
        self.assertEqual(mode, "full")

    def test_documents_alone_are_enough(self):
        """A company with no web presence but a real data room is a grounded
        run, not a refused one."""
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, _, mode = _check_grounding(
            {"searches": 0, "text_chars": 0},
            {"documents": [{"status": "Processed", "filename": "deck.pdf"}]})
        self.assertTrue(ok)
        self.assertEqual(mode, "full")

    def test_a_failed_document_does_not_count_as_retrieved(self):
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, _, _ = _check_grounding(
            {"searches": 0, "text_chars": 0},
            {"documents": [{"status": "Failed", "filename": "deck.pdf"}]})
        self.assertFalse(ok)

    def test_research_text_counts_when_the_provider_reports_no_searches(self):
        """"Unknown" is not "none".

        Some providers return no grounding metadata at all, which surfaces as
        `searches=0`. A run that came back with batches of cited prose has
        plainly retrieved something, and refusing it on a missing counter
        would be the gate misfiring on its own blind spot.
        """
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, _, mode = _check_grounding(
            {"searches": 0, "text_chars": 40000}, {"documents": []})
        self.assertTrue(ok)
        self.assertEqual(mode, "full")

    def test_mocked_runs_are_exempt(self):
        """Mock output is fabricated by construction. The gate exists to stop a
        FOUNDER seeing invented facts; in mocked mode there is no founder, and
        the only effect would be that CI could never exercise the pipeline."""
        from fundos.profile.pipeline.orchestrator import _check_grounding

        ok, _, mode = _check_grounding({"searches": 0, "text_chars": 0},
                                       {"documents": []}, mocked=True)
        self.assertTrue(ok)
        self.assertEqual(mode, "mocked")


# ---------------------------------------------------------------------------
# Document handling
# ---------------------------------------------------------------------------
class DocumentPolicyTests(TestCase):
    """Every file's treatment is decided before any work starts, so 'how will
    this be read, and why' is answerable up front rather than inferred
    afterwards from whatever text happened to come out."""

    def test_a_spreadsheet_is_never_sent_to_a_model(self):
        """Cell extraction is exact; a model reading it could only be worse."""
        from fundos.profile.pipeline import source2_documents as s2

        for name in ("model.xlsx", "memo.docx", "data.csv"):
            policy = s2.policy_for(name)
            self.assertEqual(policy.read_mode, "never")
            self.assertEqual(policy.handler, "native only")

    def test_a_pdf_is_read_by_the_document_model(self):
        from fundos.profile.pipeline import source2_documents as s2

        policy = s2.policy_for("annual_report.pdf")
        self.assertEqual(policy.read_mode, "model")
        self.assertEqual(policy.unit, "page")

    def test_a_deck_is_not_read_by_the_document_model(self):
        """Charts and screenshots do carry numbers that exist only as
        pixels — and no provider accepts a PowerPoint file, so this asked
        for a read it could not get and failed on every run. The policy now
        states the loss and the remedy instead of promising the read.
        """
        from fundos.profile.pipeline import source2_documents as s2

        policy = s2.policy_for("pitch.pptx")
        self.assertEqual(policy.read_mode, "never")
        self.assertEqual(policy.unit, "slide")
        self.assertIn("PDF", policy.description)

    def test_an_image_has_no_other_route_to_its_content(self):
        from fundos.profile.pipeline import source2_documents as s2

        self.assertEqual(s2.policy_for("cap_table.png").read_mode, "model")

    def test_an_unknown_extension_is_best_effort_without_a_model(self):
        from fundos.profile.pipeline import source2_documents as s2

        policy = s2.policy_for("mystery.xyz")
        self.assertEqual(policy.read_mode, "never")
        self.assertIn("Unrecognised", policy.description)

    def test_every_policy_explains_itself(self):
        """The description is surfaced verbatim to the founder in the Document
        Center, so a blank one is a real defect."""
        from fundos.profile.pipeline import source2_documents as s2

        for name in ("a.pdf", "b.pptx", "c.xlsx", "d.png", "e.zzz"):
            policy = s2.policy_for(name)
            self.assertTrue(len(policy.description) > 40, name)

    def test_reading_is_skipped_with_a_reason_when_disabled(self):
        from fundos.profile.pipeline import source2_documents as s2

        with override_document_model(enabled=False):
            outcome = s2._model_stage("x.pdf", "x.pdf",
                                      s2.policy_for("x.pdf"),
                                      lambda _m: None, [], None)
        self.assertEqual(outcome.status, "disabled")
        self.assertIn("switched off", outcome.reason)

    def test_a_failed_read_never_costs_the_file_its_native_text(self):
        """The model is an addition to native extraction, never a
        replacement for it — so its failure is reported, not raised."""
        from unittest import mock

        from fundos.profile.pipeline import document_ai
        from fundos.profile.pipeline import source2_documents as s2

        sections = []
        with mock.patch.object(
                document_ai, "read_document",
                side_effect=document_ai.DocumentReadError("no API key")):
            outcome = s2._model_stage("x.pdf", "x.pdf",
                                      s2.policy_for("x.pdf"),
                                      lambda _m: None, sections, None)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("no API key", outcome.reason)
        self.assertTrue(sections, "the dossier must say the read failed")


# ---------------------------------------------------------------------------
# Writing the profile
# ---------------------------------------------------------------------------
class GenerationDoesNotDestroyHumanDataTests(TestCase):
    """A generation run has not seen the founder's own entries and is in no
    position to remove them.

    The write path is shared with the human PATCH deliberately — one contract,
    no drift — but the two differ on exactly this point: a PATCH is the
    authoritative full array, a generation is not.
    """

    @classmethod
    def setUpClass(cls):
        # The section registry decides what is writable, so these tests need
        # it. Seeded once for the class rather than per test.
        super().setUpClass()
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.tenant = self.world["a"]["tenant"]
        self.user = self.world["a"]["founder"]
        with tenant_context(self.tenant.id):
            self.profile = get_or_create_profile(self.world["a"]["company"],
                                                 user=self.user)

    def _write_ai_founders(self, rows):
        from fundos.profile.section_writer import update_section_from_data
        update_section_from_data(self.profile, "founders", rows,
                                 user=self.user, by_ai=True)

    def test_a_founder_entered_row_survives_generation(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Asha Rao", designation="CEO", source="founder")

            self._write_ai_founders([
                {"name": "Invented Person", "role": "CTO",
                 "background": "From the model.", "is_founder": True},
            ])

            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))
        self.assertIn("Asha Rao", names,
                      "generation deleted a founder the human entered")
        self.assertIn("Invented Person", names,
                      "generation must still add what it found")

    def test_a_second_run_replaces_only_its_own_previous_output(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            self._write_ai_founders([{"name": "First Guess",
                                      "is_founder": True}])
            self._write_ai_founders([{"name": "Second Guess",
                                      "is_founder": True}])
            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))
        self.assertEqual(names, {"Second Guess"},
                         "an AI row from the previous run must be replaced")

    def test_the_model_does_not_duplicate_a_name_a_human_claimed(self):
        from fundos.profile.models import Founder

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Asha Rao", designation="CEO", source="founder")
            self._write_ai_founders([
                {"name": "asha rao", "role": "Chief Executive",
                 "is_founder": True}])
            rows = list(Founder.objects.filter(profile=self.profile))

        self.assertEqual(len(rows), 1, "the same person was listed twice")
        self.assertEqual(rows[0].designation, "CEO",
                         "the human's own value must win")

    def test_a_human_patch_is_still_a_full_replacement(self):
        """The other half of the rule. A PATCH body IS authoritative — the
        client just rendered those rows and the user edited them."""
        from fundos.profile.models import Founder
        from fundos.profile.section_writer import update_section_from_data

        with tenant_context(self.tenant.id):
            Founder.objects.create(
                tenant_id=self.tenant.id, profile=self.profile,
                name="Removed By User", source="founder")
            update_section_from_data(
                self.profile, "founders",
                [{"name": "Kept", "is_founder": True}], user=self.user)
            names = set(Founder.objects.filter(profile=self.profile)
                        .values_list("name", flat=True))
        self.assertEqual(names, {"Kept"})

    def test_key_people_are_split_out_of_the_founders_list(self):
        from fundos.profile.models import Founder, KeyPerson

        with tenant_context(self.tenant.id):
            self._write_ai_founders([
                {"name": "A Founder", "role": "CEO", "is_founder": True},
                {"name": "A Hire", "role": "VP Sales", "is_founder": False},
            ])
            founders = list(Founder.objects.filter(profile=self.profile)
                            .values_list("name", flat=True))
            people = list(KeyPerson.objects.filter(profile=self.profile)
                          .values_list("name", flat=True))

        self.assertEqual(founders, ["A Founder"])
        self.assertEqual(people, ["A Hire"],
                         "a non-founder must land in KeyPerson, so the "
                         "records endpoint still sees them")

    def test_a_section_the_model_left_empty_is_not_written(self):
        """Empty is a legitimate answer for a company with no funding history,
        and must not overwrite anything or count as a failure."""
        from fundos.profile import schema
        from fundos.profile.pipeline.writer import write_profile

        with tenant_context(self.tenant.id):
            generated = schema.empty_profile()
            result = write_profile(self.profile, generated, user=self.user)

        self.assertEqual(result["written"], [])
        self.assertEqual(result["failed"], [])
        self.assertIn("funding_history", result["empty"])


# ---------------------------------------------------------------------------
# The LLM adapter's text mode
# ---------------------------------------------------------------------------
class AdapterTextModeTests(TestCase):
    """A prose role must not go through the JSON parse/repair/validate path,
    and must still be counted, logged and budgeted like every other call."""

    def test_response_kind_is_validated(self):
        from fundos.llm.adapter import llm_generate

        with self.assertRaises(ValueError):
            llm_generate(role="profile_research_batch", response_kind="yaml")

    def test_a_mocked_text_role_returns_the_text_envelope(self):
        """The mocked path must be shaped like the live one, or development
        exercises a branch production never takes."""
        from fundos.llm.adapter import llm_generate

        result = llm_generate(role="profile_research_batch",
                              context={"company": {"name": "Acme"}},
                              response_kind="text")
        self.assertIn("text", result)
        self.assertIsInstance(result["text"], str)
        self.assertEqual(result["searches"], 0)
        self.assertEqual(result["grounding"], [])

    def test_the_synthesis_mock_is_a_valid_profile(self):
        from fundos.llm.adapter import llm_generate
        from fundos.profile import schema

        raw = llm_generate(role="profile_synthesis",
                           context={"company": {"name": "Acme"}})
        profile = schema.normalize_profile(raw)
        self.assertEqual(set(profile["sections"]), set(schema.section_keys()))
        self.assertTrue(profile["sections"]["company_profile"]["isComplete"])


class RoleRegistrationTests(TestCase):
    """A role missing from one of these registries fails at runtime, on a
    system with mocked AI switched off, with no way for an admin to fix it."""

    def test_both_roles_are_declared(self):
        from fundos.llm.models import LLM_ROLES

        self.assertIn("profile_research_batch", LLM_ROLES)
        self.assertIn("profile_synthesis", LLM_ROLES)

    def test_both_roles_have_a_shipped_prompt(self):
        from fundos.llm.default_prompts import DEFAULT_PROMPTS

        for role in ("profile_research_batch", "profile_synthesis"):
            self.assertIn(role, DEFAULT_PROMPTS)
            self.assertTrue(DEFAULT_PROMPTS[role]["system"].strip())
            self.assertTrue(DEFAULT_PROMPTS[role]["user"].strip())

    def test_both_roles_have_an_explicit_tier_policy(self):
        """An omission and a decision must not render identically — the defect
        v29 §2.1 traced through four roles and six releases."""
        from fundos.llm.default_prompts import ROLE_TIER_DEFAULTS

        self.assertEqual(ROLE_TIER_DEFAULTS["profile_research_batch"],
                         "advanced")
        self.assertEqual(ROLE_TIER_DEFAULTS["profile_synthesis"], "judgment")

    def test_both_roles_have_an_output_ceiling(self):
        """v29 §1.1: a role that falls through to the 2048 default produces
        truncated JSON and logs it as a success."""
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS

        self.assertGreaterEqual(
            ROLE_MAX_OUTPUT_TOKENS["profile_research_batch"], 32768)
        self.assertGreaterEqual(
            ROLE_MAX_OUTPUT_TOKENS["profile_synthesis"], 65536)

    def test_both_roles_have_a_timeout_floor(self):
        """The endpoint's single timeout is set for its ordinary calls.
        Synthesis over a full dossier is not one: on the first complete live
        run the response was still streaming when a 120s read timeout cut it
        off, losing all ten batches of research at the eleventh call."""
        from fundos.llm.adapter import ROLE_TIMEOUT_SECONDS

        self.assertGreaterEqual(
            ROLE_TIMEOUT_SECONDS["profile_research_batch"], 300)
        self.assertGreaterEqual(
            ROLE_TIMEOUT_SECONDS["profile_synthesis"], 600)

    def test_an_endpoint_timeout_cannot_drop_below_the_role_floor(self):
        """A floor, not an override: a larger endpoint value still wins, and
        a role with no floor is left entirely to the endpoint."""
        from fundos.llm.adapter import ROLE_TIMEOUT_SECONDS, resolve_timeout

        floor = ROLE_TIMEOUT_SECONDS["profile_synthesis"]
        self.assertEqual(resolve_timeout(120, "profile_synthesis"), floor)
        self.assertEqual(resolve_timeout(floor + 60, "profile_synthesis"),
                         floor + 60)
        self.assertEqual(resolve_timeout(90, "profile_qa"), 90)

    def test_research_is_declared_search_required(self):
        """An answer this role produced without searching is recollection.

        Declaring it required is what makes the fallback tier the SEARCHING
        one if its config profile is ever deleted, and what makes the
        wire-level contract check refuse a call whose search tool was switched
        off — rather than billing for an uncitable answer.
        """
        from fundos.llm.adapter import (SEARCH_EXPECTED_ROLES,
                                        SEARCH_REQUIRED_ROLES)

        self.assertIn("profile_research_batch", SEARCH_REQUIRED_ROLES)
        self.assertIn("profile_research_batch", SEARCH_EXPECTED_ROLES)

    def test_synthesis_is_not_search_required(self):
        """It reconciles a dossier it is handed. Search there would let it
        extend the evidence past what the dossier records, which is the one
        thing the two-stage split exists to prevent."""
        from fundos.llm.adapter import SEARCH_REQUIRED_ROLES

        self.assertNotIn("profile_synthesis", SEARCH_REQUIRED_ROLES)

    def test_synthesis_is_not_given_a_derived_response_schema(self):
        """Its validation contract is one key. A flat projection of that would
        describe the response without constraining any of it."""
        from fundos.llm import schemas

        self.assertNotIn("profile_synthesis", schemas.SCHEMA_IS_COMPLETE_OUTPUT)
        self.assertIsNone(schemas.json_schema_for("profile_synthesis"))

    def test_every_role_set_names_a_real_role(self):
        """The v29 defect: two entries in SEARCH_BENEFICIAL_ROLES were not
        roles at all, so the warning they existed to raise could never fire."""
        from fundos.llm.adapter import (SEARCH_BENEFICIAL_ROLES,
                                        SEARCH_REQUIRED_ROLES)
        from fundos.llm.models import LLM_ROLES

        for name in SEARCH_BENEFICIAL_ROLES | SEARCH_REQUIRED_ROLES:
            self.assertIn(name, LLM_ROLES, f"{name} is not a role")


class ReportedDatePrecisionTests(TestCase):
    """Sources date things as precisely as they know them.

    The first complete live run researched Zerodha across 235 searches into a
    288,000-character dossier with pages of dated news, and stored no news at
    all: the model reported "2024-08", `DateField` refused it, and because one
    bad row raises out of the whole entity write, every other news item went
    with it. `funding_history` was lost the same way, on "2010-08".
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command

        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.tenant = self.world["a"]["tenant"]
        self.user = self.world["a"]["founder"]
        with tenant_context(self.tenant.id):
            self.profile = get_or_create_profile(self.world["a"]["company"],
                                                 user=self.user)

    def _write(self, section_key, rows):
        from fundos.profile.section_writer import update_section_from_data

        update_section_from_data(self.profile, section_key, rows,
                                 user=self.user, by_ai=True)

    def test_partial_dates_resolve_to_the_start_of_the_period(self):
        from fundos.profile.section_writer import _as_date

        self.assertEqual(_as_date("2024-08-14"), ("2024-08-14", "day"))
        self.assertEqual(_as_date("2024-08"), ("2024-08-01", "month"))
        self.assertEqual(_as_date("2024"), ("2024-01-01", "year"))

    def test_an_unparseable_date_costs_the_date_not_the_row(self):
        from fundos.profile.section_writer import _as_date

        self.assertEqual(_as_date("last summer"), (None, ""))
        self.assertEqual(_as_date(""), (None, ""))
        self.assertEqual(_as_date(None), (None, ""))

    def test_a_month_precision_news_item_is_stored(self):
        from fundos.profile.models import NewsItem

        with tenant_context(self.tenant.id):
            self._write("news", [
                {"title": "Zerodha Fund House launches",
                 "date": "2024-08", "source": "Mint"},
            ])
            row = NewsItem.objects.filter(profile=self.profile).first()

        self.assertIsNotNone(row, "the news item was discarded")
        self.assertEqual(str(row.published_date), "2024-08-01")

    def test_one_partial_date_does_not_discard_its_whole_section(self):
        """The amplifier, and the reason this cost seventeen news items rather
        than one."""
        from fundos.profile.models import NewsItem

        with tenant_context(self.tenant.id):
            self._write("news", [
                {"title": "Exact", "date": "2024-08-14"},
                {"title": "Month only", "date": "2024-08"},
                {"title": "Undated", "date": "some time last year"},
            ])
            titles = set(NewsItem.objects.filter(profile=self.profile)
                         .values_list("headline", flat=True))

        self.assertEqual(titles, {"Exact", "Month only", "Undated"})

    def test_funding_rounds_record_what_was_actually_known(self):
        """`FundingRound.date_precision` has always specified this; nothing
        wrote to it, so every stored round asserted a day."""
        from fundos.profile.models import FundingRound

        with tenant_context(self.tenant.id):
            self._write("funding_history", [
                {"round": "Seed", "date": "2010-08"},
                {"round": "Series A", "date": "2012-03-19"},
            ])
            rounds = {r.round_name: r for r in
                      FundingRound.objects.filter(profile=self.profile)}

        self.assertEqual(rounds["Seed"].date_precision, "month")
        self.assertEqual(str(rounds["Seed"].announced_date), "2010-08-01")
        self.assertEqual(rounds["Series A"].date_precision, "day")

    def test_a_month_precision_round_reads_back_as_a_month(self):
        """Serialising the stored column would tell the client the round
        closed on the 1st — a day no source stated — and would break the
        read-edit-PATCH round trip, since writing "2010-08-01" back records
        day precision."""
        from fundos.profile.spec_serializer import build_sections

        with tenant_context(self.tenant.id):
            self._write("funding_history", [{"round": "Seed",
                                             "date": "2010-08"}])
            sections = build_sections(self.profile)

        rows = sections["funding_history"]["data"]
        row = next(r for r in rows if r.get("round") == "Seed")
        self.assertEqual(row["date"], "2010-08")


class SeededConfigProfileTests(TestCase):
    """The two pinned config profiles, as `seed_platform_config` leaves them.

    Both defects below were found by the first live run rather than by this
    suite, and both share a shape: a repair in the seeder selected rows by a
    field these two happen to carry — a tier name, `web_search=True` — and
    applied a default belonging to the rows it was written for. Neither
    raised. The pipeline pins by code, and a pin that cannot be honoured
    falls back to tier resolution silently.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command

        call_command("seed_platform_config", verbosity=0)

    def _profile(self, code):
        from fundos.llm.models import LLMConfigProfile

        row = LLMConfigProfile.objects.filter(code=code).first()
        self.assertIsNotNone(row, f"{code} was not seeded")
        return row

    def test_both_profiles_are_seeded_active_on_the_pipeline_model(self):
        for code in ("profile.research", "profile.synthesis"):
            row = self._profile(code)
            self.assertTrue(row.is_active, f"{code} was seeded inactive")
            self.assertEqual(row.model_string, "gemini-2.5-flash")

    def test_reseeding_leaves_them_active(self):
        """`_repair_tier` deactivates the other profiles on a tier when it
        repoints that tier's provider. These two carry a tier only to inherit
        its capability defaults, and were being swept up by that query — so a
        run then dispatched on the tier's model with nothing in the trace to
        say the pin had been dropped."""
        from django.core.management import call_command

        call_command("seed_platform_config", verbosity=0)
        for code in ("profile.research", "profile.synthesis"):
            self.assertTrue(self._profile(code).is_active,
                            f"{code} was deactivated by a second seed")

    def test_a_deactivated_profile_is_repaired(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile

        LLMConfigProfile.objects.filter(
            code__in=("profile.research", "profile.synthesis")).update(
                is_active=False)
        call_command("seed_platform_config", verbosity=0)
        for code in ("profile.research", "profile.synthesis"):
            self.assertTrue(self._profile(code).is_active,
                            f"{code} was left deactivated")

    def test_the_research_cap_covers_a_whole_batch(self):
        """Six searches is the deployment default, and it is right for a call
        that asks one question. A research batch asks ten. Seeded at six, all
        three calls of the first live run overran, the third tripped the
        breaker, and the remaining seven batches failed instantly against it —
        publishing six sections of seventeen and reporting success."""
        from fundos.llm.models import DEFAULT_SEARCH_MAX_USES
        from fundos.profile.pipeline.questions import SHIPPED_BATCHES

        per_batch = max(len(questions) for _, _, _, questions
                        in SHIPPED_BATCHES)
        cap = self._profile("profile.research").max_uses

        self.assertIsNotNone(cap, "a blank cap means the deployment default")
        self.assertGreater(cap, DEFAULT_SEARCH_MAX_USES)
        self.assertGreaterEqual(
            cap, per_batch,
            f"a cap of {cap} cannot cover {per_batch} questions in one call")

    def test_an_administrator_cap_survives_reseeding(self):
        """The correction above is one-time. Whatever number is there
        afterwards is the administrator's, including one that looks like a
        default."""
        from django.core.management import call_command
        from fundos.llm.models import DEFAULT_SEARCH_MAX_USES, LLMConfigProfile

        LLMConfigProfile.objects.filter(code="profile.research").update(
            max_uses=DEFAULT_SEARCH_MAX_USES)
        call_command("seed_platform_config", verbosity=0)

        self.assertEqual(self._profile("profile.research").max_uses,
                         DEFAULT_SEARCH_MAX_USES)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
class WorkerContextTests(TestCase):
    """ThreadPoolExecutor inherits none of Django's ambient state. Each of
    these is a real bug that would otherwise be silent."""

    def test_the_tenant_is_carried_into_worker_threads(self):
        """Without this every TenantManager query in a worker spans tenants."""
        from fundos.core.scoping import get_current_tenant
        from fundos.profile.pipeline.concurrency import run_concurrently

        world = make_world()
        tenant_id = str(world["a"]["tenant"].id)
        with tenant_context(tenant_id):
            seen = run_concurrently([lambda: get_current_tenant()] * 4,
                                    max_workers=4)
        self.assertEqual(seen, [tenant_id] * 4)

    def test_the_tenant_is_cleared_when_the_thread_is_released(self):
        """A pooled thread is reused. A stale tenant left on it would leak
        into whatever runs next — the one bug here that is a security issue
        rather than a correctness one.

        Asserted against the wrapper directly rather than through
        `run_concurrently`, because a pool is torn down at the end of each
        call: the reuse this guards against cannot be reproduced from
        outside, so testing it from outside would only appear to.
        """
        import contextvars

        from fundos.core.scoping import get_current_tenant
        from fundos.profile.pipeline.concurrency import in_worker_context

        world = make_world()
        tenant_id = str(world["a"]["tenant"].id)
        job = in_worker_context(
            lambda: get_current_tenant(), tenant_id=tenant_id, deal_id=None,
            context=contextvars.copy_context(), ledger_book=None)

        self.assertEqual(job(), tenant_id,
                         "the tenant must be visible DURING the job")
        self.assertIsNone(get_current_tenant(),
                          "the tenant must be cleared once the job returns, "
                          "or it leaks into the next job on this thread")

    def test_a_failing_job_does_not_cancel_its_siblings(self):
        """Without this, one bad batch throws away work already paid for."""
        from fundos.profile.pipeline.concurrency import run_concurrently

        def boom():
            raise RuntimeError("batch 3 failed")

        results = run_concurrently([lambda: "a", boom, lambda: "c"],
                                   max_workers=3)
        self.assertEqual(results[0], "a")
        self.assertIsInstance(results[1], RuntimeError)
        self.assertEqual(results[2], "c")

    def test_results_are_positional_not_completion_ordered(self):
        """The caller pairs results with the stage that produced them, so a
        reordering would attribute a failure to the wrong stage."""
        import time

        from fundos.profile.pipeline.concurrency import run_concurrently

        def slow():
            time.sleep(0.05)
            return "slow"

        results = run_concurrently([slow, lambda: "fast"], max_workers=2)
        self.assertEqual(results, ["slow", "fast"])

    def test_llm_calls_made_in_a_worker_are_counted_against_the_run(self):
        """The ledger is thread-local. Without the parent's book being adopted,
        a run that dispatched eleven calls would report one."""
        from fundos.llm import ledger
        from fundos.profile.pipeline.concurrency import run_concurrently

        ledger.reset()
        mark = ledger.mark()

        def record():
            ledger.record(role="profile_research_batch", status="mocked")

        run_concurrently([record] * 5, max_workers=5)
        self.assertEqual(len(ledger.since(mark)), 5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def override_document_model(*, enabled):
    """Context manager switching the document-model flag for one block."""
    from unittest import mock
    return mock.patch(
        "fundos.profile.pipeline.settings.document_model_enabled",
        return_value=enabled)
