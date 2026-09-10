"""
Regression tests for the Company Profile / dashboard defect pass.

Each test here pins a specific defect that shipped, named in its docstring, so
a future change that reintroduces it fails loudly rather than quietly.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from tests.conftest_helpers import auth_headers

from fundos.core.models import Company, Deal, Membership, Tenant, User
from fundos.profile.models import (
    Competitor, Founder, FundingRound, ProfileSection,
)
from fundos.profile.section_writer import (
    SectionUpdateError, update_section_from_data,
)
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import (
    build_sections, serialize_section, storage_key_for,
)


class SeededTestCase(TestCase):
    """Base: the section registry drives what is writable, so it must exist."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)


def _bootstrap(name="Regression Co"):
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Regression User")
    company = Company.objects.create(
        tenant_id=tenant.id, name=name, created_by=user)
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    profile = get_or_create_profile(company, user=user)
    return tenant, user, company, profile


class NoBareEmptyObjectsTests(SeededTestCase):
    """The reported crash.

    A freshly created company produced leadership_detail = {"cfo": {}, ...}.
    The client's generic section renderer put that value in a text position,
    React raised minified error #31 ("object with keys {}"), and because the
    only error boundary sat above the router the whole SPA blanked.

    The contract this pins: no section's data may contain an EMPTY object
    anywhere. A section with no data must still expose its field structure,
    the way the list sections already send a template row.
    """

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def _empty_objects(self, node, path="data"):
        """Every path in `node` that holds an object with no keys."""
        found = []
        if isinstance(node, dict):
            if not node:
                return [path]
            for key, value in node.items():
                found += self._empty_objects(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                found += self._empty_objects(item, f"{path}[{i}]")
        return found

    def test_no_section_emits_an_empty_object(self):
        sections = build_sections(self.profile)
        offenders = []
        for key, section in sections.items():
            offenders += self._empty_objects(section["data"], key)
        self.assertEqual(
            offenders, [],
            "a section emitted a bare {} — the client renders section values "
            f"generically and cannot display one: {offenders}")

    # `leadership_detail` and `derived_multiples` left the generated-profile
    # contract when the Company Master Data Pipeline landed, so they are no
    # longer in `build_sections`. They are NOT deleted — the section rows are
    # deactivated and the builders retained, so re-activating one in admin
    # restores it. These tests therefore address them through
    # `serialize_section`, which is the path a re-activated section takes, and
    # so still protect the original defect (a bare `{}` blanking the SPA).

    def test_leadership_detail_exposes_person_fields_when_empty(self):
        data = serialize_section(self.profile, "leadership_detail")["data"]
        for slot in ("cfo", "ceo"):
            self.assertIn("name", data[slot])
            self.assertIn("background", data[slot])
        self.assertTrue(data["founders"], "founders must never be a bare []")

    def test_leadership_detail_falls_back_to_real_founder_rows(self):
        Founder.objects.create(
            tenant_id=self.tenant.id, profile=self.profile,
            name="Asha Rao", designation="CEO", experience="Ex-Flipkart",
            source="founder")
        data = serialize_section(self.profile, "leadership_detail")["data"]
        self.assertEqual(data["founders"][0]["name"], "Asha Rao")

    def test_empty_sections_do_not_count_as_complete(self):
        """The templates must not make an empty profile look filled in."""
        self.assertFalse(build_sections(self.profile)["founders"]["isComplete"],
                         "founders reported complete on an empty profile")
        for key in ("leadership_detail", "derived_multiples"):
            self.assertFalse(
                serialize_section(self.profile, key)["isComplete"],
                f"{key} reported complete on an empty profile")


class EveryEmittedSectionIsWritableTests(SeededTestCase):
    """Six sections were emitted by GET and offered an Edit button, but had no
    branch in update_section_from_data — every save returned 422."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def _activate(self, *section_keys):
        """Re-activate retired sections, as an administrator would.

        `leadership_detail` and `derived_multiples` left the generated-profile
        contract but were deactivated rather than deleted. Activating them here
        is what makes these tests assert the thing that actually matters now:
        that "deactivated, not deleted" is a real promise and re-activating a
        section restores a working read/write path rather than a 422.
        """
        from fundos.platformcfg.models import ProfileSectionConfig
        ProfileSectionConfig.objects.filter(
            section_key__in=section_keys).update(is_active=True)

    def test_all_enhancement_sections_accept_their_own_get_payload(self):
        self._activate("leadership_detail", "derived_multiples")
        # Current wire names; the four renamed sections also still accept
        # their previous names (see section_writer._CANONICAL_SECTION_KEY).
        for key in ("investors_cap_table", "company_story",
                    "industry_research", "investment_thesis",
                    "leadership_detail", "derived_multiples"):
            with self.subTest(section=key):
                data = serialize_section(self.profile, key)["data"]
                try:
                    update_section_from_data(self.profile, key, data,
                                             user=self.user)
                except SectionUpdateError as e:
                    self.fail(f"{key} is offered for edit but rejects a "
                              f"save of its own GET payload: {e}")

    def test_the_previous_wire_names_still_resolve(self):
        """A client mid-migration must not break on the rename alone."""
        for old, new in (("investors_and_cap_table", "investors_cap_table"),
                         ("company_story_and_usp", "company_story"),
                         ("industry_and_market_research", "industry_research"),
                         ("recent_news", "news")):
            with self.subTest(section=old):
                self.assertEqual(storage_key_for(old), storage_key_for(new))

    def test_company_story_round_trips(self):
        payload = {
            "origin_story": "Founded in a garage.",
            "brand_evolution": "Renamed in 2024.",
            "milestones": [{"date": "2024-01-01", "title": "Launch",
                            "description": "First customer"}],
            "usp": "Fastest delivery in the category.",
        }
        update_section_from_data(self.profile, "company_story_and_usp",
                                 payload, user=self.user)
        out = serialize_section(self.profile, "company_story_and_usp")["data"]
        self.assertEqual(out["origin_story"], "Founded in a garage.")
        self.assertEqual(out["milestones"][0]["title"], "Launch")
        self.assertEqual(out["usp"], "Fastest delivery in the category.")

    def test_market_sizing_nested_object_round_trips(self):
        """Each figure now carries its source and assumptions (CR-11), so the
        wire shape is {value, source, assumptions} rather than a bare
        string. The round-trip guarantee is unchanged."""
        update_section_from_data(
            self.profile, "industry_and_market_research",
            {"industry_evolution": "Consolidating.",
             "market_sizing_narrative": "Top-down from the 2025 report.",
             "methodology": "Top-down",
             "market_sizing": {
                 "tam": {"value": "12B", "source": "Redseer 2025",
                         "assumptions": "All India"},
                 "sam": {"value": "3B", "source": "Redseer 2025",
                         "assumptions": "Online only"},
                 "som": {"value": "400M", "source": "Internal",
                         "assumptions": "5% share"}},
             "performance_trends": ["Margins improving"],
             "regulatory_developments": []},
            user=self.user)
        out = serialize_section(
            self.profile, "industry_and_market_research")["data"]
        self.assertEqual(out["market_sizing"]["tam"]["value"], "12B")
        self.assertEqual(out["market_sizing"]["tam"]["source"], "Redseer 2025")
        self.assertEqual(out["market_sizing"]["som"]["value"], "400M")

    def test_leadership_detail_round_trips(self):
        self._activate("leadership_detail")
        update_section_from_data(
            self.profile, "leadership_detail",
            {"cfo": {"name": "Ravi K", "role": "CFO", "background": "CA",
                     "tenure": "3y", "linkedin_url": ""},
             "founders": [], "ceo": {}, "org_structure_note": "Flat."},
            user=self.user)
        out = serialize_section(self.profile, "leadership_detail")["data"]
        self.assertEqual(out["cfo"]["name"], "Ravi K")
        self.assertEqual(out["org_structure_note"], "Flat.")

    def test_saving_an_untouched_template_stays_incomplete(self):
        """An all-blank template row must not read back as real data."""
        self._activate("derived_multiples")
        data = serialize_section(self.profile, "derived_multiples")["data"]
        update_section_from_data(self.profile, "derived_multiples", data,
                                 user=self.user)
        out = serialize_section(self.profile, "derived_multiples")
        self.assertFalse(out["isComplete"])


class NoSilentFieldLossOnSaveTests(SeededTestCase):
    """Fields the serializer emitted were dropped by the writer, so a client
    that read a section and PATCHed it back verbatim lost data with no error."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_business_model_types_survives_a_save(self):
        update_section_from_data(
            self.profile, "business_model",
            {"business_model_types": ["B2B", "SaaS"],
             "customer_type": "Enterprise", "value_proposition": "Speed",
             "delivery_model": "Cloud", "pricing_model": "Subscription",
             "sales_model": "Inside sales", "distribution_channels": ["Web"]},
            user=self.user)
        out = serialize_section(self.profile, "business_model")["data"]
        self.assertEqual(out["business_model_types"], ["B2B", "SaaS"])

    def test_invalid_business_model_types_are_filtered_not_stored(self):
        update_section_from_data(
            self.profile, "business_model",
            {"business_model_types": ["B2B", "Nonsense"],
             "customer_type": "", "value_proposition": "",
             "delivery_model": "", "pricing_model": "", "sales_model": "",
             "distribution_channels": []},
            user=self.user)
        out = serialize_section(self.profile, "business_model")["data"]
        self.assertEqual(out["business_model_types"], ["B2B"])

    def test_company_profile_funding_summary_is_writable(self):
        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": "Kitchenware", "website": "acme.com",
             "country": "IN", "macro_sector": "Consumer",
             "sub_sector": "D2C", "funding_status_name": "Series A",
             "revenue_size_name": "1-10M", "currency_id": "INR",
             "employee_strength_name": "",
             "total_funding_raised_usd_mn": 12.5,
             "last_funding_round_date": "2025-03-01",
             "latest_pre_money_usd_mn": 40, "latest_post_money_usd_mn": 52.5},
            user=self.user)
        out = serialize_section(self.profile, "company_profile")["data"]
        self.assertEqual(out["total_funding_raised_usd_mn"], 12.5,
                         "an editor-corrected total must win over the "
                         "derived figure, which requires it to be writable")
        self.assertEqual(out["latest_post_money_usd_mn"], 52.5)

    def test_competitor_analysis_fields_survive_a_save(self):
        update_section_from_data(
            self.profile, "competitors",
            [{"name": "Rival Ltd", "fy_year": 2025, "revenue": 30,
              "funding_usd_mn": 18, "status": "Challenger",
              "investors": ["Accel"], "business_model": "D2C",
              "market_positioning": "Premium",
              "latest_valuation_usd_mn": 120, "revenue_growth_pct": 42,
              "market_share_pct": 7, "relative_scale": "Larger",
              "key_differentiators": ["Brand"], "strengths": ["Distribution"],
              "weaknesses": ["Thin margins"], "ev_revenue_multiple": 4,
              "ev_ebitda_multiple": 20,
              "recent_activity": [{"activity_type": "Funding",
                                   "date": "2025-06-01",
                                   "investors_or_acquirers": ["Accel"],
                                   "target_company": "Rival Ltd",
                                   "deal_value_usd_mn": 30,
                                   "implied_valuation_multiple": 5}]}],
            user=self.user)
        row = serialize_section(self.profile, "competitors")["data"][0]
        self.assertEqual(row["market_positioning"], "Premium")
        self.assertEqual(row["strengths"], ["Distribution"])
        self.assertEqual(row["weaknesses"], ["Thin margins"])
        self.assertEqual(row["recent_activity"][0]["target_company"],
                         "Rival Ltd")
        self.assertEqual(Competitor.objects.filter(
            profile=self.profile).count(), 1)

    def test_funding_history_post_money_and_leads_survive_a_save(self):
        update_section_from_data(
            self.profile, "funding_history",
            [{"date": "2025-01-15", "round": "Series A",
              "amount_usd_mn": 10, "pre_money_usd_mn": 40,
              "post_money_usd_mn": 50,
              "investors": ["Accel", "Blume"],
              "lead_investors": ["Accel"]}],
            user=self.user)
        row = serialize_section(self.profile, "funding_history")["data"][0]
        self.assertEqual(row["post_money_usd_mn"], 50)
        self.assertEqual(row["lead_investors"], ["Accel"])
        self.assertEqual(FundingRound.objects.filter(
            profile=self.profile).count(), 1)


class ProvenanceTests(SeededTestCase):
    """The section writer stamped source="founder_entered", which is not one
    of the canonical provenance values. Onboarding's cleanup deletes
    source="founder" rows, so edited founders were invisible to it and
    duplicated on every re-submission of the onboarding form."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_edited_founders_use_the_canonical_source(self):
        update_section_from_data(
            self.profile, "founders",
            [{"name": "Asha Rao", "role": "CEO", "background": "Ex-Flipkart",
              "linkedin_url": "", "is_full_time": True}],
            user=self.user)
        sources = set(Founder.objects.filter(profile=self.profile)
                      .values_list("source", flat=True))
        self.assertEqual(sources, {"founder"})

    def test_re_onboarding_does_not_duplicate_edited_founders(self):
        update_section_from_data(
            self.profile, "founders",
            [{"name": "Asha Rao", "role": "CEO", "background": "",
              "linkedin_url": "", "is_full_time": True}],
            user=self.user)
        # What ProfileOnboardView does before recreating the founder set.
        Founder.objects.filter(profile=self.profile,
                               source="founder").delete()
        self.assertEqual(
            Founder.objects.filter(profile=self.profile).count(), 0,
            "an edited founder survived the onboarding reset and would be "
            "duplicated alongside the resubmitted one")


class SectionHistoryTests(SeededTestCase):
    """History looked up the WIRE section key. Three sections are stored under
    a different key, and company_profile is stored as company_overview, so
    their history was permanently empty."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()
        self.h = auth_headers(self.user)

    def _history(self, key):
        res = self.client.get(
            f"/api/v1/companies/{self.company.id}/profile/sections/{key}/history",
            **self.h)
        self.assertEqual(res.status_code, 200)
        return res.json()["items"]

    def test_renamed_sections_report_their_history(self):
        for key, payload in (
            ("company_story_and_usp",
             {"origin_story": "v1", "brand_evolution": "", "milestones": [],
              "usp": ""}),
            ("industry_and_market_research",
             {"industry_evolution": "v1",
              "market_sizing": {"tam": "", "sam": "", "som": ""},
              "performance_trends": [], "regulatory_developments": []}),
        ):
            with self.subTest(section=key):
                # Two edits: the first has no prior state to snapshot.
                update_section_from_data(self.profile, key, payload,
                                         user=self.user)
                update_section_from_data(self.profile, key, dict(payload),
                                         user=self.user)
                self.assertTrue(
                    self._history(key),
                    f"{key} was edited but reports no history — the lookup "
                    "is using the wire key rather than the storage key")

    def test_company_profile_history_resolves_to_company_overview(self):
        base = {"description_of_business": "v1", "website": "", "country": "",
                "macro_sector": "", "sub_sector": "",
                "funding_status_name": "", "revenue_size_name": "",
                "currency_id": "", "employee_strength_name": ""}
        update_section_from_data(self.profile, "company_profile", base,
                                 user=self.user)
        update_section_from_data(self.profile, "company_profile",
                                 {**base, "description_of_business": "v2"},
                                 user=self.user)
        self.assertTrue(
            self._history("company_profile"),
            "company_profile is stored as company_overview; the history "
            "lookup must resolve the storage key")


class FundingUnitTests(SeededTestCase):
    """FundingRound.amount_value is stored in USD MILLIONS everywhere else.
    The dashboard summary divided it by 1e6, reporting 0.0 for the same round
    the profile page displayed as $5 Mn."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_dashboard_total_matches_the_profile_figure(self):
        from fundos.profile.summary import company_summaries

        FundingRound.objects.create(
            tenant_id=self.tenant.id, profile=self.profile,
            round_name="Series A", amount_value=5, source="founder")
        FundingRound.objects.create(
            tenant_id=self.tenant.id, profile=self.profile,
            round_name="Seed", amount_value=1.5, source="founder")

        summary = company_summaries([self.company.id])[str(self.company.id)]
        profile_total = build_sections(self.profile)["company_profile"][
            "data"]["total_funding_raised_usd_mn"]

        self.assertEqual(summary["totalFundingReceivedUsdMn"], 6.5)
        self.assertEqual(
            summary["totalFundingReceivedUsdMn"], profile_total,
            "the dashboard and the profile disagree about the same rounds")

    def test_summary_exposes_profile_completeness(self):
        from fundos.profile.summary import company_summaries

        summary = company_summaries([self.company.id])[str(self.company.id)]
        self.assertIn("profileComplete", summary)


class CompanyLogoEndpointTests(SeededTestCase):
    """The client called PATCH /companies/{id}/logo from day one; the route
    was never mounted, so every logo change 404'd and reverted on reload."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()
        self.h = auth_headers(self.user)
        self.url = f"/api/v1/companies/{self.company.id}/logo"

    def test_patch_logo_url_persists(self):
        res = self.client.patch(
            self.url, {"logoUrl": "https://img.example.com/acme.png"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.logo_url,
                         "https://img.example.com/acme.png")

    def test_logo_appears_in_the_profile_payload(self):
        self.client.patch(self.url,
                          {"logoUrl": "https://img.example.com/acme.png"},
                          content_type="application/json", **self.h)
        res = self.client.get(f"/api/v1/companies/{self.company.id}/profile",
                              **self.h)
        self.assertEqual(res.json()["logoUrl"],
                         "https://img.example.com/acme.png")

    def test_empty_body_is_rejected_not_silently_accepted(self):
        res = self.client.patch(self.url, {},
                                content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 422)

    def test_delete_clears_the_logo(self):
        self.client.patch(self.url, {"logoUrl": "https://x.test/a.png"},
                          content_type="application/json", **self.h)
        res = self.client.delete(self.url, **self.h)
        self.assertEqual(res.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.logo_url, "")

    def test_another_tenants_company_is_not_reachable(self):
        other_tenant = Tenant.objects.create(name="Other")
        other_user = User.objects.create_user(
            email="other@example.com", tenant_id=other_tenant.id,
            name="Other")
        res = self.client.patch(
            self.url, {"logoUrl": "https://x.test/a.png"},
            content_type="application/json", **auth_headers(other_user))
        self.assertIn(res.status_code, (403, 404))


class ContextsTimestampTests(SeededTestCase):
    """The dashboard sorts on updatedAt and renders it as the 'last touched'
    chip, and branches on profileComplete to decide where a card opens.
    None of those fields were sent, so all three behaviours were dead."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()
        self.h = auth_headers(self.user)

    def test_context_items_carry_timestamps(self):
        Deal.objects.create(
            tenant_id=self.tenant.id, company=self.company,
            name="Series A", round_type="seed", primary_owner=self.user,
            created_by=self.user)
        items = self.client.get("/api/v1/me/contexts", **self.h).json()["items"]
        self.assertTrue(items, "expected at least one context")
        for item in items:
            with self.subTest(scope=item.get("scope")):
                self.assertIn("updatedAt", item)
                self.assertIsNotNone(item["updatedAt"])

    def test_internal_sort_keys_are_not_leaked(self):
        items = self.client.get("/api/v1/me/contexts", **self.h).json()["items"]
        for item in items:
            self.assertNotIn("_createdAt", item)
            self.assertNotIn("_sector", item)
