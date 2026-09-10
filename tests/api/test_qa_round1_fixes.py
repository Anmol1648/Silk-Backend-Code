"""
Regression tests for the Round-1 QA report (FundOS_QA_Test_Execution_Report,
2026-07-13). Each test is pinned to the bug id it protects.
"""
from django.test import TestCase, override_settings

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class QaFixBase(TestCase):
    def setUp(self):
        from django.core.management import call_command
        from fundos.config.models import AppConfiguration
        call_command("seed_initial_data", verbosity=0)
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.deal = self.world["a"]["deal"]
        self.founder = self.world["a"]["founder"]
        self.h = auth_headers(self.founder)
        self.base = f"/api/v1/deals/{self.deal.id}"

    def patch_ckb(self, fields):
        return self.client.patch(
            f"{self.base}/ckb", {"fields": fields},
            content_type="application/json", **self.h)

    def _stage2_ready(self):
        """Objectives + financials + peers + raise, mocked."""
        self.patch_ckb([
            {"fieldKey": "arr", "value": 5000000, "ccy": "INR"},
            {"fieldKey": "revenue", "value": 6000000, "ccy": "INR"},
            {"fieldKey": "sector", "value": "saas"},
        ])
        self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["hiring"], "timeline": "6m", "runway": "18m",
             "maxDilutionPct": 18},
            content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/peers/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/raise/generate", {},
                         content_type="application/json", **self.h)


class MaterialsFixes(QaFixBase):
    def test_bug006_material_id_alias_and_filename(self):
        content = b"%PDF-1.4 test deck body"
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("deck.pdf", content, "application/pdf")
        res = self.client.post(f"{self.base}/materials",
                               {"file": f, "category": "investment_material"},
                               **self.h)
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["materialId"], body["id"])
        self.assertEqual(body["filename"], "deck.pdf")

        listing = self.client.get(f"{self.base}/materials", **self.h).json()
        self.assertTrue(all("materialId" in m for m in listing["items"]))

    def test_bug008_zero_byte_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("empty.pdf", b"", "application/pdf")
        res = self.client.post(f"{self.base}/materials",
                               {"file": f, "category": "financial"},
                               **self.h)
        self.assertEqual(res.status_code, 422)

    def test_material_action_patch_exists(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("deck.pdf", b"%PDF-1.4 x", "application/pdf")
        mid = self.client.post(
            f"{self.base}/materials",
            {"file": f, "category": "investment_material"},
            **self.h).json()["materialId"]
        res = self.client.patch(f"{self.base}/materials/{mid}",
                                {"aiStatus": "improve"},
                                content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["aiStatus"], "improve")

    def test_bug007_infected_material_quarantined(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from fundos.readiness.models import MaterialAsset
        f = SimpleUploadedFile("bad.pdf", b"%PDF-1.4 y", "application/pdf")
        mid = self.client.post(
            f"{self.base}/materials",
            {"file": f, "category": "other"}, **self.h).json()["materialId"]
        MaterialAsset.objects.filter(id=mid).update(
            scan_status="infected", summary="should never surface",
            recommendations=[{"issue": "x"}])
        detail = self.client.get(f"{self.base}/materials/{mid}",
                                 **self.h).json()
        self.assertTrue(detail["quarantined"])
        self.assertEqual(detail["summary"], "")
        self.assertEqual(detail["recommendations"], [])
        res = self.client.patch(f"{self.base}/materials/{mid}",
                                {"aiStatus": "reuse"},
                                content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 409)


class ResearchFixes(QaFixBase):
    def test_bug003_funding_alias_runs(self):
        res = self.client.post(f"{self.base}/research/run",
                               {"sources": ["website", "funding"]},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 202)
        self.assertIn("public_funding", res.json()["sources"])

    def test_bug002_sources_key_served_both_ways(self):
        self.client.post(f"{self.base}/research/run", {},
                         content_type="application/json", **self.h)
        body = self.client.get(f"{self.base}/research/sources",
                               **self.h).json()
        self.assertIn("items", body)
        self.assertIn("sources", body)
        self.assertEqual(body["items"], body["sources"])


class CkbFixes(QaFixBase):
    def test_bug004_accept_ai_value_retains_provenance(self):
        from fundos.core.scoping import tenant_context
        from fundos.core.services import ckb_service
        with tenant_context(self.deal.tenant_id):
            ckb_service.set_field(self.deal, "arr", 5000000,
                                  source="ai_research",
                                  confidence=0.8, ccy="INR")
        res = self.client.patch(
            f"{self.base}/ckb/fields/arr", {"action": "accept"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        field = res.json()["field"]
        self.assertTrue(field["verified"])
        self.assertEqual(field["source"], "ai_research")   # provenance kept
        self.assertAlmostEqual(field["confidence"], 0.8)

    def test_bug004_reject_ai_value_clears_it(self):
        from fundos.core.scoping import tenant_context
        from fundos.core.services import ckb_service
        with tenant_context(self.deal.tenant_id):
            ckb_service.set_field(self.deal, "mrr", 400000,
                                  source="ai_research",
                                  confidence=0.6, ccy="INR")
        res = self.client.patch(
            f"{self.base}/ckb/fields/mrr", {"action": "reject"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["field"]["value"])

    def test_tc021_usd_equivalent_on_money_fields(self):
        self.patch_ckb([{"fieldKey": "arr", "value": 8400000, "ccy": "INR"}])
        body = self.client.get(f"{self.base}/ckb", **self.h).json()
        arr = [f for g in body["groups"].values() for f in g
               if f["fieldKey"] == "arr"][0]
        self.assertIn("usdValue", arr)
        self.assertGreater(arr["usdValue"], 0)


class StrategyFixes(QaFixBase):
    def test_bug012_bug013_peer_id_alias(self):
        self._stage2_ready()
        body = self.client.get(f"{self.base}/strategy/peers", **self.h).json()
        self.assertTrue(body["peers"])
        peer = body["peers"][0]
        self.assertEqual(peer["peerId"], peer["id"])
        # journey drill-down by the alias id
        detail = self.client.get(
            f"{self.base}/strategy/peers/{peer['peerId']}", **self.h)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("peer", detail.json())

    def test_bug015_football_field_converted(self):
        self._stage2_ready()
        self.client.post(f"{self.base}/strategy/valuation/generate", {},
                         content_type="application/json", **self.h)
        body = self.client.get(f"{self.base}/strategy/valuation",
                               **self.h).json()
        fx = body["fx"]["usdInr"]
        rng_high = body["range"]["high"]["value"]
        applicable = [m for m in body["footballField"]
                      if m.get("applicable") and (m.get("high") or 0) > 0]
        self.assertTrue(applicable)
        for m in applicable:
            # converted value must equal usd value * fx (same basis as range)
            self.assertAlmostEqual(m["high"], m["highUsd"] * fx, delta=1)
            # and must sit in the same order of magnitude as the headline
            self.assertGreater(m["high"], rng_high / 100)

    def test_bug018_blueprint_current_position_is_string(self):
        self._stage2_ready()
        self.client.post(f"{self.base}/strategy/valuation/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/blueprint/generate", {},
                         content_type="application/json", **self.h)
        body = self.client.get(f"{self.base}/strategy/blueprint",
                               **self.h).json()
        self.assertIsInstance(body["currentPosition"], str)
        self.assertIsInstance(body["executiveSummary"], str)


class ReviewAndPackageFixes(QaFixBase):
    def _to_review(self):
        self._stage2_ready()
        self.client.post(f"{self.base}/strategy/valuation/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/blueprint/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/approve", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/story/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/story/approve", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/teaser/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/deck/outline", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/deck/approve-outline", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/deck/slides", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/model/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/im/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/review/run", {},
                         content_type="application/json", **self.h)

    def test_bug021_quality_score_and_aliases(self):
        self._to_review()
        body = self.client.get(f"{self.base}/review", **self.h).json()
        self.assertIn("qualityScore", body)
        self.assertGreaterEqual(body["qualityScore"], 5)
        self.assertLessEqual(body["qualityScore"], 98)
        self.assertIn("categoryScores", body)
        self.assertEqual(body["recommendations"], body["findings"])
        self.assertEqual(body["inconsistencies"],
                         body["crossDocInconsistencies"])

    def test_bug022_approval_blocked_on_unresolved_inconsistency(self):
        self._to_review()
        from fundos.materials.models import CrossDocInconsistency, PackageReview
        review = PackageReview.objects.filter(
            deal_id=self.deal.id).order_by("-created_at").first()
        CrossDocInconsistency.objects.create(
            tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
            review=review, field_key="arr",
            values=[{"document": "ckb", "value": 5000000},
                    {"document": "model", "value": 10800000}],
            recommended_correction="Align ARR at the source.",
            resolved=False)
        res = self.client.post(f"{self.base}/package/approve", {},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 409)
        # resolving it unblocks
        CrossDocInconsistency.objects.filter(review=review).update(
            resolved=True)
        res = self.client.post(f"{self.base}/package/approve", {},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 201)

    def test_bug024_deck_outline_carries_ai_label(self):
        self._to_review()
        body = self.client.get(f"{self.base}/deck/outline", **self.h).json()
        self.assertEqual(body.get("label"), "AI Generated Insights")

    def test_tc087_im_version_number(self):
        self._to_review()
        body = self.client.get(f"{self.base}/im", **self.h).json()
        self.assertGreaterEqual(body.get("versionNo", 0), 1)
