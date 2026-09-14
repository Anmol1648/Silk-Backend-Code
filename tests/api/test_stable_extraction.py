"""The same documents give the same profile and the same scores.

Founder Education scored Excellent on one run and was missing on the next,
from the same deck. These tests hold the four things that make the answer
repeatable, and the one that recovers it when it slips anyway:

    * the two extraction roles run at temperature 0 with a fixed seed, and in
      JSON mode whenever no search tool is attached;
    * a band written loosely ("Good/Excellent") is read, not dropped;
    * a band that did not come back is asked for again, on its own;
    * a rebuilt response is never accepted if it carries fewer values than
      the old prefix salvage would have kept.
"""
import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from fundos.llm import adapter
from fundos.profile import assessment_extraction as ax


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code), response=self)


OK = _Resp(payload={"candidates": [{"content": {"parts": [{"text": "{}"}]},
                                    "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1,
                                      "candidatesTokenCount": 1}})


class _Endpoint:
    base_url = ""
    provider_kind = "gemini"
    default_model = "gemini-2.5-flash"
    timeout_seconds = 30
    api_key_env_var = ""
    api_key = "k"
    api_key_pool_size = 1
    code = "GEMINI"

    def has_api_key_in_env(self):
        return True

    def penalise_api_key(self, *a, **k):
        pass

    def disable_api_key(self, *a, **k):
        pass


class TheExtractionRolesAreRepeatable(SimpleTestCase):

    def _sent(self, role, capabilities=None, binding=None, responses=None):
        bodies = []
        responses = list(responses or [OK])

        def post(url, json=None, headers=None, timeout=None):
            bodies.append(json)
            return responses.pop(0) if len(responses) > 1 else responses[0]

        with mock.patch.object(adapter.requests, "post", side_effect=post):
            adapter._dispatch(_Endpoint(), "sys", "prompt", binding,
                              capabilities=capabilities or {}, role=role)
        return [b["generationConfig"] for b in bodies]

    def test_synthesis_runs_at_temperature_zero_with_a_seed_in_json_mode(self):
        config = self._sent("profile_synthesis")[0]
        self.assertEqual(config["temperature"], 0.0)
        self.assertEqual(config["seed"], adapter.STABLE_OUTPUT_SEED)
        self.assertEqual(config["responseMimeType"], "application/json")

    def test_an_administrators_temperature_still_wins(self):
        config = self._sent("assessment_inputs",
                            binding=SimpleNamespace(temperature=0.4,
                                                    max_output_tokens=None))[0]
        self.assertEqual(config["temperature"], 0.4)

    def test_no_json_mode_when_search_is_attached(self):
        """Gemini refuses a JSON response type alongside google_search."""
        config = self._sent("assessment_inputs",
                            capabilities={"web_search": {}})[0]
        self.assertNotIn("responseMimeType", config)
        self.assertIn("seed", config)

    def test_other_roles_are_untouched(self):
        config = self._sent("profile_research_batch")[0]
        for key in ("seed", "responseMimeType", "temperature"):
            self.assertNotIn(key, config)

    def test_a_refused_json_mode_or_seed_retries_without_them(self):
        refused = _Resp(400, '{"error": {"message": "Invalid value at '
                             '\'generation_config.response_mime_type\'"}}')
        configs = self._sent("profile_synthesis", responses=[refused, OK])
        self.assertEqual(len(configs), 2)
        self.assertNotIn("responseMimeType", configs[1])
        self.assertNotIn("seed", configs[1])


class ALooselyWrittenBandIsRead(SimpleTestCase):

    def test_exact_and_cased(self):
        self.assertEqual(ax._normalise_band("EXCELLENT"), "Excellent")
        self.assertEqual(ax._normalise_band("Good (tier-1 MBA)"), "Good")

    def test_a_range_takes_the_lower_band(self):
        self.assertEqual(ax._normalise_band("Good/Excellent"), "Good")
        self.assertEqual(ax._normalise_band("Fair to Good"), "Fair")

    def test_no_band_named(self):
        for text in ("Strong", "Not Evidenced", "", None):
            self.assertEqual(ax._normalise_band(text), "")

    def test_a_word_containing_a_band_is_not_a_band(self):
        self.assertEqual(ax._normalise_band("Goodwill"), "")


def _param(key, cat="A"):
    return SimpleNamespace(input_key=key, name=key, unit="", definition="",
                           where_to_find="", if_missing="", category_code=cat)


class AMissingBandIsAskedForAgain(SimpleTestCase):

    def test_unanswered_anchors(self):
        asked = [_param("ANC_FDR_EDU"), _param("ANC_NET"), _param("ANC_X")]
        bands = [{"inputKey": "ANC_NET", "band": "Good"},
                 {"inputKey": "ANC_X", "band": "Not Evidenced"},
                 {"inputKey": "ANC_FDR_EDU", "band": "Strong"}]
        self.assertEqual([p.input_key for p in
                          ax._unanswered_anchors(asked, bands)],
                         ["ANC_FDR_EDU"])

    def _ask(self, responses):
        calls = []

        def generate(**kwargs):
            calls.append(kwargs)
            return responses[kwargs["calling_context"]]

        profile = SimpleNamespace(
            company=SimpleNamespace(name="ACME", sector="", sub_sector=""),
            website_url="", hq_country="", home_currency="")
        anchors = [_param("ANC_FDR_EDU"), _param("ANC_NET")]
        with mock.patch("fundos.llm.adapter.llm_generate",
                        side_effect=generate), \
                mock.patch("fundos.assessment.models.ConfigAnchor.objects"), \
                mock.patch.object(ax, "vocabulary", return_value=([], [])), \
                mock.patch.object(ax, "_citable_sources", return_value=[]):
            out = ax._ask(profile, {}, [], anchors)
        return out, calls

    def test_the_missing_band_is_recovered_by_a_small_second_call(self):
        first = {"values": [], "bands": [
            {"inputKey": "ANC_NET", "band": "Good", "sourceDoc": "Deck"}]}
        retry = {"values": [], "bands": [
            {"inputKey": "ANC_FDR_EDU", "band": "Excellent",
             "sourceDoc": "Deck, Slide 20"}]}
        out, calls = self._ask({
            "profile.assessment_inputs.people_and_deal": first,
            "profile.assessment_inputs.people_and_deal.retry": retry})

        keys = sorted(b["inputKey"] for b in out["bands"])
        self.assertEqual(keys, ["ANC_FDR_EDU", "ANC_NET"])
        retry_call = [c for c in calls if c["calling_context"].endswith(".retry")]
        self.assertEqual(len(retry_call), 1)
        asked = [p["inputKey"] for p in
                 retry_call[0]["context"]["qualitative_parameters"]]
        self.assertEqual(asked, ["ANC_FDR_EDU"])
        self.assertEqual(retry_call[0]["context"]["parameters"], [])

    def test_a_complete_answer_costs_no_second_call(self):
        first = {"values": [], "bands": [
            {"inputKey": "ANC_NET", "band": "Good"},
            {"inputKey": "ANC_FDR_EDU", "band": "Excellent"}]}
        _, calls = self._ask({"profile.assessment_inputs.people_and_deal": first})
        self.assertFalse(any(c["calling_context"].endswith(".retry")
                             for c in calls))

    def test_a_failed_retry_keeps_the_first_answer(self):
        first = {"values": [], "bands": [{"inputKey": "ANC_NET", "band": "Good"}]}

        def generate(**kwargs):
            if kwargs["calling_context"].endswith(".retry"):
                raise RuntimeError("503")
            return first

        profile = SimpleNamespace(
            company=SimpleNamespace(name="ACME", sector="", sub_sector=""),
            website_url="", hq_country="", home_currency="")
        with mock.patch("fundos.llm.adapter.llm_generate",
                        side_effect=generate), \
                mock.patch("fundos.assessment.models.ConfigAnchor.objects"), \
                mock.patch.object(ax, "vocabulary", return_value=([], [])), \
                mock.patch.object(ax, "_citable_sources", return_value=[]):
            out = ax._ask(profile, {}, [], [_param("ANC_FDR_EDU"),
                                            _param("ANC_NET")])
        self.assertEqual([b["inputKey"] for b in out["bands"]], ["ANC_NET"])


class ARebuildNeverLosesWhatSalvageKept(SimpleTestCase):

    def test_leaf_count(self):
        self.assertEqual(adapter._leaf_count({"a": [1, {"b": "x"}], "c": None}),
                         2)

    def test_indentation_does_not_count_as_content(self):
        self.assertEqual(adapter._dense_len('{\n    "a": 1\n}'), 7)

    def test_a_rebuild_with_fewer_values_falls_back_to_salvage(self):
        endpoint = mock.Mock(code="GEMINI")
        salvaged = {"bands": [{"inputKey": "ANC_FDR_EDU"}, {"inputKey": "B"}]}
        with mock.patch.object(adapter, "_lenient_json_text",
                               return_value={"bands": []}), \
                mock.patch.object(adapter, "_salvage_truncated_json",
                                  return_value=salvaged), \
                mock.patch.object(adapter, "_repair_json_text",
                                  return_value=(None, [])):
            parsed, repaired = adapter._parse_json_with_repair(
                endpoint, "", '{"bands": [brok', None, None,
                role="assessment_inputs")
        # The size check alone would have accepted the rebuild (12 of 14
        # characters); the value count is what refuses it.
        self.assertEqual(parsed, salvaged)
