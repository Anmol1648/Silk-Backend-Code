"""Two rows, one company: what a list write must not let through.

A live profile listed two competitors -- different names, real companies
both -- carrying the same revenue figure and the same sentence of
description. One of them had been researched; the other had that research
copied onto it, and the copy was then presented as a finding about a company
nobody had looked up.

The rule is at the writer, so it holds for every section that stores a list
of entities and for every company created after this one. Nothing here
matches on a name, a sector or a figure that belongs to one company.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile.section_writer import (
    _dedupe_entity_rows, update_section_from_data,
)
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import serialize_section


def _bootstrap():
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Rival User")
    company = Company.objects.create(
        tenant_id=tenant.id, name="Field Co", created_by=user)
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    return tenant, user, company, get_or_create_profile(company, user=user)


class OneRowPerName(TestCase):

    def test_a_repeated_name_becomes_one_row(self):
        rows = _dedupe_entity_rows("competitors", [
            {"name": "Rival Ltd", "revenue": 30},
            {"name": "rival  ltd", "website": "rival.example"}])
        self.assertEqual(len(rows), 1)

    def test_the_merge_fills_blanks_rather_than_discarding_research(self):
        rows = _dedupe_entity_rows("competitors", [
            {"name": "Rival Ltd", "revenue": 30},
            {"name": "Rival Ltd", "website": "rival.example"}])
        self.assertEqual(rows[0]["revenue"], 30)
        self.assertEqual(rows[0]["website"], "rival.example")

    def test_the_first_answer_wins_where_both_state_one(self):
        """Merging is not arbitration: the later row does not overwrite a
        value the earlier one already gave."""
        rows = _dedupe_entity_rows("competitors", [
            {"name": "Rival Ltd", "revenue": 30},
            {"name": "Rival Ltd", "revenue": 90}])
        self.assertEqual(rows[0]["revenue"], 30)

    def test_two_real_names_stay_two_rows(self):
        rows = _dedupe_entity_rows("competitors", [
            {"name": "First Ltd", "description": "Makes tyres."},
            {"name": "Second Ltd", "description": "Sells tyres online."}])
        self.assertEqual(len(rows), 2)

    def test_an_unnamed_row_is_not_merged_into_another_unnamed_one(self):
        rows = _dedupe_entity_rows("competitors", [
            {"description": "One."}, {"description": "Two."}])
        self.assertEqual(len(rows), 2)


class ACopiedDescriptionDoesNotBecomeASecondFinding(TestCase):

    def _rows(self):
        shared = "A national manufacturer selling through 3,000 dealers."
        return _dedupe_entity_rows("competitors", [
            {"name": "First Ltd", "description": shared, "revenue": 4200,
             "fy_year": 2025, "website": "first.example"},
            {"name": "Second Ltd", "description": shared, "revenue": 4200,
             "fy_year": 2025, "website": "second.example"}])

    def test_both_companies_are_still_listed(self):
        """The rival is real; only the copied claims are not."""
        rows = self._rows()
        self.assertEqual([r["name"] for r in rows],
                         ["First Ltd", "Second Ltd"])

    def test_the_researched_row_is_untouched(self):
        first = self._rows()[0]
        self.assertEqual(first["revenue"], 4200)
        self.assertIn("dealers", first["description"])

    def test_the_copied_description_is_not_repeated(self):
        second = self._rows()[1]
        self.assertFalse(second.get("description"))

    def test_a_figure_copied_with_it_is_not_asserted(self):
        second = self._rows()[1]
        self.assertIsNone(second.get("revenue"))
        self.assertIsNone(second.get("fy_year"))

    def test_what_identifies_the_row_survives(self):
        second = self._rows()[1]
        self.assertEqual(second["name"], "Second Ltd")
        self.assertEqual(second["website"], "second.example")

    def test_a_distinct_figure_is_kept(self):
        """Only the values identical to the row copied from are dropped."""
        shared = "A national manufacturer."
        rows = _dedupe_entity_rows("competitors", [
            {"name": "First Ltd", "description": shared, "revenue": 4200},
            {"name": "Second Ltd", "description": shared, "revenue": 900}])
        self.assertEqual(rows[1]["revenue"], 900)

    def test_two_blank_descriptions_are_not_a_copy(self):
        rows = _dedupe_entity_rows("competitors", [
            {"name": "First Ltd", "revenue": 10},
            {"name": "Second Ltd", "revenue": 20}])
        self.assertEqual(rows[1]["revenue"], 20)

    def test_the_same_rule_covers_any_entity_list(self):
        """It lives at the writer, so news and people get it too."""
        shared = "Raised a round from an unnamed investor."
        rows = _dedupe_entity_rows("recent_news", [
            {"name": "One", "summary": shared},
            {"name": "Two", "summary": shared}])
        self.assertFalse(rows[1].get("summary"))


class ItHoldsThroughAnActualSave(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_the_saved_section_shows_each_company_once(self):
        shared = "A national manufacturer selling through dealers."
        update_section_from_data(self.profile, "competitors", [
            {"name": "First Ltd", "description": shared, "revenue": 4200},
            {"name": "First Ltd", "description": shared, "revenue": 4200},
            {"name": "Second Ltd", "description": shared, "revenue": 4200}],
            user=self.user)
        rows = serialize_section(self.profile, "competitors")["data"]
        self.assertEqual([r["name"] for r in rows],
                         ["First Ltd", "Second Ltd"])
        self.assertEqual(rows[0]["revenue"], 4200)
        self.assertIsNone(rows[1]["revenue"])
