"""The role binding's output ceiling becomes optional.

`max_output_tokens` was a non-null IntegerField defaulting to 2048. The
adapter takes `min(binding, role_cap)`, so the binding can only ever LOWER
the ceiling — and because every row carried the shipped default, "nobody
chose this" was indistinguishable from "an administrator chose 2048".

The consequence was invisible: `company_profile_deep_extract` is designed
for 8192 and ran at 2048, so the dossier came back cut off at the ceiling
and was logged as a successful call with a thin result.

The data step clears rows that still hold exactly the old default, so they
fall back to the role's designed budget. A value an administrator actually
chose is any other number, and those are left alone. If 2048 was deliberate
for some role, re-enter it after migrating.
"""
from django.db import migrations, models


def clear_shipped_default(apps, schema_editor):
    LLMRoleBinding = apps.get_model("llm", "LLMRoleBinding")
    LLMRoleBinding.objects.filter(max_output_tokens=2048).update(
        max_output_tokens=None)


def restore_shipped_default(apps, schema_editor):
    LLMRoleBinding = apps.get_model("llm", "LLMRoleBinding")
    LLMRoleBinding.objects.filter(max_output_tokens=None).update(
        max_output_tokens=2048)


class Migration(migrations.Migration):

    dependencies = [
        ("llm", "0010_role_choices_for_undeclared_roles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="llmrolebinding",
            name="max_output_tokens",
            field=models.IntegerField(
                blank=True, default=None, null=True,
                help_text="Leave EMPTY to use the role's designed output "
                          "budget (recommended). Set a number only to cap "
                          "this role BELOW that budget — the value never "
                          "raises it."),
        ),
        migrations.RunPython(clear_shipped_default, restore_shipped_default),
    ]
