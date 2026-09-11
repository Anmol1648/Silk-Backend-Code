"""A share nobody stated is not a share of zero.

Every revenue stream on a live profile came back `"share_percent": 0`. Read
as written, that says the company earns nothing from any of its revenue
streams -- a finding about the business, arrived at by coercion: the writer
turned a missing value into 0, and the reader turned it into 0 again with
`or 0`, so a source that listed the streams without breaking the split down
produced five confident zeros.

Zero has to stay available, because a stream that genuinely earns nothing is
a real answer. What changes is that nothing INVENTS one.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Membership, Tenant, User
from fundos.profile.section_writer import update_section_from_data
from fundos.profile.services import get_or_create_profile
from fundos.profile.spec_serializer import serialize_section


def _bootstrap():
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="Revenue User")
    company = Company.objects.create(
        tenant_id=tenant.id, name="Streams Co", created_by=user)
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    return tenant, user, company, get_or_create_profile(company, user=user)


class AnUnstatedShareStaysUnstated(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def _save(self, rows):
        update_section_from_data(self.profile, "revenue_model", rows,
                                 user=self.user)
        return serialize_section(self.profile, "revenue_model")["data"]

    def test_a_stream_with_no_share_comes_back_null(self):
        rows = self._save([{"stream": "Subscriptions"}])
        self.assertIsNone(rows[0]["share_percent"])

    def test_an_explicit_null_survives_the_round_trip(self):
        rows = self._save([{"stream": "Services", "share_percent": None}])
        self.assertIsNone(rows[0]["share_percent"])

    def test_an_explicit_zero_is_kept_as_a_real_answer(self):
        """A stream that earns nothing is something a source can say."""
        rows = self._save([{"stream": "Hardware", "share_percent": 0}])
        self.assertEqual(rows[0]["share_percent"], 0)

    def test_a_stated_share_is_unchanged(self):
        rows = self._save([{"stream": "Subscriptions", "share_percent": 80},
                           {"stream": "Services", "share_percent": 20}])
        self.assertEqual([r["share_percent"] for r in rows], [80, 20])

    def test_an_empty_string_is_not_a_zero_either(self):
        rows = self._save([{"stream": "Ads", "share_percent": ""}])
        self.assertIsNone(rows[0]["share_percent"])

    def test_a_mixed_list_keeps_each_answer_apart(self):
        rows = self._save([{"stream": "A", "share_percent": 60},
                           {"stream": "B", "share_percent": 0},
                           {"stream": "C"}])
        self.assertEqual([r["share_percent"] for r in rows], [60, 0, None])

    def test_the_empty_template_claims_no_share(self):
        """The row a client renders for an empty section must not assert 0%
        for a stream that does not exist yet."""
        data = serialize_section(self.profile, "revenue_model")["data"]
        self.assertTrue(data)
        self.assertIsNone(data[0]["share_percent"])


class TheModelIsToldWhichAnswerIsWhich(TestCase):

    def _spec(self):
        from fundos.profile.schema import SHIPPED_SECTIONS

        section = next(s for s in SHIPPED_SECTIONS
                       if s["key"] == "revenue_model")
        return str(section["fields"]["share_percent"])

    def test_the_spec_allows_null(self):
        self.assertIn("null", self._spec())

    def test_the_spec_says_what_zero_means(self):
        spec = self._spec()
        self.assertIn("0 means", spec)
