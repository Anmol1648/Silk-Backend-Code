import uuid

import django.db.models.deletion
from django.db import migrations, models

_ROLES = (
    "research_synthesis", "material_critique", "readiness_summary",
    "peer_insight", "raise_narrative", "valuation_negotiation",
    "valuation_explainer", "instrument_explainer", "strategy_blueprint",
    "investment_story", "teaser", "pitch_story", "pitch_slides",
    "fin_model_assist", "fin_review", "im_section", "im_narrative",
    "im_consistency", "package_review", "objection_sim",
    "company_profile_section", "company_profile_records",
    "company_profile_structured", "company_profile_field", "founder_profile",
    "company_profile_deep_extract", "profile_qa",
)

_TIERS = (
    ("simple", "Simple — extraction / Q&A over data already retrieved"),
    ("advanced", "Advanced — web search + complex generation"),
)


class Migration(migrations.Migration):

    dependencies = [
        ("llm", "0003_llmmodelcatalog_llmconfigprofile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="llmrolebinding",
            name="role",
            field=models.CharField(
                max_length=48, unique=True,
                choices=[(r, r) for r in _ROLES]),
        ),
        migrations.AddField(
            model_name="llmconfigprofile",
            name="tier",
            field=models.CharField(
                max_length=16, default="simple", db_index=True,
                choices=list(_TIERS),
                help_text="Simple = cheap, no tools. Advanced = web search + "
                          "complex generation."),
        ),
        migrations.CreateModel(
            name="TenantLLMTier",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("tenant_id", models.UUIDField(blank=True, db_index=True,
                                               null=True)),
                ("tier", models.CharField(max_length=16, choices=list(_TIERS))),
                ("is_active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("config_profile", models.ForeignKey(
                    db_column="config_profile_code",
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="tenant_tiers", to_field="code",
                    to="llm.llmconfigprofile")),
            ],
            options={"db_table": "tenant_llm_tier"},
        ),
        migrations.AddConstraint(
            model_name="tenantllmtier",
            constraint=models.UniqueConstraint(
                fields=["tenant_id", "tier"], name="uq_tenant_llm_tier"),
        ),
    ]
