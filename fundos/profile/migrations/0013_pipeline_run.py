"""Company Master Data Pipeline — live run progress and the activity log.

`ProfileGenerationRun` was a post-mortem: written once, after the run was over.
That answers "why was the output like that?" and cannot answer the question a
founder asks while a twenty-minute run is in flight — "is this working?". The
columns below make the run observable as it happens: which stage is active,
how long each one took, which research batches came back, how each uploaded
file was actually read, and where the evidence dossier was stored.

`ProfileRunEvent` is the narration alongside it. Append-only and bounded; it
explains what just happened, it is not an audit trail.

Additive only — no existing column is changed, so historical run rows stay
readable and simply carry the defaults.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companyprofile", "0012_assessment_inputs"),
    ]

    operations = [
        migrations.AddField(
            model_name="profilegenerationrun",
            name="status",
            field=models.CharField(
                db_index=True, default="queued", max_length=12,
                choices=[("queued", "queued"), ("running", "running"),
                         ("succeeded", "succeeded"), ("failed", "failed"),
                         ("refused", "refused")]),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="stage",
            field=models.CharField(
                blank=True, default="queued", max_length=32,
                help_text="Current pipeline stage: researching / "
                          "processing_documents / consolidating / "
                          "synthesizing / writing / completed."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="stage_timings",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="Per-stage {state, started_at, finished_at, "
                          "duration_seconds}. Says WHICH stage is eating the "
                          "wall clock, which the total alone never does."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="finished_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="progress",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="Per-source result summaries, keyed by stage."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="batches_total",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="batches_succeeded",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="batches_failed",
            field=models.JSONField(
                blank=True, default=list,
                help_text="Indices of research batches that returned no data. "
                          "A batch is one LLM call over one topic, so this "
                          "names exactly which subject areas the profile is "
                          "thin on."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="dossier_uri",
            field=models.CharField(
                blank=True, default="", max_length=512,
                help_text="Storage path of this run's consolidated.md — the "
                          "merged source material the profile was derived "
                          "from. Served through a short-lived signed URL."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="dossier_chars",
            field=models.IntegerField(
                default=0,
                help_text="Size of the dossier. A tiny one means the sources "
                          "found nothing, which is what explains a thin "
                          "profile."),
        ),
        migrations.AddField(
            model_name="profilegenerationrun",
            name="documents",
            field=models.JSONField(
                blank=True, default=list,
                help_text="Per-file extraction outcome: handler chosen, OCR "
                          "status and reason, characters recovered. Answers "
                          "'was my deck actually read?' without reading logs."),
        ),
        migrations.CreateModel(
            name="ProfileRunEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("tenant_id", models.UUIDField(blank=True, db_index=True,
                                               null=True)),
                ("at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("stage", models.CharField(blank=True, default="",
                                           max_length=32)),
                ("message", models.TextField(blank=True, default="")),
                ("t_plus_seconds", models.FloatField(
                    blank=True, null=True,
                    help_text="Seconds since the run started — easier to scan "
                              "than absolute timestamps when reading one "
                              "run's log.")),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("run", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="events",
                    to="companyprofile.profilegenerationrun")),
            ],
            options={
                "db_table": "profile_run_event",
                "ordering": ("at", "id"),
            },
        ),
        migrations.AddIndex(
            model_name="profilerunevent",
            index=models.Index(fields=["run", "at"],
                               name="profile_run_event_run_at_idx"),
        ),
    ]
