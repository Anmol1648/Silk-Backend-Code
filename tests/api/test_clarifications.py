"""
Tests for behaviour introduced by the clarifications document (C1–C12).

These lock in the decisions the financial advisors made, so a later change
that quietly reverses one of them fails here rather than in production.
"""
import uuid
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import Client, TestCase

from fundos.core.auth import issue_tokens
from fundos.core.models import Company, Deal, Membership, User
from fundos.core.scoping import set_current_tenant


def _seed():
    call_command("seed_platform_config", verbosity=0)


class ClarificationTestBase(TestCase):
    def setUp(self):
        _seed()
        self.sfx = uuid.uuid4().hex[:8]
        self.tenant = uuid.uuid4()
        set_current_tenant(self.tenant)
        self.user = User.objects.create_user(
            email=f"c-{self.sfx}@test.com", tenant_id=self.tenant)
        self.company = Company.objects.create(
            tenant_id=self.tenant, name="Testco",
            domain=f"testco-{self.sfx}.ai", hq_country="IN")
        self.deal = Deal.objects.create(
            tenant_id=self.tenant, company=self.company, name="Seed",
            primary_owner=self.user)
        Membership.objects.create(
            tenant_id=self.tenant, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        access, _ = issue_tokens(self.user)
        self.h = {"HTTP_AUTHORIZATION": f"Bearer {access}"}
        self.api = Client()
        self.base = f"/api/v1/companies/{self.company.id}"


class C1CompanyIndependenceTests(ClarificationTestBase):
    """C1 — a company is independent and may hold several concurrent deals."""

    def test_company_can_exist_without_a_deal(self):
        company = Company.objects.create(
            tenant_id=self.tenant, name="Dealless",
            domain=f"dealless-{self.sfx}.ai")
        Membership.objects.create(
            tenant_id=self.tenant, user=self.user, scope_type="company",
            scope_id=company.id, role="founder", status="active")
        res = self.api.get(f"/api/v1/companies/{company.id}/profile", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["companyName"], "Dealless")

    def test_company_supports_concurrent_deals(self):
        """An equity raise and a debt raise can run at the same time."""
        Deal.objects.create(tenant_id=self.tenant, company=self.company,
                            name="Debt facility", primary_owner=self.user)
        active = Deal.objects.filter(company=self.company, is_deleted=False,
                                     status="active").count()
        self.assertEqual(active, 2)
        # The profile is company-scoped, so there is still exactly one.
        res = self.api.get(f"{self.base}/profile", **self.h)
        self.assertEqual(res.status_code, 200)

    def test_profile_is_one_per_company(self):
        from fundos.profile.models import CompanyProfile

        self.api.get(f"{self.base}/profile", **self.h)
        self.api.get(f"{self.base}/profile", **self.h)
        self.assertEqual(
            CompanyProfile.objects.filter(company=self.company).count(), 1)


class C2CompletenessTests(ClarificationTestBase):
    """C2 — complete = mandatory sections populated AND review confirmed."""

    def test_review_alone_does_not_make_profile_complete(self):
        self.api.get(f"{self.base}/profile", **self.h)
        res = self.api.post(f"{self.base}/profile/review", data="{}",
                            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertIsNotNone(res.json()["reviewedAt"])
        # Mandatory sections are still empty, so it is not complete.
        self.assertFalse(res.json()["isComplete"])

    def test_sections_alone_do_not_make_profile_complete(self):
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile,
                                             recompute_completeness)
        from fundos.profile.section_writer import update_section_from_data

        profile = get_or_create_profile(self.company, user=self.user)
        # Completeness is now derived from actual populated data / isComplete,
        # not merely non-empty content (backend-issues combined report #4).
        # Generate real data, then finish the one section the mock can't fully
        # source (company_profile's structured fields), and assert the score
        # reaches 100% — proving it recalculates from real state.
        generate_profile(profile, user=self.user)
        update_section_from_data(profile, "company_profile", {
            "description_of_business": "We build X.",
            "website": "https://acme.inc", "country": "US",
            "macro_sector": "Healthcare", "sub_sector": "HealthTech",
            "funding_status_name": "Private",
            "employee_strength_name": "250-500",
            "revenue_size_name": "$25M-$50M", "currency_id": "USD"},
            user=self.user)
        recompute_completeness(profile)
        profile.refresh_from_db()
        self.assertEqual(float(profile.completeness_pct), 100.0)
        # No review confirmation yet.
        self.assertFalse(profile.is_complete)

    def test_creation_and_completeness_sets_differ(self):
        """C2 explicitly allows these two sets to be different."""
        from fundos.platformcfg.services import (
            completeness_section_keys, creation_section_keys,
        )
        self.assertNotEqual(set(creation_section_keys()),
                            set(completeness_section_keys()))


class C3LinkedInTests(ClarificationTestBase):
    """C3 — LinkedIn is optional; when supplied the founder confirms it."""

    def test_onboarding_succeeds_without_linkedin(self):
        res = self.api.post(
            f"{self.base}/profile/onboard",
            data='{"websiteUrl":"https://testco.ai","hqCountry":"IN",'
                 '"founders":[{"name":"Ada Lovelace"}]}',
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 202)

    def test_confirmation_is_recorded_when_linkedin_supplied(self):
        from fundos.profile.models import Founder

        self.api.post(
            f"{self.base}/profile/onboard",
            data='{"websiteUrl":"https://testco.ai","hqCountry":"IN",'
                 '"founders":[{"name":"Ada","linkedinUrl":'
                 '"https://linkedin.com/in/ada","selfConfirmed":true}]}',
            content_type="application/json", **self.h)
        founder = Founder.objects.filter(name="Ada").first()
        self.assertIsNotNone(founder)
        self.assertTrue(founder.self_confirmed)
        self.assertIsNotNone(founder.self_confirmed_at)


class C5BurnTests(ClarificationTestBase):
    """C5 — burn is an input; a company may be profitable."""

    def test_runway_from_cash_and_burn(self):
        res = self.api.post(
            f"{self.base}/cash-position",
            data='{"cashInBank":600000,"monthlyBurn":50000,"ccy":"USD",'
                 '"asOfDate":"2026-07-01","desiredRunwayMonths":18}',
            content_type="application/json", **self.h)
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertAlmostEqual(float(body["runwayMonths"]), 12.0, places=1)
        self.assertIsNotNone(body["cashExhaustionDate"])
        # 18 months wanted, 12 available → 6 × 50k still to raise.
        self.assertAlmostEqual(
            float(body["additionalCapitalRequiredUsd"]), 300000.0, places=0)

    def test_profitable_company_reports_no_runway_constraint(self):
        for burn in (0, -25000):
            res = self.api.post(
                f"{self.base}/cash-position",
                data=f'{{"cashInBank":400000,"monthlyBurn":{burn},'
                     f'"ccy":"USD","asOfDate":"2026-07-01"}}',
                content_type="application/json", **self.h)
            body = res.json()
            self.assertTrue(body["isProfitable"])
            # Never a misleading giant number.
            self.assertIsNone(body["runwayMonths"])
            self.assertIsNone(body["cashExhaustionDate"])

    def test_local_currency_is_converted_for_comparison(self):
        """C11 — input in home currency, computed in USD, rate recorded."""
        res = self.api.post(
            f"{self.base}/cash-position",
            data='{"cashInBank":42000000,"monthlyBurn":4200000,"ccy":"INR",'
                 '"asOfDate":"2026-07-01"}',
            content_type="application/json", **self.h)
        body = res.json()
        self.assertAlmostEqual(float(body["runwayMonths"]), 10.0, places=1)
        self.assertIsNotNone(body["fxRateUsed"])


class C6AdvisoryStageTests(ClarificationTestBase):
    """C6 — stages are never locked; prerequisites are advisory."""

    def test_no_stage_is_created_locked(self):
        from fundos.core.models import StageState
        from fundos.core.services.stage_state import ensure_stage_states

        ensure_stage_states(self.deal)
        self.assertEqual(
            StageState.objects.filter(deal_id=self.deal.id,
                                      status="locked").count(), 0)

    def test_hard_gate_never_blocks(self):
        from fundos.core.services import stage_state
        for stage_no in range(0, 9):
            self.assertTrue(stage_state.hard_gate_met(self.deal.id, stage_no))

    def test_manual_targets_satisfy_strategy_prerequisite(self):
        """A founder who knows their numbers may skip the guided strategy."""
        from fundos.core.services import stage_state
        from fundos.profile.services import save_deal_targets

        before = stage_state.prerequisite_status(self.deal.id, 3)
        self.assertFalse(all(p["met"] for p in before))

        save_deal_targets(self.company, self.deal.id,
                          target_raise=2000000, pre_money=8000000,
                          ccy="USD", user=self.user)

        after = stage_state.prerequisite_status(self.deal.id, 3)
        self.assertTrue(all(p["met"] for p in after))

    def test_manual_targets_complete_stage_two(self):
        """C6 — the outputs matter, not the process."""
        from fundos.core.services import stage_state
        from fundos.profile.services import save_deal_targets

        save_deal_targets(self.company, self.deal.id,
                          target_raise=1000000, pre_money=4000000,
                          ccy="USD", user=self.user)
        results = stage_state.recompute_stage_completion(self.deal.id)
        self.assertEqual(results[2], 100.0)


class DealTargetsTests(ClarificationTestBase):
    """Four headline figures: two entered, two calculated."""

    def test_post_money_and_dilution_are_calculated(self):
        res = self.api.post(
            f"{self.base}/deal-targets",
            data='{"targetRaise":2000000,"preMoneyValuation":8000000,'
                 '"ccy":"USD"}',
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        t = res.json()["targets"]
        # 1 & 2 — as entered
        self.assertEqual(t["targetRaise"], 2000000)
        self.assertEqual(t["preMoneyValuation"], 8000000)
        # 3 — pre-money + raise
        self.assertEqual(t["postMoneyValuation"], 10000000)
        # 4 — raise / post-money
        self.assertAlmostEqual(t["dilutionPct"], 20.0, places=3)

    def test_client_cannot_override_calculated_fields(self):
        """Sending post-money or dilution must not corrupt the record."""
        res = self.api.post(
            f"{self.base}/deal-targets",
            data='{"targetRaise":1000000,"preMoneyValuation":4000000,'
                 '"postMoneyValuation":99999999,"dilutionPct":1.0,'
                 '"ccy":"USD"}',
            content_type="application/json", **self.h)
        t = res.json()["targets"]
        self.assertEqual(t["postMoneyValuation"], 5000000)
        self.assertAlmostEqual(t["dilutionPct"], 20.0, places=3)

    def test_calculated_fields_track_a_change_of_inputs(self):
        from fundos.profile.services import save_deal_targets

        row = save_deal_targets(self.company, self.deal.id,
                                target_raise=1000000, pre_money=9000000,
                                ccy="USD", user=self.user)
        self.assertAlmostEqual(float(row.dilution_pct), 10.0, places=3)

        row = save_deal_targets(self.company, self.deal.id,
                                target_raise=3000000, pre_money=9000000,
                                ccy="USD", user=self.user)
        self.assertEqual(float(row.post_money_value), 12000000.0)
        self.assertAlmostEqual(float(row.dilution_pct), 25.0, places=3)

    def test_partial_update_preserves_the_other_input(self):
        from fundos.profile.services import save_deal_targets

        save_deal_targets(self.company, self.deal.id, target_raise=2000000,
                          pre_money=8000000, ccy="USD", user=self.user)
        row = save_deal_targets(self.company, self.deal.id,
                                pre_money=6000000, ccy="USD", user=self.user)
        self.assertEqual(float(row.target_raise_value), 2000000.0)
        self.assertEqual(float(row.post_money_value), 8000000.0)

    def test_incomplete_inputs_leave_calculations_null(self):
        from fundos.profile.services import save_deal_targets

        row = save_deal_targets(self.company, self.deal.id,
                                target_raise=2000000, ccy="USD",
                                user=self.user)
        self.assertIsNone(row.post_money_value)
        self.assertIsNone(row.dilution_pct)
        self.assertFalse(row.is_complete)

    def test_local_currency_is_converted_and_rate_recorded(self):
        """C11 — enter in home currency, compare in USD, record the rate."""
        res = self.api.post(
            f"{self.base}/deal-targets",
            data='{"targetRaise":168000000,"preMoneyValuation":672000000,'
                 '"ccy":"INR"}',
            content_type="application/json", **self.h)
        t = res.json()["targets"]
        self.assertEqual(t["postMoneyValuation"], 840000000)
        self.assertAlmostEqual(t["dilutionPct"], 20.0, places=3)
        self.assertAlmostEqual(t["usd"]["targetRaise"], 2000000.0, places=0)
        self.assertIsNotNone(t["fx"]["rateUsed"])

    def test_dilution_is_currency_invariant(self):
        """A ratio must not depend on the currency it was entered in."""
        from fundos.profile.services import save_deal_targets

        usd = save_deal_targets(self.company, self.deal.id,
                                target_raise=2000000, pre_money=8000000,
                                ccy="USD", user=self.user)
        inr = save_deal_targets(self.company, self.deal.id,
                                target_raise=168000000,
                                pre_money=672000000, ccy="INR",
                                user=self.user)
        self.assertAlmostEqual(float(usd.dilution_pct),
                               float(inr.dilution_pct), places=3)

    def test_targets_are_superseded_not_edited(self):
        """A change of terms leaves a trail."""
        from fundos.profile.services import save_deal_targets
        from fundos.profile.targets import DealTargets

        save_deal_targets(self.company, self.deal.id, target_raise=1000000,
                          pre_money=4000000, ccy="USD", user=self.user)
        save_deal_targets(self.company, self.deal.id, target_raise=2000000,
                          pre_money=8000000, ccy="USD", user=self.user)
        rows = DealTargets.objects.filter(deal_id=self.deal.id)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(is_active=True).count(), 1)

    def test_targets_are_mirrored_into_the_knowledge_base(self):
        """Downstream consumers read the CKB, so all four land there."""
        from fundos.core.models import CkbField
        from fundos.profile.services import save_deal_targets

        save_deal_targets(self.company, self.deal.id, target_raise=2000000,
                          pre_money=8000000, ccy="USD", user=self.user)
        keys = set(CkbField.objects.filter(deal_id=self.deal.id)
                   .values_list("field_key", flat=True))
        for expected in ("target_raise_usd", "pre_money_valuation_usd",
                         "post_money_valuation_usd", "expected_dilution_pct"):
            self.assertIn(expected, keys)

    def test_rejects_negative_amounts(self):
        # E-VAL-422 is the platform's validation convention.
        res = self.api.post(
            f"{self.base}/deal-targets",
            data='{"targetRaise":-500,"preMoneyValuation":8000000}',
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 422)

    def test_rejects_empty_submission(self):
        res = self.api.post(
            f"{self.base}/deal-targets", data='{}',
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 422)

    def test_targets_do_not_leak_across_tenants(self):
        res = self.api.get(
            f"/api/v1/companies/{uuid.uuid4()}/deal-targets", **self.h)
        self.assertEqual(res.status_code, 404)


class C9BucketTests(ClarificationTestBase):
    """C9 — seven values; the Outline's traction rules pick the default."""

    def test_seven_buckets_exist(self):
        res = self.api.get("/api/v1/config/buckets", **self.h)
        buckets = res.json()["items"]
        self.assertEqual(len(buckets), 7)
        self.assertEqual([b["label"] for b in buckets],
                         ["$100K", "$250K", "$500K", "$1M", "$2M", "$3M", "$5M"])

    def test_traction_ladder_matches_the_outline(self):
        from fundos.platformcfg.services import recommend_bucket

        cases = [
            ({"arr_usd": 0, "capital_raised_usd": 0,
              "company_registered": False}, "$100K"),
            ({"arr_usd": 0, "capital_raised_usd": 0, "months_full_time": 6},
             "$250K"),
            ({"arr_usd": 120000, "founder_full_time": True,
              "paying_customers": 12}, "$1M"),
            ({"arr_usd": 600000}, "$2M"),
            ({"arr_usd": 1200000}, "$3M"),
            ({"arr_usd": 1800000}, "$5M"),
        ]
        for signals, expected in cases:
            result = recommend_bucket(signals)
            self.assertEqual(result["bucket"].label, expected,
                             f"signals {signals} should map to {expected}")

    def test_star_founder_uplift_adds_levels(self):
        from fundos.platformcfg.services import recommend_bucket

        plain = recommend_bucket({"arr_usd": 120000, "founder_full_time": True,
                                  "paying_customers": 12})
        starred = recommend_bucket({"arr_usd": 120000, "founder_full_time": True,
                                    "paying_customers": 12,
                                    "star_founder": True})
        self.assertGreater(starred["bucket"].bucket_no, plain["bucket"].bucket_no)
        self.assertTrue(starred["uplift_applied"])

    def test_recommendation_is_a_default_not_a_constraint(self):
        """All seven remain selectable regardless of the recommendation."""
        res = self.api.get(f"{self.base}/fundraise-recommendation", **self.h)
        body = res.json()
        self.assertIsNotNone(body["recommended"])
        self.assertEqual(len(body["allBuckets"]), 7)
        self.assertTrue(body["recommended"]["isDefault"])


class AdminConfigurabilityTests(ClarificationTestBase):
    """Admin-owned configuration must actually change behaviour."""

    def test_prompts_resolve_from_database(self):
        from fundos.llm.default_prompts import resolve_prompt
        from fundos.platformcfg.models import PromptTemplate

        row = PromptTemplate.objects.filter(role="readiness_summary",
                                            is_active=True).first()
        self.assertIsNotNone(row)
        row.system_prompt = "ADMIN EDITED PROMPT"
        row.save()
        system, _ = resolve_prompt("readiness_summary")
        self.assertEqual(system, "ADMIN EDITED PROMPT")

    def test_prompt_falls_back_to_code_default_when_absent(self):
        from fundos.llm.default_prompts import resolve_prompt
        from fundos.platformcfg.models import PromptTemplate

        PromptTemplate.objects.filter(role="teaser").delete()
        system, _ = resolve_prompt("teaser")
        self.assertTrue(system, "a code default must still be returned")

    def test_renaming_the_product_changes_the_api_payload(self):
        from fundos.platformcfg.models import BrandConfig

        brand = BrandConfig.get_solo()
        brand.product_name = "Renamed"
        brand.save()
        res = self.api.get("/api/v1/config/app")
        self.assertEqual(res.json()["brand"]["productName"], "Renamed")

    def test_stage_journey_is_configuration_driven(self):
        from fundos.platformcfg.models import StageDefinition

        res = self.api.get("/api/v1/config/app")
        stages = res.json()["stages"]
        self.assertEqual(len(stages), 9)          # 0–8, PRD §2.4
        self.assertEqual(stages[1]["label"], "Company Profile")

        StageDefinition.objects.filter(stage_no=1).update(label="Renamed Stage")
        res = self.api.get("/api/v1/config/app")
        self.assertEqual(res.json()["stages"][1]["label"], "Renamed Stage")

    def test_bucket_thresholds_are_editable(self):
        from fundos.platformcfg.models import TractionRule
        from fundos.platformcfg.services import recommend_bucket

        signals = {"arr_usd": 600000}
        self.assertEqual(recommend_bucket(signals)["bucket"].label, "$2M")

        # Raise the threshold so the same company no longer qualifies.
        TractionRule.objects.filter(
            name="ARR at/above US$0.5M"
        ).update(conditions={"arr_usd_min": 900000})
        self.assertNotEqual(recommend_bucket(signals)["bucket"].label, "$2M")

    def test_countries_are_seeded_for_the_dropdown(self):
        res = self.api.get("/api/v1/config/countries")
        items = res.json()["items"]
        self.assertGreater(len(items), 100)
        # Common markets pinned to the top.
        self.assertEqual(items[0]["iso2"], "IN")
        self.assertEqual(items[0]["homeCurrency"], "INR")


class ProfileSectionTests(ClarificationTestBase):
    """Uniform section contract: edit, version, regenerate protection."""

    def test_edit_creates_a_version_and_marks_it_human_edited(self):
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile, update_section

        profile = get_or_create_profile(self.company, user=self.user)
        update_section(profile, "business_model", content="First draft",
                       user=self.user)
        update_section(profile, "business_model", content="Second draft",
                       user=self.user)

        section = ProfileSection.objects.get(
            profile=profile, section_key="business_model", is_active=True)
        self.assertEqual(section.content, "Second draft")
        self.assertTrue(section.edited_by_user)
        self.assertEqual(section.history.count(), 1)

    def test_regeneration_will_not_silently_discard_a_human_edit(self):
        from fundos.profile.services import (
            get_or_create_profile, regenerate_section, update_section,
        )

        profile = get_or_create_profile(self.company, user=self.user)
        update_section(profile, "business_model", content="Founder's own words",
                       user=self.user)
        result = regenerate_section(profile, "business_model", user=self.user)
        self.assertEqual(result["status"], "confirm_required")


class IsolationTests(ClarificationTestBase):
    """New company-scoped endpoints must not leak across tenants."""

    def setUp(self):
        super().setUp()
        other_tenant = uuid.uuid4()
        set_current_tenant(other_tenant)
        self.other_user = User.objects.create_user(
            email=f"other-{self.sfx}@test.com", tenant_id=other_tenant)
        self.other_company = Company.objects.create(
            tenant_id=other_tenant, name="Other",
            domain=f"other-{self.sfx}.ai")
        Membership.objects.create(
            tenant_id=other_tenant, user=self.other_user, scope_type="company",
            scope_id=self.other_company.id, role="founder", status="active")
        set_current_tenant(self.tenant)

    def test_cross_tenant_reads_are_not_found(self):
        for path in ("profile", "cash-position", "fundraise-recommendation"):
            res = self.api.get(
                f"/api/v1/companies/{self.other_company.id}/{path}", **self.h)
            self.assertEqual(res.status_code, 404, f"{path} leaked")

    def test_cross_tenant_write_is_rejected(self):
        res = self.api.patch(
            f"/api/v1/companies/{self.other_company.id}"
            f"/profile/sections/business_model",
            data='{"content":"injected"}',
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 404)

    def test_unauthenticated_access_is_rejected(self):
        res = Client().get(f"{self.base}/profile")
        self.assertEqual(res.status_code, 401)
