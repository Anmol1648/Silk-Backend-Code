"""Every category says what it evaluates, in both endpoints.

A screen that shows "Sector Attractiveness 3.0" before the reader knows what
the category measures is showing a number without its question.
"""
from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base


class CategoryDescriptions(Phase1Base):

    CODES = {"A", "B", "C", "D", "E", "F", "G"}

    def test_every_category_carries_exactly_two_lines(self):
        from fundos.assessment import phase1

        for code in self.CODES:
            lines = phase1.category_description(code)
            self.assertEqual(len(lines), 2, f"category {code}")
            for line in lines:
                self.assertTrue(line.strip())
                # Two SHORT lines: a paragraph defeats the purpose.
                self.assertLessEqual(len(line), 110, f"{code}: {line}")

    def test_an_unknown_code_returns_nothing_rather_than_guessing(self):
        from fundos.assessment import phase1

        self.assertEqual(phase1.category_description("Z"), [])
        self.assertEqual(phase1.category_description(""), [])

    def test_the_v2_payload_describes_each_category(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for category in response.data["categories"]:
            self.assertEqual(len(category["description"]), 2,
                             category["code"])

    def test_the_phase1_scorecard_describes_each_category(self):
        card = self._get().data["dealScorecardData"]
        for category in card["categories"]:
            self.assertEqual(len(category["description"]), 2, category["ref"])

    def test_sub_items_carry_no_description(self):
        """A sub-item's own name already says what it covers."""
        card = self._get().data["dealScorecardData"]
        for category in card["categories"]:
            for sub in category["children"]:
                self.assertNotIn("description", sub)

    def test_both_endpoints_give_the_same_description(self):
        """One source of copy, so the two screens cannot drift apart."""
        url = f"/api/v1/companies/{self.company.id}/assessment"
        v2 = {c["code"]: c["description"]
              for c in self.client.get(url, **self.headers).data["categories"]}
        p1 = {c["ref"]: c["description"]
              for c in self._get().data["dealScorecardData"]["categories"]}

        for code in set(v2) & set(p1):
            self.assertEqual(v2[code], p1[code], code)
