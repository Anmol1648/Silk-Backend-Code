"""
Seed the admin-owned configuration with working defaults.

Idempotent: re-running updates shipped defaults but never clobbers a row an
admin has edited (tracked via the `is_admin_edited` convention of bumping
version_no on prompts, and get_or_create elsewhere).

    python manage.py seed_platform_config
    python manage.py seed_platform_config --reset-prompts   # force prompt refresh
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from fundos.llm.default_prompts import DEFAULT_PROMPTS
from fundos.platformcfg.models import (
    BrandConfig, Country, FundraiseBucket, ProfileSectionConfig,
    PromptTemplate, StageDefinition, TractionRule, UiCopy, UpliftRule,
)

# --------------------------------------------------------------------------
# Countries — common markets pinned to the top, then the full list.
# --------------------------------------------------------------------------
PINNED = [
    ("IN", "IND", "India", "+91", "INR", 1),
    ("US", "USA", "United States", "+1", "USD", 2),
    ("GB", "GBR", "United Kingdom", "+44", "GBP", 3),
    ("SG", "SGP", "Singapore", "+65", "SGD", 4),
    ("AE", "ARE", "United Arab Emirates", "+971", "AED", 5),
    ("AU", "AUS", "Australia", "+61", "AUD", 6),
    ("CA", "CAN", "Canada", "+1", "CAD", 7),
    ("DE", "DEU", "Germany", "+49", "EUR", 8),
]

REST = [
    ("AF", "AFG", "Afghanistan", "+93", "AFN"), ("AL", "ALB", "Albania", "+355", "ALL"),
    ("DZ", "DZA", "Algeria", "+213", "DZD"), ("AR", "ARG", "Argentina", "+54", "ARS"),
    ("AM", "ARM", "Armenia", "+374", "AMD"), ("AT", "AUT", "Austria", "+43", "EUR"),
    ("AZ", "AZE", "Azerbaijan", "+994", "AZN"), ("BH", "BHR", "Bahrain", "+973", "BHD"),
    ("BD", "BGD", "Bangladesh", "+880", "BDT"), ("BY", "BLR", "Belarus", "+375", "BYN"),
    ("BE", "BEL", "Belgium", "+32", "EUR"), ("BO", "BOL", "Bolivia", "+591", "BOB"),
    ("BA", "BIH", "Bosnia and Herzegovina", "+387", "BAM"),
    ("BW", "BWA", "Botswana", "+267", "BWP"), ("BR", "BRA", "Brazil", "+55", "BRL"),
    ("BG", "BGR", "Bulgaria", "+359", "BGN"), ("KH", "KHM", "Cambodia", "+855", "KHR"),
    ("CM", "CMR", "Cameroon", "+237", "XAF"), ("CL", "CHL", "Chile", "+56", "CLP"),
    ("CN", "CHN", "China", "+86", "CNY"), ("CO", "COL", "Colombia", "+57", "COP"),
    ("CR", "CRI", "Costa Rica", "+506", "CRC"), ("HR", "HRV", "Croatia", "+385", "EUR"),
    ("CY", "CYP", "Cyprus", "+357", "EUR"), ("CZ", "CZE", "Czechia", "+420", "CZK"),
    ("DK", "DNK", "Denmark", "+45", "DKK"),
    ("DO", "DOM", "Dominican Republic", "+1809", "DOP"),
    ("EC", "ECU", "Ecuador", "+593", "USD"), ("EG", "EGY", "Egypt", "+20", "EGP"),
    ("SV", "SLV", "El Salvador", "+503", "USD"), ("EE", "EST", "Estonia", "+372", "EUR"),
    ("ET", "ETH", "Ethiopia", "+251", "ETB"), ("FI", "FIN", "Finland", "+358", "EUR"),
    ("FR", "FRA", "France", "+33", "EUR"), ("GE", "GEO", "Georgia", "+995", "GEL"),
    ("GH", "GHA", "Ghana", "+233", "GHS"), ("GR", "GRC", "Greece", "+30", "EUR"),
    ("GT", "GTM", "Guatemala", "+502", "GTQ"), ("HK", "HKG", "Hong Kong", "+852", "HKD"),
    ("HU", "HUN", "Hungary", "+36", "HUF"), ("IS", "ISL", "Iceland", "+354", "ISK"),
    ("ID", "IDN", "Indonesia", "+62", "IDR"), ("IQ", "IRQ", "Iraq", "+964", "IQD"),
    ("IE", "IRL", "Ireland", "+353", "EUR"), ("IL", "ISR", "Israel", "+972", "ILS"),
    ("IT", "ITA", "Italy", "+39", "EUR"), ("JM", "JAM", "Jamaica", "+1876", "JMD"),
    ("JP", "JPN", "Japan", "+81", "JPY"), ("JO", "JOR", "Jordan", "+962", "JOD"),
    ("KZ", "KAZ", "Kazakhstan", "+7", "KZT"), ("KE", "KEN", "Kenya", "+254", "KES"),
    ("KW", "KWT", "Kuwait", "+965", "KWD"), ("LV", "LVA", "Latvia", "+371", "EUR"),
    ("LB", "LBN", "Lebanon", "+961", "LBP"), ("LT", "LTU", "Lithuania", "+370", "EUR"),
    ("LU", "LUX", "Luxembourg", "+352", "EUR"), ("MY", "MYS", "Malaysia", "+60", "MYR"),
    ("MT", "MLT", "Malta", "+356", "EUR"), ("MU", "MUS", "Mauritius", "+230", "MUR"),
    ("MX", "MEX", "Mexico", "+52", "MXN"), ("MA", "MAR", "Morocco", "+212", "MAD"),
    ("MM", "MMR", "Myanmar", "+95", "MMK"), ("NP", "NPL", "Nepal", "+977", "NPR"),
    ("NL", "NLD", "Netherlands", "+31", "EUR"),
    ("NZ", "NZL", "New Zealand", "+64", "NZD"), ("NG", "NGA", "Nigeria", "+234", "NGN"),
    ("NO", "NOR", "Norway", "+47", "NOK"), ("OM", "OMN", "Oman", "+968", "OMR"),
    ("PK", "PAK", "Pakistan", "+92", "PKR"), ("PA", "PAN", "Panama", "+507", "PAB"),
    ("PY", "PRY", "Paraguay", "+595", "PYG"), ("PE", "PER", "Peru", "+51", "PEN"),
    ("PH", "PHL", "Philippines", "+63", "PHP"), ("PL", "POL", "Poland", "+48", "PLN"),
    ("PT", "PRT", "Portugal", "+351", "EUR"), ("QA", "QAT", "Qatar", "+974", "QAR"),
    ("RO", "ROU", "Romania", "+40", "RON"), ("RU", "RUS", "Russia", "+7", "RUB"),
    ("SA", "SAU", "Saudi Arabia", "+966", "SAR"), ("RS", "SRB", "Serbia", "+381", "RSD"),
    ("SK", "SVK", "Slovakia", "+421", "EUR"), ("SI", "SVN", "Slovenia", "+386", "EUR"),
    ("ZA", "ZAF", "South Africa", "+27", "ZAR"),
    ("KR", "KOR", "South Korea", "+82", "KRW"), ("ES", "ESP", "Spain", "+34", "EUR"),
    ("LK", "LKA", "Sri Lanka", "+94", "LKR"), ("SE", "SWE", "Sweden", "+46", "SEK"),
    ("CH", "CHE", "Switzerland", "+41", "CHF"), ("TW", "TWN", "Taiwan", "+886", "TWD"),
    ("TZ", "TZA", "Tanzania", "+255", "TZS"), ("TH", "THA", "Thailand", "+66", "THB"),
    ("TN", "TUN", "Tunisia", "+216", "TND"), ("TR", "TUR", "Turkey", "+90", "TRY"),
    ("UG", "UGA", "Uganda", "+256", "UGX"), ("UA", "UKR", "Ukraine", "+380", "UAH"),
    ("UY", "URY", "Uruguay", "+598", "UYU"),
    ("UZ", "UZB", "Uzbekistan", "+998", "UZS"), ("VN", "VNM", "Vietnam", "+84", "VND"),
    ("ZM", "ZMB", "Zambia", "+260", "ZMW"), ("ZW", "ZWE", "Zimbabwe", "+263", "ZWL"),
]

# --------------------------------------------------------------------------
# The seven fund-raise values (C9)
# --------------------------------------------------------------------------
BUCKETS = [
    (1, "$100K", 100_000, "Idea / Pre-Seed", ["Angels"], 5, 10),
    (2, "$250K", 250_000, "Pre-Seed", ["Angels", "MicroVC"], 8, 15),
    (3, "$500K", 500_000, "Seed", ["Angels", "MicroVC"], 10, 18),
    (4, "$1M", 1_000_000, "Seed", ["Angels", "MicroVC", "VC"], 12, 20),
    (5, "$2M", 2_000_000, "Seed+ / Pre-Series A", ["MicroVC", "VC", "FO"], 15, 22),
    (6, "$3M", 3_000_000, "Pre-Series A / Series A",
     ["MicroVC", "VC", "PE", "FO"], 15, 25),
    (7, "$5M", 5_000_000, "Series A", ["VC", "PE", "FO"], 18, 25),
]

# Traction rules from Outline §3.3 — evaluated lowest priority first.
TRACTION_RULES = [
    # Evaluated lowest-priority-number first. The ladder in Outline §3.3 is
    # cumulative — a company at US$1.8M ARR also satisfies every lower rung —
    # so the MOST demanding thresholds are checked first and the first match
    # wins. Reordering these in the admin changes the ladder without a release.
    (10, "ARR or capital at/above US$1.5M",
     {"arr_usd_min": 1_500_000},
     7,
     "ARR at or above US$1.5M supports a Series A from VCs, family offices "
     "and PE.",
     ""),

    (11, "Capital raised at/above US$1.5M",
     {"capital_raised_usd_min": 1_500_000},
     7,
     "More than US$1.5M raised to date supports a Series A.",
     ""),

    (20, "ARR at/above US$1M",
     {"arr_usd_min": 1_000_000},
     6,
     "ARR at or above US$1M supports a round from micro VCs, family offices "
     "and early-stage PE.",
     ""),

    (21, "Capital raised at/above US$1M",
     {"capital_raised_usd_min": 1_000_000},
     6,
     "More than US$1M raised to date supports a larger follow-on round.",
     ""),

    (30, "ARR at/above US$0.5M",
     {"arr_usd_min": 500_000},
     5,
     "ARR at or above US$0.5M supports a larger round from micro VCs and "
     "family offices.",
     ""),

    (31, "Capital raised at/above US$0.5M",
     {"capital_raised_usd_min": 500_000},
     5,
     "More than US$0.5M raised to date supports a follow-on round from micro "
     "VCs and family offices.",
     ""),

    (40, "Early ARR, full-time founder, paying clients",
     {"arr_usd_min": 1, "founder_full_time": True, "paying_customers_min": 1},
     4,
     "There is ARR, the founder is full-time and there are paying clients. "
     "This supports a round from micro VCs, accelerators, angels, UHNIs and "
     "family offices.",
     ""),

    # --- ARR-only rungs -------------------------------------------------
    # Rule 40 needs the full-time flag AND a customer count. Those are
    # frequently blank on a young profile, and without these rungs a company
    # with real revenue fell past every rule to the "smallest bucket"
    # fallback — a company at US$400K ARR was advised to raise US$100K.
    # These evaluate after rule 40 so a fuller profile still wins, and they
    # make bucket 3 reachable, which no rule previously did.
    (41, "ARR at/above US$250K",
     {"arr_usd_min": 250_000},
     4,
     "ARR at or above US$250K supports a round from micro VCs, angels and "
     "family offices.",
     ""),

    (42, "ARR at/above US$100K",
     {"arr_usd_min": 100_000},
     3,
     "ARR at or above US$100K supports an angel or accelerator round.",
     ""),

    (43, "Some ARR, below US$100K",
     {"arr_usd_min": 1},
     2,
     "There is early revenue but it is still below US$100K. An angel or "
     "accelerator round is the right next step.",
     ""),

    (44, "Capital raised at/above US$250K",
     {"capital_raised_usd_min": 250_000},
     3,
     "More than US$250K raised to date supports a follow-on angel round.",
     ""),

    (50, "Pre-revenue but full-time with early traction",
     {"arr_usd_max": 1, "capital_raised_usd_max": 1, "months_full_time_min": 3},
     2,
     "No ARR or capital raised yet, but the founder has been full-time for "
     "three months or more with early client traction. An angel or "
     "accelerator round is the right next step.",
     ""),

    (60, "Idea stage — unregistered, no traction",
     {"arr_usd_max": 1, "capital_raised_usd_max": 1, "company_registered": False},
     1,
     "No revenue, no capital raised and the company is not yet registered. "
     "This is idea stage.",
     "Recommended path: put in founder capital plus friends-and-family of "
     "roughly 10-20 lakhs, register the company, build some client traction, "
     "and return in about three months to raise an angel or accelerator "
     "round."),
]

# --------------------------------------------------------------------------
# Company Profile sections (PRD §5.4)
# --------------------------------------------------------------------------
SECTIONS = [
    # key, label, kind, order, llm_role, req_creation, req_complete
    ("company_overview", "Company Overview", "structured", 10,
     "company_profile_section", True, True),
    ("founders", "Founders", "list", 20, "founder_profile", False, True),
    ("key_people", "Key People", "list", 30, "company_profile_section", False, False),
    ("products_services", "Products & Services", "narrative", 40,
     "company_profile_section", False, True),
    ("customers_markets", "Customers & Markets", "narrative", 50,
     "company_profile_section", False, True),
    ("competitive_advantages", "Competitive Advantages", "narrative", 60,
     "company_profile_section", False, False),
    ("business_model", "Business Model", "narrative", 70,
     "company_profile_section", False, True),
    ("revenue_model", "Revenue Model", "structured", 80,
     "company_profile_field", False, True),
    ("company_metrics", "Company Metrics", "structured", 90,
     "company_profile_field", False, True),
    ("financial_summary", "Financial Summary", "structured", 100,
     "company_profile_field", False, False),
    ("funding_history", "Funding History", "list", 110, "", False, False),
    ("competitors", "Competitors", "list", 120, "company_profile_section", False, False),
    ("recent_news", "Recent News", "list", 130, "company_profile_section", False, False),
    # CR-09 — retired as a standalone block. Seeded inactive so generation
    # skips it (and stops paying for it); the stored content is left intact so
    # the change is reversible by re-activating the row in admin.
    ("ai_company_summary", "AI Company Summary", "narrative", 140,
     "company_profile_section", False, False),
    ("knowledge_base", "Company Knowledge Base", "documents", 150, "", False, False),
    ("document_center", "Document Center", "documents", 160, "", False, False),
    ("readiness", "Investor Readiness", "indicator", 170, "readiness_summary",
     False, False),
    # --- Enhancement set (company_profile_api_requirements Req 2 / 5 / 6 / 7).
    # These are emitted by the spec serializer and rendered by the client, but
    # were never registered here. update_section_from_data validates the key
    # against this registry, so every save of one of them was rejected with
    # "Unknown section" (surfaced as a 422) even though the UI offered an Edit
    # button. Keys below are the STORAGE keys; the API surface renames three
    # of them (see spec_serializer._STORAGE_ALIAS).
    ("cap_table", "Investors & Cap Table", "structured", 180,
     "company_profile_section", False, False),
    ("company_story", "Company Story & USP", "structured", 190,
     "company_profile_section", False, False),
    ("market_research", "Industry Overview & Market Research", "structured",
     200, "company_profile_section", False, False),
    ("investment_thesis", "Investment Thesis", "structured", 210,
     "company_profile_section", False, False),
    ("leadership_detail", "Leadership (CFO, Founders, CEO)", "structured",
     220, "company_profile_section", False, False),
    ("derived_multiples", "Derived Multiples (computed)", "structured", 230,
     "", False, False),
]

# --------------------------------------------------------------------------
# Stage journey (PRD §2.4) — prerequisites are advisory (C6)
# --------------------------------------------------------------------------
STAGES = [
    (0, "foundation", "Foundation",
     "Set up your company and invite the people who will work on the raise.",
     [], True),
    (1, "company_profile", "Company Profile",
     "Build a complete, investor-ready picture of the company. Everything "
     "downstream is generated from this.",
     [], True),
    (2, "fundraising_strategy", "Fundraising Strategy",
     "Decide how much to raise, at what valuation, and from whom — benchmarked "
     "against comparable companies.",
     [{"key": "company_profile_complete", "label": "Company Profile completed",
       "explanation": "Strategy quality depends on profile completeness. You can "
                      "proceed without it, but the recommendation will be weaker."}],
     True),
    (3, "investor_discovery", "Investor Discovery",
     "Find and prioritise the investors most likely to fund this round.",
     [{"key": "strategy_outputs_available",
       "label": "Target raise and valuation available",
       "explanation": "Investor matching uses your target raise and valuation. "
                      "Complete Stage 2, or enter these figures directly, to get "
                      "recommendations."}],
     False),
    (4, "outreach", "Outreach",
     "Run and track investor outreach across email and LinkedIn.", [], False),
    (5, "term_sheets", "Term Sheets",
     "Compare, negotiate and finalise term sheets.", [], False),
    (6, "due_diligence", "Due Diligence",
     "Coordinate financial, legal and commercial due diligence.", [], False),
    (7, "definitive_documents", "Definitive Documents",
     "SHA, SSA, SPA and related definitive documentation.", [], False),
    (8, "closing", "Closing",
     "Regulatory filings, closing mechanics and deal announcement.", [], False),
]

# --------------------------------------------------------------------------
# Editable UI copy
# --------------------------------------------------------------------------
UI_COPY = [
    ("profile.generating",
     "Building your company profile from the website, your documents and public "
     "sources. This usually takes a minute or two.",
     "Shown while profile generation runs."),
    ("profile.section.unavailable",
     "We couldn't find enough information for this section. Add details directly, "
     "or upload a document that covers it.",
     "Shown on a profile section that came back empty."),
    ("profile.linkedin.confirm",
     "I confirm I have reviewed this information and provide it as my own input.",
     "Founder confirmation required alongside LinkedIn-derived data (C3)."),
    ("stage.prereq.pending",
     "You can continue, but some inputs are still missing.",
     "Advisory prerequisite banner."),
    ("strategy.manual_override",
     "You can enter your target raise and valuation directly and skip the guided "
     "strategy if you already know what you're raising.",
     "Shown on Stage 2 — supports C6 (strategy is advisory)."),
    ("valuation.disclaimer",
     "Indicative range for discussion only — not a valuation opinion or investment "
     "advice. SEBI/FEMA advisory notes apply; consult your advisors.",
     "Regulatory note on all valuation output."),
    ("benchmark.data_unavailable",
     "Live market benchmarking data isn't connected yet. You can add peer "
     "companies manually and the analysis will use those.",
     "Shown when the market data feed is not configured."),
    ("materials.readonly",
     "Investor materials generated earlier remain available here. New material "
     "generation is being rebuilt as a dedicated assistant.",
     "Document Center note for migrated Stage 3 artefacts (C7)."),
]


class Command(BaseCommand):
    help = "Seed admin-owned platform configuration with working defaults."

    def add_arguments(self, parser):
        parser.add_argument("--reset-prompts", action="store_true",
                            help="Overwrite existing prompt rows with defaults.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report every change this command WOULD make to existing "
                 "configuration, then roll back without writing anything.")
        parser.add_argument(
            "--pin-models", action="store_true",
            help="Force every Gemini tier profile and both profile-pipeline "
                 "profiles onto the pinned model, even where the existing "
                 "model works. Normal runs only repair a tier that CANNOT "
                 "serve a call, so an install already running on another "
                 "Gemini model keeps it; this migrates one deliberately. "
                 "Pair with --dry-run first.")

    def handle(self, *args, **options):
        """Seed every block, reporting rather than aborting on failure.

        This was one atomic transaction over all blocks, so a failure in the
        LAST block (llm tiers) rolled back everything before it AND stopped the
        command — leaving an administrator who ran it with a stack trace, no
        seeded configuration, and no indication that the earlier blocks had
        worked. Each block is now its own transaction: a failure is reported
        with its remedy, the remaining blocks still run, and the command exits
        non-zero so a deploy script still notices.
        """
        blocks = [
            ("brand", self._brand),
            ("countries", self._countries),
            ("buckets", self._buckets),
            ("traction rules", self._traction),
            ("profile sections", self._sections),
            ("profile section schema", self._section_schema),
            ("research question bank", self._research_bank),
            ("stages", self._stages),
            ("prompts", lambda: self._prompts(
                reset=options.get("reset_prompts", False))),
            ("ui copy", self._copy),
            ("llm model catalog", self._llm_model_catalog),
            ("llm tiers", self._llm_tiers),
            ("llm provider selection", self._llm_provider_selection),
            ("llm role bindings", self._llm_role_bindings),
            ("profile pipeline profiles", self._profile_pipeline_profiles),
        ]
        if options.get("pin_models"):
            # Last, so it overrides anything the blocks above left in place.
            blocks.append(("model pin", self._pin_models))

        self._changes = []
        self._dry_run = bool(options.get("dry_run"))
        if self._dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — nothing will be written."))

        failed = []
        for name, fn in blocks:
            try:
                with transaction.atomic():
                    fn()
                    if self._dry_run:
                        transaction.set_rollback(True)
            except Exception as e:
                failed.append((name, e))
                self.stderr.write(self.style.ERROR(
                    f"  {name}: FAILED — {type(e).__name__}: {e}"))
                self.stderr.write(
                    f"     (the other blocks still ran; re-run this command "
                    f"after fixing the cause)")

        # Every change to configuration that ALREADY EXISTED is reported
        # together at the end. Seeding runs at deploy time, so a value an
        # administrator set by hand can be changed by a routine nobody was
        # watching — and then only be noticed three generations later, in a
        # trace, as a model name the API refuses. An overwrite has to be
        # visible at the moment it happens.
        if self._changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "EXISTING CONFIGURATION CHANGED BY THIS RUN:"))
            for what, field, before, after, why in self._changes:
                suffix = f"   ({why})" if why else ""
                self.stdout.write(
                    f"  {what} {field}: {before!r} -> {after!r}{suffix}")
            self.stdout.write(
                "  Re-run with --dry-run first if this was unexpected.")

        if failed:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(
                f"Platform configuration seeded WITH {len(failed)} FAILED "
                f"block(s): {', '.join(n for n, _ in failed)}"))
            raise SystemExit(1)
        if self._dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN complete — rolled back, nothing written."))
            return
        self.stdout.write(self.style.SUCCESS("Platform configuration seeded."))

    def _record(self, what, field, before, after, why=""):
        """Journal a change to configuration that already existed."""
        if not hasattr(self, "_changes"):
            self._changes = []
        self._changes.append((what, field, before, after, why))

    @staticmethod
    def _get_or_create_unique(model, defaults=None, **lookup):
        """get_or_create that survives pre-existing duplicate rows.

        Django's get_or_create calls .get(), which raises
        MultipleObjectsReturned the moment duplicates exist — and duplicates
        DO exist wherever a unique constraint includes a nullable column,
        because PostgreSQL treats NULLs as distinct. Seeding must be able to
        recover from that state rather than crash on it, since crashing is how
        the state goes unnoticed in the first place.
        """
        rows = list(model.objects.filter(**lookup).order_by("-pk")[:50])
        if len(rows) > 1:
            keep = rows[0]
            for extra in rows[1:]:
                extra.delete()
            return keep, False
        if rows:
            return rows[0], False
        return model.objects.create(**{**(defaults or {}), **lookup}), True

    # ------------------------------------------------------------------
    # Provider selection + role bindings.
    #
    # These were the two steps an administrator had to do by hand, and the
    # two most often missed. Both are now derived from a single fact the
    # system can check for itself: WHICH ENDPOINT ACTUALLY HAS A USABLE API
    # KEY. An endpoint whose key environment variable is unset cannot serve a
    # call, so it is not a candidate however it is configured.
    #
    # Both blocks CONVERGE rather than overwrite: they only act where the
    # configuration is absent or provably broken, so an administrator's
    # deliberate choice is never undone by a later seed run.
    # ------------------------------------------------------------------
    @staticmethod
    def _preferred_endpoint_for_kind(kind):
        """The row that should serve `kind`, by the SAME rule as _usable_endpoints.

        v29. Extracted so the endpoint-adoption step and the provider-selection
        step cannot disagree about which of two rows for one vendor is the
        real one. The tie-break order is deliberate:

          1. already referenced by a role binding  — traffic is going there
          2. active                                — an operator enabled it
          3. priority, then code                   — stable, arbitrary

        Rule 1 is the one v28 omitted, and it is the only rule that reflects
        what the system is actually doing rather than what a seeder prefers.
        """
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        rows = list(LLMEndpoint.objects.filter(provider_kind=kind))
        if not rows:
            return None
        in_use = set(LLMRoleBinding.objects
                     .values_list("primary_endpoint_id", flat=True))
        rows.sort(key=lambda e: (e.code not in in_use, not e.is_active,
                                 e.priority, e.code))
        return rows[0]

    def _usable_endpoints(self):
        """Endpoints whose API key is actually present in the environment.

        A key being set is the administrator stating which provider they
        intend to use, so an endpoint that has one is a candidate even if it
        was seeded inactive — and is activated below. An endpoint with no key
        cannot serve a call however it is configured, so it is never a
        candidate. Already-active endpoints are preferred, then priority.
        """
        from fundos.llm.models import LLMEndpoint

        from fundos.llm.models import LLMRoleBinding

        candidates = [ep for ep in LLMEndpoint.objects.order_by("priority")
                      if ep.api_key]

        # ONE endpoint per provider. Two rows for the same vendor both read
        # their key from the same environment variable, so both look usable
        # and which one wins is arbitrary — after which role bindings and
        # config profiles can end up split across the pair. This happens
        # routinely: an administrator creates the endpoint by hand with their
        # own code ("GEMINI LLM"), and the canonical row seeded alongside it
        # becomes a second candidate for the same provider.
        #
        # The row the system is already USING wins, so seeding converges on
        # the administrator's endpoint rather than migrating traffic to a
        # fresh one behind their back.
        in_use = set(LLMRoleBinding.objects
                     .values_list("primary_endpoint_id", flat=True))
        preferred = {}
        for ep in sorted(candidates,
                         key=lambda e: (e.code not in in_use, not e.is_active,
                                        e.priority, e.code)):
            preferred.setdefault(ep.provider_kind, ep)
        chosen = [ep for ep in candidates if preferred.get(ep.provider_kind)
                  is ep]
        chosen.sort(key=lambda e: (not e.is_active, e.priority))

        redundant = [ep.code for ep in candidates if ep not in chosen]
        if redundant:
            self.stdout.write(
                f"  llm endpoint: ignoring duplicate endpoint(s) "
                f"{', '.join(redundant)} — another row already serves the "
                f"same provider")

        for ep in chosen:
            if not ep.is_active:
                ep.is_active = True
                ep.save(update_fields=["is_active", "updated_at"])
                self.stdout.write(
                    f"  llm endpoint: activated {ep.code} (its API key is set)")
        return chosen

    # Models the catalog marks "retiring" that this deployment pins anyway,
    # each with the reason it was accepted. Seeding a model with a published
    # end date normally just schedules the same outage again, so the rule is
    # enforced by test and an exception has to be written down here to be
    # allowed — which keeps the decision visible in review and gives whoever
    # inherits it a list of what to revisit, rather than a silent drift onto a
    # name nobody chose.
    ACCEPTED_RETIRING_MODELS = {
        "gemini-2.5-flash":
            "Pinned by the operator: the account's keys are entitled to this "
            "model, and an unpinned tier resolving to a name the key does not "
            "serve fails the call outright. Revisit when the account moves.",
    }

    # Recommended model per provider and tier. Excludes models the catalog
    # marks as retiring, except those recorded in ACCEPTED_RETIRING_MODELS
    # above.
    _RECOMMENDED = {
        # Conservative on purpose. These are only used when a profile has NO
        # model string at all, so the right default is the one most likely to
        # actually be served by the caller's key — not the newest name.
        # Pinned to gemini-2.5-flash on every tier, deliberately and
        # explicitly. The operator running this deployment supplies keys
        # entitled to that model, and a tier silently resolving to a different
        # one is the failure this whole table exists to prevent: the call
        # succeeds, the bill differs, and nothing in the run record says which
        # model answered. It is also the model both profile-pipeline config
        # profiles already pin (PIPELINE_MODEL below), so pinning the tiers
        # here means one model serves the entire system rather than two that
        # have to be kept in step.
        #
        # The catalog marks it "retiring". That is a real end date to plan
        # against, not a reason to let the tier drift onto a name the key may
        # not serve — change it here, in one place, when the account moves.
        "gemini": {"simple": "gemini-2.5-flash",
                   "advanced": "gemini-2.5-flash",
                   "judgment": "gemini-2.5-flash"},
        "anthropic": {"simple": "claude-haiku-4-5",
                      "advanced": "claude-sonnet-4-6",
                      "judgment": "claude-opus-5"},
        "openai": {"simple": "gpt-5.6-sol-mini",
                   "advanced": "gpt-5.6-sol",
                   "judgment": "gpt-5.6-sol"},
    }

    def _llm_provider_selection(self):
        """Point each tier at whichever provider has a working key.

        Only rebuilds a tier that CANNOT work — no active profile resolves,
        its endpoint has no key, its model is not in the catalog, or the
        searching tier has search switched off. An administrator who has
        deliberately chosen a provider that works is left alone.

        Capability settings that a tier needs in order to function at all
        (see `_tier_defaults`) are filled in on every run, including on
        tiers that are otherwise healthy — those are properties of the
        provider's API rather than preferences, and leaving them to be
        discovered by an administrator is what produced an empty dossier
        and an unbounded search budget on the live install.
        """
        from fundos.llm.models import (LLMConfigProfile, LLMModelCatalog,
                                       TenantLLMTier)

        usable = self._usable_endpoints()
        if not usable:
            self.stdout.write(
                "  llm provider selection: no endpoint has a usable API key "
                "— set the key env var (e.g. GEMINI_API_KEY) and re-run")
            return

        usable_codes = {ep.code for ep in usable}
        usable_kinds = {ep.provider_kind for ep in usable}

        # Each tier is assessed SEPARATELY. This used to test the advanced
        # tier alone and return early when it was healthy, on the assumption
        # that the three tiers are configured together. They are not: an
        # administrator repairing the tier that visibly failed leaves the
        # other two as they were, and the next seed run then declared the
        # system fine while `simple` and `judgment` resolved to nothing —
        # which is a CRITICAL failure, because llm_generate raises
        # LLMUnavailable rather than falling back.
        from fundos.llm.models import ENDPOINT_KIND_TO_PROVIDER

        endpoint = usable[0]
        # An endpoint's provider_kind and a config profile's provider are two
        # different vocabularies: the kind is a wire protocol ("openai_chat",
        # "deepinfra"), the provider is a capability family ("openai"). Using
        # the kind directly created profiles coded tier.judgment.openai_chat
        # with a provider value outside CONFIG_PROVIDERS and — because
        # _RECOMMENDED is keyed by provider — an EMPTY model string.
        provider = ENDPOINT_KIND_TO_PROVIDER.get(endpoint.provider_kind,
                                                 endpoint.provider_kind)
        recommended = self._RECOMMENDED.get(provider, {})
        changed = []
        healthy = []

        for tier in ("simple", "advanced", "judgment"):
            current = None
            code = TenantLLMTier.resolve_code(tier, None)
            if not code:
                # resolve_code() filters on is_active, so a row that merely
                # got switched OFF is indistinguishable here from one that
                # was never created. Reactivating it FIRST matters: the only
                # thing wrong may be that boolean, and rebuilding the tier
                # from the recommended table would then overwrite an
                # administrator's model choice as collateral of fixing a flag.
                # That is exactly how a hand-configured judgement tier came
                # back on a model the API does not serve.
                row = TenantLLMTier.objects.filter(
                    tenant_id=None, tier=tier, is_active=False).first()
                if row:
                    row.is_active = True
                    row.save(update_fields=["is_active", "updated_at"])
                    self._record(f"tier row [{tier}]", "is_active",
                                 False, True)
                    code = TenantLLMTier.resolve_code(tier, None)
            if code:
                current = LLMConfigProfile.objects.filter(
                    code=code, is_active=True).first()
                if current is None:
                    # The row points at a profile that exists but is switched
                    # off — again, reactivate rather than rebuild.
                    off = LLMConfigProfile.objects.filter(code=code).first()
                    if off is not None:
                        off.is_active = True
                        off.save(update_fields=["is_active", "updated_at"])
                        self._record(f"profile [{off.code}]", "is_active",
                                     False, True)
                        current = off
            # "Leave it alone" must mean "it works", not merely "it is set".
            # A profile on the right provider can still be unusable: a retired
            # model name, or web search switched off on the tier whose entire
            # purpose is web search. Those are broken configuration, not an
            # administrator's preference, so they are repaired.
            reachable = current is not None and (
                current.endpoint_id in usable_codes
                or current.provider in usable_kinds)
            model_known = reachable and LLMModelCatalog.objects.filter(
                provider=current.provider,
                model_string=current.model_string).exists()
            searchable = (tier != "advanced") or (reachable
                                                  and current.web_search)
            if reachable and model_known and searchable:
                self._repair_capabilities(current, tier)
                healthy.append(f"{tier}={current.model_string}")
                continue
            if reachable:
                reason = ("its model is not in the catalog" if not model_known
                          else "web search is off on the advanced tier")
                self.stdout.write(
                    f"  llm provider selection: repairing {current.code} "
                    f"— {reason}")

            self._repair_tier(tier, endpoint, provider, recommended,
                              usable_codes, changed)

        if changed:
            self.stdout.write(
                f"  llm provider selection: {provider} via {endpoint.code} "
                f"({', '.join(changed)})")
        if healthy:
            self.stdout.write(
                f"  llm provider selection: left as-is ({', '.join(healthy)})")

    def _pin_models(self):
        """Force every Gemini profile onto the pinned model. ``--pin-models``.

        `_llm_provider_selection` only rebuilds a tier that CANNOT serve a
        call, which is the right default: an administrator who chose a working
        model should not have it changed by a routine that runs on every
        deploy. The consequence is that changing the pin in `_RECOMMENDED`
        alters what a FRESH install gets and nothing about an existing one —
        so an install seeded on an earlier default keeps it indefinitely, and
        the two profile-pipeline rows (which pin their model directly) drift
        away from the tiers.

        That split is the failure this pins shut: two model sets, both
        working, resolving differently depending on whether a call goes
        through tier resolution or through a pipeline profile — so the bill
        and the behaviour differ and nothing in the run record says why.

        Opt-in, because it overwrites a deliberate administrator choice. Every
        row it changes is reported through the same "EXISTING CONFIGURATION
        CHANGED BY THIS RUN" block as any other overwrite.
        """
        from fundos.llm.models import (LLMConfigProfile, LLMModelCatalog,
                                       TenantLLMTier)

        wanted = dict(self._RECOMMENDED.get("gemini", {}))
        targets = {}

        # The profiles the three global tiers actually resolve to, plus the
        # vendor alternates, which are what a tenant switched to Gemini lands
        # on. An alternate seeded on a different model from the tier it
        # replaces is not an alternate; it is a second configuration nobody
        # compared.
        for tier in ("simple", "advanced", "judgment"):
            model = wanted.get(tier)
            if not model:
                continue
            for code in filter(None, (TenantLLMTier.resolve_code(tier, None),
                                      f"tier.{tier}.gemini")):
                targets.setdefault(code, model)

        for code in self.PIPELINE_PROFILE_CODES:
            targets.setdefault(code, self.PIPELINE_MODEL)

        pinned, missing = [], []
        for code, model in sorted(targets.items()):
            profile = LLMConfigProfile.objects.filter(code=code).first()
            if profile is None:
                continue
            if profile.provider != "gemini":
                # A tier resolving to another vendor is a provider decision,
                # not a model one. Repointing it here would silently move
                # traffic between vendors, which is never what a model pin is.
                continue
            if not LLMModelCatalog.objects.filter(
                    provider="gemini", model_string=model,
                    is_active=True).exists():
                missing.append(model)
                continue
            if profile.model_string == model:
                continue
            self._record(f"profile [{code}]", "model_string",
                         profile.model_string, model)
            profile.model_string = model
            profile.save(update_fields=["model_string", "updated_at"])
            pinned.append(code)

        if missing:
            # Refuse rather than guess. Writing a model string the catalog
            # does not carry produces a 404 at call time that reads like a
            # broken integration.
            self.stdout.write(self.style.WARNING(
                "  model pin: NOT applied for "
                f"{', '.join(sorted(set(missing)))} — not an active row in "
                "the model catalog. Add it in Admin -> LLM model catalog, "
                "or correct the pin in _RECOMMENDED / PIPELINE_MODEL."))
        self.stdout.write(
            f"  model pin: {len(pinned)} profile(s) repointed"
            + (f" ({', '.join(pinned)})" if pinned else
               " — every Gemini profile already on the pinned model"))

        self._pin_endpoint_default(self.PIPELINE_MODEL)

    def _pin_endpoint_default(self, model):
        """Point the Gemini endpoint's own default at the pinned model too.

        `default_model` is the fallback for a call that pins no profile, so
        leaving it on a different name means "one model across the system" is
        true of the profiles and false of the endpoint — which is the same
        split this command exists to close, one level down. It is also seeded
        through `get_or_create(defaults=...)`, so it is written once at
        creation and never corrected afterwards: an install that was first
        seeded on an older name keeps it forever, and the only visible symptom
        is a preflight line naming a model no call in the run will use.
        """
        from fundos.llm.models import LLMEndpoint, LLMModelCatalog

        if not LLMModelCatalog.objects.filter(
                provider="gemini", model_string=model, is_active=True).exists():
            return
        for endpoint in LLMEndpoint.objects.filter(provider_kind="gemini"):
            if endpoint.default_model == model:
                continue
            self._record(f"endpoint [{endpoint.code}]", "default_model",
                         endpoint.default_model, model)
            endpoint.default_model = model
            endpoint.save(update_fields=["default_model", "updated_at"])
            self.stdout.write(
                f"  model pin: endpoint {endpoint.code} default -> {model}")

    def _repair_tier(self, tier, endpoint, provider, recommended,
                     usable_codes, changed):
        """Point one tier at a profile that can actually serve a call."""
        from fundos.llm.models import (LLMConfigProfile, LLMModelCatalog,
                                       TenantLLMTier)

        model = recommended.get(tier, "")
        # Prefer a catalog model that is still active.
        if model and not LLMModelCatalog.objects.filter(
                provider=provider, model_string=model,
                is_active=True).exists():
            fallback = (LLMModelCatalog.objects
                        .filter(provider=provider, is_active=True)
                        .values_list("model_string", flat=True).first())
            model = fallback or model

        searching = (tier == "advanced")
        defaults = dict(
            display_name=f"{tier.title()} tier — auto ({provider})",
            tier=tier, provider=provider, model_string=model,
            endpoint_id=endpoint.code,
            web_search=searching,
            enable_prompt_cache=True, is_active=True)
        defaults.update(self._tier_defaults(tier, provider))
        profile, created = self._get_or_create_unique(
            LLMConfigProfile, defaults=defaults,
            code=f"tier.{tier}.{provider}")

        # Converge an existing row onto this endpoint/model when it is
        # unusable, but never touch a working one.
        fields = []
        if profile.endpoint_id not in usable_codes:
            self._record(f"profile [{profile.code}]", "endpoint",
                         profile.endpoint_id, endpoint.code)
            profile.endpoint_id = endpoint.code
            fields.append("endpoint_id")
        # An administrator's model string is NEVER corrected to the
        # recommended one. It is replaced only when it is blank, or when it
        # names something the catalog has never heard of — in which case the
        # call would fail outright, so leaving it is not a kindness.
        if not profile.model_string:
            profile.model_string = model
            fields.append("model_string")
        elif not LLMModelCatalog.objects.filter(
                provider=profile.provider,
                model_string=profile.model_string).exists():
            self._record(f"profile [{profile.code}]", "model_string",
                         profile.model_string, model,
                         why="not present in the model catalog")
            profile.model_string = model
            fields.append("model_string")
        if searching and not profile.web_search:
            profile.web_search = True
            fields.append("web_search")
        if not profile.is_active:
            profile.is_active = True
            fields.append("is_active")
        if fields:
            profile.save(update_fields=list(dict.fromkeys(fields))
                         + ["updated_at"])
        self._repair_capabilities(profile, tier, created=created)

        # Deactivate other PROVIDERS' profiles for this tier slot so exactly
        # one is selectable, then repoint the global default.
        #
        # Scoped to the `tier.<tier>.*` naming this function itself uses —
        # NOT to every row that happens to carry this `tier` value. A
        # role-pinned profile such as `profile.research` also sets
        # `tier="advanced"`, purely to pick up that tier's capability
        # defaults (`_repair_capabilities`); it plays no part in "which
        # provider serves the advanced tier" and was never meant to be a
        # candidate here.
        #
        # Unscoped, this line deactivated `profile.research` and
        # `profile.synthesis` the moment `_repair_tier` next ran for their
        # tier — silently, since callers pinning them by code
        # (`llm_generate(..., config_profile="profile.research")`) treat a
        # missing/inactive profile as "fall back to tier resolution" rather
        # than as an error. A live run then dispatched on whatever model
        # `tier.advanced.gemini` happened to name — not the model the
        # pipeline's own settings module says it pins — with nothing in the
        # trace to say the pin had been dropped.
        LLMConfigProfile.objects.filter(
            tier=tier, is_active=True, code__startswith=f"tier.{tier}."
        ).exclude(code=profile.code).update(is_active=False)
        row, _ = self._get_or_create_unique(
            TenantLLMTier,
            defaults=dict(config_profile_id=profile.code, is_active=True),
            tenant_id=None, tier=tier)
        row_fields = []
        if row.config_profile_id != profile.code:
            row.config_profile_id = profile.code
            row_fields.append("config_profile")
        if not row.is_active:
            # resolve_code() filters on is_active, so an inactive global row
            # resolves to None and the tier fails CRITICAL — while the row
            # itself still exists and looks configured in the admin list.
            row.is_active = True
            row_fields.append("is_active")
        if row_fields:
            row.save(update_fields=row_fields + ["updated_at"])
        changed.append(f"{tier}={profile.model_string}")

    def _repair_capabilities(self, profile, tier, created=False):
        """Fill in the settings a tier needs in order to work at all.

        Every one of these was a WARN or an empty result on a live install,
        and none is something an administrator can reasonably be expected to
        know — they are properties of the provider's API, not preferences.
        Only UNSET values are filled, so a deliberate choice survives.

        The thinking budget is applied ONLY when this profile has just been
        created. "none" is a legitimate choice on an existing profile — and
        on Gemini it is often the RIGHT one — so a seed run must not switch
        reasoning back on behind an administrator who turned it off.
        """
        fields = []
        for key, value in self._tier_defaults(tier, profile.provider).items():
            if value and not getattr(profile, key, None):
                setattr(profile, key, value)
                fields.append(key)
        if tier == "judgment" and profile.thinking_mode == "none" and \
                created:
            # Adjudicating conflicting figures is the one place reasoning
            # earns its cost, and it is why this tier exists. "none" here is
            # the field's default rather than a choice — the vendor alternate
            # profiles were seeded without ever naming a mode — and since
            # absence now means OFF on every provider, a judgement tier left
            # at the default does no reasoning at all.
            if profile.provider == "openai":
                profile.thinking_mode, profile.thinking_budget = \
                    "effort", "medium"
            else:
                profile.thinking_mode, profile.thinking_budget = \
                    "tokens", "4000"
            fields += ["thinking_mode", "thinking_budget"]
        if tier == "advanced" and profile.provider == "gemini" and \
                profile.structured_output and created:
            # v29 — CORRECTED CLAIM, AND NARROWED TO CREATION.
            #
            # v28's comment here said Gemini "rejects" the combination and
            # that the call "fails outright". It does not. The 19 Aug grid
            # measured the call SUCCEEDING and simply not searching (0 of 4
            # configurations searched with a schema, 4 of 4 without) — the
            # opposite failure mode, and the reason it went unnoticed for six
            # releases. tier_contract.py had it right; this file contradicted
            # it in the same release, which is the exact pattern v28 set out
            # to end.
            #
            # Narrowed to `created` for the same reason as the thinking-mode
            # block above: on an existing row this is an administrator's
            # setting, not a default, and the runtime contract already drops
            # the schema at call time either way.
            profile.structured_output = False
            fields.append("structured_output")
        if fields:
            profile.save(update_fields=list(dict.fromkeys(fields))
                         + ["updated_at"])
            self.stdout.write(
                f"  llm tier capabilities: {profile.code} set "
                f"{', '.join(dict.fromkeys(fields))}")

    # Capability defaults that a tier needs in order to WORK, as opposed to
    # ones an administrator might reasonably prefer either way.
    @staticmethod
    def _tier_defaults(tier, provider):
        out = {}
        if tier == "advanced":
            # Unbounded search is a cost and latency risk, and on providers
            # that enforce the cap only by stating it in the prompt, leaving
            # it unset means nothing is stated at all.
            out["max_uses"] = 6
            out["user_location"] = {"country": "IN"}
        return out

    def _llm_role_bindings(self):
        """Ensure every role has a binding.

        A role with no binding does not fall back to a default — llm_generate
        raises LLMUnavailable and that part of the profile is never generated.
        Binding all roles by hand was the single most frequently missed step
        in setup, and its symptom (an empty profile) points nowhere near its
        cause. Existing bindings are never modified.
        """
        from fundos.llm.models import LLM_ROLES, LLMRoleBinding

        usable = self._usable_endpoints()
        if not usable:
            self.stdout.write("  llm role bindings: skipped (no endpoint has "
                              "a usable API key)")
            return
        endpoint = usable[0]

        # Some roles cannot run on any provider. `document_read` sends the
        # FILE, and only a provider that accepts documents can take it — the
        # adapter refuses the attachment elsewhere rather than answering from
        # the prompt alone. Binding it to the first usable endpoint would
        # therefore produce a role that looks configured and fails on every
        # call, which is the exact failure this whole method exists to stop.
        required_kind = {"document_read": "gemini"}

        def endpoint_for(role):
            kind = required_kind.get(role)
            if kind is None:
                return endpoint
            match = next((ep for ep in usable if ep.provider_kind == kind),
                         None)
            if match is None:
                self.stdout.write(self.style.WARNING(
                    f"  llm role bindings: {role} needs a {kind} endpoint "
                    f"with a key and none is configured — uploaded PDFs and "
                    f"decks will fall back to native text extraction."))
            return match

        created = 0
        repaired = 0
        for role in LLM_ROLES:
            binding = LLMRoleBinding.objects.filter(role=role).first()
            if binding is None:
                target = endpoint_for(role)
                if target is None:
                    continue
                LLMRoleBinding.objects.create(
                    role=role, primary_endpoint=target, is_mocked=False)
                created += 1
                continue
            # Repair only what is unusable: a binding pointing at an endpoint
            # that cannot serve a call is worse than none, because it looks
            # configured.
            wrong_kind = (role in required_kind
                          and binding.primary_endpoint is not None
                          and binding.primary_endpoint.provider_kind
                          != required_kind[role])
            if (binding.primary_endpoint is None
                    or not binding.primary_endpoint.is_active
                    or not binding.primary_endpoint.api_key
                    or wrong_kind):
                target = endpoint_for(role)
                if target is None:
                    continue
                binding.primary_endpoint = target
                binding.save(update_fields=["primary_endpoint"])
                repaired += 1

        total = LLMRoleBinding.objects.count()
        self.stdout.write(
            f"  llm role bindings: {created} created, {repaired} repointed, "
            f"{total} total (endpoint {endpoint.code})")

    def _llm_model_catalog(self):
        """Seed the selectable model list per provider (§4.4).

        Idempotent and ADDITIVE — nothing is ever removed, so a config
        profile pointing at an older model keeps working. Retire a model by
        unticking `is_active` in admin; it disappears from selection without
        breaking history.

        NOTE ON MODEL STRINGS: LLMConfigProfile.model_string is free text and
        is NOT validated against this catalog. The catalog is a convenience
        list for admins, not a constraint — a model released tomorrow can be
        typed in directly. Always confirm the exact API string against the
        provider's own docs before going live; a wrong string fails at call
        time with a 400, not at save time.

        The `notes` column carries retirement dates and pricing hints so an
        admin choosing a model can see the trade-off without leaving the page.
        """
        from fundos.llm.models import LLMModelCatalog

        # (provider, model_string, display_name, notes)
        # Rates quoted are USD per million tokens (input/output), current as
        # of August 2026. VERIFY against provider pricing pages before relying
        # on them for budgeting — vendors reprice frequently.
        starter = [
            # ---------------- Anthropic ----------------
            ("anthropic", "claude-opus-5", "Claude Opus 5",
             "$5/$25. Frontier tier. Use for the Advanced dossier only where "
             "quality testing justifies it over Sonnet."),
            ("anthropic", "claude-sonnet-5", "Claude Sonnet 5",
             "$2/$10 introductory through 31-Aug-2026, then $3/$15. "
             "RECOMMENDED default for the Advanced tier."),
            ("anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5",
             "$1/$5. RECOMMENDED default for the Simple tier (extraction)."),
            ("anthropic", "claude-fable-5", "Claude Fable 5",
             "$10/$50. Most capable, 2x Opus 5. Rarely justified for "
             "extraction or dossier work."),
            ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6 (previous)",
             "Superseded by Sonnet 5 at the same standard rate. Kept for "
             "workloads pinned to its behaviour."),
            ("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5 (undated)",
             "Undated alias. Prefer the dated string for reproducibility."),
            ("anthropic", "claude-opus-4-1", "Claude Opus 4.1 (legacy)",
             "LEGACY — roughly 3x the cost of current Opus for weaker "
             "performance. Migrate off."),

            # ---------------- OpenAI ----------------
            # Verify exact API strings against platform.openai.com/docs.
            ("openai", "gpt-5.6-sol", "GPT-5.6 Sol",
             "~$5/$30. Flagship tier. Long-context meter rises to $10/$45 "
             "above the short-context threshold."),
            ("openai", "gpt-5.6-terra", "GPT-5.6 Terra",
             "~$2/$12 after the 30-Jul-2026 cut. Good Advanced-tier value."),
            ("openai", "gpt-5.6-luna", "GPT-5.6 Luna",
             "~$0.20/$1.20 after an 80% cut on 30-Jul-2026. Cheapest capable "
             "option for the Simple tier."),
            ("openai", "gpt-5.5", "GPT-5.5 (legacy)",
             "~$5/$30. No longer on the current pricing page."),
            ("openai", "gpt-5.4", "GPT-5.4",
             "~$2.50/$15."),
            ("openai", "gpt-4o", "GPT-4o (legacy)",
             "Superseded by the GPT-5.6 family."),
            ("openai", "gpt-4o-mini", "GPT-4o mini (legacy)",
             "Superseded by GPT-5.6 Luna."),

            # ---------------- Gemini ----------------
            # Verify exact API strings against ai.google.dev.
            ("gemini", "gemini-3.1-pro", "Gemini 3.1 Pro",
             "~$2/$12 up to 200K context, ~$4/$18 above. Frontier tier."),
            ("gemini", "gemini-3.6-flash", "Gemini 3.6 Flash",
             "~$1.50/$7.50. Strong Advanced-tier value; Search grounding has "
             "a free monthly allowance on the 3.x family."),
            ("gemini", "gemini-3.5-flash", "Gemini 3.5 Flash",
             "~$1.50/$9."),
            ("gemini", "gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite",
             "~$0.30/$2.50. Simple-tier candidate."),
            ("gemini", "gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite",
             "~$0.25/$1.50. Cheapest non-retiring Gemini."),
            ("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro (retiring)",
             "RETIRING 16-Oct-2026. Migrate to 3.1 Pro."),
            ("gemini", "gemini-2.5-flash", "Gemini 2.5 Flash (retiring)",
             "RETIRING 16-Oct-2026. Migrate to 3.6 Flash."),
            ("gemini", "gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite "
             "(retiring)",
             "~$0.10/$0.40 — the cheapest rate available, but RETIRING "
             "16-Oct-2026. Do not build on it."),
        ]
        created = 0
        for provider, model_string, display, notes in starter:
            _, made = LLMModelCatalog.objects.get_or_create(
                provider=provider, model_string=model_string,
                defaults={"display_name": display, "notes": notes})
            created += int(made)
        self.stdout.write(f"  llm model catalog: {created} created, "
                          f"{LLMModelCatalog.objects.count()} total")

    # -- individual seeders -------------------------------------------------
    def _brand(self):
        if not BrandConfig.objects.exists():
            BrandConfig.objects.create(
                product_name="Silk",
                browser_title="Silk — AI Dealmaking Workspace",
                tagline="The Dealmaking OS for Founders, Investors & Bankers",
                export_footer_text="Generated by Silk",
                logo_url="/brand/silk-logo.svg",
                logo_mono_url="/brand/silk-logo-white.svg",
                favicon_url="/brand/silk-logo.svg",
                export_logo_url="/brand/silk-logo.svg",
                primary_color="#1F6F4A")
            self.stdout.write("  brand: created")
        else:
            self.stdout.write("  brand: exists (left as-is)")

    def _countries(self):
        created = 0
        for iso2, iso3, name, dial, ccy, order in PINNED:
            _, made = Country.objects.get_or_create(
                iso2=iso2, defaults={"iso3": iso3, "name": name, "dial_code": dial,
                                     "home_currency": ccy, "sort_order": order})
            created += int(made)
        for iso2, iso3, name, dial, ccy in REST:
            _, made = Country.objects.get_or_create(
                iso2=iso2, defaults={"iso3": iso3, "name": name, "dial_code": dial,
                                     "home_currency": ccy, "sort_order": 100})
            created += int(made)
        self.stdout.write(f"  countries: {created} created, "
                          f"{Country.objects.count()} total")

    def _buckets(self):
        created = 0
        for no, label, amount, stage, investors, dlow, dhigh in BUCKETS:
            _, made = FundraiseBucket.objects.get_or_create(
                bucket_no=no,
                defaults={"label": label, "amount_usd": Decimal(amount),
                          "typical_stage": stage, "investor_types": investors,
                          "typical_dilution_low_pct": Decimal(dlow),
                          "typical_dilution_high_pct": Decimal(dhigh)})
            created += int(made)
        self.stdout.write(f"  buckets: {created} created, "
                          f"{FundraiseBucket.objects.count()} total")

    def _traction(self):
        created = 0
        for priority, name, conditions, bucket_no, rationale, advice in TRACTION_RULES:
            bucket = FundraiseBucket.objects.filter(bucket_no=bucket_no).first()
            if not bucket:
                continue
            _, made = TractionRule.objects.get_or_create(
                name=name,
                defaults={"priority": priority, "conditions": conditions,
                          "recommended_bucket": bucket, "rationale": rationale,
                          "advice_note": advice})
            created += int(made)
        _, made = UpliftRule.objects.get_or_create(
            name="Star founder or exceptional sector",
            defaults={"condition_key": "star_founder", "levels": 2,
                      "rationale": "Proven founder track record or a sector with "
                                   "exceptional growth potential supports a "
                                   "materially larger round."})
        created += int(made)
        self.stdout.write(f"  traction rules: {created} created")

    def _sections(self):
        # The enhancement set converts three previously-narrative sections
        # to structured forms; existing installs already have a row for
        # each (seeded before), so get_or_create alone would never flip
        # `kind`. Reconcile the structural fields (kind/llm_role) for these
        # on every run so the deploy stays a code swap + idempotent seed,
        # with no bespoke migration. Admin-tunable fields (label,
        # requirements, ordering) are left untouched once created, so a
        # steward's customisation is preserved.
        RECONCILE = {"revenue_model", "company_metrics", "financial_summary"}
        # CR-09 — existing installs already have an ACTIVE ai_company_summary
        # row that get_or_create would never touch. Deactivate it here so the
        # change lands on a plain code swap + seed, and remove it from the
        # completeness denominator so profiles are not permanently short.
        DEACTIVATE = {"ai_company_summary"}
        created = updated = 0
        for key, label, kind, order, role, req_c, req_x in SECTIONS:
            obj, made = ProfileSectionConfig.objects.get_or_create(
                section_key=key,
                defaults={"label": label, "kind": kind, "sort_order": order,
                          "llm_role": role, "required_for_creation": req_c,
                          "required_for_completeness": req_x})
            created += int(made)
            if key in DEACTIVATE:
                fields = []
                if obj.is_active:
                    obj.is_active = False
                    fields.append("is_active")
                if obj.required_for_completeness:
                    obj.required_for_completeness = False
                    fields.append("required_for_completeness")
                if fields:
                    obj.save(update_fields=fields)
                    updated += 1
                continue
            if not made and key in RECONCILE:
                changed = False
                if obj.kind != kind:
                    obj.kind = kind
                    changed = True
                if obj.llm_role != role:
                    obj.llm_role = role
                    changed = True
                if changed:
                    obj.save(update_fields=["kind", "llm_role"])
                    updated += 1
        self.stdout.write(f"  profile sections: {created} created, "
                          f"{updated} reconciled, "
                          f"{ProfileSectionConfig.objects.count()} total")

    # ------------------------------------------------------------------
    # Company Master Data Pipeline
    # ------------------------------------------------------------------
    def _section_schema(self):
        """Give every section its container kind, spec reference and fields.

        Driven from ``fundos.profile.schema.SHIPPED_SECTIONS`` rather than a
        copy here, so the seed, the generation prompt and the response
        normalizer cannot describe three different profiles.

        Reconciles on every run, but only where the admin has said nothing:
        a blank column is filled, a populated one is left alone. That is the
        difference between a one-time alignment and an invariant that
        re-applies forever and quietly reverts an operator's edit — v29 §3.2
        documents what the latter costs.

        Four sections the previous contract emitted are DEACTIVATED, not
        deleted — their stored content survives and re-activating the row in
        admin restores them, so adopting the new contract is reversible:

          key_people        merged into `founders`, which now carries an
                            `is_founder` flag. The KeyPerson TABLE and its
                            records endpoint are untouched; only the separate
                            wire section goes.
          ai_company_summary  already seeded inactive (CR-09).
          leadership_detail   superseded by `founders[].background`.
          derived_multiples   still COMPUTED (compute_profile_ratios) — it is
                            not LLM output and nothing about it is lost; it
                            simply stops being its own top-level section.

        Everything else that is not part of the generated contract is left
        exactly as it is. `readiness`, `knowledge_base` and `document_center`
        are populated by other machinery, and switching them off here would
        remove working features under cover of a schema change.
        """
        from fundos.profile.schema import SHIPPED_SECTIONS

        # Retired by the generated-profile contract. Explicit, because
        # "everything not in the new schema" would also catch the sections
        # other subsystems own.
        RETIRE = {"key_people", "ai_company_summary", "leadership_detail",
                  "derived_multiples"}

        shipped = {s["key"]: s for s in SHIPPED_SECTIONS}
        # The wire key is what generation and the API speak; the row is keyed
        # by the STORAGE key, which differs for five sections.
        by_storage = {s["storage_key"]: s for s in SHIPPED_SECTIONS}

        filled = deactivated = 0
        for row in ProfileSectionConfig.objects.all():
            if row.section_key in RETIRE:
                if row.is_active:
                    row.is_active = False
                    row.save(update_fields=["is_active"])
                    self._record(f"section [{row.section_key}]", "is_active",
                                 True, False,
                                 "retired by the generated-profile contract; "
                                 "content preserved, re-activate to restore")
                    deactivated += 1
                continue
            spec = by_storage.get(row.section_key) or shipped.get(
                row.section_key)
            if spec is None:
                continue        # owned by another subsystem — leave alone
            changes = []
            if not row.container_kind:
                row.container_kind = spec["kind"]
                changes.append("container_kind")
            if not row.spec_ref:
                row.spec_ref = spec["ref"]
                changes.append("spec_ref")
            if not row.field_spec:
                row.field_spec = spec["fields"]
                changes.append("field_spec")
            if not row.storage_key:
                row.storage_key = spec["storage_key"]
                changes.append("storage_key")
            if not row.is_active:
                # A section the contract needs must be on. This is the one
                # value reconciled upward, because an inactive required
                # section silently removes a block from every profile.
                row.is_active = True
                changes.append("is_active")
            if changes:
                row.save(update_fields=changes)
                filled += 1
        self.stdout.write(f"  section schema: {filled} completed, "
                          f"{deactivated} deactivated")

    # The one model the company-profile pipeline runs on. One string, in one
    # place, seeded into two admin rows — after which changing it is an admin
    # edit, not a deploy. gemini-2.5-flash because it carries the 1M-token
    # input window the dossier needs and is available on unbilled keys, so a
    # fresh install can actually run the pipeline.
    PIPELINE_MODEL = "gemini-2.5-flash"

    def _profile_pipeline_profiles(self):
        """Seed the two LLM config profiles the profile pipeline pins.

        Pinning an explicit profile is what keeps this pipeline simple: it
        short-circuits tier resolution entirely, so there is no tier policy, no
        per-tenant tier row and no judgement stage to reason about — just two
        rows an administrator can see and edit.

        The two differ in exactly one respect that matters, and it is the whole
        point of the architecture:

          profile.research   web search ON   — it must reach the open web
          profile.synthesis  web search OFF  — it reconciles a dossier it is
                                               handed, and must not extend it

        Created once, then left alone. An administrator who repoints these at
        another model, or turns search off to run a cheap pass, keeps that
        choice through every subsequent deploy.
        """
        from fundos.llm.models import LLMConfigProfile
        from fundos.platformcfg.models import PlatformFlag

        endpoint = self._preferred_endpoint_for_kind("gemini")
        if endpoint is None:
            self.stdout.write(self.style.WARNING(
                "  profile pipeline: no Gemini endpoint is configured, so the "
                "two pipeline config profiles were not created. Add one under "
                "LLM -> Endpoints and re-run; until then company-profile "
                "generation will fall back to the tier for its roles."))
            return

        specs = [
            ("profile.research", "Company profile — web research (markdown "
             "out, 10 calls/run)", "advanced", True,
             self._research_search_cap()),
            ("profile.synthesis", "Company profile — dossier synthesis (JSON "
             "out, 1 call/run)", "judgment", False, None),
        ]
        created = 0
        for code, name, tier, searching, max_uses in specs:
            profile, made = self._get_or_create_unique(
                LLMConfigProfile,
                defaults=dict(
                    display_name=name, tier=tier, provider="gemini",
                    model_string=self.PIPELINE_MODEL,
                    endpoint_id=endpoint.code,
                    web_search=searching,
                    # Structured output off on BOTH. On research it would
                    # force a JSON mime type and destroy the markdown. On
                    # synthesis the response shape is three levels deep and is
                    # enforced by the normalizer, which coerces near-misses
                    # instead of rejecting them — a better guarantee than a
                    # flat responseSchema, not a weaker one.
                    structured_output=False,
                    max_uses=max_uses,
                    enable_prompt_cache=True, is_active=True,
                    system_instructions=""),
                code=code)
            created += int(made)
            if made:
                continue
            # Only ever repair what would break the call outright.
            fields = []
            if not profile.model_string:
                profile.model_string = self.PIPELINE_MODEL
                fields.append("model_string")
            if not profile.endpoint_id:
                profile.endpoint_id = endpoint.code
                fields.append("endpoint_id")
            if not profile.is_active:
                # A repair, not an override of admin intent. This row has no
                # UI path to being deliberately switched off — the setting an
                # administrator would actually reach for is
                # research_config_profile()/synthesis_config_profile()
                # pointing somewhere else, or web_search on this same row.
                # `is_active=False` here was, until now, only ever the result
                # of a bug in `_repair_tier`'s deactivation query: it matched
                # on `tier`, a field these two rows also carry (purely to pick
                # up that tier's capability defaults), and scooped them up
                # every time the matching tier's provider was next repointed.
                # An install seeded before that fix carries the damage
                # forward as ordinary data, so it is repaired here rather than
                # left for `_repair_tier` to have a chance to reintroduce.
                profile.is_active = True
                fields.append("is_active")
            if max_uses and self._cap_is_unset(profile):
                profile.max_uses = max_uses
                fields.append("max_uses")
            if fields:
                profile.save(update_fields=fields + ["updated_at"])
        PlatformFlag.set(self.CAP_SIZED_FLAG)
        self.stdout.write(
            f"  profile pipeline: {created} config profile(s) created on "
            f"{self.PIPELINE_MODEL}")

    # Codes this command owns on behalf of the profile pipeline. Every
    # cross-cutting repair in this file must exclude them: they are selected
    # by capability (`web_search=True`, a tier name) alongside rows written
    # for a different shape of call, and a default that suits those rows has
    # twice now been the wrong answer for these.
    PIPELINE_PROFILE_CODES = ("profile.research", "profile.synthesis")

    # Stamped once the research cap has been sized. After that the number is
    # the administrator's, and no seed touches it again.
    CAP_SIZED_FLAG = "pipeline.research_cap_sized"

    def _cap_is_unset(self, profile):
        """True while nobody has deliberately chosen this profile's cap.

        NULL is plainly unset. The deployment default counts as unset too, but
        ONLY until the flag below is stamped — because until this release the
        blanket repair in `_llm_tiers` wrote exactly that value into this row
        on every seed, so a six here is that repair's fingerprint rather than
        anyone's decision. Past that one correction the value is honoured
        however ordinary it looks, so an administrator who genuinely wants six
        can set six and keep it.
        """
        from fundos.llm.models import DEFAULT_SEARCH_MAX_USES
        from fundos.platformcfg.models import PlatformFlag

        if profile.max_uses is None:
            return True
        return (profile.max_uses == DEFAULT_SEARCH_MAX_USES
                and not PlatformFlag.is_set(self.CAP_SIZED_FLAG))

    # Searches a correct research call is expected to make, per question.
    #
    # Measured, not guessed: the first live run against Gemini spent 11, 14
    # and 35 searches on three ten-question batches. Four per question puts
    # the cap above the worst of those with headroom, which is what a cap on
    # an ADVISORY provider has to be — Gemini does not enforce `max_uses`
    # (see `SEARCH_ENFORCEMENT` in the adapter), so the number is not a limit
    # the provider applies but the threshold at which we call the spend
    # anomalous. Set it below what correct behaviour costs and it stops
    # describing anomalies at all.
    SEARCHES_PER_QUESTION = 4

    def _research_search_cap(self):
        """`max_uses` for the research profile, sized to the batch.

        The deployment default is six, which is right for the role it was
        chosen for: one call, one question. A research batch asks TEN, so six
        is not a cap this role can respect on any run — it overran on all
        three calls of the first live run, tripped the breaker's third strike,
        and the remaining seven batches failed instantly against an open
        breaker. The run published six sections of seventeen and reported
        itself successful.

        Nothing there was misbehaving: the breaker did what it exists to do,
        having been told a correct call was a runaway. The cap was simply
        never resized when a role that batches its questions was added.
        """
        from fundos.profile.pipeline.questions import SHIPPED_BATCHES

        per_batch = max((len(questions) for _, _, _, questions
                         in SHIPPED_BATCHES), default=10)
        return per_batch * self.SEARCHES_PER_QUESTION

    def _research_bank(self):
        """Seed the 100 research questions as ten editable batches.

        `get_or_create` on the batch code, so an administrator's edits — a
        reworded question, a deactivated batch, an added question of their own
        — survive every subsequent deploy. Questions are only created for a
        batch this command just created, for the same reason: re-adding the
        shipped ten to a batch someone has curated would undo their work on
        every seed.
        """
        from fundos.platformcfg.models import (ResearchQuestion,
                                               ResearchQuestionBatch)
        from fundos.profile.pipeline.questions import SHIPPED_BATCHES

        batches = questions = 0
        for order, (code, topic, covers, texts) in enumerate(SHIPPED_BATCHES,
                                                             start=1):
            batch, made = ResearchQuestionBatch.objects.get_or_create(
                code=code,
                defaults={"topic": topic, "covers": covers,
                          "sort_order": order * 10})
            batches += int(made)
            if not made:
                continue
            ResearchQuestion.objects.bulk_create([
                ResearchQuestion(batch=batch, text=text,
                                 sort_order=(i + 1) * 10)
                for i, text in enumerate(texts)])
            questions += len(texts)
        self.stdout.write(
            f"  research bank: {batches} batches created, {questions} "
            f"questions created, "
            f"{ResearchQuestionBatch.objects.filter(is_active=True).count()} "
            f"batches active")

    def _stages(self):
        created = 0
        for no, key, label, purpose, prereqs, implemented in STAGES:
            _, made = StageDefinition.objects.get_or_create(
                stage_no=no,
                defaults={"stage_key": key, "label": label, "purpose": purpose,
                          "prerequisites": prereqs,
                          "is_implemented": implemented})
            created += int(made)
        self.stdout.write(f"  stages: {created} created, "
                          f"{StageDefinition.objects.count()} total")

    def _prompts(self, reset=False):
        created = updated = 0
        for role, spec in DEFAULT_PROMPTS.items():
            row = PromptTemplate.objects.filter(role=role, is_active=True).first()
            if row is None:
                from fundos.llm.default_prompts import get_tier
                PromptTemplate.objects.create(
                    role=role, system_prompt=spec["system"],
                    user_prompt=spec.get("user", ""),
                    context_keys=spec.get("context_keys", []),
                    notes=spec.get("notes", ""),
                    tier=spec.get("tier", "") or get_tier(role),
                    purpose=spec.get("notes", ""))
                created += 1
            elif reset:
                from fundos.llm.default_prompts import get_tier
                row.system_prompt = spec["system"]
                row.user_prompt = spec.get("user", "")
                row.context_keys = spec.get("context_keys", [])
                row.notes = spec.get("notes", "")
                if not row.tier:
                    row.tier = spec.get("tier", "") or get_tier(role)
                if not row.purpose:
                    row.purpose = spec.get("notes", "")
                row.version_no += 1
                row.save()
                updated += 1
        self.stdout.write(f"  prompts: {created} created, {updated} reset, "
                          f"{PromptTemplate.objects.count()} total")

    def _copy(self):
        created = 0
        for key, text, description in UI_COPY:
            _, made = UiCopy.objects.get_or_create(
                key=key, defaults={"text": text, "description": description})
            created += int(made)
        self.stdout.write(f"  ui copy: {created} created")

    def _llm_tiers(self):
        """Seed provider endpoints, the Simple + Advanced tier profiles, and
        the GLOBAL default tenant tiers. Idempotent.

        PROVIDER-NEUTRAL BY DESIGN. A tenant's Simple/Advanced tier points at
        an LLMConfigProfile, and a profile can be Anthropic, Gemini or OpenAI.
        Nothing in the tiering layer is Claude-specific — switching a tenant
        to Gemini is one dropdown in admin, not a code change.

        So the profile CODES are named for their ROLE (`tier.simple`,
        `tier.advanced`), not their vendor. Vendor-named alternates are seeded
        INACTIVE alongside them so an admin can compare providers by flipping
        `is_active` and repointing the tenant tier row.

        API keys stay in the environment; only the env var NAME is stored. A
        missing key still creates the rows — the adapter fails loudly at call
        time rather than hiding a misconfiguration here.
        """
        from fundos.llm.models import (DEFAULT_SEARCH_MAX_USES,
                                       LLMConfigProfile, LLMEndpoint,
                                       TenantLLMTier)

        # --- Endpoints: one per provider the deployment might use. Only the
        #     one whose API key is present will actually serve traffic. ---
        # canonical code -> the endpoint code that actually serves that
        # provider, so the tier profiles below bind to the row IN USE rather
        # than to a name this seeder happens to prefer. Keyed by the
        # canonical code (not provider_kind) because that is what the profile
        # definitions reference, and a lookup that misses would leave a
        # dangling foreign key.
        endpoint_codes = {}
        for code, name, kind, model, env, active in (
            ("ANTHROPIC", "Anthropic (Claude)", "anthropic",
             "claude-sonnet-4-6", "ANTHROPIC_API_KEY", True),
            # The endpoint default is the model a call uses when nothing else
            # names one, so it is pinned with the tiers rather than left on a
            # newer name — an unnamed model silently differing from every named
            # one is exactly the drift this pinning exists to stop.
            ("GEMINI", "Google (Gemini)", "gemini",
             "gemini-2.5-flash", "GEMINI_API_KEY", False),
            ("OPENAI", "OpenAI", "openai_chat",
             "gpt-5.6-sol", "OPENAI_API_KEY", False),
        ):
            # v28 — do not manufacture a duplicate.
            #
            # This used to create the canonical row unconditionally, so a
            # deployment whose administrator had already made their own
            # endpoint for the same provider ("GEMINI LLM") ended up with two
            # rows reading the same API key. The dedupe logic in
            # _usable_endpoints then correctly ignored one and printed
            # "ignoring duplicate endpoint(s) GEMINI" on every seed run — the
            # seeder reporting its own output as a problem, on every install,
            # for something no administrator did.
            #
            # An existing row for the provider is now adopted instead. The
            # vendor alternate profiles below reference an endpoint by code,
            # so they are repointed at whichever row actually serves the
            # provider; `endpoint_for` resolves that.
            #
            # v29 — ORDERING ALIGNED WITH `_usable_endpoints`.
            #
            # v28 ordered by ("-is_active", "priority", "code"), which does
            # not consider which row the system is already USING. On the
            # install this was written for -- 'GEMINI' and 'GEMINI LLM' both
            # active at priority 10 -- "GEMINI" sorts first on `code`, so
            # `existing.code == code`, adoption was skipped, the duplicate
            # survived, and `endpoint_codes["GEMINI"]` stayed "GEMINI". The
            # tier profiles were then pointed at the row nothing uses while
            # `_usable_endpoints` (which DOES weigh `in_use`) sent traffic to
            # 'GEMINI LLM'. Two dedupe rules in one file, disagreeing.
            #
            # There is now one rule, and it is `_usable_endpoints`': the row
            # the role bindings already point at wins, then active, then
            # priority, then code.
            existing = self._preferred_endpoint_for_kind(kind)
            if existing is not None and existing.code != code:
                self.stdout.write(
                    f"  llm endpoint: {kind} already served by "
                    f"'{existing.code}' — not creating '{code}'")
                endpoint_codes[code] = existing.code
                continue
            endpoint_codes[code] = code
            LLMEndpoint.objects.get_or_create(
                code=code,
                defaults=dict(display_name=name, provider_kind=kind,
                              default_model=model, api_key_env_var=env,
                              timeout_seconds=120, is_active=active,
                              priority=10))

        # --- Tier profiles. Codes are ROLE-named, not vendor-named. ---
        # SIMPLE: extraction and grounded Q&A. No tools, no thinking,
        # structured output ON so the JSON-repair second call cannot fire.
        LLMConfigProfile.objects.get_or_create(
            code="tier.simple",
            defaults=dict(display_name="Simple tier (extraction, no tools)",
                          tier="simple", provider="anthropic",
                          model_string="claude-haiku-4-5",
                          endpoint_id=endpoint_codes.get("ANTHROPIC", "ANTHROPIC"),
                          web_search=False,
                          structured_output=True, thinking_mode="none",
                          enable_prompt_cache=True, is_active=True))

        # ADVANCED: the one-shot dossier. Web search bounded by max_uses,
        # because each search round trip re-bills the accumulated context.
        LLMConfigProfile.objects.get_or_create(
            code="tier.advanced",
            defaults=dict(display_name="Advanced tier (web search)",
                          tier="advanced", provider="anthropic",
                          model_string="claude-sonnet-4-6",
                          endpoint_id=endpoint_codes.get("ANTHROPIC", "ANTHROPIC"),
                          web_search=True,
                          max_uses=6, user_location={"country": "IN"},
                          # v28 — OFF. A responseSchema suppresses
                          # google_search on Gemini (measured 19 Aug 2026),
                          # and a tier defined by retrieval does not trade
                          # facts for a parsing convenience.
                          structured_output=False, thinking_mode="none",
                          enable_prompt_cache=True, is_active=True))

        # --- Vendor alternates, seeded INACTIVE. Flip is_active and repoint
        #     the TenantLLMTier row to switch a tenant to another provider. ---
        # The search sub-controls are seeded HERE, not left blank. The
        # vendor alternates previously carried web_search=True and nothing
        # else, which produced an empty capability payload: the tool was
        # attached and the search directive was not. Every field the
        # Anthropic advanced tier sets, its alternates now set too — an
        # alternate that behaves differently from the profile it replaces is
        # not an alternate.
        for code, name, tier, provider, model, endpoint, search in (
            # Pinned to gemini-2.5-flash, matching _RECOMMENDED and
            # PIPELINE_MODEL. An "alternate" is a profile you switch to
            # expecting the same behaviour; one seeded on a different model
            # from the tier it replaces is not an alternate, it is a second
            # configuration nobody compared. The 2.5 retirement date is
            # tracked in one place — change all three together.
            ("tier.simple.gemini", "Simple tier — Gemini", "simple",
             "gemini", "gemini-2.5-flash", "GEMINI", False),
            ("tier.advanced.gemini", "Advanced tier — Gemini", "advanced",
             "gemini", "gemini-2.5-flash", "GEMINI", True),
            ("tier.simple.openai", "Simple tier — OpenAI", "simple",
             "openai", "gpt-4o-mini", "OPENAI", False),
            ("tier.advanced.openai", "Advanced tier — OpenAI", "advanced",
             "openai", "gpt-4o", "OPENAI", True),
        ):
            search_fields = (dict(max_uses=DEFAULT_SEARCH_MAX_USES,
                                  user_location={"country": "IN"})
                             if search else {})
            # v28 — structured_output is OFF on a searching tier.
            #
            # Measured 19 Aug 2026 on gemini-3.5-flash: a request carrying a
            # responseSchema searched in 0 of 4 configurations, the same
            # request without one in 4 of 4. The two features are mutually
            # exclusive, and this tier exists to retrieve.
            #
            # v27.5 already drops the schema at CALL time, so seeding True was
            # not producing bad output -- it was producing a WARNING on every
            # advanced call for a setting the administrator never chose and
            # could not act on. Seeding the truth means the admin screen shows
            # what the system will actually do.
            LLMConfigProfile.objects.get_or_create(
                code=code,
                defaults=dict(display_name=name, tier=tier, provider=provider,
                              model_string=model,
                              endpoint_id=endpoint_codes.get(endpoint, endpoint),
                              web_search=search,
                              structured_output=not search,
                              enable_prompt_cache=True, is_active=False,
                              **search_fields))

        # --- Repair pass for installs seeded before the fix above ---------
        # get_or_create does nothing to an existing row, so a deployment that
        # already ran the old seeder keeps the blank cap forever and keeps
        # silently not searching. Fill it in — only where it is blank, so an
        # administrator's deliberate value is never overwritten.
        # v28 — same repair for the schema/search conflict. An install seeded
        # before this fix keeps structured_output=True on its advanced
        # profiles forever, because get_or_create does nothing to an existing
        # row. The runtime drops it anyway, so this only aligns the admin
        # screen with reality -- but an admin screen that disagrees with the
        # runtime is how this codebase ended up with two halves asserting
        # opposite answers about schema and search for six releases.
        #
        # v29 — SCOPED TO GEMINI, AND ONE-TIME.
        #
        # v28 ran this across EVERY provider on EVERY seed, which broke two
        # of its own rules:
        #
        #   * `tier_contract._apply_structured_output` is explicit that the
        #     measurement is Gemini-only and that "there is no evidence to
        #     justify degrading their output shape on a Gemini finding" for
        #     Anthropic and OpenAI. The seeder degraded them anyway.
        #   * It had no "only where unset" guard, unlike the `max_uses`
        #     repair immediately below, so an administrator could never make
        #     the opposite choice stick: the next seed silently reverted it.
        #     A repair that runs forever is not a repair, it is a policy
        #     enforced in the wrong layer.
        #
        # The runtime contract already drops the schema on Gemini advanced
        # calls, so this exists ONLY to stop the admin screen showing a tick
        # the runtime ignores. That is a one-time alignment, and it is now
        # recorded as such.
        from fundos.platformcfg.models import PlatformFlag

        already_aligned = PlatformFlag.is_set("v29.schema_search_aligned")
        if already_aligned:
            schema_fixed = 0
        else:
            schema_fixed = LLMConfigProfile.objects.filter(
                web_search=True, structured_output=True,
                provider="gemini").update(structured_output=False)
            PlatformFlag.set("v29.schema_search_aligned")
        if schema_fixed:
            self.stdout.write(
                f"  llm search: turned OFF structured output on {schema_fixed} "
                f"searching GEMINI profile(s) — a responseSchema suppresses "
                f"google_search on Gemini (measured 19 Aug 2026: 0/4 searched "
                f"with a schema, 4/4 without), so the two cannot both be on. "
                f"This is a ONE-TIME alignment; a later admin change is kept")

        # Scoped away from the profile-pipeline rows. The deployment default
        # is sized for a call that asks ONE question; `profile.research` asks
        # ten per call and sets its own cap in `_profile_pipeline_profiles`.
        # Filling six in here did not merely under-set that row, it made the
        # row look deliberately configured, so the pipeline's own "repair the
        # blank" pass then correctly declined to touch it.
        repaired = LLMConfigProfile.objects.filter(
            web_search=True, max_uses__isnull=True).exclude(
                code__in=self.PIPELINE_PROFILE_CODES).update(
                max_uses=DEFAULT_SEARCH_MAX_USES)
        if repaired:
            self.stdout.write(
                f"  llm search: filled a blank search cap on {repaired} "
                f"profile(s) that had web search on — a blank cap meant the "
                f"search tool was sent with no instruction to use it")

        # --- JUDGEMENT tier profile --------------------------------------
        # A first-class tier, resolved through TenantLLMTier exactly like
        # Simple and Advanced. No call site hardcodes it.
        #
        # web_search is deliberately OFF: this tier reasons over data another
        # call already extracted, rather than going looking for new facts.
        # Thinking is ON because adjudicating conflicting figures is exactly
        # the work it helps with — and affordable here because the INPUT is
        # the extracted JSON, not the source corpus.
        LLMConfigProfile.objects.get_or_create(
            code="tier.judgment",
            defaults=dict(display_name="Judgement tier (reasoning, no tools)",
                          tier="judgment", provider="anthropic",
                          model_string="claude-opus-5",
                          endpoint_id=endpoint_codes.get("ANTHROPIC", "ANTHROPIC"),
                          web_search=False,
                          structured_output=True, thinking_mode="tokens",
                          # v29 — the ACTIVE judgement row. v28 fixed this on
                          # the two INACTIVE vendor alternates below and left
                          # the row that actually serves the tier as a quoted
                          # string, so the defect it reported as fixed was
                          # still present on the only row that runs.
                          #
                          # It never mattered: `thinking_budget` is a
                          # CharField, so 4000 and "4000" persist identically,
                          # and the trace inconsistency was already fixed at
                          # render time by v27.5's `_budget_str()`. Corrected
                          # anyway so the seed says what it means.
                          thinking_budget=4000, enable_prompt_cache=True,
                          is_active=True))

        # Vendor alternates for the judgement tier, inactive.
        for code, name, provider, model, endpoint in (
            # gemini-3.1-pro does not exist on the public v1beta API — only
            # gemini-3.1-pro-preview does — and seeding a name the endpoint
            # refuses cost every judgement call on a live install. Names here
            # are checked against the provider's own model list, not assumed.
            # Pinned to 2.5-flash with the other two tiers: one model across
            # the system, changed in one place.
            ("tier.judgment.gemini", "Judgement tier — Gemini", "gemini",
             "gemini-2.5-flash", "GEMINI"),
            ("tier.judgment.openai", "Judgement tier — OpenAI", "openai",
             "gpt-5.6-sol", "OPENAI"),
        ):
            LLMConfigProfile.objects.get_or_create(
                code=code,
                defaults=dict(display_name=name, tier="judgment",
                              provider=provider, model_string=model,
                              endpoint_id=endpoint_codes.get(endpoint,
                                                             endpoint),
                              web_search=False,
                              # Judgement keeps its schema: no search tool to
                              # suppress, and its output feeds a parser.
                              structured_output=True,
                              # Int, not str. Rendered into the trace line,
                              # where a quoted number and a bare one broke
                              # every parser reading the log (19 Aug).
                              thinking_mode="tokens", thinking_budget=4000,
                              enable_prompt_cache=True, is_active=False))

        # --- GLOBAL (tenant_id NULL) defaults. Every tenant without its own
        #     rows uses these, so a fresh tenant works immediately. ---
        # These rows are the ones the nullable-tenant_id constraint failed to
        # protect, so they are the ones most likely to be duplicated already.
        for tier_key, profile_code in (("advanced", "tier.advanced"),
                                       ("simple", "tier.simple"),
                                       ("judgment", "tier.judgment")):
            self._get_or_create_unique(
                TenantLLMTier, defaults=dict(config_profile_id=profile_code),
                tenant_id=None, tier=tier_key)
        self.stdout.write("  llm tiers: 3 endpoints + simple/advanced/judgment "
                          "profiles (+ vendor alternates) + global defaults")
