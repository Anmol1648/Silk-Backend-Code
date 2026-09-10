"""Synthesis must not be capped by one response's output ceiling.

One call had to emit all seventeen sections. A response past the model's
output limit is cut mid-object, the adapter salvages a valid JSON prefix, and
the run reports success -- so on 08 Sep a profile published with two populated
sections out of seventeen and nothing downstream could tell. The sections
after the cut are indistinguishable from sections the dossier had nothing to
say about.

Both have the same remedy, and it is the one thing that actually scales here:
ask again, for those sections only. The second call carries the same dossier
but is asked for a handful of sections instead of all of them, so its output
sits nowhere near the ceiling.
"""
from unittest import mock

from django.test import TestCase

from fundos.profile import schema
from fundos.profile.pipeline import synthesize


def _section(key, data):
    return {"sectionKey": key, "data": data}


# `investors_cap_table` is the one section a flat string-per-field payload
# cannot satisfy: both its fields are containers, so filling them with strings
# normalizes back to empty. Written out so the fixture means "populated"
# everywhere and a retry in these tests is always the loop's doing.
_NESTED = {
    "investors_cap_table": {
        "investors_list": [{"investor_name": "Acme Ventures",
                            "investor_type": "VC"}],
        "cap_table_summary": {"total_investors": 1, "ownership": []},
    },
}


def _payload(keys):
    """A response populating exactly these section keys."""
    out = {}
    for key in keys:
        if key in _NESTED:
            out[key] = _section(key, _NESTED[key])
            continue
        spec = next(s for s in schema.sections() if s["key"] == key)
        if spec["kind"] == "array":
            field = list(spec["fields"])[0]
            out[key] = _section(key, [{field: "something"}])
        else:
            out[key] = _section(
                key, {f: "something" for f in list(spec["fields"])[:2]})
    return {"sections": out}


class ATruncatedResponseIsFinished(TestCase):

    def _run(self, responses):
        """Drive synthesis with a scripted sequence of model responses."""
        self.asked = []

        def fake_ask(sections, dossier, **kwargs):
            self.asked.append([s["key"] for s in sections])
            return responses[min(len(self.asked) - 1, len(responses) - 1)]

        with mock.patch.object(synthesize, "_ask", side_effect=fake_ask):
            return synthesize.run(mock.Mock(), company_name="ACME",
                                  website="https://acme.com",
                                  dossier="D" * 5000, documents=[])

    def test_a_complete_first_response_makes_one_call(self):
        """The healthy path must cost exactly what it costs today."""
        keys = schema.promptable_keys()
        profile, summary = self._run([_payload(keys)])

        self.assertEqual(len(self.asked), 1)
        self.assertEqual(summary["recovered_by_continuation"], 0)
        self.assertEqual(len(summary["rounds"]), 1)

    def test_sections_lost_to_truncation_are_asked_for_again(self):
        """The 08 Sep failure: only the first two sections came back."""
        keys = schema.promptable_keys()
        first, rest = keys[:2], keys[2:]

        profile, summary = self._run([_payload(first), _payload(rest)])

        self.assertEqual(len(self.asked), 2)
        # Round two asks for exactly what round one did not fill.
        self.assertNotIn(keys[0], self.asked[1])
        for key in rest:
            self.assertIn(key, self.asked[1])
        # Every section the model is asked for. `document_center` is written
        # from the run's own document rows, and none were passed here.
        self.assertEqual(summary["sections_populated"], len(keys))

    def test_the_recovered_sections_carry_their_data(self):
        keys = schema.promptable_keys()
        profile, _ = self._run([_payload(keys[:2]), _payload(keys[2:])])
        for key in keys:
            self.assertTrue(profile["sections"][key]["isComplete"], key)
            self.assertTrue(profile["sections"][key]["data"], key)

    def test_a_round_that_gains_nothing_stops_the_loop(self):
        """What remains is unevidenced, not truncated.

        Asking a third time buys the same answer at the same price.
        """
        keys = schema.promptable_keys()
        profile, summary = self._run([_payload(keys[:2]), {"sections": {}}])

        self.assertEqual(len(self.asked), 2)
        self.assertEqual(summary["recovered_by_continuation"], 0)

    def test_the_loop_is_capped(self):
        """A model that never fills a section must not be asked forever."""
        keys = schema.promptable_keys()
        # Each round fills exactly one more section, so the loop would run
        # until every section is filled if nothing capped it.
        responses = [_payload(keys[:i]) for i in range(2, len(keys) + 1)]
        self._run(responses)
        self.assertLessEqual(len(self.asked), synthesize.MAX_SYNTHESIS_ROUNDS)

    def test_a_failed_continuation_keeps_what_was_already_won(self):
        """A top-up is not a dependency."""
        keys = schema.promptable_keys()

        calls = []

        def fake_ask(sections, dossier, **kwargs):
            calls.append([s["key"] for s in sections])
            if len(calls) == 1:
                return _payload(keys[:2])
            raise RuntimeError("provider refused")

        with mock.patch.object(synthesize, "_ask", side_effect=fake_ask):
            profile, summary = synthesize.run(
                mock.Mock(), company_name="ACME", website="",
                dossier="D" * 5000, documents=[])

        self.assertTrue(profile["sections"][keys[0]]["isComplete"])
        self.assertIn("error", summary["rounds"][-1])

    def test_the_document_center_never_counts_as_missing(self):
        """It is written from our own records, never by the model."""
        keys = schema.promptable_keys()
        self.assertNotIn("document_center", keys)

        profile, summary = self._run([_payload(keys)])
        # One call: the pipeline-owned section must not have triggered a
        # continuation round asking the model to fill it.
        self.assertEqual(len(self.asked), 1)

    def test_the_rounds_are_recorded_on_the_run(self):
        """A thin profile has to explain itself to whoever reads the run."""
        keys = schema.promptable_keys()
        _, summary = self._run([_payload(keys[:2]), _payload(keys[2:])])

        rounds = summary["rounds"]
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rounds[1]["asked"], len(keys) - 2)
        self.assertGreater(rounds[1]["gained"], 0)
        self.assertTrue(rounds[1]["keys"])


class TheRequestNarrowsWithTheAsk(TestCase):
    """A continuation must not re-send the whole schema."""

    def test_the_schema_block_covers_only_the_sections_asked_for(self):
        subset = [s for s in schema.sections()
                  if s["key"] in ("founders", "competitors")]
        block = schema.schema_prompt_block(subset)

        self.assertIn('sections["founders"]', block)
        self.assertIn('sections["competitors"]', block)
        self.assertNotIn('sections["funding_history"]', block)

    def test_a_narrower_ask_is_a_smaller_prompt(self):
        """This is the whole mechanism: less asked for, less to emit."""
        everything = schema.schema_prompt_block(schema.sections())
        subset = schema.schema_prompt_block(
            [s for s in schema.sections() if s["key"] == "founders"])
        self.assertLess(len(subset), len(everything) / 2)
