"""Every citation a reader sees is clean text, on both panels.

Spreadsheet rows read as "Revenue — FY24: 56.9", not `| Revenue | 56.9 | NaN |`.
Passages are whole sentences. Markdown is gone. A quote that could not be
found in its source says so. The profile and assessment panels share one
cleaner, so the same evidence reads the same on both.
"""
from django.test import SimpleTestCase

from fundos.core.services.citation_text import clean
from fundos.profile import snippets


DOSSIER = """# Source 2: Company Documents

### Model.xlsx

## Sheet: P&L
| Metric | FY23 | FY24 | Unnamed: 3 |
| --- | --- | --- | --- |
| Revenue | 40.1 | 56.9 | NaN |
| PAT | NaN | 3.1 | |

### Deck.pdf

<!-- Page 4 -->
**Zyla** is a chronic-care company. It serves 1,20,000 patients across India. See [our site](https://zyla.in) for _details_. Its payer network covers 14 insurers. Growth was 40% in FY24.
"""


class SpreadsheetRowsRead(SimpleTestCase):

    def _snippet(self, quote):
        return snippets.locate({"source": "Model.xlsx", "quote": quote},
                               DOSSIER)

    def test_a_row_pairs_each_figure_with_its_column(self):
        text, verified = self._snippet("| Revenue | 40.1 | 56.9 |")
        self.assertTrue(verified)
        self.assertIn("Revenue — FY23: 40.1 · FY24: 56.9", text)

    def test_no_pipes_nan_or_invented_headers(self):
        text, _ = self._snippet("56.9")
        for noise in ("|", "NaN", "Unnamed", "---"):
            self.assertNotIn(noise, text)

    def test_an_empty_cell_is_skipped_not_mislabelled(self):
        text, _ = self._snippet("56.9")
        self.assertIn("PAT — FY24: 3.1", text)

    def test_a_flattened_row_keeps_its_values_in_order(self):
        self.assertEqual(clean("| FY24 | 56.9 |  | NaN |"), "FY24 · 56.9")


class PassagesAreWholeSentences(SimpleTestCase):

    def test_a_narrow_window_starts_and_ends_on_a_sentence(self):
        out = clean(snippets.expand("1,20,000 patients", DOSSIER, width=90))
        body = out.strip("… ")
        self.assertTrue(body.startswith("It serves"), body)
        self.assertTrue(body.endswith("."), body)

    def test_a_cut_passage_is_marked(self):
        out = clean(snippets.expand("1,20,000 patients", DOSSIER, width=90))
        self.assertTrue(out.startswith("…") and out.endswith("…"), out)

    def test_a_wrapped_sentence_is_rejoined_with_a_space(self):
        self.assertEqual(clean("led operations at a\nnational chain."),
                         "led operations at a national chain.")


class MarkupIsGone(SimpleTestCase):

    def test_emphasis_links_and_comments(self):
        out = clean("<!-- Slide number: 3 -->\n# Team\n**Zyla** — "
                    "[LinkedIn](https://x.com/a) _unverified_ `code`")
        self.assertEqual(out, "Team; Zyla — LinkedIn unverified code")

    def test_images_keep_meaningful_alt_only(self):
        self.assertEqual(clean("![](GoogleShape295p51.jpg) [image: Team photo]"),
                         "Image: Team photo")

    def test_underscores_inside_a_filename_survive(self):
        self.assertEqual(clean("Project_Orah_Model.xlsx"),
                         "Project_Orah_Model.xlsx")

    def test_bullets_quotes_and_rules(self):
        self.assertEqual(clean("> Raising\n---\n- Series A"),
                         "Raising; Series A")

    def test_words_and_figures_are_unchanged(self):
        self.assertEqual(clean("Revenue grew 40% to ₹56.9 Cr in FY24."),
                         "Revenue grew 40% to ₹56.9 Cr in FY24.")

    def test_it_never_raises(self):
        for bad in (None, "", 3, "   ", "|", "**", "…"):
            self.assertIsInstance(clean(bad), str)

    def test_it_is_bounded(self):
        out = clean("word " * 1000)
        self.assertLessEqual(len(out), 902)
        self.assertTrue(out.endswith("…"))


class AnUnfoundQuoteSaysSo(SimpleTestCase):

    def test_found_is_verified(self):
        _, verified = snippets.locate(
            {"source": "Deck.pdf", "quote": "1,20,000 patients",
             "locator": "Page 4"}, DOSSIER)
        self.assertTrue(verified)

    def test_not_found_is_the_cleaned_quote_unverified(self):
        self.assertEqual(snippets.locate(
            {"source": "Deck.pdf", "quote": "**Not** there"}, DOSSIER),
            ("Not there", False))

    def test_no_dossier_is_unverified(self):
        self.assertEqual(snippets.locate(
            {"source": "Deck.pdf", "quote": "Zyla"}, ""), ("Zyla", False))

    def test_no_quote_is_empty_unverified(self):
        self.assertEqual(snippets.locate({"source": "Deck.pdf"}, DOSSIER),
                         ("", False))


class AssessmentQuotesUseTheSameCleaner(SimpleTestCase):

    def test_quotes_are_cleaned_and_dedupe_on_the_clean_text(self):
        from fundos.assessment.v2_serializers import (
            _deduplicate_and_sort_citations)

        out = _deduplicate_and_sort_citations([
            {"source_type": "document", "source": "Model.xlsx", "locator": "",
             "quote": "| Revenue | 56.9 | NaN |", "url": ""},
            {"source_type": "document", "source": "Model.xlsx", "locator": "",
             "quote": "Revenue · 56.9", "url": ""},
        ])
        self.assertEqual([c["quote"] for c in out], ["Revenue · 56.9"])
