from django.apps import AppConfig


class PlatformCfgConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fundos.platformcfg"
    label = "platformcfg"
    verbose_name = "Platform Configuration"
