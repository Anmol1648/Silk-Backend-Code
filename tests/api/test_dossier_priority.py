"""The company's own documents outrank the web — including when something
has to be cut.

One live run made this concrete. The dossier came to 786,531 characters, the
cap before synthesis is 600,000, and the cut takes the tail — so 174,000
characters were dropped from the END. Web research sat first and survived
whole; the company's 552,541-character financial model sat last and lost a
quarter of itself. Then `financial_summary` came back thin, with all 35 of
its citations naming a source the truncated dossier no longer contained.

Three fixes are asserted here, and none of them is about that company:

    Documents render FIRST, so the cut falls on the web research.
    A blank spreadsheet row never reaches the dossier at all.
    A format no provider can read is refused before the call, not after.
"""
from django.test import TestCase


class DocumentsComeFirst(TestCase):

    def _dossier(self):
        from fundos.profile.pipeline.dossier import DossierWriter

        writer = DossierWriter.__new__(DossierWriter)
        writer.company_name = "Acme"
        writer.website = "acme.example"
        writer.founders = []
        writer._sections = {
            "source1": "# Source 1: Web Research\n\nSearch said this.",
            "source2": "# Source 2: Company Documents\n\n### Model.xlsx\n\n"
                       "The company said this.",
        }
        return writer._render()

    def test_the_documents_block_precedes_the_web_block(self):
        text = self._dossier()
        self.assertLess(text.index("Company Documents"),
                        text.index("Web Research"))

    def test_the_header_says_why(self):
        """The model reads this too — the ordering is an instruction."""
        self.assertIn("placed ahead of web research", self._dossier())

    def test_a_tail_cut_now_takes_the_web_research(self):
        """The whole point: what survives is the company's own material."""
        from fundos.profile.pipeline.synthesize import _truncate

        text = self._dossier()
        head, was_cut = _truncate(text, text.index("Web Research"))
        self.assertTrue(was_cut)
        self.assertIn("The company said this.", head)
        self.assertNotIn("Search said this.", head)

    def test_the_source_labels_still_resolve(self):
        """`source_labels` keys on "# Source 2" to find the document block.
        Reordering the render must not disturb it, or every citation naming
        an uploaded file would be dropped as unknown."""
        from fundos.profile.pipeline.dossier import source_labels

        labels, count = source_labels(self._dossier())
        self.assertEqual(count, 1)
        self.assertEqual(labels[0], "Model.xlsx")

    def test_the_numbering_is_left_alone(self):
        """The labels are identifiers, not positions. Renumbering them would
        break the parser, the citation vocabulary and every citation already
        stored on an existing profile."""
        from fundos.profile.pipeline.dossier import SECTION_ORDER

        titles = dict(SECTION_ORDER)
        self.assertIn("Source 1", titles["source1"])
        self.assertIn("Source 2", titles["source2"])
        self.assertEqual([key for key, _ in SECTION_ORDER],
                         ["source2", "source1"])


class AnEmptySpreadsheetRowIsNotContent(TestCase):

    def _trim(self, text):
        from fundos.profile.pipeline.source2_documents import _drop_empty_rows

        return _drop_empty_rows(text)

    def test_blank_rows_are_dropped(self):
        out = self._trim("| FY24 | 56.9 |\n|  |  |\n|  |  |\n| FY25 | 4.0 |")
        self.assertIn("FY24", out)
        self.assertIn("FY25", out)
        self.assertEqual(out.count("|  |  |"), 0)

    def test_a_row_with_one_figure_survives_whole(self):
        """Nine blanks and one number is a fact about the company."""
        row = "| FY24 |  |  |  |  |  | 56.9 |  |  |"
        self.assertIn(row, self._trim(row))

    def test_the_header_separator_survives(self):
        """Without it the table stops rendering as a table."""
        table = "| FY | Revenue |\n| --- | --- |\n| FY24 | 56.9 |"
        self.assertIn("| --- | --- |", self._trim(table))

    def test_prose_is_untouched(self):
        text = "The company reported strong growth.\n\nNo tables here."
        self.assertEqual(self._trim(text), text)

    def test_text_with_no_table_is_returned_unchanged(self):
        self.assertEqual(self._trim("plain"), "plain")

    def test_it_shrinks_a_sparse_sheet_substantially(self):
        sheet = "\n".join(["| A | B | C |", "| --- | --- | --- |"]
                          + ["|  |  |  |"] * 500
                          + ["| FY24 | 56.9 | note |"])
        out = self._trim(sheet)
        self.assertLess(len(out), len(sheet) / 10)
        self.assertIn("56.9", out)


class AFormatNoProviderReadsIsRefusedNotSent(TestCase):

    def test_a_deck_is_named_with_the_reason(self):
        from fundos.profile.pipeline import document_ai

        reason = document_ai.why_not_readable(".pptx")
        self.assertTrue(reason)
        self.assertIn("PDF", reason)

    def test_a_readable_format_has_no_reason(self):
        from fundos.profile.pipeline import document_ai

        for suffix in (".pdf", ".png", ".jpg"):
            self.assertEqual(document_ai.why_not_readable(suffix), "", suffix)

    def test_the_unreadable_format_is_no_longer_offered_to_a_provider(self):
        """It was in the MIME table, so every deck produced a 400 and a
        CRITICAL log line pointing at the model binding — which was never the
        problem."""
        from fundos.profile.pipeline import document_ai

        self.assertNotIn(".pptx", document_ai.MIME_TYPES)
        self.assertFalse(document_ai.is_supported(".pptx"))

    def test_the_formats_a_model_does_read_are_unchanged(self):
        from fundos.profile.pipeline import document_ai

        for suffix in (".pdf", ".png", ".jpeg", ".webp", ".tiff"):
            self.assertTrue(document_ai.is_supported(suffix), suffix)

    def test_the_deck_still_gets_its_slide_text(self):
        """Refusing the model read must not turn off native extraction —
        that is where the slide text and speaker notes come from. The
        handler now says 'native only' because that is what happens; it
        said 'native+model' while the model read failed on every run."""
        from fundos.profile.pipeline.source2_documents import policy_for

        policy = policy_for("deck.pptx")
        self.assertEqual(policy.handler, "native only")
        self.assertIn("slide text", policy.description)

    def test_an_unknown_suffix_is_not_claimed_unreadable(self):
        from fundos.profile.pipeline import document_ai

        self.assertEqual(document_ai.why_not_readable(""), "")
        self.assertEqual(document_ai.why_not_readable(None), "")
