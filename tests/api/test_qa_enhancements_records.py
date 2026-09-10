"""
Regression tests for the 25-Jul enhancement set — converting the
free-text profile sections into structured, validated records.

Covers:
  * Increment 2 — CRUD for the list sections (Key People, Competitors,
    Funding History, Recent News) via the generic record endpoint, plus
    the needs-input dot reconciling against real rows.
  * Increment 3 — structured numeric forms (Revenue Model, Company
    Metrics, Financial Summary) stored on section.structured, with
    server-side validation (revenue shares must total 100%), and
    field-level regeneration.

Enhancement 2 (the confirmation modal) and the record/form UI are
frontend-only and are covered by the frontend build + the import guard;
these tests exercise the backend contract those components call.
"""
from django.test import TestCase, override_settings
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

    def _profile(self):
        body = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()
        # New external contract (Gap doc §8): entity lists live inside
        # sections[key].data, not at the top level. Expose a back-compat view
        # so the behavioural assertions below keep exercising the same data
        # through the editor's records CRUD.
        secs = body.get("sections", {})

        def _rows(key):
            s = secs.get(key) or {}
            data = s.get("data")
            return data if isinstance(data, list) else []

        # Key people now live inside `founders`, flagged `is_founder: False`.
        # The KeyPerson table and its records endpoint are unchanged — only
        # the wire section merged — so this shim reads them back out of the
        # combined list and the CRUD assertions below still exercise exactly
        # the same rows.
        body.setdefault("keyPeople",
                        [dict(r, id=self._record_id("key_people", r),
                              designation=r.get("role", ""))
                         for r in _rows("founders")
                         if not r.get("is_founder", True)])
        body.setdefault("competitors",
                        [dict(r, id=self._record_id("competitors", r),
                              fundingRaisedUsd=r.get("funding_usd_mn"),
                              website=self._competitor_website(r))
                         for r in _rows("competitors")])
        body.setdefault("fundingRounds",
                        [dict(r, id=self._record_id("funding_history", r),
                              roundName=r.get("round"))
                         for r in _rows("funding_history")])
        return body

    # Records CRUD returns ids; to keep the id-based assertions working we
    # look the id up from the editor records endpoint rather than the clean
    # spec payload (which intentionally omits internal ids).
    def _record_id(self, section_key, row):
        from fundos.profile.models import (
            Competitor, FundingRound, KeyPerson,
        )
        model = {"key_people": KeyPerson, "competitors": Competitor,
                 "funding_history": FundingRound}[section_key]
        name = row.get("name") or row.get("round") or ""
        qs = model.objects.filter(profile__company_id=self.co)
        if section_key == "funding_history":
            obj = qs.filter(round_name=name).first()
        else:
            obj = qs.filter(name=name).first()
        return str(obj.id) if obj else None

    def _competitor_website(self, row):
        from fundos.profile.models import Competitor
        obj = Competitor.objects.filter(
            profile__company_id=self.co, name=row.get("name") or "").first()
        return obj.website if obj else ""

    def _sections(self):
        # Gap2 removed sectionsEditor from /profile; hit the endpoint to
        # ensure rows exist, then read needs-input from ProfileSection and
        # kind from the section config (the source of truth for both).
        self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h)
        from fundos.profile.models import CompanyProfile, ProfileSection
        from fundos.platformcfg.services import profile_sections
        kinds = {c.section_key: c.kind for c in profile_sections()}
        profile = CompanyProfile.objects.get(company_id=self.co)
        return {
            s.section_key: {"needsInput": s.needs_input, "content": s.content,
                            "kind": kinds.get(s.section_key)}
            for s in ProfileSection.objects.filter(profile=profile,
                                                   is_active=True)
        }

    def _post(self, url, body):
        return self.client.post(url, body, content_type="application/json",
                                **self.h)

    def _patch(self, url, body):
        return self.client.patch(url, body, content_type="application/json",
                                 **self.h)


class ListRecordCrud(Base):
    """Increment 2 — the four list sections behave like Founders."""

    def test_add_edit_delete_key_person(self):
        base = f"/api/v1/companies/{self.co}/profile/records/key_people"
        res = self._post(base, {"name": "Priya Nair",
                                "designation": "VP Engineering"})
        self.assertEqual(res.status_code, 201, res.content)
        rid = res.json()["id"]

        body = self._profile()
        self.assertTrue(any(p["id"] == rid and p["name"] == "Priya Nair"
                            for p in body["keyPeople"]))

        res = self._patch(f"{base}/{rid}", {"designation": "CTO"})
        self.assertEqual(res.status_code, 200, res.content)
        body = self._profile()
        person = next(p for p in body["keyPeople"] if p["id"] == rid)
        self.assertEqual(person["designation"], "CTO")

        res = self.client.delete(f"{base}/{rid}", **self.h)
        self.assertEqual(res.status_code, 204)
        body = self._profile()
        self.assertFalse(any(p["id"] == rid for p in body["keyPeople"]))

    def test_required_field_rejected(self):
        base = f"/api/v1/companies/{self.co}/profile/records/key_people"
        res = self._post(base, {"designation": "Advisor"})  # no name
        self.assertEqual(res.status_code, 422, res.content)
        self.assertIn("name", res.json().get("fields", {}))

    def test_competitor_full_fields_round_trip(self):
        base = f"/api/v1/companies/{self.co}/profile/records/competitors"
        res = self._post(base, {
            "name": "Rivalcorp", "website": "rivalcorp.com",
            "geography": "EU", "fundingRaisedUsd": "5000000",
            "positioning": "Enterprise-first", "description": "A competitor."})
        self.assertEqual(res.status_code, 201, res.content)
        body = self._profile()
        c = next(c for c in body["competitors"] if c["name"] == "Rivalcorp")
        # bare domain gets normalised to https://
        self.assertTrue(c["website"].startswith("https://"))
        self.assertEqual(c["fundingRaisedUsd"], 5000000.0)

    def test_funding_round_investors_list_coercion(self):
        base = f"/api/v1/companies/{self.co}/profile/records/funding_history"
        res = self._post(base, {"roundName": "Seed", "amount": "1000000",
                                "ccy": "USD",
                                "investors": "Acme Ventures, Beta Capital"})
        self.assertEqual(res.status_code, 201, res.content)
        body = self._profile()
        r = next(r for r in body["fundingRounds"] if r["roundName"] == "Seed")
        self.assertEqual(r["investors"], ["Acme Ventures", "Beta Capital"])

    def test_unknown_section_key_404(self):
        res = self._post(
            f"/api/v1/companies/{self.co}/profile/records/not_a_section",
            {"name": "x"})
        self.assertEqual(res.status_code, 404)


class ListSectionDot(Base):
    """The needs-input dot must reconcile for every record section, not
    just Founders/Documents (extends QA issue 3)."""

    def test_dot_clears_and_returns_for_competitors(self):
        secs = self._sections()
        if "competitors" not in secs:
            self.skipTest("no competitors section configured")
        self.assertTrue(secs["competitors"]["needsInput"])

        base = f"/api/v1/companies/{self.co}/profile/records/competitors"
        rid = self._post(base, {"name": "Rivalcorp"}).json()["id"]
        self.assertFalse(self._sections()["competitors"]["needsInput"],
                         "dot must clear once a competitor exists")

        self.client.delete(f"{base}/{rid}", **self.h)
        self.assertTrue(self._sections()["competitors"]["needsInput"],
                        "dot must return when the last record is removed")

    def test_recent_news_and_key_people_start_needing_input(self):
        secs = self._sections()
        for key in ("recent_news", "key_people"):
            if key in secs:
                self.assertTrue(secs[key]["needsInput"],
                                f"{key} should need input before any record")


class StructuredForms(Base):
    """Increment 3 — numeric/structured forms and their validation."""

    def test_section_kind_is_structured_after_seed(self):
        secs = self._sections()
        for key in ("revenue_model", "company_metrics", "financial_summary"):
            if key in secs:
                self.assertEqual(secs[key]["kind"], "structured",
                                 f"{key} must render as a structured form")

    def test_revenue_model_shares_must_total_100(self):
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/revenue_model/structured")
        bad = self._patch(url, {"items": [
            {"label": "Subscriptions", "pct": "60"},
            {"label": "Services", "pct": "20"}]})
        self.assertEqual(bad.status_code, 422, bad.content)
        self.assertIn("items", bad.json().get("fields", {}))

        good = self._patch(url, {"items": [
            {"label": "Subscriptions", "pct": "70"},
            {"label": "Services", "pct": "30"}]})
        self.assertEqual(good.status_code, 200, good.content)
        items = good.json()["structured"]["items"]
        self.assertEqual(len(items), 2)

    def test_structured_form_clears_needs_input(self):
        secs = self._sections()
        if "company_metrics" not in secs:
            self.skipTest("no company_metrics section configured")
        self.assertTrue(secs["company_metrics"]["needsInput"])
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/company_metrics/structured")
        res = self._patch(url, {"items": [
            {"label": "ARR", "value": "1200000", "unit": "USD"}]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self._sections()["company_metrics"]["needsInput"])

    def test_non_numeric_metric_value_rejected(self):
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/company_metrics/structured")
        res = self._patch(url, {"items": [
            {"label": "ARR", "value": "not-a-number"}]})
        self.assertEqual(res.status_code, 422, res.content)

    def test_field_regeneration_returns_a_value(self):
        # Mocked provider — company_profile_field returns a value for the
        # requested field without touching other fields.
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/revenue_model/fields/label/regenerate")
        res = self._post(url, {})
        self.assertEqual(res.status_code, 200, res.content)
        payload = res.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["fieldKey"], "label")
        self.assertTrue(payload["value"])


class StructuredFormHistory(Base):
    """Form edits are versioned through the same snapshot path as prose."""

    def test_form_edit_creates_history(self):
        url = (f"/api/v1/companies/{self.co}"
               f"/profile/sections/company_metrics/structured")
        self._patch(url, {"items": [{"label": "ARR", "value": "1"}]})
        self._patch(url, {"items": [{"label": "ARR", "value": "2"}]})
        hist = self.client.get(
            f"/api/v1/companies/{self.co}"
            f"/profile/sections/company_metrics/history", **self.h).json()
        self.assertGreaterEqual(len(hist["items"]), 1,
                                "a prior version should be snapshotted")
