from django.apps import AppConfig


class DiagnosticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fundos.diagnostics"
    label = "diagnostics"
    verbose_name = "Diagnostics & run history"
