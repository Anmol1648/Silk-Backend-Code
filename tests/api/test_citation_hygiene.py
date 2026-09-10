"""Citations name a source; values read like numbers a person would say.

Both halves of this were wrong in the live payload at once.

A single web-research citation arrived as a 400-character Vertex AI grounding
redirect in `source`, the research question we asked in `quote`, and a `url`
truncated to "https://vertexaisearch.cloud.google.com/grounding" — because the
URL pattern excluded hyphens and stopped at the first one. Three separate
defects on one row: the link used as a title, a prompt presented as evidence,
and every hyphenated URL in the system silently cut short.

And the values beside them read "7.5 Score", "3 Count", "1 x" and "Poor Band",
because a unit was appended whether or not it was a unit at all.
"""
from decimal import Decimal

from rest_framework import status

from fundos.assessment.v2_serializers import (_display_value,
                                              _parse_source_detail,
                                              _sanitize_source_title)
from tests.api.test_fundraising_phase1 import Phase1Base

GROUNDING = (
    "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    "AUZIYQGHh-faAFrVjliNHYVdbnQ_4LVXPo_jUJ177peEmcP0xTzMgoTleHn6In22dOv1"
    "K1i3wDV3oawxgUDcP_lf5IQGIquu2CGE38MF2fqRIkdR1cQJ88EBzMmP-ocfOcyWqFAl"
    " — Source 1: Web Research (search-grounded), Batch 10: Company Story, "
    "Industry Research & Investment Thesis, Question 6. What is the TAM, SAM "
    "and SOM for the market Zyla operates in?")


class TheGroundingRedirectNeverReachesAReader(Phase1Base):

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="SEC_TAM",
            defaults={"raw_value": "28.5", "category": "E",
                      "source_type": "profile", "source_tier": 1,
                      "source_detail": GROUNDING,
                      "confidence": Decimal("0.9"), "justification": ""})
        run_scoring(self.assessment)

    def _citations(self, ref="E.1"):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        found = {}

        def walk(node):
            if node.get("isTerminal"):
                found[node["ref"]] = node
            for child in (node.get("subitems") or node.get("children") or []):
                walk(child)

        for cat in response.data["categories"]:
            walk(cat)
        return found[ref]["evidence"]["citations"]

    def test_the_source_is_a_name_not_a_url(self):
        for citation in self._citations():
            self.assertFalse(citation["source"].startswith("http"),
                             citation["source"][:80])

    def test_no_citation_carries_a_link_at_all(self):
        """A URL is how a machine reaches a page, not how a reader is told
        where a fact came from."""
        for citation in self._citations():
            self.assertNotIn("url", citation)
            for field in ("source", "locator", "quote"):
                self.assertNotIn("http", str(citation.get(field, "")))

    def test_the_research_question_is_not_presented_as_a_quote(self):
        for citation in self._citations():
            self.assertNotIn("What is the TAM", citation["quote"])
            self.assertNotIn("Question 6", citation["quote"])

    def test_the_source_reads_as_the_channel_and_topic(self):
        sources = [c["source"] for c in self._citations()]
        self.assertIn(
            "Web Research — Company Story, Industry Research & Investment "
            "Thesis", sources)


class TheSourceTitleIsReadable(Phase1Base):

    def test_a_batch_title_drops_the_question_that_was_asked(self):
        title = _sanitize_source_title(
            "Web Research, Batch 2: Company Basics & Identity, Question 3")
        self.assertEqual(title, "Web Research — Company Basics & Identity")

    def test_a_url_only_detail_is_named_for_the_web(self):
        """However the row's source_type is stored, a bare link was found by
        searching, and that is what the reader is told."""
        source, _loc, _quote, _url = _parse_source_detail(
            "https://tracxn.com/d/companies/healthifyme",
            default_channel="Company Research")
        self.assertEqual(source, "Web Research")

    def test_a_hyphenated_url_survives_intact(self):
        """The pattern excluded hyphens, so it stopped at the first one."""
        link = "https://leapfroginvest.com/news/healthify-raises-20m-in-cash/"
        _s, _l, _q, url = _parse_source_detail(link)
        self.assertEqual(url, link)

    def test_no_source_title_is_ever_a_link(self):
        for detail in (GROUNDING,
                       "https://tracxn.com/d/companies/healthifyme",
                       "https://leapfroginvest.com/news/a-b-c/"):
            source, _l, _q, _u = _parse_source_detail(detail)
            self.assertFalse(source.startswith("http"), source[:60])
            self.assertTrue(source)


class AValueReadsLikeSomeoneWouldSayIt(Phase1Base):

    def test_a_unit_that_is_not_a_unit_is_not_printed(self):
        """"Count" and "Score" say what the number IS."""
        self.assertEqual(_display_value("3", "Count"), "3")
        self.assertEqual(_display_value("7.5", "Score"), "7.5")
        self.assertEqual(_display_value("Excellent", "Band"), "Excellent")

    def test_tight_units_sit_against_the_number(self):
        self.assertEqual(_display_value("37.6", "%"), "37.6%")
        self.assertEqual(_display_value("4", "x"), "4x")

    def test_a_real_unit_keeps_its_space(self):
        self.assertEqual(_display_value("15.0", "Years"), "15 Years")
        self.assertEqual(_display_value("28.5", "US$ Bn"), "28.5 US$ Bn")

    def test_a_float_is_rounded_once_here(self):
        self.assertEqual(_display_value("28.1747776", "INR Cr"),
                         "28.17 INR Cr")
        self.assertEqual(_display_value("-25.127218", "%"), "-25.1%")

    def test_no_awkward_unit_reaches_the_payload(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        found = []

        def walk(node):
            if node.get("isTerminal"):
                display = (node.get("value") or {}).get("display")
                if display:
                    found.append((node["ref"], display))
            for child in (node.get("subitems") or node.get("children") or []):
                walk(child)

        for cat in response.data["categories"]:
            walk(cat)
        for ref, display in found:
            for awkward in (" Count", " Score", " Band", " x", " %"):
                self.assertFalse(display.endswith(awkward),
                                 f"{ref} displays {display!r}")


class NoLinkReachesTheReader(Phase1Base):

    def test_the_payload_carries_no_citation_url_anywhere(self):
        import json

        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        citations = []

        def walk(node):
            if isinstance(node, dict):
                if "citations" in node and isinstance(node["citations"], list):
                    citations.extend(node["citations"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(json.loads(json.dumps(response.data, default=str)))
        self.assertTrue(citations, "the fixture must produce citations")
        for citation in citations:
            self.assertNotIn("url", citation)
            self.assertFalse(str(citation.get("source", "")).startswith("http"))
