"""Dev settings — DEBUG, sqlite, local storage, eager celery, console email."""
from .base import *  # noqa

DEBUG = True
ALLOWED_HOSTS = ["*"]

if not os.environ.get("FUNDOS_DB_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "var" / "fundos.dev.sqlite3",
            # Generation runs hold a write connection for minutes at a time, and
            # eager Celery means an assessment can start while a profile run is
            # still writing. The stock 5-second timeout turns that into an
            # immediate "database is locked" and a blank scorecard. Paired with
            # WAL in fundos.core.apps.
            "OPTIONS": {"timeout": 30},
        }
    }

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
CELERY_TASK_ALWAYS_EAGER = False          # run tasks inline in dev
FUNDOS_STORAGE_BACKEND = "local"

# Dev: throttling stays functional but generous (tests exercise the real
# limits via override_settings).
FUNDOS_THROTTLE_OTP_REQUEST = "1000/hour"
FUNDOS_THROTTLE_OTP_VERIFY = "1000/hour"
FUNDOS_THROTTLE_GENERATE = "10000/hour"
FUNDOS_VIRUS_SCAN_REQUIRED = False

# Print the login OTP to the runserver console — dev has no real mailbox and
# AppConfiguration SMTP, when seeded, bypasses the console email backend.
FUNDOS_OTP_ECHO_CONSOLE = True

# Default AI mode for a FRESH SEED in this environment.
#
# False, deliberately. A mocked default means a developer who has configured
# real keys still gets canned responses until they find the admin toggle, and
# mocked output is not obviously fake — it is plausible text in the right
# shape. A profile that silently came from fixtures is indistinguishable from
# one that came from the model until someone checks a figure.
#
# Failing loudly with "no API key" is the better default: it is immediate,
# unambiguous, and fixed in one step. Mocking stays available per-tenant in
# Django admin -> Application Settings for anyone who wants it.
FUNDOS_AI_MOCKED_DEFAULT = False
