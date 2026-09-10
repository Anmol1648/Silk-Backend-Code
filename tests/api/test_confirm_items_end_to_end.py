"""Confirming one item, over the real PATCH endpoint.

The counting rules are unit-tested elsewhere. What this proves is the part
that was previously only reasoned about: that a body carrying nothing but
`confirmed_fields` reaches the database, moves the breakdown, and — the risk
worth a test of its own — does NOT blank the section it was sent to.

That risk is real rather than theoretical. The PATCH handler falls through to
the legacy branch whenever the body has no `data` key, and that branch passes
`content=None, structured=None` straight into `update_section`. It is safe
only because `update_section` writes a field solely when a value was supplied.
A confirm should never be able to erase the thing it confirms, so the
behaviour is pinned here rather than left resting on that reading.
"""
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status

from tests.conftest_helpers import auth_headers, make_world


class ConfirmingOneItem(TestCase):

    SECTION = "products_services"

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.section_writer import update_section_from_data
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()

        self.world = make_world()
        self.company = self.world["a"]["company"]
        self.co = str(self.company.id)
        self.headers = auth_headers(self.world["a"]["founder"])
        self.profile = get_or_create_profile(
            self.company, user=self.world["a"]["founder"])

        # Three products, written through the real writer so they are given
        # ids exactly as a generation run would give them.
        update_section_from_data(
            self.profile, self.SECTION,
            [{"name": "Online Tyre Retail", "category": "B2C E-commerce",
              "description": "Tyres for cars, bikes and commercial vehicles."},
             {"name": "Motozee Stores", "category": "B2C Retail",
              "description": "Company-owned retail network."},
             {"name": "Dealer Financing", "category": "B2B Financial Service",
              "description": "Credit for the dealer network."}],
            user=self.world["a"]["founder"])

    # -- helpers ---------------------------------------------------------
    def _profile(self):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _items(self):
        return self._profile()["sections"][self.SECTION]["data"]

    def _entry(self, body=None):
        body = body if body is not None else self._profile()
        return next(b for b in body["readinessBreakdown"]
                    if b["sectionKey"] == self.SECTION)

    def _confirm(self, ids):
        return self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/{self.SECTION}",
            {"confirmed_fields": ids},
            content_type="application/json", **self.headers)

    # -- the contract ----------------------------------------------------
    def test_every_item_arrives_with_an_id(self):
        items = self._items()
        self.assertEqual(len(items), 3)
        ids = [i["id"] for i in items]
        self.assertTrue(all(ids), "an item with no id cannot be confirmed")
        self.assertEqual(len(set(ids)), 3, "ids must be distinct")

    def test_nothing_is_confirmed_to_begin_with(self):
        entry = self._entry()
        self.assertEqual((entry["fields"], entry["confirmed"]), (3, 0))

    def test_confirming_one_item_confirms_exactly_one(self):
        items = self._items()
        response = self._confirm([items[1]["id"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        entry = self._entry()
        self.assertEqual(entry["confirmed"], 1)
        self.assertEqual(entry["fields"], 3)

    def test_the_patch_response_already_carries_the_new_numbers(self):
        """The progress bar updates from the PATCH, with no second GET."""
        items = self._items()
        body = self._confirm([items[0]["id"]]).json()
        self.assertIn("score", body)
        self.assertIn("readinessBreakdown", body)
        self.assertEqual(self._entry(body)["confirmed"], 1)

    def test_confirming_a_second_item_adds_to_the_first(self):
        items = self._items()
        self._confirm([items[0]["id"]])
        # The client sends the WHOLE set, because the list is replaced.
        self._confirm([items[0]["id"], items[2]["id"]])
        self.assertEqual(self._entry()["confirmed"], 2)

    def test_the_list_is_replaced_not_appended(self):
        """Sending one id after two must leave one confirmed, not three."""
        items = self._items()
        self._confirm([items[0]["id"], items[1]["id"]])
        self.assertEqual(self._entry()["confirmed"], 2)
        self._confirm([items[2]["id"]])
        self.assertEqual(self._entry()["confirmed"], 1)

    def test_un_confirming_everything_returns_to_zero(self):
        items = self._items()
        self._confirm([items[0]["id"]])
        self._confirm([])
        self.assertEqual(self._entry()["confirmed"], 0)

    def test_an_id_from_another_section_confirms_nothing(self):
        self._confirm(["not-an-id-in-this-section"])
        self.assertEqual(self._entry()["confirmed"], 0)

    # -- the risk this file exists for -----------------------------------
    def test_a_confirm_only_patch_does_not_blank_the_section(self):
        """The handler passes content=None, structured=None on this path."""
        before = self._items()
        self._confirm([before[0]["id"]])
        after = self._items()

        self.assertEqual(len(after), 3, "the products were erased")
        self.assertEqual([i["name"] for i in after],
                         [i["name"] for i in before])
        self.assertEqual([i["description"] for i in after],
                         [i["description"] for i in before])

    def test_the_ids_survive_a_confirm(self):
        """An id that changed on write would strand the confirmation it was
        just given."""
        before = [i["id"] for i in self._items()]
        self._confirm([before[0]])
        self.assertEqual([i["id"] for i in self._items()], before)

    def test_the_score_moves_when_an_item_is_confirmed(self):
        start = self._profile()["score"]
        items = self._items()
        self._confirm([i["id"] for i in items])
        self.assertGreater(self._profile()["score"], start)


class ConfirmingAFieldStillWorks(TestCase):
    """Object sections confirm by field name, and that is unchanged."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.section_writer import update_section_from_data
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        update_section_from_data(
            profile, "company_profile",
            {"description_of_business": "A B2B marketplace for tyres.",
             "website": "https://tyreplex.com", "country": "IN"},
            user=self.world["a"]["founder"])

    def _entry(self):
        body = self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers).json()
        return next(b for b in body["readinessBreakdown"]
                    if b["sectionKey"] == "company_profile")

    def test_a_field_name_confirms_that_field(self):
        response = self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/company_profile",
            {"confirmed_fields": ["description_of_business", "website"]},
            content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._entry()["confirmed"], 2)

    def test_the_denominator_is_what_the_form_shows(self):
        """Twelve controls, not the fifteen keys `data` carries."""
        self.assertEqual(self._entry()["fields"], 12)

    def test_a_display_companion_cannot_be_confirmed_into_the_count(self):
        self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/company_profile",
            {"confirmed_fields": ["total_funding_raised_display"]},
            content_type="application/json", **self.headers)
        self.assertEqual(self._entry()["confirmed"], 0)


class ConfirmingFiveFoundersByPosition(TestCase):
    """The reported bug, over the real endpoint.

    A client sent `["0","1","2","3","4"]` for five founders, got a 200, and
    the breakdown came back `confirmed: 0`. The confirmations were stored and
    counted as nothing, so the score never moved and there was no error to
    explain why.
    """

    SECTION = "founders"

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.section_writer import update_section_from_data
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        self.profile = get_or_create_profile(
            self.world["a"]["company"], user=self.world["a"]["founder"])

        update_section_from_data(
            self.profile, self.SECTION,
            [{"name": "Puneet Bhaskar", "role": "Co-Founder & CEO",
              "background": "Formerly Droom and Snapdeal.",
              "is_founder": True},
             {"name": "Jiveshwar Sharma", "role": "Founder & CTO",
              "background": "Ex-Tech Head at Limetray.", "is_founder": True},
             {"name": "Rupendra Pratap Singh", "role": "Founder & COO",
              "background": "IIT Delhi.", "is_founder": True},
             {"name": "Nikhil Kalra", "role": "Co-Founder, Product & Growth",
              "background": "Ex-Product Head at Zigwheels.",
              "is_founder": True},
             {"name": "Sunish Kumar", "role": "Co-Founder",
              "background": "Listed as a director in filings.",
              "is_founder": True}],
            user=self.world["a"]["founder"])

    def _profile(self):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _entry(self, body=None):
        return next(b for b in (body or self._profile())["readinessBreakdown"]
                    if b["sectionKey"] == self.SECTION)

    def _confirm(self, keys):
        return self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/{self.SECTION}",
            {"confirmed_fields": keys},
            content_type="application/json", **self.headers)

    def test_five_positions_confirm_five_founders(self):
        response = self._confirm(["0", "1", "2", "3", "4"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        entry = self._entry()
        self.assertEqual(entry["fields"], 5)
        self.assertEqual(entry["populated"], 5)
        self.assertEqual(entry["confirmed"], 5)

    def test_the_patch_response_carries_the_same_five(self):
        entry = self._entry(self._confirm(["0", "1", "2", "3", "4"]).json())
        self.assertEqual(entry["confirmed"], 5)

    def test_the_totals_count_them(self):
        before = self._profile()["readinessTotals"]["confirmed"]
        self._confirm(["0", "1", "2", "3", "4"])
        after = self._profile()["readinessTotals"]
        self.assertEqual(after["confirmed"], before + 5)

    def test_a_fully_confirmed_section_counts_as_confirmed(self):
        self._confirm(["0", "1", "2", "3", "4"])
        body = self._profile()
        self.assertGreaterEqual(
            body["readinessTotals"]["sectionsConfirmed"], 1)

    def test_the_score_moves(self):
        before = self._profile()["score"]
        self._confirm(["0", "1", "2", "3", "4"])
        self.assertGreater(self._profile()["score"], before)

    def test_confirming_two_positions_confirms_two(self):
        self._confirm(["0", "3"])
        self.assertEqual(self._entry()["confirmed"], 2)

    def test_the_ids_are_still_the_address_that_works(self):
        items = self._profile()["sections"][self.SECTION]["data"]
        self._confirm([items[0]["id"], items[1]["id"]])
        self.assertEqual(self._entry()["confirmed"], 2)

    def test_an_id_and_its_own_position_are_one_founder(self):
        items = self._profile()["sections"][self.SECTION]["data"]
        self._confirm(["0", items[0]["id"]])
        self.assertEqual(self._entry()["confirmed"], 1)
