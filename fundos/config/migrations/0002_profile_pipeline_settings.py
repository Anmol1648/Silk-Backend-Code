"""Company Profile pipeline dials on AppConfiguration.

Real columns rather than keys in the free-form `features` blob: these are what
an operator reaches for when a run is too slow, too expensive, or producing
thin documents, and a labelled field with help text is the difference between
a setting someone can find and one they have to be told about.

The two config-profile codes are here rather than hard-coded so that switching
the whole pipeline onto another model is admin work — edit the referenced
profile under LLM -> Config profiles, or point these at different profiles
entirely.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fundosconfig", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_research_concurrency",
            field=models.IntegerField(
                default=3,
                help_text="How many research batches run at once (1-10). Each "
                          "batch is one search-grounded LLM call. Raising this "
                          "shortens a run without changing its cost; it is "
                          "bounded by the provider's per-minute rate limit, "
                          "not by us."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_ocr_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Read scanned pages and images inside uploaded "
                          "documents. Safe to leave on: with no OCR engine "
                          "installed the pipeline falls back to native text "
                          "extraction and records why. Turn off to skip the "
                          "CPU cost entirely."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_ocr_engine",
            field=models.CharField(
                default="tesseract", max_length=16,
                choices=[("tesseract", "Tesseract — fast, no GPU"),
                         ("easyocr",
                          "EasyOCR — slower, better on rotated text"),
                         ("both",
                          "Both — keep the longer result (~2x the time)")],
                help_text="Which OCR engine reads scanned pages."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_ocr_page_limit",
            field=models.IntegerField(
                default=40,
                help_text="Most pages/slides OCR'd per file. Stops one "
                          "enormous scan consuming a whole run. Truncation is "
                          "reported in the dossier, never silent."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_document_timeout_s",
            field=models.IntegerField(
                default=600,
                help_text="Per-file deadline for OCR, in seconds. Native "
                          "extraction is unaffected."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_research_config_profile",
            field=models.CharField(
                blank=True, default="profile.research", max_length=64,
                help_text="LLM config profile code for the research calls "
                          "(web search ON). Edit that profile under LLM -> "
                          "Config profiles to change the model for every "
                          "research call."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_synthesis_config_profile",
            field=models.CharField(
                blank=True, default="profile.synthesis", max_length=64,
                help_text="LLM config profile code for the single synthesis "
                          "call (web search OFF)."),
        ),
        migrations.AddField(
            model_name="appconfiguration",
            name="profile_max_dossier_chars",
            field=models.IntegerField(
                default=600000,
                help_text="Ceiling on the dossier text sent to the synthesis "
                          "call. A runaway guard, not a routine truncation — "
                          "when it bites, the dossier says so."),
        ),
    ]
