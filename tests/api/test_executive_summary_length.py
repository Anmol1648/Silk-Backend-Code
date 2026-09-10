"""The executive summary is a summary.

The profile writes at length on purpose — a founder reads it end to end. This
block is the opposite: the lines a partner reads before deciding whether to
open anything else. It was quoting both profile sections verbatim, which for a
real company ran to two thousand characters of marketing prose, and a summary
that long is not a summary, it is the document again.

The budget is applied per FIELD, not per section, because `investment_thesis`
joins the opportunity and the leadership read into one string: budget the join
and the opportunity spends everything, and the team assessment — half of what
the section exists for — never appears.
"""
from django.test import TestCase

from fundos.assessment.phase1 import NARRATIVE_SOURCES, condense
from tests.api.test_fundraising_phase1 import Phase1Base

LONG_OPENING = (
    "Zyla is an AI-based healthcare management platform dedicated to "
    "improving health outcomes through personalized care. The company "
    "provides a full spectrum of health and wellness solutions covering "
    "preventive and chronic conditions. It partners with insurers to reduce "
    "hospitalisations and with corporates to run wellness programmes. The "
    "platform is built on an advanced AI engine analysing patient history. "
    "It also features an NLP chatbot in English and Hindi."
)


class CondenseKeepsWholeSentences(TestCase):

    def test_it_takes_the_opening_sentences_and_stops(self):
        out = condense(LONG_OPENING, 2, 300)
        self.assertTrue(out.startswith("Zyla is an AI-based"))
        self.assertTrue(out.endswith("conditions."))
        self.assertNotIn("NLP chatbot", out)

    def test_it_never_cuts_mid_sentence(self):
        for limit in (80, 150, 300, 1000):
            out = condense(LONG_OPENING, 3, limit)
            self.assertTrue(out.endswith((".", "!", "?")), limit)

    def test_it_never_appends_an_ellipsis(self):
        self.assertNotIn("...", condense(LONG_OPENING, 1, 50))
        self.assertNotIn("…", condense(LONG_OPENING, 1, 50))

    def test_the_first_sentence_survives_a_budget_it_exceeds(self):
        """A summary of nothing is worse than one long first line."""
        out = condense(LONG_OPENING, 2, 10)
        self.assertTrue(out.startswith("Zyla is an AI-based"))
        self.assertTrue(out.endswith("personalized care."))

    def test_a_decimal_is_not_a_sentence_boundary(self):
        """The prose is full of "USD 28.5 billion" and "37.6% CAGR"."""
        text = ("The market reaches USD 28.5 billion by 2030 at 37.6% CAGR. "
                "A second sentence follows.")
        out = condense(text, 1, 500)
        self.assertEqual(out,
                         "The market reaches USD 28.5 billion by 2030 at "
                         "37.6% CAGR.")

    def test_blank_input_stays_blank(self):
        self.assertEqual(condense("", 2, 300), "")
        self.assertEqual(condense(None, 2, 300), "")


class TheSummaryStaysShort(Phase1Base):

    def _narrative(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        return response.data["summary"]["executive_summary"]["narrative"]

    def setUp(self):
        super().setUp()
        from fundos.profile.models import CompanyProfile, ProfileSection

        profile, _ = CompanyProfile.objects.get_or_create(
            company=self.company,
            defaults={"tenant_id": self.company.tenant_id,
                      "status": "generated"})
        ProfileSection.objects.update_or_create(
            profile=profile, section_key="company_overview",
            defaults={"is_active": True, "content": "",
                      "tenant_id": self.company.tenant_id,
                      "structured": {"description_of_business": LONG_OPENING}})
        ProfileSection.objects.update_or_create(
            profile=profile, section_key="investment_thesis",
            defaults={
                "is_active": True, "content": "",
                "tenant_id": self.company.tenant_id,
                "structured": {
                    "opportunity_explanation": (
                        "The investment case is attractive on market position "
                        "and outcomes. The Indian digital health market "
                        "reaches USD 28.5 billion by 2030 at 37.6% CAGR. A "
                        "third sentence that must not appear in the summary."),
                    "leadership_assessment": (
                        "The team combines healthcare, technology and "
                        "operations depth. A second leadership sentence that "
                        "must be cut."),
                }})

    def test_it_is_one_paragraph_not_a_list_of_them(self):
        """Where the prose lives is our problem, not the reader's."""
        narrative = self._narrative()
        self.assertIsInstance(narrative, str)
        self.assertNotIn("\n", narrative)

    def test_the_whole_narrative_fits_in_about_six_lines(self):
        narrative = self._narrative()
        self.assertLessEqual(len(narrative), 900,
                             f"narrative is {len(narrative)} chars:\n"
                             f"{narrative}")

    def test_both_sections_still_speak(self):
        narrative = self._narrative()
        self.assertIn("AI-based healthcare management", narrative)
        self.assertIn("investment case", narrative)

    def test_the_team_read_is_not_starved_by_the_opportunity(self):
        """The defect a per-section budget would reintroduce."""
        self.assertIn("healthcare, technology and operations",
                      self._narrative())

    def test_the_tail_of_each_field_is_dropped(self):
        blob = self._narrative()
        self.assertNotIn("must not appear", blob)
        self.assertNotIn("must be cut", blob)
        self.assertNotIn("NLP chatbot", blob)

    def test_it_ends_on_a_whole_sentence(self):
        narrative = self._narrative()
        self.assertTrue(narrative.endswith((".", "!", "?")), narrative[-60:])

    def test_the_budget_is_declared_per_field(self):
        for _key, fields in NARRATIVE_SOURCES:
            for field in fields:
                self.assertEqual(len(field), 3, field)
                _name, sentences, chars = field
                self.assertGreater(sentences, 0)
                self.assertGreater(chars, 0)
