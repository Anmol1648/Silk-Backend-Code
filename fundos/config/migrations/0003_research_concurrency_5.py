"""Raise the shipped research concurrency from 3 to 5.

The ten research batches are independent and network-bound, so concurrency is
the only lever that shortens a run without changing what it costs — Gemini
bills search grounding per request, not per second. The live verification run
took ~11 minutes with the research stage dominating it.

Why the data migration, and what it deliberately does not do
------------------------------------------------------------
Changing the field default alone would only affect installs created after this
migration; every existing `AppConfiguration` row already holds a stored 3, and
`get_flag` reads the column, not the default. So the value has to be moved on
existing rows for the change to mean anything.

That cannot distinguish "3 because nobody touched it" from "3 because an
operator chose it" — the two are the same integer. The migration therefore
moves ONLY rows still holding exactly the previous shipped default, leaves any
other value alone, and is reversible. An operator who genuinely wants 3 sets it
again in admin and no later migration will touch it, because 3 is no longer the
default this compares against.

Not raised to 10 (the ten batches would then all dispatch at once): the ceiling
worth respecting is the provider's per-minute quota, and a burst that trips a
429 costs a retry on every batch simultaneously, which is slower than a smaller
fan-out. 5 halves the stage's wall clock while keeping headroom under the
quota.
"""
from django.db import migrations, models

PREVIOUS_DEFAULT = 3
NEW_DEFAULT = 5


def _bump(apps, schema_editor):
    AppConfiguration = apps.get_model("fundosconfig", "AppConfiguration")
    AppConfiguration.objects.filter(
        profile_research_concurrency=PREVIOUS_DEFAULT
    ).update(profile_research_concurrency=NEW_DEFAULT)


def _unbump(apps, schema_editor):
    AppConfiguration = apps.get_model("fundosconfig", "AppConfiguration")
    AppConfiguration.objects.filter(
        profile_research_concurrency=NEW_DEFAULT
    ).update(profile_research_concurrency=PREVIOUS_DEFAULT)


class Migration(migrations.Migration):

    dependencies = [
        ("fundosconfig", "0002_profile_pipeline_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appconfiguration",
            name="profile_research_concurrency",
            field=models.IntegerField(
                default=NEW_DEFAULT,
                help_text="How many research batches run at once (1-10). Each "
                          "batch is one search-grounded LLM call. Raising this "
                          "shortens a run without changing its cost; it is "
                          "bounded by the provider's per-minute rate limit, "
                          "not by us. Ignored on SQLite, which cannot take "
                          "concurrent writers — the run log says so when it "
                          "clamps."),
        ),
        migrations.RunPython(_bump, _unbump),
    ]
