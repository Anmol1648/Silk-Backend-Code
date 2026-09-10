"""Seed the investor taxonomy, raise bands and matching defaults.

Everything seeded here is a DATA ROW, not a constant — the point of the
exercise. Re-runnable: existing rows are left alone unless --force is given.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

# 14 source categories → 9 output buckets, from the source workbook.
BUCKETS = [
    ("Angel & UHNI", 1), ("Micro VC & Accelerators", 2), ("FO", 3),
    ("Domestic VC", 4), ("Global VC", 5), ("Corp VC", 6),
    ("Domestic PE", 7), ("Global PE", 8), ("Others", 9),
]

CATEGORIES = [
    ("Angel & UHNI", "Angel & UHNI",
     "Individual angel investors and ultra-high-net-worth individuals."),
    ("Micro VC", "Micro VC & Accelerators",
     "Small venture funds, typically writing sub-$1M cheques."),
    ("Accelerator", "Micro VC & Accelerators",
     "Accelerator or incubator programmes taking small equity stakes."),
    ("FO", "FO", "Family offices investing their own capital."),
    ("Domestic VC", "Domestic VC",
     "Venture capital firms headquartered in the company's home market."),
    ("Global VC", "Global VC",
     "Venture capital firms headquartered outside the home market."),
    ("Domestic Corp VC", "Corp VC",
     "Corporate venture arms of domestic companies."),
    ("Global Corp VC", "Corp VC",
     "Corporate venture arms of international companies."),
    ("Domestic PE", "Domestic PE",
     "Private equity firms headquartered in the home market."),
    ("Global PE", "Global PE",
     "Private equity firms headquartered outside the home market."),
    ("Debt Funds", "Others", "Venture debt and credit funds."),
    ("Alt Funds", "Others", "Alternative investment funds not otherwise classified."),
    ("DFI & Sovereign Funds", "Others",
     "Development finance institutions and sovereign wealth funds."),
    ("Secondaries Fund", "Others", "Funds buying existing positions."),
]

# Three rows from the source workbook. Deliberately left as-is rather than
# extrapolated: extending past a $5M raise is a business decision about which
# cheque bands apply at Series B and Growth, not something to guess at.
RAISE_BANDS = [
    ("0.25", "0.05", "0.50", "Pre-seed / small angel rounds"),
    ("1.00", "0.50", "2.00", "Seed"),
    ("5.00", "2.00", "7.50",
     "Series A. NOTE: this is the largest band configured — add rows for "
     "Series B and Growth before running larger raises."),
]

SECTOR_ALIASES = {
    "Fintech": ["fintech", "fin-tech", "financial tech", "financial technology"],
    "Deeptech": ["deeptech", "deep tech", "deep-tech"],
    "Artificial Intelligence": ["artificial intelligence", "ai", "ai/ml",
                                "machine learning", "artificial intelligence (ai)"],
    "Healthtech": ["healthtech", "health tech", "health-tech", "health technology"],
    "Edtech": ["edtech", "ed-tech", "education technology", "education tech"],
    "Agritech": ["agritech", "agri-tech", "agriculture tech", "agri tech"],
    "Cleantech": ["cleantech", "clean tech", "clean energy", "climate tech"],
    "Logistics": ["logistics", "logistic", "logistics tech"],
    "Supply Chain": ["supply chain", "supply chain management", "scm"],
    "Ecommerce": ["ecommerce", "e-commerce", "e commerce"],
    "Enterprise Services": ["enterprise services", "enterprise tech",
                            "enterprisetech", "enterprise service"],
    "B2B SaaS": ["b2b saas", "b2b software", "saas", "software as a service"],
    "D2C": ["d2c", "direct-to-consumer", "direct to consumer"],
    "Consumer Services": ["consumer services"],
    "Foodtech": ["foodtech", "food tech", "food-tech"],
    "Media & Entertainment": ["media & entertainment", "media and entertainment",
                              "media"],
    "Proptech": ["proptech", "prop tech", "property tech"],
    "Real Estate Tech": ["real estate tech", "real estate technology",
                         "real estate"],
    "Web3": ["web3", "web 3.0", "web 3"],
    "Blockchain": ["blockchain", "blockchain technology"],
    "Crypto": ["crypto", "cryptocurrency", "digital assets"],
    "Travel Tech": ["travel tech", "traveltech", "travel technology"],
    "Retail Tech": ["retail tech", "retailtech", "retail technology"],
    "Advanced Hardware & IoT": ["advanced hardware & iot", "iot",
                                "advanced hardware", "hardware tech"],
    "Consumer Tech": ["consumer tech"],
    "Lending Tech": ["lending tech", "lendingtech"],
    "Consumer Internet": ["consumer internet"],
}


class Command(BaseCommand):
    help = "Seed investor buckets, categories, raise bands, sectors and match config."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Overwrite existing rows.")

    def handle(self, *args, **options):
        from fundos.investors.models import (CanonicalSector, DataSource,
                                             InvestorBucket, InvestorCategory,
                                             MatchConfig, RaiseBand,
                                             RefreshSchedule, SectorAlias)

        force = options["force"]

        for name, order in BUCKETS:
            InvestorBucket.objects.update_or_create(
                name=name, defaults={"sort_order": order})
        self.stdout.write(f"Buckets: {InvestorBucket.objects.count()}")

        buckets = {b.name: b for b in InvestorBucket.objects.all()}
        for name, bucket, desc in CATEGORIES:
            InvestorCategory.objects.update_or_create(
                name=name,
                defaults={"bucket": buckets[bucket], "description": desc})
        self.stdout.write(f"Categories: {InvestorCategory.objects.count()}")

        for raise_size, lo, hi, note in RAISE_BANDS:
            RaiseBand.objects.update_or_create(
                raise_size_usd_mn=Decimal(raise_size),
                defaults={"min_cheque_usd_mn": Decimal(lo),
                          "max_cheque_usd_mn": Decimal(hi), "notes": note})
        self.stdout.write(f"Raise bands: {RaiseBand.objects.count()}")

        for name, aliases in SECTOR_ALIASES.items():
            sector, _ = CanonicalSector.objects.get_or_create(
                name=name, defaults={"level": "sector"})
            for alias in aliases:
                SectorAlias.objects.get_or_create(
                    alias=alias.lower(), defaults={"sector": sector})
        self.stdout.write(
            f"Sectors: {CanonicalSector.objects.count()}, "
            f"aliases: {SectorAlias.objects.count()}")

        if force or not MatchConfig.objects.exists():
            MatchConfig.objects.update_or_create(
                name="default",
                defaults={"is_active": True, "band_test_basis": "deal_size",
                          "activity_weight": Decimal("0.700"),
                          "ticket_fit_weight": Decimal("0.300"),
                          "activity_constant": 3})
        self.stdout.write("Match config seeded (band basis: deal_size).")

        if force or not DataSource.objects.exists():
            DataSource.objects.update_or_create(
                name="inc42", defaults={
                    "adapter": "inc42_funding_galore",
                    "base_url": "https://inc42.com/tag/funding-galore/",
                    "enabled": True, "articles_per_run": 10})
        if force or not RefreshSchedule.objects.exists():
            RefreshSchedule.objects.update_or_create(
                name="weekly", defaults={"enabled": True, "day_of_week": 5,
                                         "hour": 18, "minute": 0,
                                         "timezone_name": "Asia/Kolkata"})
        schedule = RefreshSchedule.objects.first()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Refresh schedule: {schedule} — next {schedule.next_run():%a %d %b %H:%M}"))
