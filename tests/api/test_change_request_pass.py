"""
Regression tests for the Company Profile change-request pass (CR-01 to CR-14).

Each class pins one change request, named in its docstring.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase

from fundos.core.models import Company, Deal, Membership, Tenant, User
from fundos.profile.models import Competitor, ProfileDocument
from fundos.profile.section_writer import (
    SectionUpdateError, update_section_from_data,
)
from fundos.profile.services import (collect_sources, get_or_create_profile)
from fundos.profile.spec_serializer import build_sections, serialize_section


class SeededTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)


def _bootstrap(name="CR Co"):
    tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
    user = User.objects.create_user(
        email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
        name="CR User")
    company = Company.objects.create(
        tenant_id=tenant.id, name=name, created_by=user,
        domain=f"cr-{uuid.uuid4().hex[:8]}.test")
    Membership.objects.create(
        tenant_id=tenant.id, user=user, scope_type="company",
        scope_id=company.id, role="founder", status="active")
    profile = get_or_create_profile(company, user=user)
    return tenant, user, company, profile


class GenerationInputsTests(SeededTestCase):
    """CR-03 / CR-04 — generation received nothing but name, domain and country.

    Two independent causes, both pinned here:

    1. Every source was gated on an existing Deal, and at Create Company time
       no deal exists — so website, documents and research were all skipped.
    2. The research adapters were constructed with `ckb=` when the parameter
       is `ckb_snapshot`. The resulting TypeError was caught and logged at
       WARNING, so all four market sources silently returned an error payload
       instead of data, on every run, forever.
    """

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()
        self.profile.website_url = "https://example.invalid"
        self.profile.save()

    def test_sources_are_collected_without_a_pre_existing_deal(self):
        self.assertEqual(Deal.objects.filter(company=self.company).count(), 0)
        collect_sources(self.profile, user=self.user)
        self.assertEqual(
            Deal.objects.filter(company=self.company).count(), 1,
            "collect_sources must resolve a context deal; every research "
            "source is deal-scoped and was otherwise skipped entirely")

    def test_no_adapter_fails_with_the_wrong_keyword(self):
        _, failures = collect_sources(self.profile, user=self.user)
        bad = [f for f in failures if "unexpected keyword argument" in str(f.get("error", ""))]
        self.assertEqual(
            bad, [],
            "a research adapter was called with the wrong keyword — this "
            "fails inside a try/except and silently removes web research "
            "from every generation run")

    def test_adapter_error_text_is_not_fed_to_the_model(self):
        payloads, _ = collect_sources(self.profile, user=self.user)
        research = payloads.get("research") or {}
        for source, value in research.items():
            with self.subTest(source=source):
                self.assertNotIn(
                    "error", value if isinstance(value, dict) else {},
                    "an adapter's error string reached the model payload; it "
                    "is not evidence and must be reported as a failure")

    def test_document_readiness_is_observable(self):
        """Documents stored but never extracted look exactly like documents
        that were ignored. The distinction has to be visible."""
        from fundos.profile.services import document_readiness

        ProfileDocument.objects.create(
            tenant_id=self.tenant.id, profile=self.profile,
            category="company_presentation", filename="deck.pdf")
        readiness = document_readiness(self.profile)
        self.assertEqual(readiness["uploaded"], 1)
        self.assertIn("analysed", readiness)
        self.assertIn("pending", readiness)


class CompanyLinkedInTests(SeededTestCase):
    """CR-01 / CR-02 — the company's own LinkedIn page.

    The dashboard shortcut linked to the FIRST FOUNDER's personal profile
    because no company-level field existed to point at.
    """

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_profile_exposes_a_company_linkedin_field(self):
        payload = build_sections(self.profile)
        self.assertIsNotNone(payload)
        self.profile.linkedin_url = "https://linkedin.com/company/acme"
        self.profile.save()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.linkedin_url,
                         "https://linkedin.com/company/acme")

    def test_dashboard_link_is_the_company_page_not_a_founder(self):
        from fundos.profile.models import Founder
        from fundos.profile.summary import company_summaries

        Founder.objects.create(
            tenant_id=self.tenant.id, profile=self.profile, name="Asha Rao",
            linkedin_url="https://linkedin.com/in/asharao", source="founder")

        links = company_summaries([self.company.id])[
            str(self.company.id)]["attachmentLinks"]
        self.assertEqual(
            links["companyLinkedin"], "",
            "with no company page recorded the shortcut must be empty, not "
            "a founder's personal profile")

        self.profile.linkedin_url = "https://linkedin.com/company/acme"
        self.profile.save()
        links = company_summaries([self.company.id])[
            str(self.company.id)]["attachmentLinks"]
        self.assertEqual(links["companyLinkedin"],
                         "https://linkedin.com/company/acme")


class MarketSizingProvenanceTests(SeededTestCase):
    """CR-11 — a market size with no stated source is unverifiable."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_each_figure_carries_source_and_assumptions(self):
        # Renamed on the wire by the generated-profile contract; storage and
        # shape are unchanged, and the previous name still resolves on write.
        data = build_sections(self.profile)["industry_research"]["data"]
        for key in ("tam", "sam", "som"):
            with self.subTest(figure=key):
                self.assertIn("source", data["market_sizing"][key])
                self.assertIn("assumptions", data["market_sizing"][key])
        self.assertIn("market_sizing_narrative", data)
        self.assertIn("methodology", data)

    def test_sourced_figures_round_trip(self):
        update_section_from_data(
            self.profile, "industry_and_market_research",
            {"industry_evolution": "Consolidating.",
             "market_sizing_narrative": "Derived top-down from the 2025 report.",
             "methodology": "Top-down, India only.",
             "market_sizing": {
                 "tam": {"value": "USD 12B", "source": "Redseer 2025",
                         "assumptions": "All India online + offline"},
                 "sam": {"value": "USD 3B", "source": "Redseer 2025",
                         "assumptions": "Online only"},
                 "som": {"value": "USD 400M", "source": "Internal estimate",
                         "assumptions": "5% share in 3 years"}},
             "performance_trends": [], "regulatory_developments": []},
            user=self.user)
        out = serialize_section(
            self.profile, "industry_and_market_research")["data"]
        self.assertEqual(out["market_sizing"]["tam"]["source"], "Redseer 2025")
        self.assertEqual(out["market_sizing"]["som"]["assumptions"],
                         "5% share in 3 years")
        self.assertEqual(out["market_sizing_narrative"],
                         "Derived top-down from the 2025 report.")

    def test_legacy_scalar_market_sizing_still_renders(self):
        """Profiles generated before this change stored a bare string. They
        must not break or silently disappear."""
        from fundos.profile.services import update_section

        update_section(self.profile, "market_research",
                       structured={"market_sizing": {"tam": "USD 12B"}},
                       user=self.user)
        out = serialize_section(
            self.profile, "industry_and_market_research")["data"]
        self.assertEqual(out["market_sizing"]["tam"]["value"], "USD 12B")
        self.assertEqual(out["market_sizing"]["tam"]["source"], "")


class CompetitorDescriptionTests(SeededTestCase):
    """CR-12 — figures alone cannot tell you if a competitor is comparable."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_description_is_emitted_and_writable(self):
        update_section_from_data(
            self.profile, "competitors",
            [{"name": "Rival Ltd",
              "description": "D2C ceramic cookware brand selling online.",
              "website": "https://rival.example.com",
              "status": "Challenger", "investors": ["Accel"]}],
            user=self.user)
        row = serialize_section(self.profile, "competitors")["data"][0]
        self.assertEqual(row["description"],
                         "D2C ceramic cookware brand selling online.")
        self.assertEqual(row["website"], "https://rival.example.com")

    def test_description_is_not_overwritten_by_business_model(self):
        """The deep-extract fan-out wrote the business model into the
        description column, which is why competitors showed figures with
        nothing explaining who they were."""
        update_section_from_data(
            self.profile, "competitors",
            [{"name": "Rival Ltd", "description": "Sells cookware direct.",
              "business_model": "D2C"}],
            user=self.user)
        row = serialize_section(self.profile, "competitors")["data"][0]
        self.assertEqual(row["description"], "Sells cookware direct.")
        self.assertEqual(row["business_model"], "D2C")

    def test_differentiators_do_not_land_in_investors(self):
        update_section_from_data(
            self.profile, "competitors",
            [{"name": "Rival Ltd", "investors": ["Accel"],
              "key_differentiators": ["Own kilns", "Faster delivery"]}],
            user=self.user)
        row = serialize_section(self.profile, "competitors")["data"][0]
        self.assertEqual(row["investors"], ["Accel"])
        self.assertEqual(row["key_differentiators"],
                         ["Own kilns", "Faster delivery"])


class UrlValidationTests(SeededTestCase):
    """CR-14 — link fields must hold a URL, not free text."""

    def setUp(self):
        self.tenant, self.user, self.company, self.profile = _bootstrap()

    def test_free_text_news_link_is_rejected(self):
        with self.assertRaises(SectionUpdateError):
            update_section_from_data(
                self.profile, "recent_news",
                [{"title": "Raise announced", "link": "saw it in the paper"}],
                user=self.user)

    def test_valid_news_link_is_accepted(self):
        update_section_from_data(
            self.profile, "recent_news",
            [{"title": "Raise announced",
              "link": "https://example.com/story", "source": "Example"}],
            user=self.user)
        row = serialize_section(self.profile, "recent_news")["data"][0]
        self.assertEqual(row["link"], "https://example.com/story")

    def test_bare_domain_is_normalised_rather_than_rejected(self):
        update_section_from_data(
            self.profile, "recent_news",
            [{"title": "Raise announced", "link": "example.com/story"}],
            user=self.user)
        row = serialize_section(self.profile, "recent_news")["data"][0]
        self.assertEqual(row["link"], "https://example.com/story")

    def test_empty_link_remains_allowed(self):
        update_section_from_data(
            self.profile, "recent_news",
            [{"title": "Raise announced", "link": ""}], user=self.user)
        self.assertEqual(
            serialize_section(self.profile, "recent_news")["data"][0]["link"], "")


class RetiredSectionTests(SeededTestCase):
    """CR-09 — AI Company Summary is retired as a standalone block."""

    def test_ai_company_summary_is_inactive_after_seeding(self):
        from fundos.platformcfg.models import ProfileSectionConfig

        cfg = ProfileSectionConfig.objects.filter(
            section_key="ai_company_summary").first()
        self.assertIsNotNone(cfg)
        self.assertFalse(cfg.is_active,
                         "retired section must not be generated")
        self.assertFalse(cfg.required_for_completeness,
                         "a retired section must not hold completeness down")

    def test_seeding_deactivates_an_already_active_row(self):
        """Existing installs already have an ACTIVE row that get_or_create
        would never touch — the seed has to reconcile it."""
        from fundos.platformcfg.models import ProfileSectionConfig

        ProfileSectionConfig.objects.filter(
            section_key="ai_company_summary").update(
                is_active=True, required_for_completeness=True)
        call_command("seed_platform_config", verbosity=0)
        cfg = ProfileSectionConfig.objects.get(section_key="ai_company_summary")
        self.assertFalse(cfg.is_active)
        self.assertFalse(cfg.required_for_completeness)
