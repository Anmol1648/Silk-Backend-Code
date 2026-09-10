"""
End-to-end API flow tests (Doc 8 §6) with ai_mocked=True and eager Celery.

Covers: OTP login; CKB PATCH + AI-suggestion parking; the full happy path
Stage 1 (research → readiness) → Stage 2 (objectives → peers → raise →
valuation → blueprint → approve) → Stage 3 (story → teaser → deck →
model → im → review → package); hard-gate E-GATE-423 before approval.
"""
from django.core import mail
from django.test import TestCase, override_settings

from fundos.core.scoping import tenant_context
from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class FlowTestBase(TestCase):
    def setUp(self):
        from fundos.config.models import AppConfiguration
        from django.core.management import call_command
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


class AuthTests(FlowTestBase):
    def test_otp_login_flow(self):
        import hashlib
        from fundos.core.models import OtpToken

        response = self.client.post(
            "/api/v1/auth/otp/request",
            {"email": self.founder.email}, content_type="application/json")
        self.assertEqual(response.status_code, 200)

        token = OtpToken.objects.get(identity=self.founder.email)
        # Recover the code by brute-forcing is unnecessary — craft a known one.
        token.otp_hash = hashlib.sha256(b"123456").hexdigest()
        token.save()
        response = self.client.post(
            "/api/v1/auth/otp/verify",
            {"email": self.founder.email, "code": "123456"},
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("accessToken", response.json())
        # single-use
        response = self.client.post(
            "/api/v1/auth/otp/verify",
            {"email": self.founder.email, "code": "123456"},
            content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_otp_request_uniform_for_unknown_email(self):
        response = self.client.post(
            "/api/v1/auth/otp/request", {"email": "ghost@nowhere.io"},
            content_type="application/json")
        self.assertEqual(response.status_code, 200)


class CkbTests(FlowTestBase):
    def test_patch_and_read(self):
        response = self.patch_ckb([
            {"fieldKey": "arr", "value": 120000000, "ccy": "INR"},
            {"fieldKey": "sector", "value": "SaaS"}])
        self.assertEqual(response.status_code, 200)
        response = self.client.get(f"{self.base}/ckb", **self.h)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        flat = {f["fieldKey"]: f for group in body["groups"].values()
                for f in group}
        self.assertEqual(flat["arr"]["value"], 120000000)
        self.assertTrue(flat["arr"]["verified"])   # founder edits verify

    def test_numeric_validation_e_val_422(self):
        response = self.patch_ckb([{"fieldKey": "arr", "value": -5}])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "E-VAL-422")

    def test_ai_never_overwrites_verified(self):
        """BR-M0-011: AI research parks a suggestion on verified fields."""
        from fundos.core.models import CkbField
        from fundos.core.services import ckb_service

        self.patch_ckb([{"fieldKey": "sector", "value": "SaaS"}])
        with tenant_context(self.deal.tenant_id):
            field, _ = ckb_service.set_field(
                self.deal, "sector", "Fintech", source="ai_research",
                verify=False)
        field = CkbField.objects.get(deal_id=self.deal.id,
                                     field_key="sector")
        self.assertEqual(field.value, "SaaS")            # untouched
        self.assertIsNotNone(field.suggested_value_json)  # parked

        # Founder rejects the suggestion.
        response = self.client.post(
            f"{self.base}/ckb/suggestions/sector", {"action": "reject"},
            content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 200)
        field.refresh_from_db()
        self.assertIsNone(field.suggested_value_json)


class StageGateTests(FlowTestBase):
    def test_stage3_workspace_reads_before_strategy_approval(self):
        """CR-02 / C6 — navigation is never blocked.

        This previously asserted E-GATE-423 on the workspace READ, which
        made the whole Stage-3 page render as a locked panel. The read now
        succeeds and reports the outstanding prerequisite instead; the
        generation actions remain guarded (see the test below), which is
        where the integrity constraint belongs.
        """
        response = self.client.get(f"{self.base}/workspace", **self.h)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["available"])
        self.assertIn("prerequisites", body)
        self.assertEqual(body["documents"], [])

    def test_stage3_generation_still_refused_without_strategy(self):
        """The page opens; the actions do not."""
        for path in ("story/generate", "model/generate", "im/generate"):
            response = self.client.post(f"{self.base}/{path}", {},
                                        content_type="application/json",
                                        **self.h)
            self.assertGreaterEqual(response.status_code, 400, path)

    def test_stages_are_never_locked(self):
        """C6 / PRD §2.2 — stages are never locked.

        This previously asserted that Stage 3 was hard-gated and could not be
        opened. Per the clarifications the gate is now ADVISORY: the stage
        opens, and only the actions that genuinely need an approved strategy
        stay disabled. Opening a stage must therefore succeed.
        """
        response = self.client.post(f"{self.base}/stages/3/override", **self.h)
        self.assertEqual(response.status_code, 200)

    def test_prerequisites_are_reported_not_enforced(self):
        """Prerequisites surface as met/pending rather than blocking entry."""
        from django.core.management import call_command

        from fundos.core.services import stage_state

        # Stage definitions (and their advisory prerequisites) are
        # admin-owned configuration, so seed them for this test.
        call_command("seed_platform_config", verbosity=0)

        # No approved strategy and no manually entered targets yet.
        prereqs = stage_state.prerequisite_status(self.deal.id, 3)
        self.assertTrue(prereqs, "stage 3 should declare a prerequisite")
        self.assertFalse(any(p["met"] for p in prereqs))
        # …but the gate helper never blocks.
        self.assertTrue(stage_state.hard_gate_met(self.deal.id, 3))


class FullJourneyTests(FlowTestBase):
    def seed_ckb(self):
        self.patch_ckb([
            {"fieldKey": "arr", "value": 120000000, "ccy": "INR"},
            {"fieldKey": "revenue", "value": 120000000, "ccy": "INR"},
            {"fieldKey": "burn", "value": 4000000, "ccy": "INR"},
            {"fieldKey": "runway_months", "value": 14},
            {"fieldKey": "growth_rate", "value": 90},
            {"fieldKey": "sector", "value": "SaaS"},
            {"fieldKey": "geography", "value": "India"},
            {"fieldKey": "customers", "value": 40},
            {"fieldKey": "employees", "value": 25},
            {"fieldKey": "founding_year", "value": 2021},
        ])

    def test_happy_path_stage1_to_3(self):
        self.seed_ckb()

        # --- Stage 1: research (mocked adapters/synthesis) ---
        response = self.client.post(f"{self.base}/research/run", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/research/sources", **self.h)
        self.assertEqual(response.status_code, 200)
        sources = {s["sourceType"]: s for s in response.json()["items"]}
        self.assertNotIn("linkedin", sources)      # legally gated (BR-M1-003)
        self.assertIn("market", sources)

        # --- Stage 1: readiness ---
        # Issue 12: generation is async like every other generator.
        response = self.client.post(f"{self.base}/readiness/generate", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        job = response.json()
        self.assertIn("jobId", job)
        response = self.client.get(
            f"{self.base}/jobs/{job['jobId']}", **self.h)
        self.assertEqual(response.json()["status"], "succeeded")
        response = self.client.get(f"{self.base}/readiness", **self.h)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["generated"])
        self.assertIn(body["overallBand"],
                      ("Ready", "Needs preparation", "Not yet ready"))
        self.assertEqual(body["label"], "AI Generated Insights")
        self.assertIn("overallScore", body)        # Issue 14: always surfaced
        response = self.client.get(f"{self.base}/readiness/gaps", **self.h)
        self.assertEqual(response.status_code, 200)

        # --- Stage 2 ---
        response = self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["gtm"], "timeline": "6m", "runway": "18m",
             "maxDilutionPct": 20},
            content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 201)

        for step in ("peers", "raise", "valuation"):
            response = self.client.post(
                f"{self.base}/strategy/{step}/generate", {},
                content_type="application/json", **self.h)
            self.assertEqual(response.status_code, 202, step)

        response = self.client.get(f"{self.base}/strategy/peers", **self.h)
        self.assertEqual(response.status_code, 200)
        peers = response.json()
        self.assertGreaterEqual(len(peers["peers"]), 3)
        self.assertTrue(all(p["similarityBreakdown"]
                            for p in peers["peers"]))   # explainable

        response = self.client.get(f"{self.base}/strategy/raise", **self.h)
        raise_body = response.json()
        self.assertEqual(len(raise_body["scenarios"]), 3)

        response = self.client.get(f"{self.base}/strategy/valuation", **self.h)
        valuation = response.json()
        self.assertIn("disclaimer", valuation)     # SEBI, inseparable
        self.assertLess(valuation["range"]["low"]["value"],
                        valuation["range"]["high"]["value"])

        response = self.client.post(
            f"{self.base}/strategy/blueprint/generate", {},
            content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/strategy/blueprint", **self.h)
        blueprint = response.json()
        self.assertGreaterEqual(len(blueprint["alternatives"]), 3)

        response = self.client.post(
            f"{self.base}/strategy/approve",
            {"selectedInstrument": "equity"},
            content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 201)
        profile = response.json()
        self.assertTrue(profile["preferredInvestorCategories"]["primary"])

        # --- Stage 3 (gate now open) ---
        response = self.client.get(f"{self.base}/workspace", **self.h)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(f"{self.base}/story/generate", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/story", **self.h)
        story = response.json()
        self.assertEqual(len(story["components"]), 12)
        self.assertGreaterEqual(len(story["positioningOptions"]), 3)

        response = self.client.post(f"{self.base}/teaser/generate", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/teaser", **self.h)
        teaser = response.json()
        mandatory = [s for s in teaser["sections"] if s["isMandatory"]]
        self.assertGreaterEqual(len(mandatory), 4)

        # Deck — two-step, story-first: slides blocked until outline approved.
        response = self.client.post(f"{self.base}/deck/outline", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 201)
        response = self.client.post(f"{self.base}/deck/slides", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 423)   # BR-M3-040 / E-GATE-423
        response = self.client.post(f"{self.base}/deck/approve-outline", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"{self.base}/deck/slides", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.json()["slides"]), 8)

        # Financial model — generate, read statements, edit an assumption.
        response = self.client.post(f"{self.base}/model/generate", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/model/statements", **self.h)
        statements = response.json()
        self.assertIn("pl.revenue", statements["lines"])
        self.assertEqual(len(statements["lines"]["pl.revenue"]), 36)
        month12_before = statements["lines"]["pl.revenue"][11]

        response = self.client.patch(
            f"{self.base}/model/assumptions",
            {"group": "revenue", "key": "arpa_monthly", "value": 200000},
            content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 200)
        response = self.client.get(f"{self.base}/model/statements", **self.h)
        month12_after = response.json()["lines"]["pl.revenue"][11]
        self.assertGreater(month12_after, month12_before)   # [DET] recompute

        # IM
        response = self.client.post(f"{self.base}/im/generate", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/im", **self.h)
        self.assertEqual(len(response.json()["sections"]), 16)

        # Review, then package approval freezes the baseline.
        response = self.client.post(f"{self.base}/review/run", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 202)
        response = self.client.get(f"{self.base}/review", **self.h)
        review = response.json()
        self.assertEqual(len(review["categoryNotes"]), 6)

        response = self.client.post(f"{self.base}/package/approve", {},
                                    content_type="application/json", **self.h)
        if response.status_code == 409:
            # BUG-022 guardrail: the assumption edit above moved the model's
            # ARR away from the CKB's — approval is (correctly) blocked
            # until the source fact is aligned and the review re-run.
            from fundos.core.scoping import tenant_context
            with tenant_context(self.deal.tenant_id):
                from fundos.materials.models import FinancialKpi, FinancialModel
                model = FinancialModel.objects.filter(
                    deal_id=self.deal.id).first()
                kpi = FinancialKpi.objects.filter(
                    model=model, scenario="base", kpi_key="arr",
                    month_index=1).first()
            self.patch_ckb([{"fieldKey": "arr",
                             "value": float(kpi.value), "ccy": "INR"}])
            response = self.client.post(f"{self.base}/review/run", {},
                                        content_type="application/json",
                                        **self.h)
            self.assertEqual(response.status_code, 202)
            response = self.client.post(f"{self.base}/package/approve", {},
                                        content_type="application/json",
                                        **self.h)
        self.assertEqual(response.status_code, 201)
        package = response.json()
        self.assertEqual(len(package["documentVersionIds"]), 4)

        # Masterplan reflects progress.
        response = self.client.get(f"{self.base}/masterplan", **self.h)
        stages = {s["stageNo"]: s for s in response.json()["stages"]}
        self.assertTrue(stages[3]["hardGateMet"])
        self.assertGreater(stages[3]["completionPct"], 50)

    def test_package_approval_requires_review(self):
        self.seed_ckb()
        # Fast-forward to an approved profile without any Stage-3 docs.
        self.client.put(f"{self.base}/strategy/objectives",
                        {"purposes": ["gtm"], "timeline": "6m",
                         "runway": "18m"},
                        content_type="application/json", **self.h)
        for step in ("peers", "raise", "valuation", "blueprint"):
            self.client.post(f"{self.base}/strategy/{step}/generate", {},
                             content_type="application/json", **self.h)
        self.client.post(f"{self.base}/strategy/approve", {},
                         content_type="application/json", **self.h)
        response = self.client.post(f"{self.base}/package/approve", {},
                                    content_type="application/json", **self.h)
        self.assertEqual(response.status_code, 409)


class IdempotencyTests(FlowTestBase):
    def test_idempotent_replay_same_key(self):
        """Same Idempotency-Key + body → replayed response; different body →
        E-IDEMP-409 (Doc 4 §0)."""
        payload = {"purposes": ["gtm"], "timeline": "6m", "runway": "18m"}
        first = self.client.put(
            f"{self.base}/strategy/objectives", payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-1", **self.h)
        self.assertEqual(first.status_code, 201)
        replay = self.client.put(
            f"{self.base}/strategy/objectives", payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-1", **self.h)
        self.assertEqual(replay.status_code, first.status_code)
        self.assertEqual(replay.json(), first.json())

        conflict = self.client.put(
            f"{self.base}/strategy/objectives",
            {"purposes": ["hiring"], "timeline": "6m", "runway": "18m"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="key-1", **self.h)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"], "E-IDEMP-409")
