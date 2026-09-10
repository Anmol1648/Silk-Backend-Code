import json
from django.test import TestCase
from tests.conftest_helpers import auth_headers, make_world

class SpecContract(TestCase):
    def setUp(self):
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo(); cfg.ai_mocked = True; cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.h = auth_headers(self.w["a"]["founder"])

    def test_profile_sections_is_object_keyed(self):
        body = self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h).json()
        secs = body["sections"]
        self.assertIsInstance(secs, dict)
        # The 17-section generated-profile contract.
        #
        # Four sections left it when the Company Master Data Pipeline landed:
        # key_people merged into `founders` (which now carries `is_founder`),
        # ai_company_summary was already retired under CR-09,
        # leadership_detail is superseded by `founders[].background`, and
        # derived_multiples is still COMPUTED but is no longer its own
        # top-level section. All four are deactivated rather than deleted.
        #
        # Four more were renamed on the wire only; storage is unchanged and
        # the previous names still resolve on write.
        expected = {"company_profile","founders","products_services",
            "customers_markets","competitive_advantages","business_model",
            "revenue_model","company_metrics","financial_summary",
            "funding_history","competitors","news","investors_cap_table",
            "company_story","industry_research","investment_thesis",
            "document_center"}
        self.assertEqual(set(secs.keys()), expected)
        # each wrapped
        for k,v in secs.items():
            self.assertEqual(set(v.keys()), {"sectionKey","isComplete","lastUpdatedAt","data"})
        # no duplicate top-level arrays
        for gone in ["founders","keyPeople","competitors","fundingRounds","news"]:
            self.assertNotIn(gone, body, f"{gone} must not be a top-level array")
        # retired sections are absent from the contract
        for excl in ["knowledge_base","readiness","key_people",
                     "ai_company_summary","leadership_detail",
                     "derived_multiples"]:
            self.assertNotIn(excl, secs)

    def test_gap2_legacy_keys_removed_and_readiness_present(self):
        body = self.client.get(f"/api/v1/companies/{self.co}/profile", **self.h).json()
        # Gap2 / Open Item #13: the legacy duplicated shapes are gone.
        self.assertNotIn("sectionsEditor", body)
        self.assertNotIn("editorRecords", body)
        # readiness now has a clear home (top-level, outside the 14 sections)
        # and uses the standard §8 wrapper (issue #3).
        self.assertIn("readiness", body)
        self.assertEqual(set(body["readiness"].keys()),
                         {"sectionKey", "isComplete", "lastUpdatedAt", "data"})
        # documents stays top-level (§5.2 shape).
        self.assertIn("documents", body)

    def test_gap2_narrative_sections_populate_data_after_generation(self):
        # The core Gap2 finding: products_services / customers_markets /
        # competitive_advantages / business_model must land in sections.data,
        # not only in prose. Run generation (mocked) and assert data is filled.
        from fundos.profile.services import generate_profile, get_or_create_profile
        profile = get_or_create_profile(self.company, user=self.w["a"]["founder"])
        generate_profile(profile, user=self.w["a"]["founder"])

        secs = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()["sections"]
        # list-shaped
        for key in ["products_services", "customers_markets",
                    "competitive_advantages"]:
            self.assertTrue(len(secs[key]["data"]) >= 1,
                            f"{key}.data must be populated after generation")
        # object-shaped
        bm = secs["business_model"]["data"]
        self.assertTrue(any(v for v in bm.values()),
                        "business_model.data must be populated after generation")

    # ---- section-data-integrity doc fixes ----

    def _patch(self, section_key, data):
        import json
        return self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/{section_key}",
            data=json.dumps({"data": data}),
            content_type="application/json", **self.h)

    def _get_section(self, section_key):
        body = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()
        return body["sections"][section_key]

    def test_patch_object_section_persists_and_echoes(self):
        # Issue #5/#6: standardized {data:...} must persist and echo the §8
        # wrapper, no per-section special-casing.
        payload = {
            "description_of_business": "We build X", "website": "https://acme.inc",
            "country": "US", "macro_sector": "Healthcare", "sub_sector": "HealthTech",
            "funding_status_name": "Private", "employee_strength_name": "250-500",
            "revenue_size_name": "$25M-$50M", "currency_id": "USD",
        }
        r = self._patch("company_profile", payload)
        self.assertEqual(r.status_code, 200, r.content)
        echo = r.json()
        self.assertEqual(set(echo.keys()),
                         {"sectionKey", "isComplete", "lastUpdatedAt", "data"})
        self.assertEqual(echo["data"]["macro_sector"], "Healthcare")
        # Persisted — a fresh GET shows the saved values.
        after = self._get_section("company_profile")
        self.assertEqual(after["data"]["macro_sector"], "Healthcare")
        self.assertEqual(after["data"]["sub_sector"], "HealthTech")

    def test_patch_list_section_persists(self):
        # Same contract for an array section (issue #6).
        founders = [{
            "name": "Dr. Sarah Johnson", "role": "CEO & Co-Founder",
            "background": "Former CMO. 15+ years.",
            "linkedin_url": "https://linkedin.com/in/sarahjohnson",
            "is_full_time": True,
        }]
        r = self._patch("founders", founders)
        self.assertEqual(r.status_code, 200, r.content)
        after = self._get_section("founders")["data"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["role"], "CEO & Co-Founder")
        self.assertTrue(after[0]["is_full_time"])

    def test_isComplete_reflects_field_completeness(self):
        # Issue #4: company_profile must NOT be complete with core fields blank.
        partial = {"website": "https://acme.inc", "country": "US",
                   "currency_id": "USD"}
        self._patch("company_profile", partial)
        self.assertFalse(self._get_section("company_profile")["isComplete"],
                         "should be incomplete with 6 core fields blank")
        full = {
            "description_of_business": "We build X", "website": "https://acme.inc",
            "country": "US", "macro_sector": "Healthcare", "sub_sector": "HealthTech",
            "funding_status_name": "Private", "employee_strength_name": "250-500",
            "revenue_size_name": "$25M-$50M", "currency_id": "USD",
        }
        self._patch("company_profile", full)
        self.assertTrue(self._get_section("company_profile")["isComplete"])

    def test_readiness_uses_data_wrapper(self):
        # Issue #3: readiness must use the same wrapper as every section.
        body = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()
        self.assertIn("readiness", body)
        self.assertIn("data", body["readiness"])
        self.assertIn("summary", body["readiness"]["data"])
        self.assertNotIn("summary", body["readiness"],
                         "summary must live under data, not at top level")

    # ---- empty-field templates (4th report) ----

    # Expected field set per list section (§7), used to assert templates.
    _LIST_FIELDS = {
        # `is_founder` distinguishes a founder from another key person: the
        # two used to be separate sections and are now one list, because the
        # question "who runs this company" is one question. Storage still
        # keeps two tables.
        # `id` is the row's own primary key, carried so a Confirm sticks to
        # the person it was given to rather than to whatever ends up in that
        # position after the next research run.
        "founders": {"id", "name", "role", "background", "linkedin_url",
                     "is_full_time", "is_founder"},
        "products_services": {"id", "name", "category", "description"},
        "customers_markets": {"id", "market", "customer_type", "geography"},
        "competitive_advantages": {"id", "title", "description"},
        "revenue_model": {"id", "stream", "share_percent"},
        "company_metrics": {"id", "metric", "value", "unit"},
        # Req 1: `status` removed; post_money_usd_mn + lead_investors added.
        # The amount as the source stated it — figure, currency and scale —
        # beside the USD companion derived from it. `amount_usd_mn` keeps its
        # name and meaning for existing callers; what changed is that it is
        # now computed rather than being the only thing stored, so a round
        # the deck states as INR 20 crore reads as INR 20 crore instead of
        # being converted to 2.4 at a rate nobody recorded.
        "funding_history": {"id", "date", "round",
                            "amount", "currency", "denomination",
                            "amount_display", "fx_rate", "fx_as_of",
                            "amount_usd_mn",
                            "pre_money_usd_mn", "post_money_usd_mn",
                            "investors", "lead_investors"},
        # Req 8: original six unchanged, eleven analysis fields added.
        # `description` and `website` join the set because the serializer has
        # always emitted them on a POPULATED row (CR-12) while the template
        # omitted them — so this assertion was passing on a template that did
        # not match the rows it is a template for. Widened, not relaxed: the
        # two shapes now have to agree, and the schema asks for both.
        "competitors": {"id", "name", "description", "website",
                        "fy_year", "revenue", "funding_usd_mn",
                        "status", "investors", "business_model",
                        "market_positioning", "latest_valuation_usd_mn",
                        "revenue_growth_pct", "market_share_pct",
                        "relative_scale", "key_differentiators", "strengths",
                        "weaknesses", "ev_revenue_multiple",
                        "ev_ebitda_multiple", "recent_activity"},
        "news": {"id", "title", "date", "description", "source", "link"},
    }

    def test_empty_list_sections_return_field_template(self):
        # 4th report: an empty list section must return ONE template row with
        # all fields present (empty), never a bare [] — and stay incomplete.
        secs = self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()["sections"]
        for key, fields in self._LIST_FIELDS.items():
            data = secs[key]["data"]
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1,
                             f"{key} should return one template row when empty")
            self.assertEqual(set(data[0].keys()), fields,
                             f"{key} template must expose all §7 fields")
            self.assertFalse(secs[key]["isComplete"],
                             f"{key} with only a template must be incomplete")

    def test_generation_creates_founder_rows(self):
        # 4th report (populate half): generation must actually create Founder
        # rows so the array fills with real data, not stay empty.
        from fundos.profile.models import Founder
        from fundos.profile.services import generate_profile, get_or_create_profile
        profile = get_or_create_profile(self.company, user=self.w["a"]["founder"])
        generate_profile(profile, user=self.w["a"]["founder"])
        self.assertTrue(Founder.objects.filter(profile=profile).exists(),
                        "generation must create founder rows")
        founders = self.client.get(f"/api/v1/companies/{self.co}/profile",
                                   **self.h).json()["sections"]["founders"]
        self.assertTrue(founders["data"][0]["name"],
                        "founders array should carry a real name after generation")
        self.assertTrue(founders["isComplete"])

    # ---- backend-issues-combined report ----

    def _patch(self, section_key, data):
        import json
        return self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/{section_key}",
            data=json.dumps({"data": data}),
            content_type="application/json", **self.h)

    def _get_section(self, section_key):
        return self.client.get(f"/api/v1/companies/{self.co}/profile",
                               **self.h).json()["sections"][section_key]

    def test_issue1_financial_summary_populates_on_generation(self):
        from fundos.profile.services import generate_profile, get_or_create_profile
        profile = get_or_create_profile(self.company, user=self.w["a"]["founder"])
        generate_profile(profile, user=self.w["a"]["founder"])
        fin = self._get_section("financial_summary")
        self.assertTrue(len(fin["data"]["financials"]) >= 1,
                        "financial_summary.financials must populate on generation")
        self.assertTrue(len(fin["data"]["observations"]) >= 1,
                        "financial_summary.observations must populate")
        self.assertTrue(fin["isComplete"])

    def test_issue2_competitors_patch_keeps_all_fields(self):
        # fy_year, revenue, investors must persist — not be dropped.
        rows = [{
            "name": "ServiceNow", "fy_year": 2025, "revenue": 10700,
            "funding_usd_mn": 149, "status": "Public",
            "investors": ["NYSE Listed"],
        }, {
            "name": "Freshworks", "fy_year": 2025, "revenue": 720,
            "funding_usd_mn": 149, "status": "Public",
            "investors": ["Accel", "CapitalG", "Sequoia Capital"],
        }]
        r = self._patch("competitors", rows)
        self.assertEqual(r.status_code, 200, r.content)
        data = self._get_section("competitors")["data"]
        by_name = {c["name"]: c for c in data}
        self.assertEqual(by_name["ServiceNow"]["fy_year"], 2025)
        self.assertEqual(by_name["ServiceNow"]["revenue"], 10700)
        self.assertEqual(by_name["ServiceNow"]["investors"], ["NYSE Listed"])
        self.assertEqual(by_name["Freshworks"]["investors"],
                         ["Accel", "CapitalG", "Sequoia Capital"])
        self.assertEqual(by_name["Freshworks"]["revenue"], 720)

    def test_issue4_completeness_recalculates_on_patch(self):
        before = self.client.get(f"/api/v1/companies/{self.co}/profile",
                                 **self.h).json()["completenessPct"]
        # Fill a required section fully via PATCH.
        self._patch("company_profile", {
            "description_of_business": "We build X", "website": "https://acme.inc",
            "country": "US", "macro_sector": "Healthcare", "sub_sector": "HealthTech",
            "funding_status_name": "Private", "employee_strength_name": "250-500",
            "revenue_size_name": "$25M-$50M", "currency_id": "USD"})
        after = self.client.get(f"/api/v1/companies/{self.co}/profile",
                                **self.h).json()["completenessPct"]
        self.assertGreater(after, before,
                           "completenessPct must move after a section is completed")

    def test_issue5_attachment_links_field_names(self):
        # Ensure a company-scope context exists for this user so attachmentLinks
        # is exercised (the base fixture only wires deal-scope memberships).
        from fundos.core.models import Membership
        Membership.objects.get_or_create(
            tenant_id=self.company.tenant_id, user=self.w["a"]["founder"],
            scope_type="company", scope_id=self.company.id,
            defaults={"role": "founder", "status": "active"})
        body = self.client.get("/api/v1/me/contexts", **self.h).json()
        comp = [i for i in body["items"] if i["scope"] == "company"]
        self.assertTrue(comp, "expected a company-scope context")
        links = comp[0]["attachmentLinks"]
        self.assertEqual(set(links.keys()), {
            "founderProfile", "companyUrl", "companyPresentation",
            "financialModel", "annualReportFinancialStatements",
            "otherDocuments"})
        self.assertNotIn("productDeck", links)
        self.assertNotIn("other", links)

    # ---- financial-summary structure + contexts sort order ----

    def test_financial_summary_empty_sends_field_template(self):
        # Even with no data, financials must carry one placeholder row with
        # every key present and null (self-describing empty state).
        from fundos.profile.services import get_or_create_profile
        get_or_create_profile(self.company, user=self.w["a"]["founder"])
        fin = self.client.get(f"/api/v1/companies/{self.co}/profile",
                              **self.h).json()["sections"]["financial_summary"]
        self.assertEqual(fin["data"]["financials"],
                         # Req 3: year -> financial_year,
                         # growth_pct -> yoy_revenue_growth_pct; is_estimate
                         # and ev_revenue_multiple are new.
                         [{"financial_year": None, "is_estimate": False,
                           "revenue_m": None, "ebitda_m": None,
                           "yoy_revenue_growth_pct": None,
                           "ev_revenue_multiple": None}])
        self.assertEqual(fin["data"]["observations"], [])
        self.assertFalse(fin["isComplete"],
                         "a null placeholder must not count as complete")

    def test_contexts_default_newest_first_and_sort_params(self):
        import time
        from fundos.core.models import Company, Membership
        u = self.w["a"]["founder"]
        for nm in ["Alpha Co", "Beta Co", "Gamma Co"]:
            c = Company.objects.create(
                tenant_id=self.company.tenant_id, name=nm, created_by=u)
            Membership.objects.create(
                tenant_id=self.company.tenant_id, user=u,
                scope_type="company", scope_id=c.id, role="founder",
                status="active")
            time.sleep(0.01)

        def company_names(qs=""):
            body = self.client.get(f"/api/v1/me/contexts{qs}", **self.h).json()
            return [i["companyName"] for i in body["items"]
                    if i["scope"] == "company"]

        # Default: newest first.
        default_order = company_names()
        self.assertEqual(default_order[:3], ["Gamma Co", "Beta Co", "Alpha Co"])
        # Explicit sort by name ascending.
        self.assertEqual(
            company_names("?sortBy=companyName&order=asc")[:3],
            ["Alpha Co", "Beta Co", "Gamma Co"])
        # Descending name.
        self.assertEqual(
            company_names("?sortBy=companyName&order=desc")[:3],
            ["Gamma Co", "Beta Co", "Alpha Co"])
        # No internal sort-only keys leak into the response.
        body = self.client.get("/api/v1/me/contexts", **self.h).json()
        leaked = [k for i in body["items"] for k in i if k.startswith("_")]
        self.assertEqual(leaked, [])

    def test_contexts_company_items_enriched(self):
        body = self.client.get("/api/v1/me/contexts", **self.h).json()
        comp = [i for i in body["items"] if i["scope"]=="company"]
        if comp:
            it = comp[0]
            for f in ["sector","lastRaise","totalFundingReceivedUsdMn","attachmentLinks"]:
                self.assertIn(f, it, f"company item missing {f}")
            self.assertIn("companyUrl", it["attachmentLinks"])
        deals = [i for i in body["items"] if i["scope"]=="deal"]
        for d in deals:
            self.assertNotIn("attachmentLinks", d, "deal items must stay unchanged")
