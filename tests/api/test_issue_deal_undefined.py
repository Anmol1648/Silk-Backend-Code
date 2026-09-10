"""
Regression tests for the 13-Jul production incident (fundos_issue_2):

Symptoms in the gunicorn log:
  POST /api/v1/contexts/switch            -> 500
  GET  /api/v1/deals/undefined/masterplan -> 404
  GET  /api/v1/deals/undefined/ckb        -> 404

Root cause: POST /companies/{id}/deals returned the new deal's id under the
key "dealId", but the client read "id" -> undefined -> the SPA navigated to
/deals/undefined and every subsequent call used the literal string
"undefined". /contexts/switch then blew up with a 500 because looking up a
non-UUID raised Django's ValidationError (not Deal.DoesNotExist).

These tests lock in both fixes.
"""
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


class DealCreateResponseContractTests(TestCase):
    """The create-deal response must carry a usable identifier."""

    def setUp(self):
        self.world = make_world()
        self.user = self.world["a"]["founder"]
        # Deal creation is company-owner-gated, so create a company through the
        # API — that makes this user its owner.
        created = self.client.post(
            "/api/v1/companies",
            data={"name": "Acme Labs", "hqCountry": "IN"},
            content_type="application/json",
            **auth_headers(self.user))
        self.assertEqual(created.status_code, 201)
        self.company_id = created.json()["id"]

    def test_create_deal_returns_both_id_and_dealId(self):
        response = self.client.post(
            f"/api/v1/companies/{self.company_id}/deals",
            data={"name": "Series B Round", "roundType": "Series B"},
            content_type="application/json",
            **auth_headers(self.user))
        self.assertEqual(response.status_code, 201)
        body = response.json()

        # Both keys present and identical — a client reading either works.
        self.assertIn("dealId", body)
        self.assertIn("id", body)
        self.assertEqual(body["id"], body["dealId"])

        # And crucially: neither is undefined/empty.
        self.assertTrue(body["id"])

        # The returned id must actually be usable on a deal-scoped route.
        follow = self.client.get(f"/api/v1/deals/{body['id']}/masterplan",
                                 **auth_headers(self.user))
        self.assertEqual(follow.status_code, 200)


class MalformedDealIdTests(TestCase):
    """A malformed deal id must never produce a 500."""

    def setUp(self):
        self.world = make_world()
        self.user = self.world["a"]["founder"]

    def test_contexts_switch_with_undefined_string_is_not_500(self):
        response = self.client.post(
            "/api/v1/contexts/switch",
            data={"dealId": "undefined"},
            content_type="application/json",
            **auth_headers(self.user))
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 422)   # treated as missing
        self.assertEqual(response.json()["error"], "E-VAL-422")

    def test_contexts_switch_with_missing_dealId_is_422(self):
        response = self.client.post(
            "/api/v1/contexts/switch", data={},
            content_type="application/json",
            **auth_headers(self.user))
        self.assertEqual(response.status_code, 422)

    def test_contexts_switch_with_garbage_uuid_is_not_500(self):
        response = self.client.post(
            "/api/v1/contexts/switch",
            data={"dealId": "not-a-uuid-at-all"},
            content_type="application/json",
            **auth_headers(self.user))
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 403)   # generic forbidden
        self.assertEqual(response.json()["error"], "E-AUTHZ-403")

    def test_deal_scoped_route_with_undefined_is_not_500(self):
        """/deals/undefined/masterplan must be a clean 4xx, never a 500."""
        response = self.client.get("/api/v1/deals/undefined/masterplan",
                                   **auth_headers(self.user))
        self.assertNotEqual(response.status_code, 500)
        self.assertIn(response.status_code, (403, 404))
