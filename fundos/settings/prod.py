"""
Production settings (Gap G11 — completed profile).

Everything secret/deploy-specific comes from environment variables; the full
variable set is documented in Doc 9 §4 and docs/configuration/, and the
deployment procedure in docs/operations/.
"""
from .base import *  # noqa
from .base import env

DEBUG = False

# --- Signing keys: REQUIRED, never inherited ---
#
# `base` gives SECRET_KEY a development fallback so a fresh checkout runs, and
# FUNDOS_JWT_SECRET in turn defaults to SECRET_KEY. Left alone, a production
# deployment that forgot the variable would boot happily and sign every access
# and refresh token with a value printed in this repository — anyone who can
# read the source could mint a token for any user of any tenant, and nothing
# would look wrong.
#
# Requiring them here turns that into a startup failure, which is the only
# safe way for it to fail. Set both; do not set them to the same value.
SECRET_KEY = env("FUNDOS_SECRET_KEY", required=True)
FUNDOS_JWT_SECRET = env("FUNDOS_JWT_SECRET", required=True)

# --- TLS / cookies / HSTS ---
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Hosts & CORS (explicit allowlist — never "*" in prod) ---
ALLOWED_HOSTS = env("FUNDOS_ALLOWED_HOSTS", required=True).split(",")
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in env("FUNDOS_CORS_ALLOWED_ORIGINS", required=True).split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# --- Database: PostgreSQL (India DC) with pooling/timeouts ---
DATABASES["default"].update({                                    # noqa: F405
    "CONN_MAX_AGE": env("FUNDOS_DB_CONN_MAX_AGE", default=60, cast=int),
    "OPTIONS": {
        "connect_timeout": env("FUNDOS_DB_CONNECT_TIMEOUT", default=10,
                               cast=int),
    },
})

# --- Cache & Celery broker: Redis (separated broker/result/cache DBs) ---
REDIS_URL = env("FUNDOS_REDIS_URL", required=True)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": f"{REDIS_URL}/1",
    }
}
CELERY_BROKER_URL = f"{REDIS_URL}/0"
CELERY_RESULT_BACKEND = f"{REDIS_URL}/2"
CELERY_TASK_ALWAYS_EAGER = False

# --- Email (SMTP) ---
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("FUNDOS_SMTP_HOST", required=True)
EMAIL_PORT = env("FUNDOS_SMTP_PORT", default=587, cast=int)
EMAIL_HOST_USER = env("FUNDOS_SMTP_USER", default="")
EMAIL_HOST_PASSWORD = env("FUNDOS_SMTP_PASSWORD", default="")
EMAIL_USE_TLS = env("FUNDOS_SMTP_TLS", default=True, cast=bool)
DEFAULT_FROM_EMAIL = env("FUNDOS_FROM_EMAIL", default="no-reply@fundos.in")

# --- Storage: OCI/Azure India bucket (S3-compatible connector), or a
#     local volume for on-prem enterprise deployments (e.g. /d01). ---
FUNDOS_STORAGE_BACKEND = env("FUNDOS_STORAGE_BACKEND", default="oci")
FUNDOS_STORAGE_BUCKET = env("FUNDOS_STORAGE_BUCKET", required=True)
FUNDOS_STORAGE_ENDPOINT = env("FUNDOS_STORAGE_ENDPOINT", default="")
# When FUNDOS_STORAGE_BACKEND=local, files live under this root. On the
# gyain enterprise deployment this is a dedicated volume, e.g.
# /d01/fundos-data/media. FUNDOS_MEDIA_ROOT is accepted as an alias.
# Defaults to the DURABLE data directory, not the code tree. Under a
# release-per-deploy layout the previous default (BASE_DIR/var/storage) put
# uploaded documents inside the release folder, where the next deployment
# orphaned them.
FUNDOS_STORAGE_LOCAL_ROOT = env(
    "FUNDOS_STORAGE_LOCAL_ROOT",
    default=env("FUNDOS_MEDIA_ROOT",
                default=str(FUNDOS_DATA_DIR / "storage")),  # noqa: F405
)

# --- Virus scanning (Gap G8): REQUIRED in prod — scans fail closed ---
FUNDOS_CLAMAV_HOST = env("FUNDOS_CLAMAV_HOST", required=True)
FUNDOS_CLAMAV_PORT = env("FUNDOS_CLAMAV_PORT", default=3310, cast=int)
FUNDOS_VIRUS_SCAN_REQUIRED = True

# --- Rate limits (Gap G15): tight production defaults ---
FUNDOS_THROTTLE_OTP_REQUEST = env("FUNDOS_THROTTLE_OTP_REQUEST",
                                  default="6/hour")
FUNDOS_THROTTLE_OTP_VERIFY = env("FUNDOS_THROTTLE_OTP_VERIFY",
                                 default="30/hour")
FUNDOS_THROTTLE_GENERATE = env("FUNDOS_THROTTLE_GENERATE",
                               default="60/hour")

# --- Structured JSON logging with request id ---
#
# IMPORTANT — read before editing:
# base.py already defines the full logging tree we want in production:
#   * the `generation_file` handler -> LOG_DIR/silk_generation.log
#     (SharedRotatingFileHandler, `diagnostic` formatter),
#   * the `request_context` filter that injects request_id/tenant_id/deal_id,
#   * and the five fundos.* loggers (generation, profile, llm, research,
#     assessment) each wired to BOTH `generation_file` and `console`.
#
# The previous prod.py rebuilt LOGGING and, in doing so, dropped the
# generation_file handler and those loggers — so silk_generation.log was
# never created in prod/uat and the full trace only ever reached stdout.
#
# The correct production customisation is SMALL: keep base's handlers and
# loggers exactly as they are, and only (a) make the console formatter emit
# the JSON shape the platform log collector expects, and (b) let the log
# level be tuned from the environment. We therefore mutate in place and
# NEVER reassign LOGGING["handlers"] or LOGGING["loggers"].
#
# The console `json` formatter here intentionally matches base's field set
# (request_id, tenant_id, deal_id) so it stays compatible with the
# request_context filter attached to the console handler. Do not remove
# tenant_id/deal_id — the filter supplies them and the collector expects them.

_LOG_LEVEL = env("FUNDOS_LOG_LEVEL", default="INFO")

# (a) console -> JSON for the platform log collector, keeping base's field set.
LOGGING["formatters"]["json"] = {                                # noqa: F405
    "format": (
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s",'
        '"request_id":"%(request_id)s","tenant_id":"%(tenant_id)s",'
        '"deal_id":"%(deal_id)s","msg":"%(message)s"}'
    ),
}
LOGGING["handlers"]["console"]["formatter"] = "json"             # noqa: F405

# (b) environment-tunable levels. Root stays console-only; the fundos.*
#     loggers (defined in base) keep their generation_file + console fan-out.
LOGGING["root"]["level"] = _LOG_LEVEL                            # noqa: F405
LOGGING["root"]["handlers"] = ["console"]                        # noqa: F405

# Optional: allow FUNDOS_LOG_LEVEL to also raise/lower the trace verbosity of
# the generation loggers without touching their handler wiring.
for _lg in ("fundos.generation", "fundos.profile", "fundos.llm",
            "fundos.research", "fundos.assessment"):
    if _lg in LOGGING["loggers"]:                               # noqa: F405
        LOGGING["loggers"][_lg]["level"] = env(                 # noqa: F405
            "FUNDOS_GENERATION_LOG_LEVEL",
            default=LOGGING["loggers"][_lg]["level"],           # noqa: F405
        )

# Issues 9/20: default AI mode for a fresh seed in this environment.
FUNDOS_AI_MOCKED_DEFAULT = False
