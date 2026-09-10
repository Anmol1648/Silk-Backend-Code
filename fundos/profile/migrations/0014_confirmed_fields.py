"""
Requirements 3 & 4: Add confirmed_fields to ProfileSection.

Stores which fields within a section the founder has explicitly confirmed
(moved from AI Draft → Confirmed). Used by the readiness score engine to
distinguish AI-generated content from founder-verified content.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companyprofile", "0013_pipeline_run"),
    ]

    operations = [
        migrations.AddField(
            model_name="profilesection",
            name="confirmed_fields",
            field=models.JSONField(
                default=list, blank=True,
                help_text="Field keys the founder has explicitly confirmed."),
        ),
    ]
