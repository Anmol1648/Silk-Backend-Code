import base64, json
from django.test import TestCase
from tests.conftest_helpers import auth_headers, make_world


class DeleteAndLogo(TestCase):
    def setUp(self):
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = True; cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.user = self.w["a"]["founder"]
        self.h = auth_headers(self.user)

    # ---- item 1: delete company ----
    def _make_owned_company(self, name="DeleteMe Inc"):
        from fundos.core.models import Company, Membership
        c = Company.objects.create(tenant_id=self.company.tenant_id,
                                   name=name, created_by=self.user)
        Membership.objects.create(tenant_id=self.company.tenant_id,
            user=self.user, scope_type="company", scope_id=c.id,
            role="founder", status="active")
        return c

    def test_delete_company_cascades(self):
        from fundos.core.models import Company, Deal, Membership
        from fundos.profile.models import CompanyProfile, Founder
        from fundos.profile.services import get_or_create_profile
        c = self._make_owned_company()
        cid = str(c.id)
        # give it a profile + founder + a deal
        profile = get_or_create_profile(c, user=self.user)
        Founder.objects.create(tenant_id=c.tenant_id, profile=profile,
                               name="X", source="founder", sort_order=1)
        r = self.client.post(f"/api/v1/companies/{cid}/deals",
                             {"name": "Seed", "roundType": "seed"},
                             content_type="application/json", **self.h)
        self.assertEqual(r.status_code, 201, r.content)

        # delete
        resp = self.client.delete(f"/api/v1/companies/{cid}", **self.h)
        self.assertEqual(resp.status_code, 204, resp.content)

        # everything gone
        self.assertFalse(Company.all_objects.filter(id=cid).exists())
        self.assertFalse(CompanyProfile.objects.filter(company_id=cid).exists())
        self.assertFalse(Founder.objects.filter(profile__company_id=cid).exists())
        self.assertFalse(Deal.all_objects.filter(company_id=cid).exists())
        self.assertFalse(Membership.all_objects.filter(scope_id=c.id).exists())
        # and it no longer shows in contexts
        ctx = self.client.get("/api/v1/me/contexts", **self.h).json()
        self.assertNotIn(cid, [i.get("companyId") for i in ctx["items"]])

    def test_delete_company_requires_owner(self):
        # a company the user does not own
        from fundos.core.models import Company
        other = Company.objects.create(tenant_id=self.company.tenant_id,
                                       name="NotMine")
        resp = self.client.delete(f"/api/v1/companies/{other.id}", **self.h)
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(Company.all_objects.filter(id=other.id).exists())

    # ---- item 3: logo ----
    def test_onboard_accepts_logo_url_and_returns_it(self):
        c = self._make_owned_company("LogoCo")
        cid = str(c.id)
        logo = "https://img.logo.dev/google.com?token=pk_abc"
        r = self.client.post(f"/api/v1/companies/{cid}/profile/onboard",
            data=json.dumps({"websiteUrl": "https://google.com",
                             "hqCountry": "US", "logoUrl": logo,
                             "founders": []}),
            content_type="application/json", **self.h)
        self.assertEqual(r.status_code, 202, r.content)
        # GET /profile returns logoUrl at top level
        prof = self.client.get(f"/api/v1/companies/{cid}/profile", **self.h).json()
        self.assertEqual(prof["logoUrl"], logo)
        # me/contexts company item carries logoUrl
        ctx = self.client.get("/api/v1/me/contexts", **self.h).json()
        item = [i for i in ctx["items"]
                if i.get("scope") == "company" and i.get("companyId") == cid]
        self.assertTrue(item and item[0]["logoUrl"] == logo)

    def test_onboard_accepts_logo_base64(self):
        c = self._make_owned_company("LogoB64Co")
        cid = str(c.id)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()
        r = self.client.post(f"/api/v1/companies/{cid}/profile/onboard",
            data=json.dumps({"websiteUrl": "https://x.com", "hqCountry": "US",
                             "logoBase64": png, "founders": []}),
            content_type="application/json", **self.h)
        self.assertEqual(r.status_code, 202, r.content)
        prof = self.client.get(f"/api/v1/companies/{cid}/profile", **self.h).json()
        self.assertTrue(prof["logoUrl"], "base64 upload should yield a stored URL")
