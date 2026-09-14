"""A profile keeps its sections AND their citations when the model slips.

A live Zyla Health run:

    profile_synthesis returned JSON that would not parse; recovered the good
    prefix locally (13030 of 198194 characters kept)
    synthesis round 2 ... asking again for 14 section(s)

Fourteen complete, cited sections were thrown away over one bad character in
a verbatim quote. The continuation round refilled them with data and almost
no citations -- 15 citations in the whole profile -- and the run reported
success. Extra sources for founders were discarded on save, and citations
naming a file in the model's own words ("Orah teaser deck") were dropped.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from fundos.llm import adapter
from fundos.profile import schema
from fundos.profile.pipeline import synthesize

BS = chr(92)


class TheResponseIsRepairedNotCut(SimpleTestCase):

    def test_an_unescaped_quote_in_a_quote_is_repaired(self):
        parsed, fixes = adapter._repair_json_text(
            '{"s1": {"quote": "Revenue grew to "56.9 Cr" in FY24"}, '
            '"s2": {"data": "kept"}}')
        self.assertEqual(parsed["s1"]["quote"],
                         'Revenue grew to "56.9 Cr" in FY24')
        self.assertEqual(parsed["s2"]["data"], "kept")
        self.assertIn("unescaped quote", fixes)

    def test_a_raw_line_break_inside_a_value(self):
        parsed, _ = adapter._repair_json_text('{"a": "one\ntwo", "b": 2}')
        self.assertEqual(parsed, {"a": "one\ntwo", "b": 2})

    def test_a_trailing_comma(self):
        parsed, _ = adapter._repair_json_text('{"a": [1, 2,], "b": 3,}')
        self.assertEqual(parsed, {"a": [1, 2], "b": 3})

    def test_a_comma_inside_a_value_is_not_touched(self):
        parsed, _ = adapter._repair_json_text('{"a": "x, ]", "b": [1,],}')
        self.assertEqual(parsed["a"], "x, ]")

    def test_an_invalid_escape(self):
        parsed, _ = adapter._repair_json_text(
            '{"a": "C:' + BS + 'data' + BS + 'd"}')
        self.assertEqual(parsed["a"], "C:" + BS + "data" + BS + "d")

    def test_a_missing_comma_is_not_guessed_at(self):
        """Structure the model got wrong is not rewritten into other data."""
        self.assertEqual(adapter._repair_json_text('{"a": "x" "b": "y"}'),
                         (None, []))

    def test_the_whole_long_response_survives(self):
        good = ", ".join(f'"s{i}": {{"data": "value {i}"}}' for i in range(40))
        text = '{"s_bad": {"quote": "the "best" clinic"}, ' + good + "}"
        endpoint = mock.Mock(code="GEMINI")
        parsed, repaired = adapter._parse_json_with_repair(
            endpoint, "", text, None, None, role="profile_synthesis")
        self.assertTrue(repaired)
        self.assertEqual(len(parsed), 41)

    def test_a_quote_followed_by_a_comma_is_rebuilt(self):
        """The strict repair misses it: the parser fails a word later."""
        endpoint = mock.Mock(code="GEMINI")
        good = ", ".join(f'"s{i}": {{"data": "value {i}"}}' for i in range(40))
        text = '{"s_bad": {"quote": "rated "Best", leader in care"}, ' + good + "}"
        parsed, repaired = adapter._parse_json_with_repair(
            endpoint, "", text, None, None, role="profile_synthesis")
        self.assertTrue(repaired)
        self.assertEqual(len(parsed), 41)
        self.assertEqual(parsed["s_bad"]["quote"],
                         'rated "Best", leader in care')

    def test_unquoted_figures_and_literals(self):
        parsed = adapter._lenient_json_text(
            '{"rev": 1,20,000, "cap": Rs 56.9 Cr, "x": NaN, "y": True}')
        self.assertEqual(parsed, {"rev": "1,20,000", "cap": "Rs 56.9 Cr",
                                  "x": None, "y": True})

    def test_a_missing_comma_between_members(self):
        self.assertEqual(adapter._lenient_json_text('{"a": "x"\n "b": "y"}'),
                         {"a": "x", "b": "y"})

    def test_an_array_of_numbers_is_not_read_as_one_figure(self):
        self.assertEqual(adapter._lenient_json_text('{"a": [1,20,300]}'),
                         {"a": [1, 20, 300]})

    def test_the_fault_is_named_in_the_log_context(self):
        context = adapter._fault_context('{"a": "x" "b": 1}')
        self.assertIn("char", context)
        self.assertIn('"b"', context)

    def test_unrepairable_text_still_falls_back_to_salvage(self):
        endpoint = mock.Mock(code="GEMINI")
        text = '{"a": {"x": 1}, "b": {"y": 2} "c": 3}'
        parsed, repaired = adapter._parse_json_with_repair(
            endpoint, "", text, None, None, role="profile_synthesis")
        self.assertTrue(repaired)
        self.assertIn("a", parsed)


DOSSIER = """# Consolidated research dossier: ACME

# Source 2: Company Documents

### Deck.pdf

ACME builds widgets.

# Source 1: Web Research

## Batch 1: Company Basics & Identity

ACME is a widget company.
""" + ("filler text. " * 400)


def _cited(key, cited=True):
    spec = next(s for s in schema.sections() if s["key"] == key)
    if key == "investors_cap_table":
        data = {"investors_list": [{"investor_name": "Acme Ventures",
                                    "investor_type": "VC"}],
                "cap_table_summary": {"total_investors": 1, "ownership": []}}
        field = "investors_list"
    elif spec["kind"] == "array":
        field = list(spec["fields"])[0]
        data = [{field: "something"}]
    else:
        fields = list(spec["fields"])[:2]
        data = {f: "something" for f in fields}
        field = fields[0]
    section = {"sectionKey": key, "data": data}
    if cited:
        address = "0" if isinstance(data, list) else field
        section["sources"] = {address: {"source": "Deck.pdf",
                                        "quote": "ACME builds widgets."}}
    return section


def _payload(keys, cited=True):
    return {"sections": {k: _cited(k, cited) for k in keys}}


class AContinuationMustCite(TestCase):

    def _run(self, responses):
        self.asked, self.flags = [], []

        def fake_ask(sections, dossier, **kwargs):
            self.asked.append([s["key"] for s in sections])
            self.flags.append(kwargs.get("continuation", False))
            return responses[min(len(self.asked) - 1, len(responses) - 1)]

        with mock.patch.object(synthesize, "_ask", side_effect=fake_ask):
            return synthesize.run(mock.Mock(), company_name="ACME",
                                  website="https://acme.com",
                                  dossier=DOSSIER, documents=[])

    def test_a_fully_cited_first_response_makes_one_call(self):
        keys = schema.promptable_keys()
        self._run([_payload(keys)])
        self.assertEqual(len(self.asked), 1)

    def test_many_uncited_sections_are_asked_for_again(self):
        """The live run: data everywhere, citations almost nowhere."""
        keys = schema.promptable_keys()
        first = {"sections": {**_payload(keys[:2])["sections"],
                              **_payload(keys[2:], cited=False)["sections"]}}
        profile, summary = self._run([first, _payload(keys[2:])])

        self.assertEqual(len(self.asked), 2)
        self.assertEqual(sorted(self.asked[1]), sorted(keys[2:]))
        for key in keys:
            self.assertTrue(profile["sections"][key].get("sources"), key)

    def test_one_uncited_section_does_not_cost_a_whole_call(self):
        keys = schema.promptable_keys()
        first = {"sections": {**_payload(keys[:-1])["sections"],
                              **_payload(keys[-1:], cited=False)["sections"]}}
        self._run([first])
        self.assertEqual(len(self.asked), 1)

    def test_a_cited_section_is_never_replaced_by_an_uncited_one(self):
        keys = schema.promptable_keys()
        first = {"sections": {**_payload(keys[:3])["sections"],
                              **_payload(keys[3:], cited=False)["sections"]}}
        # The follow-up returns everything uncited again: nothing is gained.
        profile, _ = self._run([first, _payload(keys, cited=False)])
        for key in keys[:3]:
            self.assertTrue(profile["sections"][key].get("sources"), key)

    def test_the_follow_up_is_told_citations_are_required(self):
        keys = schema.promptable_keys()
        self._run([_payload(keys[:2]), _payload(keys[2:])])
        self.assertEqual(self.flags, [False, True])

    def test_the_rule_is_in_the_follow_up_prompt(self):
        captured = {}

        def fake_generate(**kwargs):
            captured.update(kwargs["section_context"])
            return {}

        with mock.patch("fundos.llm.adapter.llm_generate",
                        side_effect=fake_generate):
            synthesize._ask(schema.sections()[:2], "D", company_name="A",
                            website="", source_labels=["Deck.pdf"],
                            continuation=True)
        self.assertIn("MUST carry its `sources`", captured["schema_block"])

    def test_a_filled_but_uncited_section_still_beats_an_empty_one(self):
        keys = schema.promptable_keys()
        profile, _ = self._run([_payload(keys[:2]),
                                _payload(keys[2:], cited=False)])
        for key in keys[2:]:
            self.assertTrue(profile["sections"][key]["isComplete"], key)


class ExtraSourcesForListItemsAreKept(SimpleTestCase):

    def _store(self, sources, data, served):
        from fundos.profile.pipeline import writer

        section = mock.Mock()
        with mock.patch("fundos.profile.models.ProfileSection.objects") as rows, \
                mock.patch("fundos.profile.spec_serializer.serialize_section",
                           return_value={"data": served}):
            rows.filter.return_value.first.return_value = section
            writer._store_sources(mock.Mock(), "founders", None, data, sources)
        return section.field_sources

    def test_a_second_source_is_stored_under_the_item(self):
        stored = self._store(
            {"0": {"source": "Deck.pdf", "quote": "A"},
             "0.1": {"source": "Batch 2: Founders", "quote": "B"},
             "1.2": {"source": "Deck.pdf", "quote": "C"}},
            data=[{"name": "Asha Rao"}, {"name": "Ravi Iyer"}],
            served=[{"id": "u1", "name": "Asha Rao"},
                    {"id": "u2", "name": "Ravi Iyer"}])
        self.assertEqual(set(stored), {"u1", "u1.1", "u2.2"})
        self.assertEqual(stored["u1.1"]["quote"], "B")

    def test_a_name_with_a_version_number_is_not_split(self):
        stored = self._store(
            {"Widget 2.0": {"source": "Deck.pdf", "quote": "A"}},
            data=[{"name": "Widget 2.0"}],
            served=[{"id": "w1", "name": "Widget 2.0"}])
        self.assertEqual(set(stored), {"w1"})


LABELS = ["Project Orah Teaser_vff.pptx", "Project Orah_Financial Model_vf.xlsx",
          "Batch 1: Company Basics & Identity",
          "Batch 2: Founders & Leadership",
          "Batch 7: Financial Performance"]


class ASourceNamedInOtherWordsIsFound(SimpleTestCase):

    def _match(self, claim):
        return schema.canonical_source(claim, LABELS)

    def test_a_file_described_rather_than_copied(self):
        self.assertEqual(self._match("Orah teaser deck"),
                         "Project Orah Teaser_vff.pptx")

    def test_a_research_batch_in_plain_words(self):
        self.assertEqual(self._match("Founders and Leadership research"),
                         "Batch 2: Founders & Leadership")

    def test_an_exact_label_is_unchanged(self):
        self.assertEqual(self._match("Batch 7: Financial Performance"),
                         "Batch 7: Financial Performance")

    def test_a_site_not_in_the_dossier_is_still_dropped(self):
        """Attaching it to some batch would invent a source."""
        self.assertEqual(self._match("LinkedIn"), "")

    def test_a_generic_channel_is_still_dropped(self):
        self.assertEqual(self._match("Web Research"), "")

    def test_a_name_inside_two_files_names_neither(self):
        self.assertEqual(self._match("Project Orah"), "")

    def test_a_tie_on_words_names_neither(self):
        self.assertEqual(schema.canonical_source(
            "Orah widget deck", ["Orah Widget Deck A.pdf",
                                 "Orah Widget Deck B.pdf"]), "")
