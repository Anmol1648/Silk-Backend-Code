"""
Regression tests for the Round-2 tester report (FundOS_Issue_Report_2076,
Jul 2026). Each test is pinned to the issue number it protects.
"""
from django.test import TestCase, override_settings

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class IssueFixBase(TestCase):
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


class CkbCatalogue(IssueFixBase):
    def test_issue5_fresh_deal_serves_field_groups(self):
        """Issues 5/6/16: a brand-new deal must expose the full editable
        field catalogue, not `groups: {}`."""
        body = self.client.get(f"{self.base}/ckb", **self.h).json()
        self.assertTrue(body["groups"], "groups must never be empty")
        for g in ("company", "business", "financial", "customers", "team"):
            self.assertIn(g, body["groups"], f"group '{g}' missing")
        fin_keys = {f["fieldKey"] for f in body["groups"]["financial"]}
        for key in ("arr", "mrr", "revenue", "burn", "cash_balance", "cac"):
            self.assertIn(key, fin_keys, f"financial field '{key}' missing")
        arr = [f for f in body["groups"]["financial"]
               if f["fieldKey"] == "arr"][0]
        self.assertIsNone(arr["value"])
        self.assertFalse(arr["verified"])
        self.assertEqual(arr["ccy"], "INR")

    def test_issue16_arr_entry_unblocks_valuation_prereq(self):
        """The catalogue field is genuinely writable — the E-VAL-422
        'ckb.arr required' dead-end is gone once the founder can type ARR."""
        res = self.client.patch(
            f"{self.base}/ckb",
            {"fields": [{"fieldKey": "arr", "value": 42000000,
                         "ccy": "INR"}]},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 200)
        body = self.client.get(f"{self.base}/ckb", **self.h).json()
        arr = [f for f in body["groups"]["financial"]
               if f["fieldKey"] == "arr"][0]
        self.assertEqual(float(arr["value"]), 42000000.0)
        self.assertTrue(arr["verified"])


class CrossTenantMembership(IssueFixBase):
    def _external_user(self):
        """A user homed in their OWN tenant (self-signup), like the
        tester's kothiyalanmol27@gmail.com account."""
        from fundos.core.services.onboarding import signup
        user, _ = signup("external.collab@example.com", name="External")
        return user

    def test_issue3_invited_cross_tenant_member_can_access_deal(self):
        external = self._external_user()
        self.assertNotEqual(external.tenant_id, self.deal.tenant_id)

        res = self.client.post(
            f"{self.base}/members",
            {"email": external.email, "role": "team"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 201)

        eh = auth_headers(external)
        # contexts must list the shared deal
        ctx = self.client.get("/api/v1/me/contexts", **eh).json()
        deal_ids = [c.get("dealId") for c in ctx["items"]]
        self.assertIn(str(self.deal.id), deal_ids)
        # context switch must succeed (was 403)
        res = self.client.post("/api/v1/contexts/switch",
                               {"dealId": str(self.deal.id)},
                               content_type="application/json", **eh)
        self.assertEqual(res.status_code, 200)
        # deal-scoped data must load (was 403)
        res = self.client.get(f"{self.base}/masterplan", **eh)
        self.assertEqual(res.status_code, 200)
        res = self.client.get(f"{self.base}/ckb", **eh)
        self.assertEqual(res.status_code, 200)

    def test_issue3_outsider_still_403(self):
        """The cross-tenant fix must NOT weaken isolation: a user with no
        membership still gets a clean 403."""
        from fundos.core.services.onboarding import signup
        outsider, _ = signup("outsider@example.com", name="Outsider")
        oh = auth_headers(outsider)
        res = self.client.get(f"{self.base}/masterplan", **oh)
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/v1/contexts/switch",
                               {"dealId": str(self.deal.id)},
                               content_type="application/json", **oh)
        self.assertEqual(res.status_code, 403)

    def test_issue4_member_events_create_notifications(self):
        external = self._external_user()
        self.client.post(f"{self.base}/members",
                         {"email": external.email, "role": "team"},
                         content_type="application/json", **self.h)
        # owner sees an invite notification
        notif = self.client.get("/api/v1/notifications", **self.h).json()
        types = [n["type"] for n in notif["items"]]
        self.assertIn("member.invited", types)
        # invited user sees one too
        eh = auth_headers(external)
        notif = self.client.get("/api/v1/notifications", **eh).json()
        self.assertIn("member.invited", [n["type"] for n in notif["items"]])
        # removal notifies as well
        self.client.delete(f"{self.base}/members/{external.id}", **self.h)
        notif = self.client.get("/api/v1/notifications", **self.h).json()
        self.assertIn("member.removed", [n["type"] for n in notif["items"]])


class CompanyDedupe(IssueFixBase):
    def test_issue2_repeat_wizard_does_not_duplicate_company(self):
        from fundos.core.models import Company
        payload = {"name": "Meridian Labs", "domain": "", "hqCountry": "IN"}
        r1 = self.client.post("/api/v1/companies", payload,
                              content_type="application/json", **self.h)
        r2 = self.client.post("/api/v1/companies", payload,
                              content_type="application/json", **self.h)
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        count = Company.all_objects.filter(
            name__iexact="Meridian Labs", is_deleted=False).count()
        self.assertEqual(count, 1)


class ReadinessContract(IssueFixBase):
    def test_issue8_get_readiness_is_200_before_generation(self):
        res = self.client.get(f"{self.base}/readiness", **self.h)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["generated"])
        self.assertEqual(body["dimensions"], [])

    def _generate(self):
        self.client.patch(
            f"{self.base}/ckb",
            {"fields": [{"fieldKey": "arr", "value": 42000000, "ccy": "INR"},
                        {"fieldKey": "growth_rate", "value": 80},
                        {"fieldKey": "founding_year", "value": 2023},
                        {"fieldKey": "runway_months", "value": 9},
                        {"fieldKey": "sector", "value": "saas"}]},
            content_type="application/json", **self.h)
        return self.client.post(f"{self.base}/readiness/generate", {},
                                content_type="application/json", **self.h)

    def test_issue12_generate_returns_202_with_job(self):
        res = self._generate()
        self.assertEqual(res.status_code, 202)
        body = res.json()
        self.assertIn("jobId", body)
        self.assertIn("poll", body)
        job = self.client.get(f"{self.base}/jobs/{body['jobId']}",
                              **self.h).json()
        self.assertEqual(job["status"], "succeeded")

    def test_issue14_numeric_score_always_served(self):
        self._generate()
        body = self.client.get(f"{self.base}/readiness", **self.h).json()
        self.assertIn("overallScore", body)
        self.assertGreaterEqual(body["overallScore"], 0)
        self.assertTrue(any("subScore" in d for d in body["dimensions"]))

    def test_issue13_mock_summary_consistent_with_dimensions(self):
        """Issues 9/13: the narrative must be derived from the real
        deterministic result — a dimension the engine marks 'missing' can
        never be praised in the summary."""
        self._generate()
        body = self.client.get(f"{self.base}/readiness", **self.h).json()
        summary = body["summary"]
        self.assertIn(body["overallBand"], summary)
        missing = [d["dimension"] for d in body["dimensions"]
                   if d["status"] == "missing"]
        ready = [d["dimension"] for d in body["dimensions"]
                 if d["status"] == "ready"]
        for dim in missing:
            # a missing dimension may be listed only under the
            # not-yet-evidenced clause, never in the well-documented one
            if "Well documented:" in summary:
                well = summary.split("Well documented:")[1].split(".")[0]
                self.assertNotIn(dim, well)
        # every dimension carries a status-consistent explanation
        for d in body["dimensions"]:
            self.assertTrue(d["explanation"])

    def test_issue10_gap_kinds_present(self):
        self._generate()
        gaps = self.client.get(f"{self.base}/readiness/gaps",
                               **self.h).json()["items"]
        kinds = {g["kind"] for g in gaps}
        self.assertIn("recommendation", kinds)
        # texts are real strings, never null/"None"
        for g in gaps:
            self.assertTrue(g["text"] and g["text"] != "None")


class PeerGroupA(IssueFixBase):
    def test_issue15_group_a_is_populated_for_an_in_universe_company(self):
        """TC-049 regression, NARROWED in v29.

        The original assertion was "Group A must never be empty", and its
        scenario is an AGRITECH company against `_FIXTURE_PEERS` — four SaaS
        companies and one Fintech. The only way to satisfy it was to promote
        an unrelated SaaS peer into "Closest Comparables" and tell an
        agritech founder it was their closest comparable.

        That is the same mis-grouping the assessment path deliberately
        refuses: `resolve_sub_sector` will not fuzzy-match below a high floor
        because a confident number against the wrong comparators is worse
        than a blank. Both halves of the product now follow that rule.

        What Issue 15 was actually complaining about survives and is tested
        here: a company that IS in the peer universe must not fall through to
        an empty Group A merely because nothing clears the absolute >=60 bar.
        The relative promotion still fires at or above
        `PEER_PROMOTION_FLOOR`.

        The out-of-sector case is asserted in
        `tests/api/test_v29_defect_closure.py`. To restore the pre-v29
        behaviour exactly, set `strategy.services.PEER_PROMOTION_FLOOR = 0`.
        """
        self.client.patch(
            f"{self.base}/ckb",
            {"fields": [{"fieldKey": "sector", "value": "SaaS"},
                        {"fieldKey": "subsector", "value": "Vertical SaaS"},
                        {"fieldKey": "business_model", "value": "b2b_saas"},
                        {"fieldKey": "arr", "value": 150000000, "ccy": "INR"},
                        {"fieldKey": "growth_rate", "value": 85},
                        {"fieldKey": "employees", "value": 55}]},
            content_type="application/json", **self.h)
        self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["hiring"], "timeline": "6m", "runway": "18m",
             "maxDilutionPct": 18},
            content_type="application/json", **self.h)
        res = self.client.post(f"{self.base}/strategy/peers/generate", {},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 202)
        body = self.client.get(f"{self.base}/strategy/peers", **self.h).json()
        groups = {p["groupCode"] for p in body["peers"]}
        self.assertIn(
            "A", groups,
            "a company inside the peer universe must still get Closest "
            "Comparables — this is the half of Issue 15 that was right")

    def test_issue15_does_not_fabricate_a_comparable_out_of_sector(self):
        """The half of Issue 15 that was wrong, stated as its own contract."""
        self.client.patch(
            f"{self.base}/ckb",
            {"fields": [{"fieldKey": "sector", "value": "agritech"},
                        {"fieldKey": "arr", "value": 42000000, "ccy": "INR"},
                        {"fieldKey": "growth_rate", "value": 80},
                        {"fieldKey": "employees", "value": 22}]},
            content_type="application/json", **self.h)
        self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["hiring"], "timeline": "6m", "runway": "18m",
             "maxDilutionPct": 18},
            content_type="application/json", **self.h)
        res = self.client.post(f"{self.base}/strategy/peers/generate", {},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 202)
        body = self.client.get(f"{self.base}/strategy/peers", **self.h).json()
        promoted = [p for p in body["peers"] if p["groupCode"] == "A"]
        for p in promoted:
            self.assertGreaterEqual(
                float(p.get("similarityScore") or 0), 45.0,
                f"{p['name']} was presented as a Closest Comparable to an "
                f"agritech company on a score below the promotion floor")


class ChunkedUploadSession(IssueFixBase):
    def test_issue7_session_serves_uploadid_and_accepts_raw_chunks(self):
        res = self.client.post(
            f"{self.base}/uploads/sessions",
            {"filename": "big.pdf", "size": 60 * 1024 * 1024,
             "mime": "application/pdf", "category": "financial"},
            content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertIn("uploadId", body)
        self.assertEqual(body["uploadId"], body["sessionId"])
        # a raw octet-stream chunk PUT is accepted (the UI's wire format)
        res = self.client.put(
            f"{self.base}/uploads/sessions/{body['uploadId']}/chunks/0",
            data=b"%PDF-1.4 raw chunk bytes",
            content_type="application/octet-stream", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["chunksReceived"], 1)
