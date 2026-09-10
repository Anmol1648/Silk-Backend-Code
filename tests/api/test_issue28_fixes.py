"""
Regression tests for the six tester issues logged 30-Jul (Issue28 round).

Frontend-only concerns (#2 sidebar consistency, #3 CTA wording, #6 the
in-app mock banner) are covered by the frontend build + import guard and by
the API flags asserted here; these tests exercise the backend contract each
fix relies on:

  #1 Financial Summary / Company Metrics sync to the CKB, so the valuation
     reads what the founder entered in the Company Profile.
  #4 A document is registered as a company-scoped ProfileDocument (so it
     shows in the Document Center) AND a deal material (so it feeds AI).
  #5 AI generation auto-populates structured records / form items, without
     overwriting founder-entered data.
  #6 The profile payload carries an honest `aiMocked` flag.
"""
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile

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

    def _profile(self):
        return self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()

    def _patch(self, url, body):
        import json
        return self.client.patch(url, json.dumps(body),
                                 content_type="application/json", **self.h)


class Issue1_FinancialsSyncToCkb(Base):
    def _deal(self):
        from fundos.core.models import Deal
        return Deal.objects.filter(company=self.company).order_by("created_at").first()

    def _ckb_value(self, field_key):
        from fundos.core.models import CkbField
        deal = self._deal()
        f = CkbField.objects.filter(deal_id=deal.id, field_key=field_key,
                                    is_deleted=False).first()
        return f.value_num if f else None

    def test_financial_summary_revenue_syncs_to_ckb(self):
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/financial_summary/structured")
        res = self._patch(url, {"items": [
            {"fiscalYear": "FY23", "revenue": "5000000", "grossMarginPct": "55",
             "ebitda": "-1000000", "ccy": "INR"},
            {"fiscalYear": "FY24", "revenue": "8000000", "grossMarginPct": "62",
             "ebitda": "-500000", "ccy": "INR"}]})
        self.assertEqual(res.status_code, 200, res.content)
        # Latest fiscal year's revenue is what reaches the CKB.
        self.assertEqual(float(self._ckb_value("revenue")), 8000000.0)
        self.assertEqual(float(self._ckb_value("ebitda")), -500000.0)
        self.assertEqual(float(self._ckb_value("gross_margin")), 62.0)

    def test_company_metrics_arr_syncs_to_ckb(self):
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/company_metrics/structured")
        res = self._patch(url, {"items": [
            {"label": "ARR", "value": "12000000", "unit": "INR"},
            {"label": "Headcount", "value": "40", "unit": ""}]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(float(self._ckb_value("arr")), 12000000.0)


class Issue4_OnboardingDocsInDocumentCenter(Base):
    def test_company_document_upload_appears_in_profile(self):
        # The onboarding form now uploads via this company-scoped endpoint;
        # the doc must show in the Document Center (profile payload) under
        # its category.
        res = self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": SimpleUploadedFile("deck.pdf", b"%PDF-1.4 mock",
                                        content_type="application/pdf"),
             "category": "company_presentation"}, **self.h)
        self.assertEqual(res.status_code, 201, res.content)
        docs = self._profile()["documents"]
        self.assertTrue(any(d["category"] == "company_presentation"
                            and d["filename"] == "deck.pdf" for d in docs),
                        "uploaded doc must appear in the Document Center")

    def test_upload_also_registers_a_deal_material(self):
        from fundos.readiness.models import MaterialAsset
        from fundos.core.models import Deal
        self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": SimpleUploadedFile("model.xlsx", b"PK mockxlsx",
                                        content_type="application/vnd.ms-excel"),
             "category": "financial_model"}, **self.h)
        deal_ids = list(Deal.objects.filter(company=self.company)
                        .values_list("id", flat=True))
        self.assertTrue(
            MaterialAsset.objects.filter(deal_id__in=deal_ids,
                                         category="financial_model").exists(),
            "upload must also feed the AI material pipeline")


class Issue5_StructuredAutoPopulate(Base):
    def test_generation_populates_records_and_forms(self):
        # Onboard + generate (mocked provider returns demo structured rows).
        import json
        self.client.post(
            f"/api/v1/companies/{self.co}/profile/onboard",
            json.dumps({"websiteUrl": "https://helios.example",
                        "hqCountry": "IN", "homeCurrency": "INR",
                        "founders": [{"name": "Asha", "isFullTime": True,
                                      "selfConfirmed": True}]}),
            content_type="application/json", **self.h)
        # Trigger generation synchronously.
        from fundos.profile.services import generate_profile, get_or_create_profile
        profile = get_or_create_profile(self.company, user=self.w["a"]["founder"])
        generate_profile(profile, user=self.w["a"]["founder"])

        body = self._profile()
        # Record sections now hold AI-seeded rows — under the new contract
        # (Gap doc §8) they live in sections[key].data.
        secs = body["sections"]
        # Key people merged into `founders`, flagged `is_founder: False`. The
        # KeyPerson table and its records endpoint are unchanged; only the
        # wire section merged, because "who runs this company" is one
        # question and a model asked to sort people into our two tables gets
        # it wrong.
        people = [p for p in secs["founders"]["data"]
                  if not p.get("is_founder", True)]
        self.assertTrue(len(people) >= 1,
                        "key people should be auto-populated")
        self.assertTrue(len(secs["competitors"]["data"]) >= 1,
                        "competitors should be auto-populated")
        # Revenue model seeded with at least one stream.
        rev_items = secs.get("revenue_model", {}).get("data") or []
        self.assertTrue(len(rev_items) >= 1, "revenue model should be seeded")

    def test_generation_does_not_overwrite_founder_records(self):
        # A founder-entered competitor must survive generation.
        import json
        add = self.client.post(
            f"/api/v1/companies/{self.co}/profile/records/competitors",
            json.dumps({"name": "Founders Own Competitor"}),
            content_type="application/json", **self.h)
        self.assertEqual(add.status_code, 201)

        from fundos.profile.services import generate_profile, get_or_create_profile
        profile = get_or_create_profile(self.company, user=self.w["a"]["founder"])
        generate_profile(profile, user=self.w["a"]["founder"])

        names = [c["name"] for c in
                 self._profile()["sections"]["competitors"]["data"]]
        self.assertIn("Founders Own Competitor", names,
                      "AI generation must not clobber founder-entered records")


class Issue6_MockFlagSurfaced(Base):
    def test_profile_reports_ai_mocked_true(self):
        self.assertTrue(self._profile()["aiMocked"])

    def test_profile_reports_ai_mocked_false_when_live(self):
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = False; cfg.save()
        self.assertFalse(self._profile()["aiMocked"])
