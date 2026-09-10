"""
Regression tests for the Silk 2.0 review findings (23 Jul 2026).

Each test pins one defect found while verifying the Silk 2.0 build against
the PRD, the change specification and clarifications C1-C12.
"""
from django.test import TestCase, override_settings
from django.core.management import call_command

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class SilkReviewBase(TestCase):
    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.deal = self.w["a"]["deal"]
        self.h = auth_headers(self.w["a"]["founder"])
        self.hB = auth_headers(self.w["b"]["founder"])


class BucketLadderCoverage(SilkReviewBase):
    """C9 lists seven bands; every one must be reachable, and a company
    with real revenue must never be dropped to the smallest for want of an
    unrelated flag."""

    def test_every_bucket_is_reachable(self):
        from fundos.platformcfg.models import FundraiseBucket, TractionRule
        reachable = set(TractionRule.objects.filter(is_active=True)
                        .values_list("recommended_bucket__bucket_no", flat=True))
        defined = set(FundraiseBucket.objects.values_list("bucket_no",
                                                          flat=True))
        self.assertEqual(defined - reachable, set(),
                         "some C9 bands can never be recommended")

    def test_revenue_alone_drives_the_ladder(self):
        """A profile without the full-time / paying-customer flags used to
        fall past every rule and be advised the smallest amount."""
        from fundos.platformcfg.services import recommend_bucket
        r = recommend_bucket({"arr_usd": 400_000})
        self.assertFalse(r["insufficient_data"])
        self.assertGreaterEqual(r["bucket"].bucket_no, 4,
                                "US$400K ARR must not map to the smallest band")

    def test_ladder_is_monotonic(self):
        from fundos.platformcfg.services import recommend_bucket
        last = 0
        for arr in (0, 50_000, 120_000, 300_000, 600_000, 1_100_000,
                    1_500_000, 4_000_000):
            no = recommend_bucket({"arr_usd": arr})["bucket"].bucket_no
            self.assertGreaterEqual(no, last, f"went backwards at ARR {arr}")
            last = no

    def test_boundary_is_lower_bound_inclusive(self):
        from fundos.platformcfg.services import recommend_bucket
        self.assertEqual(
            recommend_bucket({"arr_usd": 1_500_000})["bucket"].bucket_no, 7)

    def test_uplift_cannot_exceed_top_band(self):
        from fundos.platformcfg.services import recommend_bucket
        r = recommend_bucket({"arr_usd": 5_000_000, "star_founder": True})
        self.assertLessEqual(r["bucket"].bucket_no, 7)

    def test_empty_profile_is_idea_stage_not_a_fallback(self):
        """An empty profile legitimately matches the Outline's idea-stage
        rule, with its founder-capital advice — that is a real answer."""
        from fundos.platformcfg.services import recommend_bucket
        r = recommend_bucket({})
        self.assertFalse(r["insufficient_data"])
        self.assertEqual(r["bucket"].bucket_no, 1)
        self.assertTrue(r["advice"], "idea stage must carry the next-step advice")

    def test_unresolvable_rules_report_insufficient_not_false_advice(self):
        """Safety net: if an admin deactivates the ladder, the system must
        say it cannot advise rather than presenting the smallest band."""
        from fundos.platformcfg.models import TractionRule
        from fundos.platformcfg.services import recommend_bucket
        TractionRule.objects.update(is_active=False)
        r = recommend_bucket({"arr_usd": 400_000, "star_founder": True})
        self.assertTrue(r["insufficient_data"])
        self.assertEqual(r["uplift_applied"], [],
                         "a placeholder must never be uplifted")

    def test_api_exposes_all_seven_bands_and_the_flag(self):
        res = self.client.get(
            f"/api/v1/companies/{self.co}/fundraise-recommendation", **self.h)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body["allBuckets"]), 7)
        rec = body["recommended"]
        self.assertIn("insufficientData", rec)
        self.assertIn("missingSignals", rec)
        # A genuine recommendation is flagged as the default; a placeholder
        # is not, so the UI can tell them apart.
        self.assertEqual(rec["isDefault"], not rec["insufficientData"])

    def test_api_marks_a_placeholder_when_rules_cannot_resolve(self):
        from fundos.platformcfg.models import TractionRule
        TractionRule.objects.update(is_active=False)
        res = self.client.get(
            f"/api/v1/companies/{self.co}/fundraise-recommendation", **self.h)
        rec = res.json()["recommended"]
        self.assertTrue(rec["insufficientData"])
        self.assertFalse(rec["isDefault"],
                         "a placeholder must not be flagged as the default")


class ErrorEnvelope(SilkReviewBase):
    """Validation errors must be readable and machine-usable — not a
    stringified Python/DRF repr with an empty fields{}."""

    def test_onboarding_validation_envelope(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/profile/onboard",
                               {"hqCountry": "IN"},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 422)
        body = res.json()
        self.assertNotIn("ErrorDetail", str(body["detail"]))
        self.assertIn("websiteUrl", body["fields"])

    def test_deal_targets_validation_envelope(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/deal-targets",
                               {"targetRaise": "abc"},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 422)
        body = res.json()
        self.assertNotIn("ErrorDetail", str(body["detail"]))
        self.assertEqual(body["fields"].get("targetRaise"), "Enter a number.")

    def test_dict_detail_is_split_by_the_base_exception(self):
        from fundos.core.exceptions import DomainValidationError
        exc = DomainValidationError({"a": "Bad a.", "b": "Bad b."})
        self.assertEqual(exc.fields, {"a": "Bad a.", "b": "Bad b."})
        self.assertNotIn("ErrorDetail", str(exc.detail))


class WorkspaceIsReadableBeforeApproval(SilkReviewBase):
    """CR-02 / C6: navigation is never blocked. Reading the Stage-3
    workspace before a strategy is approved must render, while every
    generation action stays refused."""

    def test_workspace_read_returns_200_with_prerequisites(self):
        res = self.client.get(f"/api/v1/deals/{self.deal.id}/workspace",
                              **self.h)
        self.assertEqual(res.status_code, 200,
                         "the workspace page must render (was E-GATE-423)")
        body = res.json()
        self.assertFalse(body["available"])
        self.assertIn("prerequisites", body)
        self.assertEqual(body["documents"], [])

    def test_generation_actions_are_still_refused(self):
        """The integrity constraint belongs on actions, not on pages."""
        for path in ("story/generate", "model/generate", "im/generate",
                     "package/approve"):
            res = self.client.post(f"/api/v1/deals/{self.deal.id}/{path}", {},
                                   content_type="application/json", **self.h)
            self.assertGreaterEqual(
                res.status_code, 400,
                f"{path} must still be refused without an approved strategy")

    def test_workspace_read_is_still_tenant_isolated(self):
        res = self.client.get(f"/api/v1/deals/{self.deal.id}/workspace",
                              **self.hB)
        self.assertGreaterEqual(res.status_code, 400)


class DocumentCentreAccessibility(SilkReviewBase):
    """C7: migrated Stage-3 artefacts must be accessible read-only, which
    means openable — listing a filename is not access."""

    def test_document_payload_carries_a_download_field(self):
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        ProfileDocument.objects.create(
            tenant_id=self.company.tenant_id, profile=profile,
            category="teaser", filename="Teaser v1.pdf",
            is_readonly=True, origin_stage="legacy_stage_3")

        res = self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        self.assertEqual(res.status_code, 200)
        docs = res.json()["documents"]
        self.assertTrue(docs)
        entry = docs[0]
        self.assertIn("downloadUrl", entry,
                      "Document Center entries must expose a download link")
        self.assertTrue(entry["isReadonly"])

    def test_missing_storage_degrades_to_null_not_an_error(self):
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        ProfileDocument.objects.create(
            tenant_id=self.company.tenant_id, profile=profile,
            category="other", filename="No file.pdf")
        res = self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["documents"][0]["downloadUrl"])


class ZeroIsAValue(SilkReviewBase):
    """C5 allows a zero burn (profitable company). A saved zero must survive
    a reload rather than being serialised away as absent."""

    def test_zero_burn_round_trips(self):
        url = f"/api/v1/companies/{self.co}/cash-position"
        res = self.client.post(url, {"cashInBank": 2500000, "monthlyBurn": 0,
                                     "ccy": "INR"},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["monthlyBurn"], 0)
        self.assertTrue(res.json()["isProfitable"])
        self.assertIsNone(res.json()["runwayMonths"])

        again = self.client.get(url, **self.h)
        self.assertEqual(again.json()["monthlyBurn"], 0,
                         "a saved zero burn must not come back as null")

    def test_zero_cash_round_trips(self):
        url = f"/api/v1/companies/{self.co}/cash-position"
        self.client.post(url, {"cashInBank": 0, "monthlyBurn": 100000,
                               "ccy": "INR"},
                         content_type="application/json", **self.h)
        self.assertEqual(self.client.get(url, **self.h).json()["cashInBank"], 0)
