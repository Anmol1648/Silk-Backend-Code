"""
Regression tests for QA_Issues_and_Enhancements (25-Jul).
Issues 2, 3 and 6 are backend-observable; issues 1 and 4 are frontend-only
(CSS / a missing import) and are covered by the frontend build + the
undefined-import static check.
"""
from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command

from tests.conftest_helpers import auth_headers, make_world


@override_settings(CELERY_TASK_ALWAYS_EAGER=True,
                   CELERY_TASK_EAGER_PROPAGATES=True)
class Base(TestCase):
    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = True; cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.h = auth_headers(self.w["a"]["founder"])

    def _sections(self):
        # Gap2 removed the legacy sectionsEditor array from /profile. Hit the
        # endpoint (which ensures the profile + section rows exist) then read
        # the needs-input flags straight from ProfileSection.
        self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        from fundos.profile.models import CompanyProfile, ProfileSection
        profile = CompanyProfile.objects.get(company_id=self.co)
        return {
            s.section_key: {"needsInput": s.needs_input, "content": s.content}
            for s in ProfileSection.objects.filter(profile=profile,
                                                   is_active=True)
        }


class StructuredSectionStatus(Base):
    """Issue 3 — the orange 'needs input' dot stayed on Founders and the
    Document Center after they were populated."""

    def test_founders_dot_clears_when_a_founder_is_added(self):
        from fundos.profile.services import (get_or_create_profile,
                                             recompute_structured_sections)
        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        recompute_structured_sections(profile)
        secs = self._sections()
        if "founders" in secs:
            self.assertTrue(secs["founders"]["needsInput"],
                            "founders should need input before any are added")
        res = self.client.post(f"/api/v1/companies/{self.co}/founders",
                               {"name": "Anaya Rao", "selfConfirmed": True},
                               content_type="application/json", **self.h)
        self.assertEqual(res.status_code, 201)
        secs = self._sections()
        if "founders" in secs:
            self.assertFalse(secs["founders"]["needsInput"],
                             "founders dot must clear once a founder exists")

    def test_document_center_dot_clears_on_upload(self):
        secs = self._sections()
        if "document_center" in secs:
            self.assertTrue(secs["document_center"]["needsInput"])
        res = self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": SimpleUploadedFile("deck.pdf", b"%PDF-1.4 x",
                                        content_type="application/pdf"),
             "category": "company_presentation"}, **self.h)
        self.assertEqual(res.status_code, 201)
        secs = self._sections()
        if "document_center" in secs:
            self.assertFalse(secs["document_center"]["needsInput"],
                             "document centre dot must clear after upload")

    def test_document_center_dot_returns_when_last_doc_removed(self):
        created = self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": SimpleUploadedFile("d.pdf", b"%PDF-1.4 x",
                                        content_type="application/pdf"),
             "category": "other"}, **self.h).json()
        secs = self._sections()
        if "document_center" not in secs:
            self.skipTest("no document_center section configured")
        self.assertFalse(secs["document_center"]["needsInput"])
        self.client.delete(
            f"/api/v1/companies/{self.co}/documents/{created['id']}", **self.h)
        secs = self._sections()
        self.assertTrue(secs["document_center"]["needsInput"],
                        "dot must return when the last document is removed")


class BrandingReflectsConfig(Base):
    """Issue 6 — a Product Name / tagline set in Django admin must reach the
    client. Browser title already worked; the sidebar and login did not,
    which was a frontend rendering bug — but the payload must carry the
    fields for the UI to render."""

    def test_config_app_carries_product_name_and_tagline(self):
        from fundos.platformcfg.models import BrandConfig
        row = BrandConfig.get_solo()
        row.product_name = "Silk QA Test"
        row.tagline = "test tagline"
        row.save()
        body = self.client.get("/api/v1/config/app", **self.h).json()
        self.assertEqual(body["brand"]["productName"], "Silk QA Test")
        self.assertEqual(body["brand"]["tagline"], "test tagline")
        self.assertIn("browserTitle", body["brand"])

    def test_product_name_helper_follows_config(self):
        from fundos.platformcfg.models import BrandConfig
        from fundos.platformcfg.services import product_name
        row = BrandConfig.get_solo()
        row.product_name = "Renamed Co"
        row.save()
        self.assertEqual(product_name(), "Renamed Co")
