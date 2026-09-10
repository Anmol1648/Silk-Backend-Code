"""
seed_initial_data (Doc 6 §13 / Doc 9 promotion path).

Idempotent — safe to re-run. Seeds: AppConfiguration singleton,
ScoringConfig v1, ResearchSourceFlags (linkedin gated), EVENT alert
definitions for the standard event keys, LLM endpoints + role bindings
(mocked), master values, InvestorCategoryMap, SectorTaxonomyMap, and
(with --demo) a demo tenant/user/company/deal.
"""
import uuid

from django.core.management.base import BaseCommand
from django.utils import timezone

STANDARD_EVENT_KEYS = [
    ("fundos.readiness.ready", "Readiness assessment ready",
     "The readiness assessment for {company} is ready ({band})."),
    ("fundos.strategy.approved", "Strategy approved",
     "The fundraising strategy for {company} was approved."),
    ("fundos.package.approved", "Investor package approved",
     "The investor package for {company} (v{version}) was approved."),
    ("fundos.member.invited", "Member invited",
     "{email} was invited to {company} as {role}."),
    ("fundos.doc.generated", "Document generated",
     "A new {doc_type} draft is ready for {company}."),
    ("fundos.review.complete", "Package review complete",
     "The package review for {company} finished with {findings} findings."),
    ("fundos.generation.failed", "AI generation failed",
     "Generation of {what} failed for deal {deal_id}: {error}"),
    ("fundos.email.failed", "Email delivery failed",
     "An email failed to send: {error}"),
    ("celery.task.slow", "Slow background task",
     "Task {task} ran for {seconds}s."),
    ("celery.task.failed", "Background task failed",
     "Task {task} failed: {error}"),
]

LLM_ROLES = [
    "research_synthesis", "material_critique", "readiness_summary",
    "peer_insight", "raise_narrative", "valuation_negotiation",
    "instrument_explainer", "strategy_blueprint", "investment_story",
    "teaser", "pitch_story", "pitch_slides", "fin_model_assist",
    "fin_review", "im_section", "im_narrative", "im_consistency",
    "package_review", "objection_sim",
    # Company Profile generation roles — previously omitted here, so in a LIVE
    # (non-mocked) environment these raised "no LLM binding configured". Bound
    # explicitly so profile generation works against a real endpoint.
    "company_profile_section", "company_profile_records",
    "company_profile_structured", "founder_profile", "company_profile_field",
    "company_profile_deep_extract", "profile_qa",
    # Stage 2 of the two-stage dossier. Without a binding here, llm_generate
    # raises "no LLM binding configured" at call time and the judgement stage
    # silently degrades to stage-1 output.
    "company_profile_judgment",
]

MASTER_VALUES = {
    "m_round_type": ["pre_seed", "seed", "pre_series_a", "series_a",
                     "series_b", "bridge"],
    "m_timeline": ["3m", "6m", "9m", "12m"],
    "m_runway_band": ["12m", "18m", "24m", "36m"],
    "m_instrument": ["equity", "safe", "convertible_note", "ccd"],
    "m_purpose": ["product", "gtm", "hiring", "working_capital",
                  "expansion", "runway_extension"],
    "m_investor_type": ["Angels", "Micro VCs", "Seed VCs", "Series A VCs",
                        "Growth", "Family Offices", "Accelerators",
                        "Corporate VCs"],
    "m_geography": ["India", "US", "SEA", "MENA", "Europe"],
    "m_material_category": ["investment_material", "financial", "legal",
                            "company", "other"],
}

INVESTOR_CATEGORY_MAP = [
    # (round_type, low_usd, high_usd, primary, secondary, optional)
    ("pre_seed", 0, 1_000_000,
     ["Angels", "Micro VCs"], ["Accelerators"], ["Family Offices"]),
    ("seed", 250_000, 3_000_000,
     ["Seed VCs", "Micro VCs"], ["Angels", "Family Offices"],
     ["Corporate VCs"]),
    ("pre_series_a", 1_000_000, 8_000_000,
     ["Seed VCs", "Series A VCs"], ["Family Offices"], ["Growth"]),
    ("series_a", 3_000_000, 20_000_000,
     ["Series A VCs"], ["Corporate VCs", "Family Offices"], ["Growth"]),
    ("bridge", 0, 5_000_000,
     ["Existing investors"], ["Family Offices", "Angels"], ["Micro VCs"]),
]

SECTOR_ADJACENCY = [
    ("SaaS", ["Fintech", "HealthTech", "EdTech"]),
    ("Fintech", ["SaaS", "InsurTech"]),
    ("HealthTech", ["SaaS", "BioTech"]),
    ("EdTech", ["SaaS", "Consumer"]),
    ("D2C", ["Consumer", "Marketplace"]),
]


class Command(BaseCommand):
    help = "Seed FundOS initial configuration (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true",
                            help="Also create a demo tenant/user/deal.")

    def handle(self, *args, **options):
        self.seed_app_configuration()
        self.seed_scoring_config()
        self.seed_research_flags()
        self.seed_llm_registry()
        self.seed_event_alerts()
        self.seed_master_values()
        self.seed_investor_category_map()
        self.seed_sector_taxonomy()
        if options["demo"]:
            self.seed_demo()
        self.stdout.write(self.style.SUCCESS("seed_initial_data complete."))

    # ------------------------------------------------------------------

    def seed_app_configuration(self):
        from django.conf import settings
        from fundos.config.models import AppConfiguration
        if not AppConfiguration.get_solo():
            # Tester Issues 9/20: mocked AI leaked into tested environments
            # because the seed hardcoded ai_mocked=True. The default is now
            # settings-driven: True only in dev; uat/prod seed with live AI
            # (and llm_generate fails loudly with E-LLM-502 when no live
            # endpoint is configured, instead of silently serving mocks).
            mocked = bool(getattr(settings, "FUNDOS_AI_MOCKED_DEFAULT", True))
            AppConfiguration.objects.create(ai_mocked=mocked)
            self.stdout.write(f"  + AppConfiguration (ai_mocked={mocked})")

    def seed_scoring_config(self):
        from fundos.config.models import ScoringConfig
        from fundos.engines.default_config import DEFAULT_SCORING_CONFIG_V1
        if not ScoringConfig.objects.exists():
            ScoringConfig.objects.create(version_no=1, is_active=True,
                                         data=DEFAULT_SCORING_CONFIG_V1)
            self.stdout.write("  + ScoringConfig v1 (active)")

    def seed_research_flags(self):
        from fundos.config.models import ResearchSourceFlags
        from fundos.research.models import SOURCE_TYPES
        for source in SOURCE_TYPES:
            ResearchSourceFlags.objects.get_or_create(
                source_type=source,
                defaults={"enabled": source != "linkedin",
                          # BR-M1-003: LinkedIn stays legally gated
                          "legal_cleared": False})
        self.stdout.write("  + ResearchSourceFlags (linkedin gated)")

    def seed_llm_registry(self):
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding
        mock, _ = LLMEndpoint.objects.get_or_create(
            code="MOCK",
            defaults={"provider_kind": "custom_rest", "base_url": "",
                      "default_model": "mock-1", "api_key_env_var": "",
                      "is_active": True, "priority": 100})
        openai_ep, _ = LLMEndpoint.objects.get_or_create(
            code="OPENAI-1",
            defaults={"provider_kind": "openai_chat",
                      "base_url": "https://api.openai.com/v1",
                      "default_model": "gpt-4o-mini",
                      "api_key_env_var": "FUNDOS_LLM_OPENAI_KEY",
                      "is_active": True, "priority": 10})
        for role in LLM_ROLES:
            LLMRoleBinding.objects.get_or_create(
                role=role,
                defaults={"primary_endpoint": openai_ep,
                          "fallback_endpoint": mock, "is_mocked": True})
        self.stdout.write(f"  + LLM registry ({len(LLM_ROLES)} role bindings, "
                          "mocked)")

    def seed_event_alerts(self):
        from fundos.core.models import AlertDefinition
        for event_key, heading, template in STANDARD_EVENT_KEYS:
            AlertDefinition.all_objects.get_or_create(
                event_key=event_key, is_deleted=False,
                defaults={"name": heading,
                          "source_type": AlertDefinition.SourceType.EVENT,
                          "heading": heading,
                          "insight_user_query": template,
                          "notify_email": False, "notify_in_app": True,
                          "notify_whatsapp": False, "is_active": True})
        self.stdout.write(f"  + {len(STANDARD_EVENT_KEYS)} EVENT alert "
                          "definitions")

    def seed_master_values(self):
        from fundos.config.models import MasterValue
        count = 0
        for table, codes in MASTER_VALUES.items():
            for i, code in enumerate(codes):
                _, created = MasterValue.objects.get_or_create(
                    table=table, code=code,
                    defaults={"label": code.replace("_", " ").title(),
                              "sort_order": i})
                count += int(created)
        self.stdout.write(f"  + MasterValue rows ({count} new)")

    def seed_investor_category_map(self):
        from fundos.config.models import InvestorCategoryMap
        for round_type, low, high, primary, secondary, optional in \
                INVESTOR_CATEGORY_MAP:
            InvestorCategoryMap.objects.get_or_create(
                round_type=round_type,
                raise_band=f"usd_{int(low)}_{int(high)}",
                defaults={"band_low_usd": low, "band_high_usd": high,
                          "primary": primary, "secondary": secondary,
                          "optional": optional})
        self.stdout.write("  + InvestorCategoryMap")

    def seed_sector_taxonomy(self):
        from fundos.config.models import SectorTaxonomyMap
        for sector, adjacent in SECTOR_ADJACENCY:
            SectorTaxonomyMap.objects.get_or_create(
                founder_sector=sector,
                defaults={"canonical_sector": sector,
                          "adjacent_sectors": adjacent})
        self.stdout.write("  + SectorTaxonomyMap")

    def seed_demo(self):
        from fundos.core.models import Company, Deal, Membership, Tenant, User
        from fundos.core.scoping import tenant_context

        tenant = Tenant.objects.filter(name="Demo Tenant").first() or \
            Tenant.objects.create(name="Demo Tenant")
        with tenant_context(tenant.id):
            user = User.objects.filter(email="founder@demo.fundos.in",
                                       is_deleted=False).first()
            if not user:
                user = User.objects.create_user(
                    email="founder@demo.fundos.in", tenant_id=tenant.id,
                    name="Demo Founder")
            company, _ = Company.all_objects.get_or_create(
                tenant_id=tenant.id, name="Acme AI", is_deleted=False,
                defaults={"domain": "acme.example", "hq_country": "IN"})
            deal = Deal.all_objects.filter(
                tenant_id=tenant.id, company=company,
                name="Acme Seed Round", is_deleted=False).first()
            if not deal:
                deal = Deal.objects.create(
                    tenant_id=tenant.id, company=company,
                    name="Acme Seed Round", round_type="seed",
                    primary_owner=user, created_by=user)
            Membership.all_objects.get_or_create(
                tenant_id=tenant.id, user=user, scope_type="deal",
                scope_id=deal.id, role="founder", is_deleted=False,
                defaults={"status": "active"})
        self.stdout.write(self.style.SUCCESS(
            f"  + demo tenant/user/deal (deal_id={deal.id})"))
