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

from fundos.assessment.v2_serializers import (_CONTAINER_RE, _FILENAME_RE,
                                              _locator_in,
                                              _sanitize_source_title)
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
