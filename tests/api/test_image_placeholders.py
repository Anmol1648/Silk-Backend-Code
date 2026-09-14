"""A picture the model cannot see is not something to send it.

A converted deck carried this into the dossier:

    … ![](GoogleShape295p51.jpg) ![](GoogleShape299p51.jpg)
    ![](GoogleShape300p51.jpg) SOC 2 27,001 ![](GoogleShape155p37.jpg) …

Every picture on every slide becomes a markdown image pointing at a file
that exists only inside the .pptx. The model reading the dossier cannot open
any of them, so each is tokens spent on nothing, wedged between the words
that matter.
"""
from django.test import TestCase

from fundos.profile.pipeline.source2_documents import (
    _drop_image_placeholders as clean,
)


class PlaceholdersAreRemoved(TestCase):

    def test_the_reported_line_comes_back_as_its_words(self):
        out = clean("… ![](GoogleShape295p51.jpg) ![](GoogleShape299p51.jpg) "
                    "![](GoogleShape300p51.jpg) SOC 2 27,001 "
                    "![](GoogleShape155p37.jpg) …")
        self.assertNotIn("GoogleShape", out)
        self.assertNotIn("![", out)
        self.assertIn("SOC 2 27,001", out)

    def test_a_filename_as_alt_text_is_not_a_description(self):
        out = clean("![image.png](Picture3.jpg)\nRevenue grew.")
        self.assertNotIn("image.png", out)
        self.assertIn("Revenue grew.", out)

    def test_generic_alt_words_are_dropped(self):
        for alt in ("image", "Picture 3", "shape12", "chart", "img"):
            self.assertNotIn(alt, clean(f"![{alt}](x.jpg) text"), alt)

    def test_no_run_of_spaces_is_left_behind(self):
        self.assertEqual(clean("A ![](a.jpg) ![](b.jpg) B"), "A B")

    def test_an_alt_text_that_is_a_file_path_goes(self):
        """A live deck carried `/home/claude/work/team_assets/...png` as the
        alt text of every LinkedIn icon on the team slide."""
        out = clean("B.Tech ![/home/claude/work/team_assets/team_linkedin_purple.png]"
                    "(Picture7.png) IIT Bombay")
        self.assertEqual(out, "B.Tech IIT Bombay")

    def test_no_stack_of_blank_lines_is_left_behind(self):
        out = clean("Top\n\n![](a.jpg)\n\n![](b.jpg)\n\n![](c.jpg)\n\nBottom")
        self.assertNotIn("\n\n\n", out)


class WhatTheModelCanUseIsKept(TestCase):

    def test_a_written_description_survives_as_text(self):
        """It is the only thing an image contributes to a text reading."""
        out = clean("![Revenue by region, FY24](chart1.png)")
        self.assertIn("[image: Revenue by region, FY24]", out)

    def test_the_slide_marker_survives(self):
        """It is how a citation says "Slide 20"."""
        out = clean("<!-- Slide number: 20 -->\n![](a.jpg)\nLeadership")
        self.assertIn("<!-- Slide number: 20 -->", out)

    def test_a_link_is_not_an_image(self):
        out = clean("See [the report](https://example.com/r.pdf).")
        self.assertIn("[the report](https://example.com/r.pdf)", out)

    def test_indentation_survives(self):
        out = clean("- item\n    - nested ![](a.jpg)")
        self.assertIn("    - nested", out)

    def test_text_with_no_images_is_unchanged(self):
        text = "Plain prose.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
        self.assertEqual(clean(text), text)

    def test_table_cells_survive(self):
        out = clean("| FY | Revenue |\n| --- | --- |\n| FY24 | 56.9 ![](a.jpg) |")
        self.assertIn("56.9", out)
        self.assertIn("| FY24 |", out)


from fundos.profile.pipeline.source2_documents import (  # noqa: E402
    _drop_slide_boilerplate as tidy,
)


def _deck(slides):
    """A converted deck: marker, lines, empty notes heading, per slide."""
    out = []
    for n, lines in enumerate(slides, start=1):
        out.append(f"<!-- Slide number: {n} -->")
        out.extend(lines)
        out.append("")
        out.append("### Notes:")
        out.append("")
    return "\n".join(out)


FOOTER = ["zyla", "Proprietary and confidential"]


class SlideBoilerplateIsRemoved(TestCase):

    def test_an_empty_notes_heading_goes(self):
        self.assertNotIn("### Notes:", tidy(_deck([["Hello"], ["World"]])))

    def test_a_notes_heading_with_notes_stays(self):
        text = ("<!-- Slide number: 1 -->\nHello\n\n### Notes:\n"
                "Presenter: stress the figure.\n")
        out = tidy(text)
        self.assertIn("### Notes:", out)
        self.assertIn("stress the figure", out)

    def test_a_repeated_footer_is_kept_once(self):
        out = tidy(_deck([["Slide A"] + FOOTER, ["Slide B"] + FOOTER,
                          ["Slide C"] + FOOTER, ["Slide D"] + FOOTER]))
        self.assertEqual(out.count("Proprietary and confidential"), 1)
        self.assertEqual(out.count("zyla"), 1)

    def test_every_slide_marker_survives(self):
        """They are how a citation says "Slide 20"."""
        out = tidy(_deck([["A"] + FOOTER, ["B"] + FOOTER, ["C"] + FOOTER,
                          ["D"] + FOOTER]))
        for n in range(1, 5):
            self.assertIn(f"<!-- Slide number: {n} -->", out)

    def test_every_slides_own_content_survives(self):
        out = tidy(_deck([["Alpha"] + FOOTER, ["Beta"] + FOOTER,
                          ["Gamma"] + FOOTER, ["Delta"] + FOOTER]))
        for word in ("Alpha", "Beta", "Gamma", "Delta"):
            self.assertIn(word, out)


class NothingThatIsAFactIsTouched(TestCase):

    def test_a_recurring_figure_is_never_treated_as_a_footer(self):
        """A number that recurs is a fact, not decoration."""
        out = tidy(_deck([["Revenue ₹56.9 Cr"], ["Revenue ₹56.9 Cr"],
                          ["Revenue ₹56.9 Cr"], ["Revenue ₹56.9 Cr"]]))
        self.assertEqual(out.count("Revenue ₹56.9 Cr"), 4)

    def test_a_short_deck_is_not_footer_scanned(self):
        """Two or three slides sharing a line is coincidence, not a footer."""
        out = tidy(_deck([["Intro", "zyla"], ["Close", "zyla"]]))
        self.assertEqual(out.count("zyla"), 2)

    def test_a_line_on_only_a_few_slides_of_many_stays(self):
        slides = [[f"Slide {chr(65 + i)}"] for i in range(10)]
        slides[0].append("Appendix note")
        slides[5].append("Appendix note")
        out = tidy(_deck(slides))
        self.assertEqual(out.count("Appendix note"), 2)

    def test_headings_are_never_treated_as_footers(self):
        out = tidy(_deck([["## Market"], ["## Market"], ["## Market"],
                          ["## Market"]]))
        self.assertEqual(out.count("## Market"), 4)

    def test_a_document_without_slides_is_unchanged(self):
        text = "Plain report.\n\nzyla\n\nzyla\n\nzyla\n\nzyla"
        self.assertEqual(tidy(text), text)
