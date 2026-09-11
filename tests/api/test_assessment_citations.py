"""An assessment citation names a document or it names web research.

The panel showed `Company Research` with a globe icon over a locator reading
`slide 20`. Web research has no slides, and no file called "Company Research"
exists to open.

What was stored is a document CATEGORY:

    "Company Presentation, slide 20"

`Company Presentation` was not in the sanitizer's list of recognised channels,
so it read as a bare topic and had a channel prefixed onto it. The channel was
picked by a ternary on `source_type` —

    "Web Research"  if "web" in source_type or "research" in source_type
    "Company Deck"  if "deck" in source_type
    "Company Research"                                    <- otherwise

— and `source_type` only ever holds "document" or "profile", so the first two
branches were unreachable and every citation in the system got the third. The
badge was then decided from that manufactured string: "research" matched, and
a deck's slide 20 was served as web.

A category is also not a document: a reader cannot open "Company Presentation",
and two uploads in it are indistinguishable. It IS an exact ProfileDocument
label though, so resolving it to the company's file is a lookup rather than a
guess.
"""
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status

from fundos.assessment.v2_serializers import (_CONTAINER_RE, _FILENAME_RE,
                                              _locator_in,
                                              _sanitize_source_title)
from tests.api.test_fundraising_phase1 import Phase1Base
from tests.conftest_helpers import make_world


class ADocumentCategoryIsNotATopic(TestCase):
    """The root of the reported defect."""

    def _title(self, raw):
        return _sanitize_source_title(raw, default_channel="Web Research")

    def test_a_category_keeps_its_own_name(self):
        self.assertEqual(self._title("Company Presentation"),
                         "Company Presentation")

    def test_every_upload_category_is_recognised(self):
        for category in ("Company Presentation", "Financial Model",
                         "Annual Report", "Teaser",
                         "Information Memorandum"):
            self.assertEqual(self._title(category), category,
                             f"{category!r} had a channel prefixed onto it")

    def test_a_research_topic_still_gets_its_channel(self):
        """The prefix is right for a topic and wrong for a category; removing
        it outright stripped web research of its own name."""
        self.assertEqual(self._title("Founders & Leadership"),
                         "Web Research — Founders & Leadership")

    def test_a_filename_never_gets_a_channel(self):
        self.assertEqual(self._title("Project Orah Teaser_vff.pptx"),
                         "Project Orah Teaser_vff.pptx")


class ALocatorIsSaidTheWayItsFileTypeSaysIt(TestCase):

    def test_a_deck_has_slides(self):
        self.assertEqual(_locator_in("deck.pptx", "slide 20"), "Slide 20")

    def test_a_pdf_has_pages(self):
        self.assertEqual(_locator_in("report.pdf", "page 12"), "Page 12")

    def test_a_pdf_cited_as_a_slide_is_corrected(self):
        """Saying "slide 20" about a PDF is the small wrongness that tells a
        reader nobody checked."""
        self.assertEqual(_locator_in("report.pdf", "slide 20"), "Page 20")

    def test_a_sheet_reference_is_left_intact(self):
        self.assertEqual(_locator_in("model.xlsx", "Sheet: P&L, Cell: G18"),
                         "Sheet: P&L, Cell: G18")

    def test_an_unknown_type_keeps_what_it_was_given(self):
        self.assertEqual(_locator_in("notes.txt", "page 3"), "page 3")

    def test_no_locator_stays_empty(self):
        self.assertEqual(_locator_in("deck.pptx", ""), "")


class TheMergedDossierIsNotASource(TestCase):
    """It is the file we BUILT from the deck and the web research. Citing it
    is citing the library rather than the book, and 44 stored rows do."""

    def test_the_container_is_recognised(self):
        for text in ("Consolidated research dossier",
                     "consolidated research dossier: Amazon, Batch 3",
                     "document_extracts"):
            self.assertTrue(_CONTAINER_RE.search(text), text)

    def test_a_real_source_is_not_mistaken_for_it(self):
        for text in ("Company Presentation", "Project Orah Teaser_vff.pptx",
                     "Web Research — Founders & Leadership"):
            self.assertFalse(_CONTAINER_RE.search(text), text)


class AFilenameIsRecognisedByItsExtension(TestCase):

    def test_real_uploads_are_filenames(self):
        for name in ("Project Orah Teaser_vff.pptx",
                     "Project Orah_Financial Model_vf.xlsx",
                     "TyrePlex Investor Deck Detailed June26 OS.pdf"):
            self.assertTrue(_FILENAME_RE.search(name), name)

    def test_a_category_or_topic_is_not(self):
        for text in ("Company Presentation", "Founders & Leadership",
                     "Consolidated research dossier"):
            self.assertFalse(_FILENAME_RE.search(text), text)


class ACategoryResolvesToTheCompanysOwnFile(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile

        self.world = make_world()
        self.company = self.world["a"]["company"]
        self.profile = get_or_create_profile(
            self.company, user=self.world["a"]["founder"])
        self.Document = ProfileDocument

    def _add(self, category, filename):
        self.Document.objects.create(
            profile=self.profile, tenant_id=self.company.tenant_id,
            category=category, filename=filename)

    def _resolve(self, category):
        from fundos.assessment.v2_serializers import (
            _document_category_filename)

        class _Assessment:
            deal = type("D", (), {"company_id": self.company.id})()

        return _document_category_filename(_Assessment(), category)

    def test_one_upload_in_the_category_resolves_to_its_filename(self):
        self._add("company_presentation", "Project Orah Teaser_vff.pptx")
        self.assertEqual(self._resolve("Company Presentation"),
                         "Project Orah Teaser_vff.pptx")

    def test_two_uploads_in_the_category_resolve_to_neither(self):
        """Naming the wrong upload reads as precision and sends a reader to
        the wrong file."""
        self._add("company_presentation", "Deck A.pptx")
        self._add("company_presentation", "Deck B.pptx")
        self.assertEqual(self._resolve("Company Presentation"), "")

    def test_the_same_file_listed_twice_still_resolves(self):
        """A duplicate row is not an ambiguity."""
        self._add("company_presentation", "Project Orah Teaser_vff.pptx")
        self._add("company_presentation", "Project Orah Teaser_vff.pptx")
        self.assertEqual(self._resolve("Company Presentation"),
                         "Project Orah Teaser_vff.pptx")

    def test_a_category_the_company_has_nothing_in_resolves_to_nothing(self):
        self._add("company_presentation", "Deck.pptx")
        self.assertEqual(self._resolve("Financial Model"), "")

    def test_no_category_resolves_to_nothing(self):
        self.assertEqual(self._resolve(""), "")

    def test_a_company_with_no_documents_resolves_to_nothing(self):
        self.assertEqual(self._resolve("Company Presentation"), "")


class TheInventedNamesAreGone(TestCase):
    """The whole point: no label a reader cannot act on."""

    def test_the_source_file_no_longer_manufactures_them(self):
        import inspect

        from fundos.assessment import v2_serializers

        source = inspect.getsource(v2_serializers)
        # Mentioned in comments explaining the defect, never assigned.
        for invented in ('= "Company Research"', '"Company Research" if',
                         'else "Company Research"',
                         'else "Company Document"'):
            self.assertNotIn(invented, source,
                             f"{invented} is still produced")


class TheExtractionIsToldWhatTheSourcesAreCalled(TestCase):
    """The write side of the same defect.

    44 stored rows cite "Consolidated research dossier" — the merged file we
    BUILT from the deck and the research, which is the library rather than the
    book. The code comment beside that fallback says why it exists and names
    its own cause:

        "The model had read the evidence; it just had no filename to quote,
         because what it was given is a merged corpus rather than a set of
         files."

    The dossier IS assembled from headings — `### <filename>` per upload,
    `## Batch N: <topic>` per research batch — and every line sits under one.
    That vocabulary existed; the prompt never carried it.
    """

    DOSSIER = """# Consolidated research dossier: Zyla

## Batch 2: Founders & Leadership

# Source 2: Company Documents

### Project Orah Teaser_vff.pptx

### Project Orah_Financial Model_vf.xlsx
"""

    def _labels(self, dossier=None):
        from fundos.profile.assessment_extraction import _citable_sources
        return _citable_sources(
            {"dossier": self.DOSSIER if dossier is None else dossier})

    def test_the_uploaded_files_are_offered_first(self):
        self.assertEqual(self._labels()[:2],
                         ["Project Orah Teaser_vff.pptx",
                          "Project Orah_Financial Model_vf.xlsx"])

    def test_the_research_batches_are_offered_too(self):
        self.assertIn("Batch 2: Founders & Leadership", self._labels())

    def test_no_dossier_offers_nothing_rather_than_failing(self):
        """With no list the prompt carries none and the existing fallback
        still applies — this adds a vocabulary, it does not remove a net."""
        self.assertEqual(self._labels(""), [])
        from fundos.profile.assessment_extraction import _citable_sources
        self.assertEqual(_citable_sources({}), [])
        self.assertEqual(_citable_sources(None), [])

    def test_the_prompt_points_at_the_list(self):
        from fundos.profile.assessment_extraction import EXTRACTION_SYSTEM

        self.assertIn("citable_sources", EXTRACTION_SYSTEM)
        self.assertIn("copy one of those strings EXACTLY", EXTRACTION_SYSTEM)

    def test_the_prompt_says_documents_are_preferred(self):
        from fundos.profile.assessment_extraction import EXTRACTION_SYSTEM

        self.assertIn("PREFER AN UPLOADED DOCUMENT", EXTRACTION_SYSTEM)

    def test_the_prompt_explains_why_the_dossier_is_not_a_source(self):
        from fundos.profile.assessment_extraction import EXTRACTION_SYSTEM

        self.assertIn("names the container", EXTRACTION_SYSTEM)

    def test_the_fallback_is_still_there(self):
        """Deliberately unchanged. Removing it once cost 22 parameters —
        every qualitative anchor — and it is only safe to remove after a run
        shows the model naming real sources."""
        from fundos.profile.assessment_extraction import (DOSSIER_SOURCE,
                                                          DOSSIER_TIER)

        self.assertEqual(DOSSIER_SOURCE, "Consolidated research dossier")
        self.assertEqual(DOSSIER_TIER, 3)


class TheContainerIsCaughtInEveryFormItTakes(TestCase):
    """A live Zyla run cited "Dossier - point 3" on every parameter.

    It is the same merged corpus with an index on it, and it was served as
    source_type "document" at tier 1 -- the strongest evidence label in the
    system, on the weakest possible source. The pattern matched only the full
    phrase "Consolidated research dossier", so the short form went straight
    through wearing it.
    """

    def test_the_indexed_form_is_caught(self):
        for text in ("Dossier — point 3", "Dossier — point 8", "dossier"):
            self.assertTrue(_CONTAINER_RE.search(text), text)

    def test_the_long_form_is_still_caught(self):
        self.assertTrue(_CONTAINER_RE.search("Consolidated research dossier"))

    def test_a_company_named_dossier_keeps_its_own_file(self):
        """The pattern matches a leading "dossier"; a real upload from a
        company called Dossier Analytics must not be discarded as the merged
        corpus. Asking "is this a file?" first makes that impossible."""
        from fundos.assessment.v2_serializers import _FILENAME_RE

        name = "Dossier Analytics Ltd.pdf"
        self.assertTrue(_FILENAME_RE.search(name),
                        "the filename test must win over the container test")

    def test_a_real_source_is_untouched(self):
        for text in ("Company Presentation", "Project Orah Teaser_vff.pptx",
                     "Web Research — Founders & Leadership"):
            self.assertFalse(_CONTAINER_RE.search(text), text)


class TheReferenceIsLookedUpByInputKey(TestCase):
    """`build_v2_parameter_detail` passed the spec ref -- "A.1.d" -- to
    tables keyed by input key. No ref is ever a key there, so every lookup
    missed and that endpoint returned `"rubric": null, "anchor": null` for
    every parameter in every assessment, while the tree endpoint looked the
    same tables up by key and returned them fine."""

    def _reference(self):
        from fundos.assessment import v2_serializers

        if v2_serializers.v2_ref is None:
            self.skipTest("the V2 reference package is not on this machine")
        return v2_serializers.v2_ref

    def test_an_input_key_finds_its_rubric(self):
        reference = self._reference()
        self.assertTrue(reference.rubric_for("TEAM_FDR_EXP"))

    def test_an_input_key_finds_its_anchor(self):
        reference = self._reference()
        self.assertTrue(reference.anchor_for("ANC_FDR_EDU"))

    def test_a_spec_ref_finds_neither(self):
        """The key that was being passed. Pinned so the two builders cannot
        drift apart again."""
        reference = self._reference()
        self.assertFalse(reference.rubric_for("A.1.d"))
        self.assertFalse(reference.anchor_for("A.1.a"))

    def test_the_detail_builder_uses_the_input_key(self):
        import inspect

        from fundos.assessment import v2_serializers

        source = inspect.getsource(v2_serializers.build_v2_parameter_detail)
        self.assertIn('rubric_key = getattr(cfg, "input_key", "") or ref',
                      source)


class TheParameterDetailEndpointEndToEnd(Phase1Base):
    """Through the real endpoint, not the helpers.

    Every other test here exercises a function. These go over HTTP, because
    the two defects this file is about were BOTH invisible at the unit level:
    the rubric lookup was correct in one builder and wrong in another, and
    the container citation only surfaced as tier 1 once it had been through
    the serializer.
    """

    KEY = "TEAM_FDR_EXP"

    def _detail(self, key=None):
        url = (f"/api/v1/companies/{self.company.id}/assessment/parameters/"
               f"{key or self.KEY}")
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def _store(self, detail, justification="Fifteen years in the sector."):
        from fundos.assessment.models import ParameterValue

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key=self.KEY,
            defaults={"source_detail": detail,
                      "justification": justification,
                      "source_type": "document"})

    def _reference_loaded(self):
        from fundos.assessment import v2_serializers
        return v2_serializers.v2_ref is not None

    # -- the rubric lookup ------------------------------------------------
    def test_a_numeric_parameter_carries_its_rubric(self):
        """It returned null for every parameter in every assessment, because
        the spec ref was passed to a table keyed by input key."""
        if not self._reference_loaded():
            self.skipTest("the V2 reference package is not on this machine")
        body = self._detail()
        self.assertIsNotNone(body.get("rubric"),
                             "the rubric lookup is missing again")

    def test_an_anchor_parameter_carries_its_bands(self):
        if not self._reference_loaded():
            self.skipTest("the V2 reference package is not on this machine")
        body = self._detail("ANC_FDR_EDU")
        self.assertIsNotNone(body.get("anchor"),
                             "the anchor lookup is missing again")

    def test_the_stage_is_reported(self):
        self.assertIn("stage", self._detail())

    def test_the_parameter_block_is_always_present(self):
        self.assertIn("parameter", self._detail())

    # -- citations --------------------------------------------------------
    def test_the_merged_dossier_is_not_served_as_a_source(self):
        """A live run cited "Dossier - point 3" on every parameter, served as
        a tier 1 document."""
        self._store("Dossier — point 3")
        citations = self._detail()["parameter"]["evidence"]["citations"]
        for citation in citations:
            self.assertNotIn("dossier", (citation["source"] or "").lower(),
                             "the container reached the reader")

    def test_the_container_never_claims_to_be_a_verified_document(self):
        self._store("Consolidated research dossier")
        for citation in self._detail()["parameter"]["evidence"]["citations"]:
            self.assertFalse(
                citation["source_type"] == "document"
                and citation.get("source_tier") == 1
                and "dossier" in (citation["source"] or "").lower())

    def test_a_named_file_survives_with_its_locator(self):
        self._store("Project Orah_Financial Model_vf.xlsx, Summary!B4")
        citation = self._detail()["parameter"]["evidence"]["citations"][0]
        self.assertEqual(citation["source"],
                         "Project Orah_Financial Model_vf.xlsx")
        self.assertIn("Summary", citation["locator"])
        self.assertEqual(citation["source_type"], "document")

    def test_a_research_batch_is_served_as_web(self):
        self._store("Source 1: Batch 2: Founders & Leadership")
        citation = self._detail()["parameter"]["evidence"]["citations"][0]
        self.assertEqual(citation["source_type"], "web")
        self.assertIn("Founders & Leadership", citation["source"])

    def test_no_citation_is_named_company_research(self):
        """The reported defect, over the wire."""
        for detail in ("Company Presentation, slide 20",
                       "Source 1: Batch 2: Founders & Leadership",
                       "Dossier — point 3"):
            self._store(detail)
            for citation in self._detail()["parameter"]["evidence"][
                    "citations"]:
                self.assertNotIn(citation["source"],
                                 ("Company Research", "Company Deck",
                                  "Company Document"), detail)

    def test_a_slide_locator_is_never_served_as_web(self):
        """A globe over slide 20 is impossible."""
        self._store("Company Presentation, slide 20")
        for citation in self._detail()["parameter"]["evidence"]["citations"]:
            if "slide" in (citation.get("locator") or "").lower():
                self.assertEqual(citation["source_type"], "document")

    def test_no_citation_carries_a_link(self):
        self._store("Project Orah Teaser_vff.pptx, slide 4")
        for citation in self._detail()["parameter"]["evidence"]["citations"]:
            self.assertFalse(citation.get("url"))

    def test_an_unparameterised_key_is_not_a_crash(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment/parameters/"
            f"NO_SUCH_KEY", **self.headers)
        self.assertIn(response.status_code,
                      (status.HTTP_200_OK, status.HTTP_404_NOT_FOUND))


class TheRubricComesFromTheDatabaseNotADirectory(TestCase):
    """`v2_ref` is imported from "Fundraising Strategy V2.0" -- a reference
    folder outside src/ that is not deployed, and was never meant to be. The
    import fails on the server, the failure is swallowed, and every rubric
    and anchor comes back null across the whole product.

    Live proof: /api/v1/assessment/reference answers 503 "V2 reference layer
    unavailable", and 0 of 55 terminals on a real assessment carry either.

    The same data is already in ConfigRubric and ConfigAnchor, imported from
    the workbook -- which is how the engine bands these values at all. A
    parameter cannot score "Excellent" without its thresholds, and prod's
    scorecard is full of bands.
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        from fundos.assessment import v2_serializers

        # Reproduce the server exactly: reference package unavailable.
        self._saved = v2_serializers.v2_ref
        v2_serializers.v2_ref = None
        self.v2 = v2_serializers

    def tearDown(self):
        self.v2.v2_ref = self._saved

    def _config_loaded(self):
        """Seed the two rows these tests need.

        Seeding does not load the workbook, so relying on it skipped seven of
        these -- which is the same as not having written them. These are the
        real shapes, taken from the imported table.
        """
        from decimal import Decimal

        from fundos.assessment.models import ConfigAnchor, ConfigRubric

        for stage, cuts in (("Series A", (10, 6, 3)), ("Seed", (10, 6, 3)),
                            ("Series B", (12, 8, 4)), ("Growth", (15, 10, 5))):
            ConfigRubric.objects.get_or_create(
                input_key="TEAM_FDR_EXP", stage=stage, tenant_id=None,
                defaults={
                    "ref_code": "A.1.d", "is_active": True,
                    "metric_name": "Founder Years of Experience in the "
                                   "Industry",
                    "unit": "Years", "direction": "Higher",
                    "cut_excellent": Decimal(cuts[0]),
                    "cut_good": Decimal(cuts[1]),
                    "cut_fair": Decimal(cuts[2]),
                    "rationale": "Deep domain time predicts judgement."})
        ConfigAnchor.objects.get_or_create(
            input_key="ANC_FDR_EDU", tenant_id=None,
            defaults={
                "ref_code": "A.1.a", "is_active": True, "version": 2,
                "parameter_name": "Founder Education in the Field",
                "scoring_basis": "Anchor-scored",
                "excellent_def": "Degree directly in the domain.",
                "good_def": "Top-tier institution, adjacent field.",
                "fair_def": "Unrelated field, no domain qualification.",
                "poor_def": "No relevant education.",
                "evidence_required": "Institution, degree, year."})

    def test_a_rubric_is_returned_without_the_reference_package(self):
        self._config_loaded()
        self.assertIsNotNone(self.v2._rubric_for("TEAM_FDR_EXP"))

    def test_an_anchor_is_returned_without_the_reference_package(self):
        self._config_loaded()
        self.assertIsNotNone(self.v2._anchor_for("ANC_FDR_EDU"))

    def test_the_rubric_carries_every_stage(self):
        self._config_loaded()
        stages = self.v2._rubric_for("TEAM_FDR_EXP")["stages"]
        self.assertIn("Series A", stages)
        self.assertGreaterEqual(len(stages), 2)

    def test_a_monotonic_rubric_carries_its_cut_points(self):
        self._config_loaded()
        rubric = self.v2._rubric_for("TEAM_FDR_EXP")
        self.assertEqual(rubric["kind"], "monotonic")
        self.assertIn("excellent", rubric["stages"]["Series A"])

    def test_the_rubric_names_its_metric_and_unit(self):
        self._config_loaded()
        rubric = self.v2._rubric_for("TEAM_FDR_EXP")
        self.assertTrue(rubric["metric"])
        self.assertEqual(rubric["unit"], "Years")
        self.assertEqual(rubric["parameter"], "A.1.d")

    def test_an_anchor_carries_all_four_written_bands(self):
        self._config_loaded()
        bands = self.v2._anchor_for("ANC_FDR_EDU")["bands"]
        for name in ("Excellent", "Good", "Fair", "Poor"):
            self.assertTrue(bands.get(name), f"{name} band is empty")

    def test_an_anchor_says_what_evidence_it_needs(self):
        self._config_loaded()
        self.assertTrue(
            self.v2._anchor_for("ANC_FDR_EDU")["evidenceRequired"])

    def test_a_key_with_no_config_returns_nothing_rather_than_failing(self):
        self.assertIsNone(self.v2._rubric_for("NO_SUCH_PARAMETER"))
        self.assertIsNone(self.v2._anchor_for("NO_SUCH_PARAMETER"))

    def test_no_key_returns_nothing(self):
        self.assertIsNone(self.v2._rubric_for(""))
        self.assertIsNone(self.v2._anchor_for(None))

    def test_a_spec_ref_is_still_not_a_key(self):
        """Pinned: the lookup is by input key, in the database too."""
        self.assertIsNone(self.v2._rubric_for("A.1.d"))


class TheNumericTraceComesFromTheDatabaseToo(TestCase):
    """`v2_engine` ships in the same undeployed reference folder as `v2_ref`.

    Anchor rows carry traces because those are built inline; only the NUMERIC
    branch reached outside for `band_for`. A live Zyla assessment showed
    exactly that split -- every anchor traced, `TEAM_FDR_EXP` empty.

    The cut-points are in ConfigRubric, which is where the engine that
    produced the band read them from, so the trace is rebuilt from the same
    numbers rather than approximated.
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        from fundos.assessment import v2_serializers

        # Reproduce the server: the engine is not importable.
        self._saved = v2_serializers.v2_engine
        v2_serializers.v2_engine = None
        self.v2 = v2_serializers
        self._seed()

    def tearDown(self):
        self.v2.v2_engine = self._saved

    def _seed(self):
        from decimal import Decimal

        from fundos.assessment.models import ConfigRubric

        ConfigRubric.objects.get_or_create(
            input_key="TEAM_FDR_EXP", stage="Series A", tenant_id=None,
            defaults={"ref_code": "A.1.d", "is_active": True,
                      "metric_name": "Founder Years of Experience",
                      "unit": "Years", "direction": "Higher",
                      "cut_excellent": Decimal(10), "cut_good": Decimal(6),
                      "cut_fair": Decimal(3),
                      "rationale": "Deep domain time predicts judgement."})
        ConfigRubric.objects.get_or_create(
            input_key="DD_RAISE_MULT", stage="Series A", tenant_id=None,
            defaults={"ref_code": "D.1", "is_active": True,
                      "metric_name": "Current Raise / Last Raise",
                      "unit": "x", "direction": "Range",
                      "ideal_min": Decimal("1.5"), "ideal_max": Decimal(3),
                      "good_tolerance_pct": Decimal(25),
                      "fair_tolerance_pct": Decimal(50),
                      "rationale": "A sensible step-up shows progress."})

    def _trace(self, key, value, unit=""):
        return self.v2._trace_from_config(key, value, "Series A", unit)

    def test_a_numeric_row_gets_a_trace_without_the_engine(self):
        self.assertIsNotNone(self._trace("TEAM_FDR_EXP", 15.0, "Years"))

    def test_the_trace_names_the_cut_point_that_was_cleared(self):
        trace = self._trace("TEAM_FDR_EXP", 15.0, "Years")
        self.assertEqual(trace["matched_cut_point"], 10.0)
        self.assertIn("Excellent", trace["explanation"])

    def test_a_value_below_every_cut_point_says_so(self):
        trace = self._trace("TEAM_FDR_EXP", 1.0, "Years")
        self.assertIsNone(trace["matched_cut_point"])
        self.assertIn("clears no cut-point", trace["explanation"])

    def test_a_range_parameter_traces_its_band_not_its_cuts(self):
        trace = self._trace("DD_RAISE_MULT", 1.5, "x")
        self.assertEqual(trace["method"], "range")
        self.assertEqual(trace["thresholds"],
                         {"ideal_min": 1.5, "ideal_max": 3.0})

    def test_the_tolerance_bands_are_widened_from_the_ideal(self):
        bands = self._trace("DD_RAISE_MULT", 1.5, "x")["inputs"]
        self.assertEqual(bands["good_band"], [1.125, 3.75])
        self.assertEqual(bands["fair_band"], [0.75, 4.5])

    def test_a_lower_is_better_parameter_reads_the_other_way(self):
        from decimal import Decimal

        from fundos.assessment.models import ConfigRubric

        ConfigRubric.objects.create(
            input_key="FIN_BURN", stage="Series A", tenant_id=None,
            ref_code="B.9", is_active=True, metric_name="Monthly Burn",
            unit="x", direction="Lower", cut_excellent=Decimal(2),
            cut_good=Decimal(4), cut_fair=Decimal(6))
        trace = self._trace("FIN_BURN", 1.0, "x")
        self.assertEqual(trace["operator"], "<=")
        self.assertIn("at most", trace["explanation"])

    def test_the_rationale_travels_with_the_trace(self):
        self.assertTrue(self._trace("TEAM_FDR_EXP", 15.0)["rationale"])

    def test_no_rubric_row_means_no_trace_rather_than_a_guess(self):
        self.assertIsNone(self._trace("NO_SUCH_PARAMETER", 5.0))

    def test_no_value_means_no_trace(self):
        self.assertIsNone(self._trace("TEAM_FDR_EXP", None))

    def test_another_stage_falls_back_rather_than_returning_nothing(self):
        """A rubric held for one stage still explains the number."""
        trace = self.v2._trace_from_config("TEAM_FDR_EXP", 15.0, "Growth")
        self.assertIsNotNone(trace)
