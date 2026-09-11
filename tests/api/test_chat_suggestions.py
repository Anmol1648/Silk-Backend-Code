"""What the chat offers to ask, per section.

The Q&A box took a typed question and offered nothing, so a founder looking
at a readiness ring had to invent the question that would move it.

Every rule here reads the three numbers the readiness breakdown already
reports -- fields, populated, confirmed -- so a suggestion cannot describe a
state the section is not in, and nothing is specific to a company, a sector
or a section: the same rules run over all seventeen.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile import suggestions
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import serialize_profile


def _row(key="company_profile", fields=10, populated=10, confirmed=10,
         blank=None):
    return {"sectionKey": key, "fields": fields, "populated": populated,
            "confirmed": confirmed, "weight": 1.0,
            "blankFields": list(blank or [])}


def _kinds(row):
    return [s["kind"] for s in suggestions.attach([row])[0]["suggestions"]]


class ASectionWithNothingLeftToDoSaysNothing(TestCase):

    def test_a_complete_confirmed_section_suggests_nothing(self):
        self.assertEqual(_kinds(_row()), [])

    def test_the_key_is_present_even_when_empty(self):
        """A client should never have to test for absence."""
        rows = suggestions.attach([_row()])
        self.assertEqual(rows[0]["suggestions"], [])


class AGapDecidesWhatIsWorthAsking(TestCase):

    def test_blank_fields_produce_a_fill_suggestion(self):
        self.assertIn("fill", _kinds(_row(fields=12, populated=10,
                                          confirmed=10,
                                          blank=["website", "country"])))

    def test_unconfirmed_rows_produce_a_confirm_suggestion(self):
        self.assertIn("confirm", _kinds(_row(fields=5, populated=5,
                                             confirmed=0)))

    def test_an_empty_section_is_a_different_ask(self):
        kinds = _kinds(_row(fields=0, populated=0, confirmed=0))
        self.assertEqual(kinds, ["empty"])

    def test_a_section_with_both_gaps_offers_both(self):
        self.assertEqual(_kinds(_row(fields=12, populated=10, confirmed=1,
                                     blank=["website"])),
                         ["fill", "confirm"])

    def test_no_section_returns_more_than_the_cap(self):
        rows = suggestions.attach([_row(fields=40, populated=2, confirmed=0,
                                        blank=[f"f{i}" for i in range(38)])])
        self.assertLessEqual(len(rows[0]["suggestions"]),
                             suggestions.MAX_PER_SECTION)

    def test_an_empty_section_does_not_also_ask_for_confirmation(self):
        """There is nothing to confirm, and asking would be nonsense."""
        self.assertNotIn("confirm", _kinds(_row(fields=3, populated=0,
                                                confirmed=0)))


class TheBlanksAreNamedNotCounted(TestCase):

    def _text(self, blank):
        row = suggestions.attach([_row(fields=12, populated=10, confirmed=10,
                                       blank=blank)])[0]
        return row["suggestions"][0]

    def test_a_field_is_named_as_a_reader_says_it(self):
        self.assertIn("Description of business",
                      self._text(["description_of_business"])["label"])

    def test_an_acronym_is_shouted_not_capitalised(self):
        self.assertIn("URL", self._text(["website_url"])["label"])

    def test_two_blanks_read_as_a_list(self):
        label = self._text(["website", "country"])["label"]
        self.assertIn("Website and Country", label)

    def test_a_long_list_is_trimmed_with_a_count(self):
        label = self._text(["a_one", "b_two", "c_three", "d_four",
                            "e_five"])["label"]
        self.assertIn("2 more", label)

    def test_a_list_section_gap_has_no_field_to_name(self):
        """Its gap is a row that does not exist yet, and a row has no name
        until it does -- so the ask counts rather than inventing one."""
        row = suggestions.attach([_row(key="founders", fields=5, populated=3,
                                       confirmed=3, blank=[])])[0]
        text = row["suggestions"][0]
        self.assertIn("2", text["question"])
        self.assertTrue(text["label"])


class EverySuggestionIsSendableAndExplained(TestCase):

    def _all(self):
        rows = suggestions.attach([
            _row(key="company_profile", fields=12, populated=10, confirmed=1,
                 blank=["website", "country"]),
            _row(key="founders", fields=3, populated=3, confirmed=0),
            _row(key="news", fields=0, populated=0, confirmed=0)])
        return [s for row in rows for s in row["suggestions"]]

    def test_each_carries_something_the_chat_can_post(self):
        """Not necessarily a question -- "Summarise X so I can check it" is
        the right ask for an unconfirmed section, and a question mark would
        make it a worse one."""
        for item in self._all():
            text = item["question"].strip()
            self.assertGreater(len(text), 20, item)
            self.assertTrue(text.endswith(("?", ".")), item)

    def test_each_carries_a_chip_label(self):
        for item in self._all():
            self.assertTrue(item["label"].strip(), item)

    def test_each_says_why_it_was_suggested(self):
        for item in self._all():
            self.assertTrue(item["why"].strip(), item)

    def test_each_names_the_section_it_came_from(self):
        for item in self._all():
            self.assertTrue(item["sectionKey"])

    def test_the_section_is_named_as_a_reader_would_say_it(self):
        """Not `company_profile`, and not `8.1 Company Overview`."""
        item = self._all()[0]
        self.assertNotIn("company_profile", item["question"])
        self.assertNotIn("8.", item["question"])


class TheyRideOnTheReadinessBreakdown(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
            name="Chat User")
        company = Company.objects.create(
            tenant_id=tenant.id, name="Suggestion Co", created_by=user)
        Membership.objects.create(
            tenant_id=tenant.id, user=user, scope_type="company",
            scope_id=company.id, role="founder", status="active")
        self.company = company
        self.profile = get_or_create_profile(company, user=user)

    def _breakdown(self):
        return serialize_profile(self.profile,
                                 self.company)["readinessBreakdown"]

    def test_every_row_carries_the_key(self):
        rows = self._breakdown()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("suggestions", row)

    def test_a_fresh_profile_is_told_where_to_start(self):
        rows = self._breakdown()
        kinds = {s["kind"] for row in rows for s in row["suggestions"]}
        self.assertTrue(kinds <= {"empty", "fill", "confirm"})
        self.assertTrue(kinds, "a profile with nothing in it suggests nothing")

    def test_the_blanks_are_reported_beside_the_counts(self):
        for row in self._breakdown():
            self.assertIn("blankFields", row)
            self.assertIsInstance(row["blankFields"], list)

    def test_the_counts_are_unchanged_by_any_of_this(self):
        """Suggestions are added beside the numbers, never derived into
        them: the readiness score divides by these."""
        for row in self._breakdown():
            self.assertLessEqual(row["populated"], row["fields"])
            self.assertLessEqual(row["confirmed"], row["fields"])
