"""Every citation a reader sees is a short, clean quote, on both panels.

The model's own quote, with markup removed: no pipes, no NaN, no image paths,
no markdown. Not a passage pulled from the document around it — that was
tried and read as a wall of slide text. The profile and assessment panels
share one cleaner, so the same evidence reads the same on both.
"""
from django.test import SimpleTestCase

from fundos.core.services.citation_text import MAX_CHARS, clean


class ImagesAreNeverPartOfAQuote(SimpleTestCase):

    def test_the_live_slide_20_image_paths_are_gone(self):
        out = clean("17 years experience. B.Tech, IIT Bombay. Image: "
                    "/home/claude/work/team_assets/team_linkedin_purple.png; "
                    "Image: /home/claude/work/team_assets/team_linkedin_purple.png")
        self.assertEqual(out, "17 years experience. B.Tech, IIT Bombay.")

    def test_markdown_images_and_image_tags(self):
        self.assertEqual(
            clean("Team ![](GoogleShape295p51.jpg) [image: Team photo] here"),
            "Team here")


class TablesRead(SimpleTestCase):

    def test_a_flattened_row_keeps_its_values_in_order(self):
        self.assertEqual(clean("| FY24 | 56.9 |  | NaN |"), "FY24 · 56.9")

    def test_a_table_pairs_figures_with_their_columns(self):
        out = clean("| Metric | FY23 | FY24 |\n| --- | --- | --- |\n"
                    "| Revenue | 40.1 | 56.9 |")
        self.assertEqual(out, "Revenue — FY23: 40.1 · FY24: 56.9")


class MarkupIsGone(SimpleTestCase):

    def test_emphasis_links_and_comments(self):
        out = clean("<!-- Slide number: 3 -->\n# Team\n**Zyla** — "
                    "[LinkedIn](https://x.com/a) _unverified_ `code`")
        self.assertEqual(out, "Team; Zyla — LinkedIn unverified code")

    def test_underscores_inside_a_filename_survive(self):
        self.assertEqual(clean("Project_Orah_Model.xlsx"),
                         "Project_Orah_Model.xlsx")

    def test_a_wrapped_sentence_is_rejoined_with_a_space(self):
        self.assertEqual(clean("led operations at a\nnational chain."),
                         "led operations at a national chain.")

    def test_words_and_figures_are_unchanged(self):
        self.assertEqual(clean("Revenue grew 40% to ₹56.9 Cr in FY24."),
                         "Revenue grew 40% to ₹56.9 Cr in FY24.")

    def test_it_never_raises(self):
        for bad in (None, "", 3, "   ", "|", "**", "…"):
            self.assertIsInstance(clean(bad), str)


class AQuoteIsShort(SimpleTestCase):

    def test_it_is_bounded(self):
        out = clean("word " * 1000)
        self.assertLessEqual(len(out), MAX_CHARS + 2)
        self.assertTrue(out.endswith("…"))

    def test_the_limit_is_small(self):
        self.assertLessEqual(MAX_CHARS, 300)


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
