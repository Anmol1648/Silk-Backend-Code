"""What a provenance chip shows a reader.

One live panel:

    {"name": "Project Orah Teaser_vff.pptx", "locator": "Slide 20",
     "snippet": "Khushboo Aggarwal"}

The snippet is the model's own quote — the name, and nothing around it.
Technically a quote; useless as evidence, because a reader cannot tell
whether the document says what the field claims. And one source, where the
value may have been evidenced by several.

Three things asserted here:

    The snippet is the model's own short quote, cleaned of markup. A passage
    pulled from the document around it was tried and read as a wall of slide
    text, so it was withdrawn.

    Every source is returned, not the first one.

    Uploaded documents come before web research, the same precedence the
    dossier itself uses.
"""
from django.test import TestCase



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

    def test_a_quote_stays_short_in_storage(self):
        """A quote is a short extract, not a passage."""
        from fundos.profile.schema import QUOTE_LIMIT

        long_quote = "word " * 200
        out = self._coerce({"0": {"source": "Deck.pptx",
                                  "quote": long_quote}})
        self.assertLessEqual(len(out["0"]["quote"]), QUOTE_LIMIT)
        self.assertLessEqual(QUOTE_LIMIT, 400)


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
