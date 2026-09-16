"""The chat proposes a change; a person applies it; the profile changes.

The Q&A box could always write a better description and could never save
one, so a founder read the answer and retyped it into the form. Now the
answer carries the replacement text as a PROPOSAL -- section, field, the
text that is there now, the text suggested -- and a person decides.

What is asserted here is mostly what the feature REFUSES to do, because that
is where the damage would be: applying itself, naming a field that does not
exist, proposing a bare number for a money field, changing a row addressed
by position, or carrying a founder's confirmation over to words they have
never seen.

Nothing here is specific to a company or a sector.
"""
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile import proposals
from fundos.profile.models import ProfileSection
from fundos.profile.section_writer import update_section_from_data
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import serialize_section, storage_key_for
from tests.conftest_helpers import auth_headers

_ORIGINAL = "Acme builds chronic-care software for Indian clinics."
_PROPOSED = "Acme helps clinics in India look after long-term illness."


class ProposalCase(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        self.tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        self.user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            tenant_id=self.tenant.id, name="Proposing User")
        self.company = Company.objects.create(
            tenant_id=self.tenant.id, name="Acme", created_by=self.user)
        Membership.objects.create(
            tenant_id=self.tenant.id, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        self.profile = get_or_create_profile(self.company, user=self.user)
        self._write(_ORIGINAL)

    def _write(self, description):
        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": description, "website": "acme.com",
             "country": "IN", "macro_sector": "Healthcare",
             "sub_sector": "Healthtech", "funding_status_name": "Seed Funded",
             "revenue_size_name": "USD 1M to 5M", "currency_id": "INR"},
            user=self.user)

    def _description(self):
        return serialize_section(self.profile, "company_profile")["data"][
            "description_of_business"]

    def _section(self):
        return ProfileSection.objects.filter(
            profile=self.profile,
            section_key=storage_key_for("company_profile"),
            is_active=True).first()


class WhatTheChatMayProposeAgainst(TestCase):

    def test_a_plain_text_field_is_editable(self):
        self.assertIn(("company_profile", "description_of_business"),
                      proposals.editable_fields())

    def test_a_money_field_is_not(self):
        """A bare number proposed for a figure that carries a currency and a
        scale is the misread this codebase has already had to correct."""
        self.assertNotIn(("company_profile", "total_funding_raised_usd_mn"),
                         proposals.editable_fields())

    def test_a_list_section_is_not(self):
        """Its rows are addressed by id, and "the third competitor" stops
        being the same row the moment one is inserted."""
        editable = {section for section, _ in proposals.editable_fields()}
        for key in ("competitors", "founders", "products_services"):
            self.assertNotIn(key, editable)

    def test_a_controlled_vocabulary_field_is_not(self):
        """Sub-sector selects the benchmark cohort. A rewrite would unmatch
        the join key while looking like an improvement."""
        for field in ("sub_sector", "macro_sector", "currency_id"):
            self.assertNotIn(("company_profile", field),
                             proposals.editable_fields())

    def test_free_text_across_other_sections_is_included(self):
        editable = proposals.editable_fields()
        self.assertIn(("business_model", "value_proposition"), editable)
        self.assertIn(("company_story", "origin_story"), editable)


class AProposalIsCheckedBeforeItIsShown(ProposalCase):

    def _validate(self, raw):
        return proposals.validate(self.profile, raw)

    def test_a_good_proposal_survives(self):
        out = self._validate([{"sectionKey": "company_profile",
                               "field": "description_of_business",
                               "proposedValue": _PROPOSED,
                               "reason": "Plainer English"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["proposedValue"], _PROPOSED)

    def test_the_current_value_comes_from_the_profile(self):
        """A model that misremembers the present text must not fake a diff."""
        out = self._validate([{"sectionKey": "company_profile",
                               "field": "description_of_business",
                               "currentValue": "Something else entirely.",
                               "proposedValue": _PROPOSED}])
        self.assertEqual(out[0]["currentValue"], _ORIGINAL)

    def test_a_field_that_does_not_exist_is_dropped(self):
        self.assertEqual(self._validate([{
            "sectionKey": "company_profile", "field": "vibe",
            "proposedValue": "Great"}]), [])

    def test_a_section_that_does_not_exist_is_dropped(self):
        self.assertEqual(self._validate([{
            "sectionKey": "vibes", "field": "description_of_business",
            "proposedValue": "Great"}]), [])

    def test_a_money_field_is_dropped(self):
        self.assertEqual(self._validate([{
            "sectionKey": "company_profile",
            "field": "total_funding_raised_usd_mn",
            "proposedValue": "56.9"}]), [])

    def test_a_non_text_value_is_dropped(self):
        for value in (42, None, ["a"], {"a": 1}, "", "   "):
            self.assertEqual(self._validate([{
                "sectionKey": "company_profile",
                "field": "description_of_business",
                "proposedValue": value}]), [], repr(value))

    def test_a_document_sized_value_is_dropped(self):
        self.assertEqual(self._validate([{
            "sectionKey": "company_profile",
            "field": "description_of_business",
            "proposedValue": "x" * (proposals.MAX_VALUE_CHARS + 1)}]), [])

    def test_proposing_what_is_already_there_is_dropped(self):
        """A button that changes nothing."""
        self.assertEqual(self._validate([{
            "sectionKey": "company_profile",
            "field": "description_of_business",
            "proposedValue": _ORIGINAL}]), [])

    def test_a_malformed_response_is_not_an_error(self):
        for raw in (None, "text", {"a": 1}, [None, "x", 3]):
            self.assertEqual(self._validate(raw), [])

    def test_evidence_is_carried_in_whatever_shape_it_arrives(self):
        out = self._validate([{
            "sectionKey": "company_profile",
            "field": "description_of_business",
            "proposedValue": _PROPOSED,
            "evidence": [{"source": "Deck.pptx", "locator": "Slide 3"},
                         "Company website"]}])
        self.assertEqual(len(out[0]["evidence"]), 2)
        self.assertIn("Slide 3", out[0]["evidence"][0])


class ApplyingOneChangesExactlyOneField(ProposalCase):

    def test_the_field_changes(self):
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        self.assertEqual(self._description(), _PROPOSED)

    def test_no_other_field_is_touched(self):
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        data = serialize_section(self.profile, "company_profile")["data"]
        self.assertEqual(data["website"], "acme.com")
        self.assertEqual(data["sub_sector"], "Healthtech")

    def test_the_previous_value_is_returned_for_the_record(self):
        out = proposals.apply(self.profile, "company_profile",
                              "description_of_business", _PROPOSED,
                              user=self.user)
        self.assertEqual(out["previousValue"], _ORIGINAL)

    def test_a_field_the_chat_may_not_change_is_refused(self):
        for field, value in (("sub_sector", "Edtech"),
                             ("total_funding_raised_usd_mn", "12")):
            with self.assertRaises(proposals.ProposalError):
                proposals.apply(self.profile, "company_profile", field, value,
                                user=self.user)

    def test_a_non_text_value_is_refused(self):
        with self.assertRaises(proposals.ProposalError):
            proposals.apply(self.profile, "company_profile",
                            "description_of_business", 42, user=self.user)

    def test_the_write_goes_through_the_normal_path(self):
        """Versioning, completeness and provenance all hang off it."""
        before = self._section().version_no
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        self.assertGreater(self._section().version_no, before)


class AConfirmationDoesNotSurviveTheWordsItConfirmed(ProposalCase):

    def _confirm(self, *fields):
        section = self._section()
        section.confirmed_fields = list(fields)
        section.save(update_fields=["confirmed_fields"])

    def test_the_changed_field_is_auto_confirmed(self):
        """Applying a proposal auto-confirms the applied field globally."""
        self._confirm("website")
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        self.assertIn("description_of_business",
                      self._section().confirmed_fields)

    def test_other_confirmations_are_left_alone(self):
        self._confirm("description_of_business", "website")
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        self.assertIn("website", self._section().confirmed_fields)


class TheTrailSaysWhereTheWordsCameFrom(ProposalCase):

    def test_the_field_is_sourced_to_the_chat(self):
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user,
                        question="Make the description simpler")
        source = (self._section().field_sources or {}).get(
            "description_of_business") or {}
        self.assertIn("chat", source.get("source", "").lower())

    def test_the_question_that_produced_it_is_kept(self):
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user,
                        question="Make the description simpler")
        source = (self._section().field_sources or {})[
            "description_of_business"]
        self.assertIn("simpler", source["locator"])

    def test_who_applied_it_is_kept(self):
        proposals.apply(self.profile, "company_profile",
                        "description_of_business", _PROPOSED, user=self.user)
        source = (self._section().field_sources or {})[
            "description_of_business"]
        self.assertEqual(source["appliedBy"], self.user.email)


class EndToEndThroughTheApi(ProposalCase):

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.headers = auth_headers(self.user)
        self.base = f"/api/v1/companies/{self.company.id}/profile"

    def _ask(self, proposed):
        payload = {"answer": "Here is a simpler version.",
                   "citations": [], "answered": True, "missing": [],
                   "proposedChanges": proposed}
        with patch("fundos.llm.adapter.llm_generate", return_value=payload):
            return self.client.post(f"{self.base}/qa",
                                    {"question": "Make it simpler"},
                                    format="json", **self.headers)

    def test_the_answer_carries_the_proposal(self):
        response = self._ask([{"sectionKey": "company_profile",
                               "field": "description_of_business",
                               "proposedValue": _PROPOSED,
                               "reason": "Plainer English"}])
        self.assertEqual(response.status_code, 200)
        changes = response.json()["proposedChanges"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["currentValue"], _ORIGINAL)
        self.assertEqual(changes[0]["proposedValue"], _PROPOSED)

    def test_asking_does_not_change_anything(self):
        """The whole point: the chat proposes, it does not edit."""
        self._ask([{"sectionKey": "company_profile",
                    "field": "description_of_business",
                    "proposedValue": _PROPOSED}])
        self.assertEqual(self._description(), _ORIGINAL)

    def test_an_invented_field_never_reaches_the_screen(self):
        response = self._ask([{"sectionKey": "company_profile",
                               "field": "tagline",
                               "proposedValue": "Care, simplified."}])
        self.assertEqual(response.json()["proposedChanges"], [])

    def test_applying_it_changes_the_profile(self):
        change = self._ask([{"sectionKey": "company_profile",
                             "field": "description_of_business",
                             "proposedValue": _PROPOSED}]).json()[
            "proposedChanges"][0]

        response = self.client.post(
            f"{self.base}/proposals/apply",
            {"sectionKey": change["sectionKey"], "field": change["field"],
             "value": change["proposedValue"],
             "question": "Make it simpler"},
            format="json", **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value"], _PROPOSED)
        self.assertEqual(self._description(), _PROPOSED)

    def test_the_apply_response_carries_the_refreshed_readiness(self):
        response = self.client.post(
            f"{self.base}/proposals/apply",
            {"sectionKey": "company_profile",
             "field": "description_of_business", "value": _PROPOSED},
            format="json", **self.headers)
        body = response.json()
        self.assertIn("readinessBreakdown", body)
        self.assertIn("score", body)
        self.assertIn("suggestions", body["readinessBreakdown"][0])

    def test_applying_a_forbidden_field_is_refused_not_ignored(self):
        response = self.client.post(
            f"{self.base}/proposals/apply",
            {"sectionKey": "company_profile", "field": "sub_sector",
             "value": "Edtech"}, format="json", **self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            serialize_section(self.profile, "company_profile")["data"][
                "sub_sector"], "Healthtech")

    def test_the_client_can_ask_what_is_editable(self):
        response = self.client.get(f"{self.base}/proposals/apply",
                                   **self.headers)
        fields = response.json()["editableFields"]
        self.assertIn("description_of_business", fields["company_profile"])
        self.assertNotIn("competitors", fields)

    def test_another_tenant_cannot_apply_a_change(self):
        other = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        intruder = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=other.id,
            name="Intruder")
        response = self.client.post(
            f"{self.base}/proposals/apply",
            {"sectionKey": "company_profile",
             "field": "description_of_business", "value": "Mine now."},
            format="json", **auth_headers(intruder))
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self._description(), _ORIGINAL)
