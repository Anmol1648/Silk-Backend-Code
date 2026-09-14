"""What a provenance chip shows a reader.

One live panel:

    {"name": "Project Orah Teaser_vff.pptx", "locator": "Slide 20",
     "snippet": "Khushboo Aggarwal"}

The snippet is the model's own quote — the name, and nothing around it.
Technically a quote; useless as evidence, because a reader cannot tell
whether the document says what the field claims. And one source, where the
value may have been evidenced by several.

Three things asserted here:

    The snippet is the DOCUMENT'S words, found by searching the dossier the
    run was actually built from. Nothing is written or paraphrased — when
    the passage cannot be found, the model's own quote stands.

    Every source is returned, not the first one.

    Uploaded documents come before web research, the same precedence the
    dossier itself uses.
"""
from django.test import TestCase

from fundos.profile import snippets


DOSSIER = """# Consolidated research dossier: Zyla Health

---

# Source 2: Company Documents

### Project Orah Teaser_vff.pptx

## Slide 20

Leadership. Khushboo Aggarwal, Co-founder & Chief Executive Officer.
Twelve years in chronic care; previously led clinical operations at a
national diagnostics chain. Responsible for the care-delivery model and
the payer relationships that carry it.

---

# Source 1: Web Research (search-grounded)

## Batch 2: Founders & Leadership

Zyla's leadership is publicly listed on the company website.
"""


class TheSnippetIsTheDocumentsOwnWords(TestCase):

    def test_a_bare_name_becomes_the_passage_around_it(self):
        out = snippets.expand("Khushboo Aggarwal", DOSSIER)
        self.assertIn("Khushboo Aggarwal", out)
        self.assertIn("Co-founder", out)
        self.assertGreater(len(out), len("Khushboo Aggarwal") * 5)

    def test_it_is_the_document_verbatim_not_a_paraphrase(self):
        out = snippets.expand("Khushboo Aggarwal", DOSSIER)
        self.assertIn("Twelve years in chronic care", out)

    def test_an_extract_says_it_is_one(self):
        # Narrower than this small dossier, so something is actually left out.
        out = snippets.expand("Khushboo Aggarwal", DOSSIER, width=200)
        self.assertTrue(out.startswith("…") or out.endswith("…"))

    def test_a_quote_that_is_not_there_expands_to_nothing(self):
        """Never invent a passage for a quote the document does not carry."""
        self.assertEqual(snippets.expand("Revenue was 400 crore", DOSSIER), "")

    def test_a_very_short_quote_is_not_searched(self):
        """Two words match in a hundred places and the first is as likely
        wrong as right."""
        self.assertEqual(snippets.expand("of", DOSSIER), "")

    def test_wrapping_does_not_stop_a_match(self):
        """The quote is one line; the document wraps it across two."""
        out = snippets.expand("Co-founder & Chief Executive Officer", DOSSIER)
        self.assertIn("Khushboo", out)

    def test_case_does_not_stop_a_match(self):
        self.assertIn("Khushboo", snippets.expand("KHUSHBOO AGGARWAL",
                                                  DOSSIER))

    def test_the_snippet_is_bounded(self):
        out = snippets.expand("Khushboo Aggarwal", DOSSIER, width=200)
        self.assertLess(len(out), 400)

    def test_it_does_not_start_or_end_mid_word(self):
        out = snippets.expand("Khushboo Aggarwal", DOSSIER, width=120)
        body = out.strip("… ").strip()
        self.assertTrue(body[0].isalnum() or body[0] in "#*(",
                        f"starts mid-word: {body[:20]!r}")


class TheModelsQuoteIsTheFallbackNeverTheInvention(TestCase):

    def test_the_quote_stands_when_the_dossier_is_missing(self):
        citation = {"source": "Deck.pptx", "quote": "Khushboo Aggarwal"}
        self.assertEqual(snippets.for_citation(citation, ""),
                         "Khushboo Aggarwal")

    def test_the_quote_stands_when_the_passage_is_not_found(self):
        citation = {"source": "Project Orah Teaser_vff.pptx",
                    "quote": "Something not present"}
        self.assertEqual(snippets.for_citation(citation, DOSSIER),
                         "Something not present")

    def test_the_document_wins_when_it_can_be_found(self):
        citation = {"source": "Project Orah Teaser_vff.pptx",
                    "quote": "Khushboo Aggarwal"}
        self.assertIn("Co-founder", snippets.for_citation(citation, DOSSIER))

    def test_a_citation_with_no_quote_yields_nothing_rather_than_noise(self):
        self.assertEqual(snippets.for_citation(
            {"source": "Project Orah Teaser_vff.pptx"}, DOSSIER), "")

    def test_it_never_raises(self):
        for bad in (None, {}, {"quote": None}, {"quote": 3}):
            snippets.for_citation(bad, DOSSIER)


class EverySourceIsKeptNotTheFirst(TestCase):

    def _coerce(self, raw, allowed=None):
        from fundos.profile.schema import _coerce_sources

        return _coerce_sources({"sources": raw}, key="founders",
                               allowed=allowed)

    def test_two_sources_for_one_value_both_survive(self):
        out = self._coerce({"0": {
            "name": {"source": "Deck.pptx", "locator": "Slide 20",
                     "quote": "Khushboo Aggarwal"},
            "background": {"source": "Batch 2: Founders & Leadership",
                           "quote": "Twelve years in chronic care"}}})
        self.assertEqual(len(out), 2)
        self.assertIn("0", out)
        self.assertIn("0.1", out)

    def test_the_one_with_a_quote_leads(self):
        out = self._coerce({"0": {
            "a": {"source": "Deck.pptx"},
            "b": {"source": "Batch 2: Founders", "quote": "Twelve years"}}})
        self.assertEqual(out["0"]["quote"], "Twelve years")

    def test_a_duplicate_source_is_not_listed_twice(self):
        out = self._coerce({"0": {
            "a": {"source": "Deck.pptx", "locator": "Slide 20"},
            "b": {"source": "Deck.pptx", "locator": "Slide 20"}}})
        self.assertEqual(len(out), 1)

    def test_a_single_source_is_unchanged(self):
        out = self._coerce({"0": {"source": "Deck.pptx", "quote": "X"}})
        self.assertEqual(list(out), ["0"])

    def test_an_extra_naming_an_unknown_source_is_dropped(self):
        """The same check the first one gets — a citation nobody can turn to
        is worse than an honest blank."""
        out = self._coerce({"0": {
            "a": {"source": "Deck.pptx", "quote": "X"},
            "b": {"source": "Something Invented", "quote": "Y"}}},
            allowed=["Deck.pptx"])
        self.assertEqual(list(out), ["0"])

    def test_a_longer_quote_survives_storage(self):
        """400 characters cut the sentence a figure sat in about as often as
        it kept it."""
        from fundos.profile.schema import QUOTE_LIMIT

        long_quote = "word " * 200
        out = self._coerce({"0": {"source": "Deck.pptx",
                                  "quote": long_quote}})
        self.assertGreater(len(out["0"]["quote"]), 400)
        self.assertLessEqual(len(out["0"]["quote"]), QUOTE_LIMIT)


class TheReaderGetsThemAll(TestCase):

    def _for(self, store, address):
        from fundos.profile.views import _citations_for

        return _citations_for(store, address)

    def test_the_exact_match_and_its_extras_come_back_together(self):
        store = {
            "abc": {"source": "Deck.pptx", "quote": "A"},
            "abc.1": {"source": "Batch 2: Founders", "quote": "B"},
        }
        self.assertEqual(len(self._for(store, "abc")), 2)

    def test_the_exact_match_leads(self):
        store = {
            "abc": {"source": "Deck.pptx", "quote": "A"},
            "abc.1": {"source": "Batch 2: Founders", "quote": "B"},
        }
        self.assertEqual(self._for(store, "abc")[0]["source"], "Deck.pptx")

    def test_a_field_with_one_source_is_unchanged(self):
        store = {"abc": {"source": "Deck.pptx", "quote": "A"}}
        self.assertEqual(len(self._for(store, "abc")), 1)

    def test_a_duplicate_across_the_two_is_not_repeated(self):
        store = {
            "abc": {"source": "Deck.pptx", "locator": "S20", "quote": "A"},
            "abc.1": {"source": "Deck.pptx", "locator": "S20", "quote": "A"},
        }
        self.assertEqual(len(self._for(store, "abc")), 1)


class DocumentsComeBeforeWeb(TestCase):

    def _order(self, sources):
        from fundos.profile.views import _documents_first

        return [s["type"] for s in _documents_first(sources)]

    def test_a_document_outranks_web_research(self):
        self.assertEqual(
            self._order([{"type": "web"}, {"type": "document"}]),
            ["document", "web"])

    def test_attribution_sits_between_them(self):
        self.assertEqual(
            self._order([{"type": "web"}, {"type": "attribution"},
                         {"type": "document"}]),
            ["document", "attribution", "web"])

    def test_two_documents_keep_the_order_they_were_cited_in(self):
        """The rank decides between KINDS, not between sources of one kind."""
        from fundos.profile.views import _documents_first

        out = _documents_first([{"type": "document", "name": "Deck.pptx"},
                                {"type": "document", "name": "Model.xlsx"}])
        self.assertEqual([s["name"] for s in out],
                         ["Deck.pptx", "Model.xlsx"])

    def test_an_unknown_type_goes_last_rather_than_first(self):
        self.assertEqual(
            self._order([{"type": "mystery"}, {"type": "document"}]),
            ["document", "mystery"])

    def test_an_empty_list_is_fine(self):
        self.assertEqual(self._order([]), [])
        self.assertEqual(self._order(None), [])


LAYERED = """# Consolidated research dossier: Zyla Health

## Founders (research leads — unverified)
4. _Do not treat anything in this list as evidence on its own._
- **Khushboo Aggarwal** — https://www.linkedin.com/in/khushbooaggarwal/

---

# Source 2: Company Documents (native extraction + document model)

## Company Presentation

### Project Orah Teaser_vff.pptx

#### Extracted content

<!-- Slide number: 19 -->
# Our investors
Backed by leading healthcare funds.

<!-- Slide number: 20 -->
# Leadership team
Khushboo Aggarwal, Co-founder & CEO. Twelve years in chronic care.

<!-- Slide number: 21 -->
# The ask
Raising a Series A.

---

# Source 1: Web Research (search-grounded)

## Batch 2: Founders & Leadership

Khushboo Aggarwal previously led clinical operations at a diagnostics chain.
"""


class TheSnippetComesFromWhereTheCitationPoints(TestCase):
    """A live panel labelled "Project Orah Teaser_vff.pptx · Slide 20" showed
    the research-leads list from the TOP of the dossier — the first place the
    founder's name appeared — and then the Source 2 preamble. The right name,
    from the wrong place, under the right label: a citation that passes
    review looking correct."""

    def _snippet(self, source, quote, locator=""):
        return snippets.for_citation(
            {"source": source, "quote": quote, "locator": locator}, LAYERED)

    def test_a_slide_citation_shows_that_slide(self):
        out = self._snippet("Project Orah Teaser_vff.pptx",
                            "Khushboo Aggarwal", "Slide 20")
        self.assertIn("Co-founder & CEO", out)

    def test_it_never_shows_the_research_leads_list(self):
        out = self._snippet("Project Orah Teaser_vff.pptx",
                            "Khushboo Aggarwal", "Slide 20")
        self.assertNotIn("linkedin", out)
        self.assertNotIn("research leads", out)
        self.assertNotIn("Do not treat", out)

    def test_it_never_shows_a_neighbouring_slide(self):
        out = self._snippet("Project Orah Teaser_vff.pptx",
                            "Khushboo Aggarwal", "Slide 20")
        self.assertNotIn("Series A", out)
        self.assertNotIn("healthcare funds", out)

    def test_a_research_citation_shows_its_own_batch(self):
        out = self._snippet("Batch 2: Founders & Leadership",
                            "Khushboo Aggarwal")
        self.assertIn("diagnostics chain", out)
        self.assertNotIn("Co-founder & CEO", out)

    def test_a_file_citation_with_no_slide_searches_the_whole_file(self):
        out = self._snippet("Project Orah Teaser_vff.pptx",
                            "Khushboo Aggarwal")
        self.assertIn("Co-founder & CEO", out)
        self.assertNotIn("linkedin", out)

    def test_a_retyped_filename_still_finds_its_file(self):
        out = self._snippet("Project Orah Teaser vff.pptx",
                            "Khushboo Aggarwal", "Slide 20")
        self.assertIn("Co-founder & CEO", out)

    def test_an_unlocatable_source_falls_back_to_the_quote_not_the_dossier(self):
        """The whole-dossier fallback is exactly what produced the defect."""
        self.assertEqual(self._snippet("Something.pdf", "Khushboo Aggarwal"),
                         "Khushboo Aggarwal")

    def test_a_slide_title_does_not_end_the_files_section(self):
        """A converted deck renders each slide title as `# Title`."""
        out = self._snippet("Project Orah Teaser_vff.pptx", "Series A",
                            "Slide 21")
        self.assertIn("Raising a Series A", out)

    def test_the_dossiers_markup_is_not_shown_to_a_reader(self):
        out = self._snippet("Project Orah Teaser_vff.pptx",
                            "Khushboo Aggarwal", "Slide 20")
        self.assertNotIn("<!--", out)
        self.assertNotIn("# Leadership", out)
        self.assertIn("Leadership team", out)
