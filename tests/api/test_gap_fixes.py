"""
Gap-closure tests — one class per gap-analysis finding plus the additional
gaps discovered in the cross-functionality sweep (refresh token, signed
file downloads, BR-S2-013 enforcement, OTP TTL from settings).
"""
import hashlib

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fundos.core.auth import issue_tokens
from fundos.core.models import (
    CkbField, GenerationJob, Membership, OtpToken, User,
)
from tests.conftest_helpers import make_world


def _client(user):
    client = APIClient()
    access, _ = issue_tokens(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def _seed_config():
    from django.core.management import call_command
    call_command("seed_initial_data", "--demo", verbosity=0)


class OtpContractTests(TestCase):
    """G1 + sweep: field alias, distinct errors, TTL from settings."""

    def setUp(self):
        self.world = make_world()
        self.founder = self.world["a"]["founder"]

    def _issue(self, code="123456"):
        OtpToken.issue(self.founder.email, code,
                       ttl_seconds=300, max_attempts=5)

    def test_verify_accepts_otp_field_alias(self):
        self._issue()
        response = APIClient().post(
            "/api/v1/auth/otp/verify",
            {"email": self.founder.email, "otp": "123456"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("accessToken", response.json())

    def test_missing_code_is_422_not_403(self):
        self._issue()
        response = APIClient().post(
            "/api/v1/auth/otp/verify",
            {"email": self.founder.email}, format="json")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "E-VAL-422")

    def test_wrong_code_is_403(self):
        self._issue()
        response = APIClient().post(
            "/api/v1/auth/otp/verify",
            {"email": self.founder.email, "code": "000000"}, format="json")
        self.assertEqual(response.status_code, 403)

    @override_settings(FUNDOS_OTP_TTL_SECONDS=300)
    def test_request_uses_settings_ttl(self):
        response = APIClient().post(
            "/api/v1/auth/otp/request",
            {"email": self.founder.email}, format="json")
        self.assertEqual(response.status_code, 200)
        token = OtpToken.objects.get(identity=self.founder.email)
        from django.utils import timezone
        remaining = (token.expires_at - timezone.now()).total_seconds()
        self.assertLessEqual(remaining, 300 + 5)   # 5-minute TTL, not 10


class RefreshTokenTests(TestCase):
    """Sweep: refresh tokens were issued but there was no endpoint."""

    def setUp(self):
        self.world = make_world()
        self.founder = self.world["a"]["founder"]

    def test_refresh_rotates_tokens(self):
        _, refresh = issue_tokens(self.founder)
        response = APIClient().post("/api/v1/auth/refresh",
                                    {"refreshToken": refresh}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("accessToken", response.json())

    def test_access_token_rejected_as_refresh(self):
        access, _ = issue_tokens(self.founder)
        response = APIClient().post("/api/v1/auth/refresh",
                                    {"refreshToken": access}, format="json")
        self.assertEqual(response.status_code, 403)


class OnboardingTests(TestCase):
    """G2: signup → (explicit) company → deal, all through the API."""

    def test_full_onboarding_flow(self):
        # 1) self-signup creates tenant + user ONLY — never a company
        #    (company-profile-generation-flow.md §2.2; backend-issues #3).
        response = APIClient().post(
            "/api/v1/auth/signup",
            {"email": "new-founder@startup.in", "name": "New Founder",
             "companyName": "Startup Pvt Ltd"}, format="json")
        self.assertEqual(response.status_code, 202)
        user = User.objects.get(email="new-founder@startup.in")
        self.assertIsNotNone(user.tenant_id)

        # 2) OTP login (craft a known code)
        token = OtpToken.objects.get(identity=user.email)
        token.otp_hash = hashlib.sha256(b"424242").hexdigest()
        token.save(update_fields=["otp_hash"])
        response = APIClient().post(
            "/api/v1/auth/otp/verify",
            {"email": user.email, "code": "424242"}, format="json")
        self.assertEqual(response.status_code, 200)

        client = _client(user)
        # 3) new user lands on the empty state — NO company auto-created
        response = client.get("/api/v1/companies")
        self.assertEqual(len(response.json()["items"]), 0,
                         "signup must not auto-create a company")

        # 3b) company is created only via the explicit Create Company flow
        response = client.post("/api/v1/companies",
                               {"name": "Startup Pvt Ltd"}, format="json")
        self.assertEqual(response.status_code, 201)
        company_id = response.json()["id"]

        # 4) create a deal — provisions membership + stages + CKB
        response = client.post(f"/api/v1/companies/{company_id}/deals",
                               {"name": "Seed Round", "roundType": "seed"},
                               format="json")
        self.assertEqual(response.status_code, 201)
        deal_id = response.json()["dealId"]

        # 5) the deal is immediately usable
        response = client.get(f"/api/v1/deals/{deal_id}/masterplan")
        self.assertEqual(response.status_code, 200)
        response = client.get(f"/api/v1/deals/{deal_id}/ckb")
        self.assertEqual(response.status_code, 200)

    def test_signup_uniform_for_existing_user(self):
        world = make_world()
        response = APIClient().post(
            "/api/v1/auth/signup",
            {"email": world["a"]["founder"].email}, format="json")
        self.assertEqual(response.status_code, 202)   # never reveals existence

    def test_deal_creation_requires_company_ownership(self):
        world = make_world()
        outsider = world["b"]["founder"]
        company = world["a"]["company"]
        response = _client(outsider).post(
            f"/api/v1/companies/{company.id}/deals",
            {"name": "Hijack"}, format="json")
        self.assertEqual(response.status_code, 403)


class ContextSwitchTests(TestCase):
    """G3: POST /contexts/switch records the active deal."""

    def setUp(self):
        self.world = make_world()

    def test_switch_and_reflect_in_contexts(self):
        founder, deal = (self.world["a"]["founder"], self.world["a"]["deal"])
        client = _client(founder)
        response = client.post("/api/v1/contexts/switch",
                               {"dealId": str(deal.id)}, format="json")
        self.assertEqual(response.status_code, 200)
        response = client.get("/api/v1/me/contexts")
        self.assertEqual(response.json()["lastActiveDealId"], str(deal.id))

    def test_switch_requires_membership(self):
        founder = self.world["a"]["founder"]
        other_deal = self.world["b"]["deal"]
        response = _client(founder).post(
            "/api/v1/contexts/switch",
            {"dealId": str(other_deal.id)}, format="json")
        self.assertEqual(response.status_code, 403)


class Stage2JourneyMixin:
    """Drives the Stage-2 journey up to a given point."""

    def _login(self):
        self.founder = self.world["a"]["founder"]
        self.deal = self.world["a"]["deal"]
        self.client_api = _client(self.founder)
        self.base = f"/api/v1/deals/{self.deal.id}"

    def _fill_ckb(self):
        self.client_api.patch(f"{self.base}/ckb", {"fields": [
            {"fieldKey": "sector", "value": "SaaS"},
            {"fieldKey": "arr", "value": 120000000, "ccy": "INR"},
            {"fieldKey": "revenue", "value": 120000000, "ccy": "INR"},
            {"fieldKey": "burn", "value": 4000000, "ccy": "INR"},
        ]}, format="json")

    def _objectives(self):
        return self.client_api.put(f"{self.base}/strategy/objectives", {
            "purposes": ["growth"], "timeline": "6m", "runway": "18m",
        }, format="json")

    def _generate(self, what):
        return self.client_api.post(f"{self.base}/strategy/{what}/generate",
                                    {}, format="json")


class ValuationIntegrityTests(Stage2JourneyMixin, TestCase):
    """G5 + G7: ordering enforced, degenerate valuation refused, approval
    invariant."""

    def setUp(self):
        _seed_config()
        self.world = make_world()
        self._login()

    def test_valuation_blocked_before_raise(self):
        self._fill_ckb()
        response = self._generate("valuation")
        self.assertEqual(response.status_code, 409)   # ordering (G5)

    def test_valuation_refused_without_financials(self):
        self._objectives()
        self._generate("peers")
        # No ARR/revenue in the CKB → objectives→raise still work,
        # but valuation must refuse instead of persisting zeros.
        self._generate("raise")
        response = self._generate("valuation")
        self.assertIn(response.status_code, (409, 422))
        from fundos.strategy.models import Valuation
        self.assertFalse(Valuation.objects.filter(
            deal_id=self.deal.id).exists())   # nothing degenerate persisted

    def test_happy_path_valuation_is_nonzero_and_inr(self):
        self._fill_ckb()
        self._objectives()
        self._generate("peers")
        self._generate("raise")
        response = self._generate("valuation")
        self.assertEqual(response.status_code, 202)
        response = self.client_api.get(f"{self.base}/strategy/valuation")
        body = response.json()
        self.assertGreater(body["range"]["high"]["value"], 0)
        # G13: presentation edge is INR with the USD value preserved
        self.assertEqual(body["range"]["high"]["ccy"], "INR")
        self.assertIn("usdValue", body["range"]["high"])
        self.assertEqual(body["fx"]["presentationCcy"], "INR")

    def test_approval_rejects_zero_valuation(self):
        self._fill_ckb()
        self._objectives()
        self._generate("peers")
        self._generate("raise")
        self._generate("valuation")
        self.client_api.post(f"{self.base}/strategy/blueprint/generate",
                             {}, format="json")
        # Corrupt the persisted valuation to simulate the G5 legacy state.
        from fundos.strategy.models import Valuation
        Valuation.objects.filter(deal_id=self.deal.id).update(
            range_low_value=0, range_high_value=0)
        response = self.client_api.post(f"{self.base}/strategy/approve",
                                        {}, format="json")
        self.assertEqual(response.status_code, 409)   # G7 invariant


class JobStatusTests(Stage2JourneyMixin, TestCase):
    """G6: generate returns a job handle; the job endpoint reports status."""

    def setUp(self):
        _seed_config()
        self.world = make_world()
        self._login()

    def test_generate_returns_pollable_job(self):
        self._fill_ckb()
        self._objectives()
        response = self._generate("peers")
        body = response.json()
        self.assertIn("jobId", body)
        response = self.client_api.get(f"{self.base}/jobs/{body['jobId']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "succeeded")  # eager dev
        self.assertIsNotNone(response.json()["artifactId"])

    def test_failed_job_records_error(self):
        self._fill_ckb()
        # raise before objectives → the view pre-check rejects synchronously
        response = self._generate("raise")
        self.assertEqual(response.status_code, 422)
        # valuation before raise → 409 and no queued job left dangling
        response = self._generate("valuation")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(GenerationJob.objects.filter(
            deal_id=self.deal.id, status="queued").exists())

    def test_completion_notification_created(self):
        self._fill_ckb()
        self._objectives()
        self._generate("peers")
        from fundos.core.models import Notification
        self.assertTrue(Notification.objects.filter(
            recipient=self.founder, type="generation.completed").exists())


class PeerCurationRuleTests(Stage2JourneyMixin, TestCase):
    """Sweep: BR-S2-013 — never drop below 3 comparables."""

    def setUp(self):
        _seed_config()
        self.world = make_world()
        self._login()

    def test_remove_blocked_at_three_peers(self):
        self._fill_ckb()
        self._objectives()
        self._generate("peers")
        from fundos.strategy.models import PeerCompany
        peers = list(PeerCompany.objects.filter(deal_id=self.deal.id))
        # Remove until 3 remain — the next removal must 409.
        removable = peers[:len(peers) - 3]
        for peer in removable:
            response = self.client_api.delete(
                f"{self.base}/strategy/peers/{peer.id}")
            self.assertEqual(response.status_code, 204)
        last = PeerCompany.objects.filter(deal_id=self.deal.id).first()
        response = self.client_api.delete(
            f"{self.base}/strategy/peers/{last.id}")
        self.assertEqual(response.status_code, 409)


class ContractAliasTests(Stage2JourneyMixin, TestCase):
    """G9: POST aliases where frontends guessed wrong."""

    def setUp(self):
        _seed_config()
        self.world = make_world()
        self._login()

    def test_objectives_post_alias(self):
        response = self.client_api.post(f"{self.base}/strategy/objectives", {
            "purposes": ["growth"], "timeline": "6m", "runway": "18m",
        }, format="json")
        self.assertEqual(response.status_code, 201)

    def test_ckb_per_field_patch(self):
        response = self.client_api.patch(
            f"{self.base}/ckb/fields/sector",
            {"value": "Fintech"}, format="json")
        self.assertEqual(response.status_code, 200)
        field = CkbField.objects.get(deal_id=self.deal.id,
                                     field_key="sector")
        self.assertEqual(field.value, "Fintech")


class ThrottlingTests(TestCase):
    """G15: OTP request throttled per identity+IP."""

    def setUp(self):
        self.world = make_world()

    @override_settings(FUNDOS_THROTTLE_OTP_REQUEST="2/hour")
    def test_otp_request_throttled(self):
        from django.core.cache import cache
        cache.clear()
        client = APIClient()
        payload = {"email": self.world["a"]["founder"].email}
        self.assertEqual(client.post("/api/v1/auth/otp/request", payload,
                                     format="json").status_code, 200)
        self.assertEqual(client.post("/api/v1/auth/otp/request", payload,
                                     format="json").status_code, 200)
        response = client.post("/api/v1/auth/otp/request", payload,
                               format="json")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "E-RATE-429")


class DashboardTests(TestCase):
    """Sweep: /dashboard and /stage-state design surfaces."""

    def setUp(self):
        _seed_config()
        self.world = make_world()
        self.client_api = _client(self.world["a"]["founder"])
        self.base = f"/api/v1/deals/{self.world['a']['deal'].id}"

    def test_dashboard(self):
        response = self.client_api.get(f"{self.base}/dashboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("stages", body)
        self.assertIn("ckbCompletenessPct", body)

    def test_stage_state(self):
        response = self.client_api.get(f"{self.base}/stage-state")
        self.assertEqual(response.status_code, 200)
        # PRD §2.4 — the journey is now nine stages (0-8): Investor
        # Materials is no longer a numbered stage, and everything after it
        # shifts down by one. The count comes from the admin-configured
        # stage registry rather than a literal.
        from fundos.platformcfg.services import stages as configured_stages
        self.assertEqual(len(response.json()["stages"]), len(configured_stages()))
