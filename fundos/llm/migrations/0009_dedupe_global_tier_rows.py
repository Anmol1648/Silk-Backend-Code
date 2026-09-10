"""De-duplicate global tier/budget rows, then constrain them properly.

The unique constraints on TenantLLMTier and TenantLLMBudget both include
`tenant_id`, which is nullable — and PostgreSQL treats NULLs as DISTINCT in a
unique index. The GLOBAL rows (tenant_id IS NULL) were therefore never
protected, so repeated seeding could create duplicates. Once duplicated,
`get_or_create` raised MultipleObjectsReturned and aborted the entire
seed_platform_config run partway through, leaving configuration half-applied.

This migration removes the duplicates (keeping the most recently updated row
of each group) and adds constraints that actually cover the NULL case.
"""
from django.db import migrations, models
import django.db.models.functions


def dedupe(apps, schema_editor):
    Tier = apps.get_model("llm", "TenantLLMTier")
    seen = {}
    for row in Tier.objects.filter(tenant_id__isnull=True).order_by("-updated_at"):
        if row.tier in seen:
            row.delete()          # older duplicate
        else:
            seen[row.tier] = row.pk

    Budget = apps.get_model("llm", "TenantLLMBudget")
    globals_ = list(Budget.objects.filter(tenant_id__isnull=True)
                    .order_by("-updated_at"))
    for row in globals_[1:]:
        row.delete()

    # Tenant-scoped rows are covered by the existing constraint, but a
    # database that predates it may still hold duplicates that would block
    # the new index. Clear those the same way.
    seen_pairs = set()
    for row in Tier.objects.filter(tenant_id__isnull=False).order_by("-updated_at"):
        key = (row.tenant_id, row.tier)
        if key in seen_pairs:
            row.delete()
        else:
            seen_pairs.add(key)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [("llm", "0008_diagnostics")]

    operations = [
        migrations.RunPython(dedupe, noop),
        migrations.AddConstraint(
            model_name="tenantllmtier",
            constraint=models.UniqueConstraint(
                condition=models.Q(("tenant_id__isnull", True)),
                fields=("tier",), name="uq_tenant_llm_tier_global"),
        ),
        migrations.AddConstraint(
            model_name="tenantllmbudget",
            constraint=models.UniqueConstraint(
                django.db.models.functions.Coalesce(
                    "tenant_id",
                    models.Value("00000000-0000-0000-0000-000000000000",
                                 output_field=models.UUIDField())),
                name="uq_tenant_llm_budget_any"),
        ),
    ]
