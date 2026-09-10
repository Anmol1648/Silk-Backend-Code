"""
Requirement 1: Master lookup tables + seed data.

Creates five lookup tables (sectors, sub_sectors, funding_statuses,
revenue_sizes, currencies) and seeds them with the canonical values
from the requirements document.
"""
from django.db import migrations, models
import django.db.models.deletion


def seed_data(apps, schema_editor):
    """Populate lookup tables with the canonical seed data."""
    Sector = apps.get_model("platformcfg", "Sector")
    SubSector = apps.get_model("platformcfg", "SubSector")
    FundingStatus = apps.get_model("platformcfg", "FundingStatus")
    RevenueSize = apps.get_model("platformcfg", "RevenueSize")
    Currency = apps.get_model("platformcfg", "Currency")

    # 3.1 Sectors (30 entries)
    sectors = [
        "Ecommerce", "Fintech", "Enterprise Services", "Cleantech",
        "Healthtech", "Consumer Services", "Deeptech",
        "Artificial Intelligence", "Advanced Hardware & IoT",
        "Media & Entertainment", "Travel Tech", "Agritech", "Edtech",
        "Real Estate Tech", "Logistics", "Foodtech",
        "Alcoholic Beverages", "Web3", "Automotive & Mobility Tech",
        "Gaming & Esports", "HR Tech", "Legal Tech",
        "Aerospace & Defence", "Manufacturing & Industrial Tech",
        "InsurTech", "WealthTech", "Beauty & Personal Care",
        "SportsTech", "Renewable Energy & Power",
        "Semiconductor & Electronics",
    ]
    for name in sectors:
        Sector.objects.get_or_create(name=name, defaults={"is_active": True})

    # 3.2 Sub-sectors (60 entries)
    sub_sectors = [
        "D2C & Consumer Brands", "Horizontal SaaS", "Lending & Credit",
        "Healthtech", "Application & Dev Tools", "EV & Mobility",
        "Micro-Mobility & Shared Transport", "Climate & Clean Tech",
        "Media & Content", "Hyperlocal Services", "Fintech SaaS",
        "Transport, Aerial & Maritime", "Spacetech",
        "Web3 & Blockchain", "Investment & Wealth Tech", "Edtech",
        "B2C Ecommerce & Marketplaces", "Quick Commerce & Delivery",
        "Real Estate Tech", "Vertical SaaS",
        "Logistics & Supply Chain", "B2B Ecommerce",
        "Gaming & AR/VR", "Market Linkage", "Enterprise Services",
        "Fitness & Wellness", "Manufacturing & Defence", "Payments",
        "Travel & Hospitality", "Agritech", "Banking", "Insurtech",
        "Foodtech & QSR", "Auto Components & Aftermarket",
        "Used Vehicle & Auto Marketplaces",
        "EV Charging & Battery Infra", "Esports & Fantasy Sports",
        "Recruitment & Staffing Tech", "Workforce & Payroll Management",
        "Legal & Compliance Tech", "Defence & Security Tech",
        "Industrial Automation & Robotics",
        "Industrial B2B Marketplaces",
        "General Insurance Distribution",
        "Health & Life Insurance Tech",
        "Wealth Management Platforms", "Alternative Investments Tech",
        "Beauty & Personal Care Brands", "Sports Leagues & Content",
        "Solar & Renewable Energy", "Semiconductor Design & Fab",
        "Consumer Electronics", "Cybersecurity",
        "Data & Analytics Infrastructure", "Drones & Robotics",
        "Space Launch & Satellite Tech",
        "Farm Inputs & Agri-Marketplaces", "Dairy & Agri-Processing",
        "B2B Industrial Goods Marketplaces", "HR & People Analytics",
    ]
    for name in sub_sectors:
        SubSector.objects.get_or_create(
            name=name, defaults={"is_active": True})

    # 3.3 Funding statuses (22 entries)
    funding_statuses = [
        (1, "Bootstrapped"), (2, "Family & Friends Funded"),
        (3, "Angel Funded"), (4, "Pre-Seed Funded"),
        (5, "Seed Funded"), (6, "Series A"), (7, "Series B"),
        (8, "Series C"), (9, "Series D+"), (10, "VC Funded"),
        (11, "Venture Debt"), (12, "PE Backed"),
        (13, "Corporate/Strategic Backed"),
        (14, "Government/Grant Funded"), (15, "Crowdfunded"),
        (16, "Revenue-Based Financing"), (17, "Public"),
        (18, "Acquired"), (19, "Merged"), (20, "Dormant"),
        (21, "Shut Down"), (22, "Other"),
    ]
    for order, name in funding_statuses:
        FundingStatus.objects.get_or_create(
            name=name, defaults={"sort_order": order})

    # 3.4 Revenue sizes (10 entries)
    revenue_sizes = [
        (1, "Pre-revenue"), (2, "USD 0 to 500K"),
        (3, "USD 500K to 1M"), (4, "USD 1M to 5M"),
        (5, "USD 5M to 10M"), (6, "USD 10M to 25M"),
        (7, "USD 25M to 50M"), (8, "USD 50M to 100M"),
        (9, "USD 100M+"), (10, "Other"),
    ]
    for order, name in revenue_sizes:
        RevenueSize.objects.get_or_create(
            name=name, defaults={"sort_order": order})

    # 3.5 Currencies (8 entries)
    currencies = [
        ("USD", "$"), ("INR", "₹"), ("EUR", "€"), ("GBP", "£"),
        ("SGD", "S$"), ("AED", "AED"), ("JPY", "¥"), ("CAD", "C$"),
    ]
    for code, symbol in currencies:
        Currency.objects.get_or_create(
            code=code, defaults={"symbol": symbol})


def unseed(apps, schema_editor):
    """Reverse: clear seeded rows (tables are dropped anyway)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("platformcfg", "0005_research_bank_and_section_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="Sector",
            fields=[
                ("id", models.AutoField(auto_created=True,
                    primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"db_table": "lookup_sector", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="SubSector",
            fields=[
                ("id", models.AutoField(auto_created=True,
                    primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("sector", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="sub_sectors", to="platformcfg.sector")),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"db_table": "lookup_sub_sector", "ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="FundingStatus",
            fields=[
                ("id", models.AutoField(auto_created=True,
                    primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("sort_order", models.IntegerField(default=0)),
            ],
            options={"db_table": "lookup_funding_status",
                     "ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="RevenueSize",
            fields=[
                ("id", models.AutoField(auto_created=True,
                    primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("sort_order", models.IntegerField(default=0)),
            ],
            options={"db_table": "lookup_revenue_size",
                     "ordering": ["sort_order"]},
        ),
        migrations.CreateModel(
            name="Currency",
            fields=[
                ("id", models.AutoField(auto_created=True,
                    primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=10, unique=True)),
                ("symbol", models.CharField(blank=True, default="",
                                            max_length=5)),
            ],
            options={"db_table": "lookup_currency", "ordering": ["code"]},
        ),
        migrations.RunPython(seed_data, unseed),
    ]
