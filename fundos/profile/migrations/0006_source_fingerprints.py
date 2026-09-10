"""Source fingerprints for skip-if-unchanged generation.

Regenerating a profile whose sources have not moved produced a byte-identical
result at full price. These two columns let the generator compare the current
source bundle against the one that produced the existing content and skip the
work — at profile level and per section.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companyprofile", "0005_investor"),
    ]

    operations = [
        migrations.AddField(
            model_name="companyprofile",
            name="sources_hash",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=64,
                help_text="SHA-256 of the collected sources at last "
                          "generation."),
        ),
        migrations.AddField(
            model_name="profilesection",
            name="source_hash",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="SHA-256 of the sources used to generate this "
                          "section."),
        ),
    ]
