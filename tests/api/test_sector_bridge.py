"""The sector on the assessment must come from the profile that researched it.

Categories E and F carry 30% of the rating between them, and both are scored
against the benchmark group the sub-sector resolves to. An assessment that
never learns its own sector scores that 30% against nothing.
"""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import make_world


class SectorReachesTheAssessment(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)

    def setUp(self):
        from fundos.profile.models import CompanyProfile, ProfileSection

        self.world = make_world()
        self.company = self.world["a"]["company"]
        self.deal = self.world["a"]["deal"]
        self.tenant = self.world["a"]["tenant"].id

        profile, _ = CompanyProfile.objects.get_or_create(
            company=self.company,
            defaults={"tenant_id": self.tenant, "website_url": "https://zyla.in"})
        self.profile = profile

        # Exactly what step 1 stores for a researched company.
        ProfileSection.objects.update_or_create(
            profile=profile, section_key="company_profile",
            defaults={"tenant_id": self.tenant,
                      "structured": {"macro_sector": "Healthcare",
                                     "sub_sector": "Digital Health"}})

    def _assessment(self):
        from fundos.assessment.models import Assessment
        return Assessment.objects.create(
            tenant_id=self.tenant, company=self.company, deal=self.deal,
            deal_stage="Series A", capital_raised_usd_mn=Decimal("5"),
            status="draft")

    def test_bridge_carries_the_profile_sector_onto_the_assessment(self):
        from fundos.assessment.profile_bridge import _apply_sector

        assessment = self._assessment()
        self.assertEqual(assessment.sector, "")

        _apply_sector(assessment, self.profile)

        assessment.refresh_from_db()
        self.assertEqual(assessment.sector, "Healthcare")
        self.assertTrue(assessment.sub_sector,
                        "sub-sector must resolve to a benchmark group or the "
                        "raw label, never stay blank")


class SectorReachesTheV2Payload(SectorReachesTheAssessment):
    """`inputs.sectors` must carry the researched sector, not an empty list.

    The V2 serializer resolves the sector from the profile when the assessment
    itself never learned one. That fallback is what a client reads, so it is
    asserted against the endpoint rather than the helper.
    """

    def setUp(self):
        super().setUp()
        from tests.conftest_helpers import auth_headers
        self.headers = auth_headers(self.world["a"]["founder"])

    def test_v2_inputs_carry_the_sector(self):
        from fundos.assessment.models import Assessment
        from fundos.assessment.services import run_scoring

        assessment = self._assessment()
        assessment.status = "scored"
        assessment.save(update_fields=["status"])

        url = f"/api/v1/companies/{self.company.id}/assessment"
        data = self.client.get(url, **self.headers).data

        self.assertEqual(data["inputs"]["sectors"], ["Healthcare"])
        self.assertTrue(data["inputs"]["sub_sector"])
