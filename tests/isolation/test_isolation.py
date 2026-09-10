"""
Isolation tests (Doc 8 §6 — built FIRST): cross-tenant and cross-deal access
must fail with the E-AUTHZ-403 envelope; a user with two deals sees only the
requested deal's data.
"""
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


class CrossTenantIsolationTests(TestCase):
    def setUp(self):
        self.world = make_world()

    def test_cross_tenant_deal_access_forbidden(self):
        """Founder A must not read Tenant B's deal — generic 403, never 404
        (VAL-M0-001: existence is never revealed)."""
        deal_b = self.world["b"]["deal"]
        response = self.client.get(
            f"/api/v1/deals/{deal_b.id}/masterplan",
            **auth_headers(self.world["a"]["founder"]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "E-AUTHZ-403")

    def test_cross_deal_same_tenant_forbidden(self):
        """Membership is per-deal: Founder A1 cannot read Deal A-2."""
        deal_a2 = self.world["a2"]["deal"]
        response = self.client.get(
            f"/api/v1/deals/{deal_a2.id}/ckb",
            **auth_headers(self.world["a"]["founder"]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "E-AUTHZ-403")

    def test_member_sees_own_deal(self):
        deal_a = self.world["a"]["deal"]
        response = self.client.get(
            f"/api/v1/deals/{deal_a.id}/masterplan",
            **auth_headers(self.world["a"]["founder"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dealId"], str(deal_a.id))

    def test_unauthenticated_gets_401_or_403(self):
        deal_a = self.world["a"]["deal"]
        response = self.client.get(f"/api/v1/deals/{deal_a.id}/masterplan")
        self.assertIn(response.status_code, (401, 403))

    def test_contexts_scoped_to_caller(self):
        response = self.client.get(
            "/api/v1/me/contexts",
            **auth_headers(self.world["a"]["founder"]))
        self.assertEqual(response.status_code, 200)
        ids = [c["dealId"] for c in response.json()["items"]]
        self.assertIn(str(self.world["a"]["deal"].id), ids)
        self.assertNotIn(str(self.world["b"]["deal"].id), ids)
        self.assertNotIn(str(self.world["a2"]["deal"].id), ids)

    def test_owner_only_action_blocked_for_team(self):
        """stage.override is owner-only (BR-M0-031)."""
        from fundos.core.models import Membership, User
        deal_a = self.world["a"]["deal"]
        team = User.objects.create_user(
            email="team-a@x.io", tenant_id=deal_a.tenant_id, name="Team A")
        Membership.objects.create(
            tenant_id=deal_a.tenant_id, user=team, scope_type="deal",
            scope_id=deal_a.id, role="team", status="active")
        response = self.client.post(
            f"/api/v1/deals/{deal_a.id}/stages/2/override",
            **auth_headers(team))
        self.assertEqual(response.status_code, 403)
