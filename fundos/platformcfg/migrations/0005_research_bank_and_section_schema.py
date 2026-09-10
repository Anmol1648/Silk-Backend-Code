"""Company Master Data Pipeline — admin-owned research bank and output schema.

Two things move out of Python and into the admin:

* the 100 research questions the pipeline asks about a company, as ten
  editable batches;
* each profile section's container kind and FIELD SPEC, so the shape of the
  generated profile is configuration rather than a constant that has to agree
  with the prompt, the normalizer and the serializer in four places at once.

Additive only. Every new column is nullable or defaulted and every reader
falls back to the shipped values when a row says nothing, so an install that
migrates but has not re-seeded keeps working exactly as before.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platformcfg", "0004_platformflag"),
    ]

    operations = [
        # --- the generated profile's output contract ----------------------
        migrations.AddField(
            model_name="profilesectionconfig",
            name="container_kind",
            field=models.CharField(
                blank=True, default="", max_length=8,
                choices=[("object", "Object — data is a single record"),
                         ("array", "Array — data is a list of records")],
                help_text="Whether this section's `data` is one object or a "
                          "list of them. Blank uses the shipped default for "
                          "this section."),
        ),
        migrations.AddField(
            model_name="profilesectionconfig",
            name="spec_ref",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="Reference shown to the model, e.g. '8.1 Company "
                          "Overview'. Orients it on what depth the section "
                          "expects."),
        ),
        migrations.AddField(
            model_name="profilesectionconfig",
            name="field_spec",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="{field name: human-readable type description}. "
                          "Rendered verbatim into the generation prompt — "
                          "write the description as an instruction to the "
                          "model, e.g. 'string - ISO-2 country code of "
                          "headquarters, e.g. IN'. Leave empty to use the "
                          "shipped spec for this section."),
        ),
        migrations.AddField(
            model_name="profilesectionconfig",
            name="storage_key",
            field=models.CharField(
                blank=True, default="", max_length=64,
                help_text="Internal key this section's data is stored under, "
                          "when it differs from the section key. Set by the "
                          "seed; changing it on a live install orphans "
                          "existing data."),
        ),

        # --- the research question bank -----------------------------------
        migrations.CreateModel(
            name="ResearchQuestionBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("code", models.CharField(
                    db_index=True, max_length=64, unique=True,
                    help_text="Stable identifier, e.g. 'funding_investors'. "
                              "Used by the seeder to avoid duplicating a "
                              "batch it already created.")),
                ("topic", models.CharField(
                    max_length=128,
                    help_text="Shown to the model and in progress narration, "
                              "e.g. 'Funding History & Investors'.")),
                ("covers", models.CharField(
                    blank=True, default="", max_length=255,
                    help_text="Which profile sections this batch feeds, e.g. "
                              "'8.10 Funding History'. Tells the model what "
                              "depth to answer at, and tells a reader of a "
                              "partial dossier which sections a failed batch "
                              "left thin.")),
                ("sort_order", models.IntegerField(default=100)),
                ("is_active", models.BooleanField(
                    default=True,
                    help_text="Deactivate to stop asking this topic. Each "
                              "batch is one billed, search-grounded call, so "
                              "switching batches off is the most direct cost "
                              "control available.")),
            ],
            options={
                "db_table": "research_question_batch",
                "ordering": ("sort_order", "code"),
                "verbose_name": "Research question batch",
                "verbose_name_plural": "Research question batches",
            },
        ),
        migrations.CreateModel(
            name="ResearchQuestion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("text", models.TextField(
                    help_text="The question. Use {company_name} and {website} "
                              "as placeholders — they are substituted per "
                              "company. Any other brace expression is sent "
                              "literally.")),
                ("sort_order", models.IntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
                ("batch", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="questions",
                    to="platformcfg.researchquestionbatch")),
            ],
            options={
                "db_table": "research_question",
                "ordering": ("sort_order", "id"),
            },
        ),
    ]
