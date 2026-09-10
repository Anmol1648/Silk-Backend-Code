from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("platformcfg", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompttemplate",
            name="tier",
            field=models.CharField(
                max_length=16, blank=True, default="",
                choices=[("", "Use shipped default"),
                         ("simple", "Simple — no web search (extraction / Q&A)"),
                         ("advanced", "Advanced — web search + complex generation")],
                help_text="Which LLM this prompt runs on. Advanced enables web "
                          "search (used for the exhaustive Company Profile). "
                          "Simple is the cheap, no-tools model for reading/Q&A "
                          "over data already retrieved. Leave blank to use the "
                          "shipped default for this role."),
        ),
        migrations.AddField(
            model_name="prompttemplate",
            name="purpose",
            field=models.TextField(
                blank=True, default="",
                help_text="Plain-English explanation of what this prompt is "
                          "for and when it runs."),
        ),
    ]
