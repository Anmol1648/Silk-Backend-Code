"""Cost-optimisation layer.

* Cache-aware token accounting on the call log (reads/writes bill at
  different rates and are NOT included in prompt_tokens).
* Cache read/write rates on the price book, defaulting to derived multiples
  of the input rate so an existing price book keeps working untouched.
* Per-config-profile prompt-cache toggle.
* TenantLLMBudget — a monthly spend ceiling enforced before dispatch.
"""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("llm", "0004_tiering"),
    ]

    operations = [
        migrations.AddField(
            model_name="llmcalllog",
            name="cache_read_tokens",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="llmcalllog",
            name="cache_write_tokens",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="llmmodelcost",
            name="cache_read_cost_per_1k_inr",
            field=models.DecimalField(
                decimal_places=6, default=0, max_digits=12,
                help_text="Blank/0 → derived as 0.1x the input rate."),
        ),
        migrations.AddField(
            model_name="llmmodelcost",
            name="cache_write_cost_per_1k_inr",
            field=models.DecimalField(
                decimal_places=6, default=0, max_digits=12,
                help_text="Blank/0 → derived as 1.25x the input rate "
                          "(5-minute TTL)."),
        ),
        migrations.AddField(
            model_name="llmconfigprofile",
            name="enable_prompt_cache",
            field=models.BooleanField(
                default=True,
                help_text="Cache the stable prompt prefix across calls. Leave "
                          "ON unless the model does not support prompt "
                          "caching."),
        ),
        migrations.CreateModel(
            name="TenantLLMBudget",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("tenant_id", models.UUIDField(
                    blank=True, db_index=True, null=True,
                    help_text="NULL = global default ceiling for tenants "
                              "without a row.")),
                ("monthly_cap_inr", models.DecimalField(
                    decimal_places=2, default=0, max_digits=14,
                    help_text="0 = unlimited. Otherwise calls are refused "
                              "once month-to-date spend reaches this "
                              "figure.")),
                ("alert_at_pct", models.IntegerField(
                    default=80,
                    help_text="Log a warning once month-to-date spend crosses "
                              "this percentage of the cap.")),
                ("is_active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "tenant_llm_budget"},
        ),
        migrations.AddConstraint(
            model_name="tenantllmbudget",
            constraint=models.UniqueConstraint(
                fields=["tenant_id"], name="uq_tenant_llm_budget"),
        ),
    ]
