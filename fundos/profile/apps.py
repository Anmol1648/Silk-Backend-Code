from django.apps import AppConfig


class ProfileConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fundos.profile"
    label = "companyprofile"
    verbose_name = "Company Profile"
