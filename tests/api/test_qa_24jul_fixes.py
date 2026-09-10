"""
Regression tests for the 24-Jul QA findings (Issue_Documentation_24Jul).
Each test is pinned to the issue number it protects.
"""
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class Qa24Base(TestCase):
    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.h = auth_headers(self.w["a"]["founder"])
        self.hB = auth_headers(self.w["b"]["founder"])


class AdminSite(TestCase):
    """Issue 9 — /admin returned 404: django.contrib.admin was installed and
    models registered, but no URL was ever mounted."""

    def test_admin_is_mounted(self):
        res = self.client.get("/admin/")
        self.assertIn(res.status_code, (200, 302))

    def test_admin_without_trailing_slash_redirects(self):
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 301)
        self.assertTrue(res["Location"].endswith("/admin/"))

    def test_static_url_is_absolute(self):
        """A relative STATIC_URL made the admin resolve its CSS against the
        current path, so it rendered unstyled behind a proxy."""
        from django.conf import settings
        self.assertTrue(settings.STATIC_URL.startswith("/"))


class DefaultDealResolution(Qa24Base):
    """Issues 6 & 7 — the company-scoped strategy page had no way to learn
    which deal to use, so it rendered without a deal context and crashed on
    `dealId`."""

    def test_default_deal_endpoint(self):
        res = self.client.get(f"/api/v1/companies/{self.co}/default-deal",
                              **self.h)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["dealId"])
        self.assertEqual(body["companyId"], self.co)

    def test_default_deal_is_stable(self):
        first = self.client.get(f"/api/v1/companies/{self.co}/default-deal",
                                **self.h).json()["dealId"]
        second = self.client.get(f"/api/v1/companies/{self.co}/default-deal",
                                 **self.h).json()["dealId"]
        self.assertEqual(first, second,
                         "repeated calls must not mint new deals")

    def test_default_deal_is_tenant_isolated(self):
        res = self.client.get(f"/api/v1/companies/{self.co}/default-deal",
                              **self.hB)
        self.assertGreaterEqual(res.status_code, 400)

    def test_profile_exposes_default_deal_id(self):
        """Issue 8 — member management was unreachable because the Members
        link is gated on a field the profile never returned."""
        res = self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("defaultDealId"),
                        "profile must carry defaultDealId for member access")


class OnboardingIsRepeatable(Qa24Base):
    """Issue 5 — onboarding intermittently returned HTTP 500."""

    def _payload(self, website="https://meridianagri.in"):
        return {
            "websiteUrl": website, "hqCountry": "IN",
            "founders": [{"name": "Anaya Rao",
                          "linkedinUrl": "https://linkedin.com/in/anaya",
                          "selfConfirmed": True}],
        }

    def test_repeated_onboarding_does_not_500(self):
        url = f"/api/v1/companies/{self.co}/profile/onboard"
        for attempt in range(3):
            res = self.client.post(url, self._payload(),
                                   content_type="application/json", **self.h)
            self.assertEqual(res.status_code, 202,
                             f"attempt {attempt + 1}: {res.content[:200]}")

    def test_second_company_with_same_website_does_not_500(self):
        """The live-unique domain constraint turned this into an unhandled
        IntegrityError — the 'random' 500 that succeeded on retry."""
        other = self.client.post("/api/v1/companies",
                                 {"name": "Meridian Labs"},
                                 content_type="application/json",
                                 **self.h).json()
        first = self.client.post(
            f"/api/v1/companies/{self.co}/profile/onboard", self._payload(),
            content_type="application/json", **self.h)
        self.assertEqual(first.status_code, 202)
        second = self.client.post(
            f"/api/v1/companies/{other['id']}/profile/onboard",
            self._payload(), content_type="application/json", **self.h)
        self.assertEqual(second.status_code, 202,
                         f"domain clash must not fail the run: "
                         f"{second.content[:200]}")

    def test_creating_a_company_with_a_taken_domain_does_not_500(self):
        """Same defect, the other entry point.

        `ProfileOnboardView` derives a domain from the website URL and was
        fixed for this — `_payload()`'s website gives `self.company` the
        domain `meridianagri.in` in the test above. `POST /companies` accepts
        a domain directly and went through `onboarding.create_company`
        unguarded, so an unrelated second signup choosing the same domain
        (deliberately, or a coincidence — "acme.com" is not a rare guess)
        crashed with a raw IntegrityError on `uq_company_domain_live`, which
        is unique across the whole platform, not per tenant.
        """
        first = self.client.post(
            "/api/v1/companies",
            {"name": "First Co", "domain": "shared-domain.test"},
            content_type="application/json", **self.h).json()
        self.assertEqual(first.get("domain"), "shared-domain.test")

        # A second, unrelated tenant reaching for the same domain.
        second = self.client.post(
            "/api/v1/companies",
            {"name": "Second Co", "domain": "shared-domain.test"},
            content_type="application/json", **self.hB)
        self.assertEqual(second.status_code, 201,
                         f"domain clash on creation must not 500: "
                         f"{second.content[:200]}")
        # The convenience field is dropped, not the request. A blank domain
        # serialises as null (_serialise_company: `c.domain or None`).
        self.assertIsNone(second.json().get("domain"))

    def test_reusing_a_name_does_not_steal_a_taken_domain(self):
        """The idempotent-reuse branch updates domain too, and was not
        guarded either: resubmitting the same company name with a domain
        already claimed elsewhere hit the same constraint."""
        self.client.post(
            "/api/v1/companies",
            {"name": "Other Founder Co", "domain": "already-claimed.test"},
            content_type="application/json", **self.hB)
        first = self.client.post(
            "/api/v1/companies", {"name": "My Co"},
            content_type="application/json", **self.h).json()
        self.assertIsNone(first.get("domain"))

        again = self.client.post(
            "/api/v1/companies",
            {"name": "My Co", "domain": "already-claimed.test"},
            content_type="application/json", **self.h)
        self.assertEqual(again.status_code, 201,
                         f"resubmit with a taken domain must not 500: "
                         f"{again.content[:200]}")
        self.assertEqual(again.json()["id"], first["id"],
                         "resubmitting the same name must reuse, not "
                         "duplicate, the company")
        self.assertIsNone(again.json().get("domain"),
                          "the taken domain must not be assigned")

    def test_founders_are_not_duplicated_on_resubmit(self):
        from fundos.profile.models import Founder
        url = f"/api/v1/companies/{self.co}/profile/onboard"
        for _ in range(3):
            self.client.post(url, self._payload(),
                             content_type="application/json", **self.h)
        count = Founder.objects.filter(name="Anaya Rao").count()
        self.assertEqual(count, 1,
                         f"re-submitting duplicated founders ({count} rows)")


class DocumentCenterUpload(Qa24Base):
    """Issue 4 — documents could only be attached during onboarding."""

    def _file(self, name="deck.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 test content",
                                  content_type="application/pdf")

    def test_upload_after_profile_creation(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/documents",
                               {"file": self._file(),
                                "category": "company_presentation"}, **self.h)
        self.assertEqual(res.status_code, 201, res.content[:250])
        body = res.json()
        self.assertEqual(body["filename"], "deck.pdf")
        self.assertFalse(body["isReadonly"])

        listing = self.client.get(f"/api/v1/companies/{self.co}/profile",
                                  **self.h).json()["documents"]
        self.assertTrue(any(d["filename"] == "deck.pdf" for d in listing))

    def test_upload_requires_a_file(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/documents",
                               {"category": "other"}, **self.h)
        self.assertEqual(res.status_code, 422)
        self.assertIn("file", res.json()["fields"])

    def test_unknown_category_falls_back_to_other(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/documents",
                               {"file": self._file("x.pdf"),
                                "category": "nonsense"}, **self.h)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["category"], "other")

    def test_upload_is_tenant_isolated(self):
        res = self.client.post(f"/api/v1/companies/{self.co}/documents",
                               {"file": self._file(), "category": "other"},
                               **self.hB)
        self.assertGreaterEqual(res.status_code, 400)

    def test_readonly_legacy_document_cannot_be_deleted(self):
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        doc = ProfileDocument.objects.create(
            tenant_id=self.company.tenant_id, profile=profile,
            category="teaser", filename="Teaser v1.pdf", is_readonly=True,
            origin_stage="legacy_stage_3")
        res = self.client.delete(
            f"/api/v1/companies/{self.co}/documents/{doc.id}", **self.h)
        self.assertEqual(res.status_code, 422)

    def test_uploaded_document_can_be_deleted(self):
        created = self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": self._file(), "category": "other"}, **self.h).json()
        res = self.client.delete(
            f"/api/v1/companies/{self.co}/documents/{created['id']}", **self.h)
        self.assertEqual(res.status_code, 204)


class SectionActionsAreUniform(Qa24Base):
    """Issue 3 — Key People, Funding History, Competitors and Recent News
    offered no edit or history, so they could not be maintained by hand."""

    LIST_SECTIONS = ("key_people", "funding_history", "competitors",
                     "recent_news")

    def test_list_sections_are_editable_and_versioned(self):
        for key in self.LIST_SECTIONS:
            url = f"/api/v1/companies/{self.co}/profile/sections/{key}"
            res = self.client.patch(url, {"content": f"Manual note for {key}."},
                                    content_type="application/json", **self.h)
            self.assertEqual(res.status_code, 200,
                             f"{key} must be editable: {res.content[:160]}")
            again = self.client.patch(url, {"content": "Revised."},
                                      content_type="application/json",
                                      **self.h)
            self.assertEqual(again.status_code, 200)
            hist = self.client.get(f"{url}/history", **self.h)
            self.assertEqual(hist.status_code, 200)
            self.assertTrue(hist.json()["items"],
                            f"{key} must record version history")

    def test_manual_content_survives_a_profile_read(self):
        key = "competitors"
        self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/{key}",
            {"content": "Peer A, Peer B."},
            content_type="application/json", **self.h)
        self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        from fundos.profile.models import CompanyProfile, ProfileSection
        profile = CompanyProfile.objects.get(company_id=self.co)
        row = ProfileSection.objects.get(profile=profile, section_key=key,
                                         is_active=True)
        self.assertEqual(row.content, "Peer A, Peer B.")


class Branding(TestCase):
    """The product marks must be served even before an admin sets them."""

    def test_brand_payload_carries_logo_defaults(self):
        from fundos.platformcfg.services import brand
        b = brand()
        self.assertTrue(b["logoUrl"])
        self.assertTrue(b["logoMonoUrl"])

    def test_seeded_brand_keeps_logos(self):
        call_command("seed_platform_config", verbosity=0)
        from fundos.platformcfg.services import brand
        b = brand()
        self.assertTrue(b["logoUrl"].endswith(".svg"))
        self.assertTrue(b["logoMonoUrl"].endswith(".svg"))
