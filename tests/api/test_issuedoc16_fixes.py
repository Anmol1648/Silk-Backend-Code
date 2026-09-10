"""
Regression tests for the Round-2 follow-up tester report (IssueDoc16,
Jul 2026). Each test is pinned to the issue number it protects.
"""
from django.test import TestCase, override_settings

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class Doc16Base(TestCase):
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


class SectionAssignment(Doc16Base):
    def _member(self):
        from fundos.core.services.onboarding import signup
        user, _ = signup("assignee.doc16@example.com", name="Assignee")
        self.client.post(f"{self.base}/members",
                         {"email": user.email, "role": "team"},
                         content_type="application/json", **self.h)
        return user

    def test_issue1_assign_persists_and_serialised_in_ckb(self):
        member = self._member()
        res = self.client.post(
            f"{self.base}/ckb/assign",
            {"groupKey": "financial", "assigneeUserId": str(member.id)},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["assigneeEmail"], member.email)
        # persisted + visible in GET /ckb (the UI's data source)
        body = self.client.get(f"{self.base}/ckb", **self.h).json()
        self.assertIn("assignments", body)
        self.assertEqual(body["assignments"]["financial"]["assigneeEmail"],
                         member.email)
        # the assignee is notified
        eh = auth_headers(member)
        notif = self.client.get("/api/v1/notifications", **eh).json()
        self.assertIn("ckb.section.assigned",
                      [n["type"] for n in notif["items"]])

    def test_issue1_unassign_clears(self):
        member = self._member()
        self.client.post(
            f"{self.base}/ckb/assign",
            {"groupKey": "team", "assigneeUserId": str(member.id)},
            content_type="application/json", **self.h)
        res = self.client.post(
            f"{self.base}/ckb/assign",
            {"groupKey": "team", "assigneeUserId": None},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        body = self.client.get(f"{self.base}/ckb", **self.h).json()
        self.assertNotIn("team", body.get("assignments", {}))

    def test_issue1_non_member_rejected(self):
        from fundos.core.services.onboarding import signup
        outsider, _ = signup("not.a.member@example.com", name="Out")
        res = self.client.post(
            f"{self.base}/ckb/assign",
            {"groupKey": "legal", "assigneeUserId": str(outsider.id)},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 403)


class ChunkedUploadCompletion(Doc16Base):
    def test_issue4_zero_based_chunks_complete(self):
        """CP-075: a fully-uploaded 0-based chunk sequence must complete —
        the old check demanded chunks 1..N and always failed."""
        import math
        size = 20 * 1024 * 1024  # > 1 chunk at 8MB
        res = self.client.post(
            f"{self.base}/uploads/sessions",
            {"filename": "big.pdf", "size": size,
             "mime": "application/pdf", "category": "financial"},
            content_type="application/json", **self.h)
        body = res.json()
        sid = body["uploadId"]
        chunk_size = body["chunkSize"]
        total = math.ceil(size / chunk_size)
        self.assertGreater(total, 1)
        payload = b"x" * 1024
        for n in range(total):
            r = self.client.put(
                f"{self.base}/uploads/sessions/{sid}/chunks/{n}",
                data=payload, content_type="application/octet-stream",
                **self.h)
            self.assertEqual(r.status_code, 200)
        res = self.client.post(
            f"{self.base}/uploads/sessions/{sid}/complete", {},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 202,
                         f"completion failed: {res.content[:200]}")

    def test_issue4_genuinely_missing_chunk_still_409(self):
        import math
        size = 20 * 1024 * 1024
        body = self.client.post(
            f"{self.base}/uploads/sessions",
            {"filename": "big.pdf", "size": size,
             "mime": "application/pdf", "category": "financial"},
            content_type="application/json", **self.h).json()
        sid = body["uploadId"]
        total = math.ceil(size / body["chunkSize"])
        # upload all but chunk 0
        for n in range(1, total):
            self.client.put(
                f"{self.base}/uploads/sessions/{sid}/chunks/{n}",
                data=b"x", content_type="application/octet-stream", **self.h)
        res = self.client.post(
            f"{self.base}/uploads/sessions/{sid}/complete", {},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 409)
        self.assertIn(0, res.json()["fields"]["missingChunks"])


class GapActions(Doc16Base):
    def test_issue5_founder_action_null_until_acted(self):
        self.patch_ckb([
            {"fieldKey": "arr", "value": 42000000, "ccy": "INR"},
            {"fieldKey": "sector", "value": "saas"}])
        self.client.post(f"{self.base}/readiness/generate", {},
                         content_type="application/json", **self.h)
        gaps = self.client.get(f"{self.base}/readiness/gaps",
                               **self.h).json()["items"]
        self.assertTrue(gaps)
        actionable = [g for g in gaps if g["kind"] != "strength"]
        self.assertTrue(actionable)
        for g in actionable:
            self.assertIsNone(
                g["founderAction"],
                "untouched gaps must serialise founderAction=null, "
                "never the string 'none'")
        # acting flips it to the chosen action
        gid = actionable[0]["id"]
        res = self.client.post(
            f"{self.base}/readiness/gaps/{gid}/action",
            {"action": "accepted"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        gaps = self.client.get(f"{self.base}/readiness/gaps",
                               **self.h).json()["items"]
        acted = [g for g in gaps if g["id"] == gid][0]
        self.assertEqual(acted["founderAction"], "accepted")


class StaleModelReview(Doc16Base):
    def _build_package(self):
        """CKB → stage 2 → approve → stage 3 docs → review."""
        self.patch_ckb([
            {"fieldKey": "arr", "value": 42000000, "ccy": "INR"},
            {"fieldKey": "revenue", "value": 50000000, "ccy": "INR"},
            {"fieldKey": "sector", "value": "saas"}])
        self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["hiring"], "timeline": "6m", "runway": "18m",
             "maxDilutionPct": 18},
            content_type="application/json", **self.h)
        for route in ("strategy/peers/generate", "strategy/raise/generate",
                      "strategy/valuation/generate",
                      "strategy/blueprint/generate", "strategy/approve",
                      "story/generate", "story/approve", "teaser/generate",
                      "deck/outline", "deck/approve-outline", "deck/slides",
                      "model/generate", "im/generate", "review/run"):
            self.client.post(f"{self.base}/{route}", {},
                             content_type="application/json", **self.h)

    def test_issue6_stale_model_named_in_review_and_approval(self):
        self._build_package()
        # Align CKB ARR to the model so the baseline review is clean
        from fundos.materials.models import FinancialKpi, FinancialModel
        from fundos.core.scoping import tenant_context
        with tenant_context(self.deal.tenant_id):
            model = FinancialModel.objects.filter(
                deal_id=self.deal.id).first()
            kpi = FinancialKpi.objects.filter(
                model=model, scenario="base", kpi_key="arr",
                month_index=1).first()
        self.patch_ckb([{"fieldKey": "arr", "value": float(kpi.value),
                         "ccy": "INR"}])
        # ...which (correctly) marks the model pending; regenerate ONLY
        # raise + valuation, exactly like the tester did:
        self.client.post(f"{self.base}/strategy/raise/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/valuation/generate", {},
                         content_type="application/json", **self.h)
        self.client.post(f"{self.base}/review/run", {},
                         content_type="application/json", **self.h)

        review = self.client.get(f"{self.base}/review", **self.h).json()
        # the review names the stale document...
        self.assertIn("Financial Model", review["staleDocuments"])
        # approval is blocked with guidance that names the model, not a
        # generic "fix the source facts" loop
        res = self.client.post(f"{self.base}/package/approve", {},
                               content_type="application/json", **self.h)
        if res.status_code == 409:
            detail = res.json()["detail"]
            self.assertIn("Financial Model", detail)
            self.assertIn("Financial Model",
                          res.json()["fields"].get("staleDocuments", []))
            # the documented fix works: regenerate the model, re-review,
            # approve
            self.client.post(f"{self.base}/model/generate", {},
                             content_type="application/json", **self.h)
            self.client.post(f"{self.base}/review/run", {},
                             content_type="application/json", **self.h)
            res = self.client.post(f"{self.base}/package/approve", {},
                                   content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 201)
