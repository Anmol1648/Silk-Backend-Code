"""Per-file extraction detail on MaterialAsset.

"Uploaded but unread" was the most misleading state in the system: the founder
saw their file listed and assumed it had been used. In practice the extractor
handled PDF text layers, `.txt` and `.csv`, and returned an empty string for
DOCX, XLSX and PPTX with a comment promising machinery that was never built —
so a pitch deck and a financial model contributed nothing at all, invisibly.
`ocr_applied` existed as a column and was always False, because no code path
ever ran OCR.

These columns record what the extractor actually decided and did for each
file, so "was my deck read, and how?" is answerable from the row rather than
from logs. Defaults are chosen so existing rows read as "we do not know",
which is the truth about them.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("readiness", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="materialasset",
            name="file_type",
            field=models.CharField(
                blank=True, default="", max_length=16,
                help_text="Lowercase extension, e.g. '.pptx'. Decides the "
                          "handler."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="handler",
            field=models.CharField(
                blank=True, default="", max_length=32,
                help_text="How this type is treated: native+ocr / native only "
                          "/ ocr only / native (best effort)."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="native_chars",
            field=models.IntegerField(
                default=0,
                help_text="Characters native extraction recovered. Zero on a "
                          "PDF means it is almost certainly a scan."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_status",
            field=models.CharField(
                blank=True, default="", max_length=12,
                help_text="pending / triggered / skipped / disabled / failed."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_reason",
            field=models.CharField(
                blank=True, default="", max_length=500,
                help_text="Why OCR did or did not run for this specific file. "
                          "A 'skipped' with no reason is indistinguishable "
                          "from a bug."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_engines",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_units_total",
            field=models.IntegerField(
                default=0,
                help_text="Pages/slides/images considered for OCR."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_units_ocred",
            field=models.IntegerField(
                default=0,
                help_text="Of those, how many went through an engine."),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_chars",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="ocr_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="materialasset",
            name="extraction_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
