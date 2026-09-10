"""
FundOS settings — base (shared across environments).

Conventions (Doc 8 §2):
  * one env() helper; ALL secrets via environment (names in Doc 9 §4)
  * anything runtime-togglable lives in AppConfiguration / feature flags,
    never in settings
  * middleware order per Doc 6 Appendix G (FundOS variant)
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _running_tests():
    """True when this process is the test runner.

    The suite must not see the developer's ``.env``. It is written to run with
    no network and no provider key, and several tests assert exactly that
    state — that vendor alternates stay inactive, that a role with no usable
    key is left unbound. Loading a real key file flips those from "asserting
    the unconfigured path works" to "failing because the machine happens to
    have keys", which is the worst kind of test failure: it depends on who runs
    it, and it appears in the diff of an unrelated change.

    Checked from ``sys.argv`` rather than a settings module of its own, because
    the suite deliberately runs under ``fundos.settings.dev`` — the same module
    a developer serves from — so the two cannot be told apart by settings.
    """
    return ("test" in sys.argv[1:2]) or bool(os.environ.get("FUNDOS_NO_ENV_FILE"))


def _load_env_file(path=None):
    """Populate ``os.environ`` from a ``.env`` beside ``manage.py``, if present.

    Settings read ``os.environ`` directly, so before this an operator had to
    export every variable in the shell that launched the process — which works
    for one command and silently does not for the next terminal, for Celery,
    or for a scheduled run. The provider key being set in one place and unset
    in another is the single most common cause of "it worked when I ran it".

    Deliberate properties:

    * **A real environment variable always wins.** The file is a default, never
      an override, so a value exported for one command cannot be shadowed by a
      stale file — and production, where variables come from systemd, behaves
      exactly as it did before.
    * **No dependency.** ``python-dotenv`` is not in ``requirements.txt``, and
      importing whatever happens to be in the virtualenv is how an install
      works on one machine and not another. This parser handles the subset that
      matters: ``KEY=value``, ``export KEY=value``, ``#`` comments, and quoted
      values.
    * **Never raises.** An unreadable or malformed file degrades to "no file",
      because a settings module that throws at import takes down every
      management command including the ones you would use to diagnose it.

    The file is gitignored (``.env`` and ``.env.*``); ``deploy/befundos.env.template``
    is the tracked example.
    """
    if path is None and _running_tests():
        # Declining to read the file ourselves is not enough: a third-party
        # import can load it for us. ``magika`` — pulled in by MarkItDown, and
        # therefore imported the first time a test uploads a document — calls
        # ``dotenv.load_dotenv(find_dotenv())`` at import time, which walks up
        # from the working directory, finds this same ``.env`` and pushes every
        # key in it into ``os.environ`` midway through the suite.
        #
        # That produced the worst shape of failure: tests that pass alone and
        # fail in company, because seeding picks a provider by which key is
        # visible, and the key only became visible once an unrelated test
        # touched document extraction. Setting the flag before that import can
        # happen is the only pre-emptive hook available, since the load runs at
        # module scope. (Honoured by python-dotenv >= 1.1; on an older pin the
        # flag is simply ignored and the leak returns, so keep it current.)
        os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
        return []
    if path:
        candidate = Path(path)
    elif os.environ.get("FUNDOS_ENV_FILE"):
        candidate = Path(os.environ.get("FUNDOS_ENV_FILE"))
    elif (BASE_DIR / ".env").is_file():
        candidate = BASE_DIR / ".env"
    else:
        candidate = BASE_DIR.parent / ".env"
    try:
        if not candidate.is_file():
            return []
        loaded = []
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            if not key or key in os.environ:
                continue            # a real environment variable wins
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ[key] = value
            loaded.append(key)
        return loaded
    except OSError:
        return []


ENV_FILE_KEYS = _load_env_file()


def env(name, default=None, required=False, cast=None):
    """Single environment accessor (Doc 8 §2). Secrets are env-only."""
    val = os.environ.get(name, default)
    if required and val in (None, ""):
        raise RuntimeError(f"Required environment variable {name} is not set")
    if cast and val is not None:
        if cast is bool:
            return str(val).lower() in ("1", "true", "yes", "on")
        return cast(val)
    return val


SECRET_KEY = env("FUNDOS_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = False
ALLOWED_HOSTS = env("FUNDOS_ALLOWED_HOSTS", default="*").split(",")

APP_NAME = "FundOS"

# ---------------------------------------------------------------------------
# Applications — apps map 1:1 to modules M0–M3 (Doc 8 §1)
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "django_filters",
    "corsheaders",
    # M0 — Foundation & Platform Core
    "fundos.core",
    "fundos.config",
    # Admin-owned runtime configuration: prompts, branding, stages,
    # fund-raise buckets, countries, FX, UI copy. Loaded before the domain
    # apps because they resolve configuration at import-adjacent call time.
    "fundos.platformcfg",
    "fundos.llm",
    "fundos.docs",
    # Stage 1 — Company Profile
    "fundos.profile",
    "fundos.assessment",
    "fundos.research",
    "fundos.readiness",
    # Stage 2 — Fundraising Strategy
    "fundos.strategy",
    # Stage 3 — Investor Discovery: investor universe, deal data, matching.
    # Separate from `assessment` because the base table is one row per
    # investor PER DEAL, not one row per firm.
    "fundos.investors",
    # Cross-cutting: run records and correlated events. Loaded last so its
    # log handler can see every other app's logger.
    "fundos.diagnostics",
    # Investor materials — retained as Company Profile addenda (C7)
    "fundos.materials",
]

# Middleware order: Doc 6 Appendix G, FundOS delta — tenant middlewares
# dropped (tenant comes from the JWT), RequestID early, Idempotency after
# Authentication (Authentication happens in DRF, so idempotency middleware
# defers body-hash replay until the view layer resolves the user; see
# core.middleware.IdempotencyMiddleware).
MIDDLEWARE = [
    "fundos.diagnostics.services.CorrelationMiddleware",
    "fundos.core.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "fundos.core.middleware.IdempotencyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fundos.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "fundos.wsgi.application"
ASGI_APPLICATION = "fundos.asgi.application"

# ---------------------------------------------------------------------------
# Database — PostgreSQL (India DC) in uat/prod; overridden in dev
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("FUNDOS_DB_NAME", default="fundos"),
        "USER": env("FUNDOS_DB_USER", default="fundos"),
        "PASSWORD": env("FUNDOS_DB_PASSWORD", default=""),
        "HOST": env("FUNDOS_DB_HOST", default="localhost"),
        "PORT": env("FUNDOS_DB_PORT", default="5432"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "core.User"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Leading slash matters: a relative "static/" makes the admin resolve its
# CSS against the current path (/admin/static/...), so the admin rendered
# unstyled behind a proxy (QA 24-Jul issue 9).
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# DRF (Doc 8 §2 / Doc 6 Appendix H)
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "fundos.core.auth.IdpJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ("v1",),
    "DEFAULT_PAGINATION_CLASS": "fundos.core.api.pagination.FundosCursorPagination",
    "PAGE_SIZE": 25,
    "EXCEPTION_HANDLER": "fundos.core.exceptions.fundos_exception_handler",
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
}

# JWT (issued by /auth/otp/verify — managed-IdP compatible claims)
FUNDOS_JWT_SECRET = env("FUNDOS_JWT_SECRET", default=SECRET_KEY)
FUNDOS_JWT_ALGORITHM = "HS256"
FUNDOS_JWT_ACCESS_TTL_SECONDS = 60 * 60          # 1 hour
FUNDOS_JWT_REFRESH_TTL_SECONDS = 60 * 60 * 24 * 14

# OTP mechanics (Doc 6 Appendix E)
FUNDOS_OTP_TTL_SECONDS = 300
FUNDOS_OTP_MAX_ATTEMPTS = 5
FUNDOS_OTP_REQUEST_RATE_PER_HOUR = 6
# Local sign-in without a mailbox: echo the login code to the server console.
# Off everywhere but dev — turning this on elsewhere puts live codes in the
# application log.
FUNDOS_OTP_ECHO_CONSOLE = env("FUNDOS_OTP_ECHO_CONSOLE", default=False,
                              cast=bool)

# Idempotency (GC-17): (tenant, user, path, key) → sha256(body)+response, 24h
FUNDOS_IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24

# Currency presentation (Gap G13) — engines compute in USD; the API edge
# presents in this currency and exposes the rate as an assumption.
FUNDOS_PRESENTATION_CCY = env("FUNDOS_PRESENTATION_CCY", default="INR")
FUNDOS_USD_INR = env("FUNDOS_USD_INR", default=84.0, cast=float)

# Rate limiting (Gap G15) — DRF SimpleRateThrottle rates per environment.
FUNDOS_THROTTLE_OTP_REQUEST = env("FUNDOS_THROTTLE_OTP_REQUEST",
                                  default="6/hour")
FUNDOS_THROTTLE_OTP_VERIFY = env("FUNDOS_THROTTLE_OTP_VERIFY",
                                 default="30/hour")
FUNDOS_THROTTLE_GENERATE = env("FUNDOS_THROTTLE_GENERATE",
                               default="60/hour")

# Document extraction — OCR binary location.
#
# A HOST property, not a policy one, so it lives here and not in
# AppConfiguration: a filesystem path in a shared admin table is wrong the
# moment there is more than one worker host. Blank means auto-detect (PATH,
# then the standard Windows install locations). WHICH engine to use is a
# policy choice and is admin-configurable.
FUNDOS_TESSERACT_CMD = env("FUNDOS_TESSERACT_CMD", default="")

# Virus scanning (Gap G8) — ClamAV daemon; REQUIRED ⇒ fail closed.
FUNDOS_CLAMAV_HOST = env("FUNDOS_CLAMAV_HOST", default="")
FUNDOS_CLAMAV_PORT = env("FUNDOS_CLAMAV_PORT", default=3310, cast=int)
FUNDOS_VIRUS_SCAN_REQUIRED = env("FUNDOS_VIRUS_SCAN_REQUIRED",
                                 default=False, cast=bool)

# ---------------------------------------------------------------------------
# Celery (Doc 8 §2 / Doc 6 Appendix B — settings keys prefixed CELERY_)
# ---------------------------------------------------------------------------
REDIS_URL = env("FUNDOS_REDIS_URL", default="redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_TIME_LIMIT = 900
CELERY_TASK_ALWAYS_EAGER = False

# Celery health signals (Doc 6 §6.5)
CELERY_SLOW_TASK_ALERT_SECONDS = env("CELERY_SLOW_TASK_ALERT_SECONDS", default=300, cast=int)
CELERY_TASK_FAILURE_ALERT = env("CELERY_TASK_FAILURE_ALERT", default=True, cast=bool)

# ---------------------------------------------------------------------------
# Storage backend selection (Doc 6 §8) — OCI/S3-compatible default, Local dev
# ---------------------------------------------------------------------------
FUNDOS_STORAGE_BACKEND = env("FUNDOS_STORAGE_BACKEND", default="local")  # local|s3|oci|azure
FUNDOS_STORAGE_LOCAL_ROOT = env("FUNDOS_STORAGE_LOCAL_ROOT", default=str(BASE_DIR / "var" / "storage"))
FUNDOS_STORAGE_BUCKET = env("FUNDOS_STORAGE_BUCKET", default="fundos")
FUNDOS_STORAGE_ENDPOINT_URL = env("FUNDOS_STORAGE_ENDPOINT_URL", default=None)  # OCI S3-compat endpoint

# Upload rules (VAL-M1-020 / BR-M1-023)
FUNDOS_UPLOAD_DIRECT_MAX_BYTES = 50 * 1024 * 1024          # 50 MB direct multipart
FUNDOS_UPLOAD_CHUNKED_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB per file (video)
FUNDOS_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024                 # 8 MB chunks
FUNDOS_UPLOAD_ALLOWED_MIME_PREFIXES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument",  # docx/xlsx/pptx
    "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "text/csv", "text/plain",
    "image/", "video/",
    "application/zip", "application/x-zip-compressed",
)

# Email defaults (fallback only — runtime settings live in AppConfiguration)
DEFAULT_FROM_EMAIL = env("FUNDOS_DEFAULT_FROM_EMAIL", default="noreply@fundos.in")
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

# ---------------------------------------------------------------------------
# Logging — structured, request_id/tenant_id/deal_id carried by the filter
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Durable data directory.
#
# BASE_DIR is the CODE directory. Under a release-per-deploy layout
# (…/releases/<timestamp>/…) that directory is replaced on every deploy, so
# anything defaulted under it — uploaded documents, diagnostic logs — is
# orphaned by the next release. Defaulting data paths to BASE_DIR is therefore
# a silent data-loss bug waiting on the next deployment, not merely untidy.
#
# This resolves a durable location automatically:
#   * release layout  ->  <deploy-root>/shared/data   (a sibling of releases/)
#   * plain checkout  ->  <BASE_DIR>/var             (unchanged behaviour)
# FUNDOS_DATA_DIR overrides it explicitly.
# ---------------------------------------------------------------------------
def _durable_data_dir(base: Path) -> Path:
    """A directory that survives deployment, derived from the code path."""
    parts = [p.lower() for p in base.parts]
    for marker in ("releases", "release", "versions"):
        if marker in parts:
            # /d01/fundos/releases/2026-08-10/FUNDOS/... -> /d01/fundos/shared/data
            root = Path(*base.parts[:parts.index(marker)])
            return root / "shared" / "data"
    return base / "var"


FUNDOS_DATA_DIR = Path(env("FUNDOS_DATA_DIR",
                           default=str(_durable_data_dir(BASE_DIR))))

# Where diagnostic logs are written. Override with FUNDOS_LOG_DIR.
LOG_DIR = Path(env("FUNDOS_LOG_DIR", default=str(FUNDOS_DATA_DIR / "logs")))
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:      # read-only filesystem — fall back to console only
    pass

# --- Raw LLM request/response body capture (opt-in; see fundos.llm.adapter) --
# OFF by default. When true, the adapter writes the full outgoing prompt and
# the raw incoming response for every LLM call to a DEDICATED file
# (llm_bodies.log), NOT to silk_generation.log — bodies are large (a synthesis
# prompt is ~600 KB) and would otherwise evict the step trace. These bodies
# contain tenant-confidential material (uploaded documents, scraped content),
# so enable only for a debugging session and turn it back off.
# FUNDOS_LOG_LLM_BODY_MAXCHARS caps each captured body; 0 means "no cap".
FUNDOS_LOG_LLM_BODIES = env("FUNDOS_LOG_LLM_BODIES", default=False, cast=bool)
FUNDOS_LOG_LLM_BODY_MAXCHARS = env("FUNDOS_LOG_LLM_BODY_MAXCHARS",
                                   default=8000, cast=int)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {"()": "fundos.core.logging.RequestContextFilter"},
    },
    "formatters": {
        "json": {
            "format": (
                '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s",'
                '"request_id":"%(request_id)s","tenant_id":"%(tenant_id)s",'
                '"deal_id":"%(deal_id)s","msg":"%(message)s"}'
            )
        },
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        # One line per step, timestamped, greppable by run id.
        "diagnostic": {
            "format": "%(asctime)s %(levelname)-7s %(name)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_context"],
            "formatter": "json",
        },
        # Dedicated diagnostic log for Company Profile generation.
        #
        # Generation failures are hard to diagnose because they are spread
        # across sources, the LLM adapter and the section fan-out, and most of
        # them fail quietly. Every step of every run writes one line here with
        # a shared run id, so a single file answers "where did it stop?".
        # Kept separate from the application log so it can be handed to
        # support without shipping everything else.
        # Rotation is handled by our own subclass because more than one
        # process holds this file open — `runserver` runs two, a deployed
        # stack runs one per worker — and the stdlib handler cannot rename a
        # file Windows still has a handle on. See SharedRotatingFileHandler.
        "generation_file": {
            "class": "fundos.core.logging.SharedRotatingFileHandler",
            "filename": str(LOG_DIR / "silk_generation.log"),
            "maxBytes": 20 * 1024 * 1024,      # 20 MB
            "backupCount": 10,                  # ~200 MB retained
            "encoding": "utf-8",
            # Open on first write, not at configuration time. The autoreloader
            # parent configures logging but never writes a generation line, so
            # without this it holds a handle purely to keep the child from
            # rotating — which is the whole contention in local development.
            "delay": True,
            "formatter": "diagnostic",
            "level": "DEBUG",
        },
        # Dedicated raw-body log for LLM calls (opt-in via FUNDOS_LOG_LLM_BODIES).
        # Separate file, larger rotation: a single synthesis prompt is ~600 KB,
        # so these must never share silk_generation.log or they would evict the
        # step trace within a couple of runs. Own handler + a propagate=False
        # logger keep bodies out of the console/journald stream too.
        "llm_bodies_file": {
            "class": "fundos.core.logging.SharedRotatingFileHandler",
            "filename": str(LOG_DIR / "llm_bodies.log"),
            "maxBytes": 200 * 1024 * 1024,     # 200 MB — bodies are large
            "backupCount": 5,                   # ~1 GB retained
            "encoding": "utf-8",
            "delay": True,
            "formatter": "diagnostic",
            "level": "DEBUG",
        },
    },
    "loggers": {
        # fundos.generation carries the step-by-step trace and the diagnosis.
        "fundos.generation": {
            "handlers": ["generation_file", "console"],
            "level": "DEBUG",
            "propagate": False,
        },
        # The pipeline's own modules also write to the diagnostic file, so a
        # single file has the trace AND the surrounding detail.
        "fundos.profile": {
            "handlers": ["generation_file", "console"],
            "level": "INFO",
            "propagate": False,
        },
        "fundos.llm": {
            "handlers": ["generation_file", "console"],
            "level": "INFO",
            "propagate": False,
        },
        # Raw LLM bodies — their OWN file only. propagate=False so a 600 KB
        # prompt never reaches silk_generation.log, the console or journald.
        "fundos.llm.bodies": {
            "handlers": ["llm_bodies_file"],
            "level": "DEBUG",
            "propagate": False,
        },
        "fundos.research": {
            "handlers": ["generation_file", "console"],
            "level": "INFO",
            "propagate": False,
        },
        # v23: the assessment pipeline writes here too. Without this its
        # logging reached the console only and never the diagnostic file, so
        # the one artefact you send when asking "why did this deal score like
        # that?" was missing the bridge counts, the extraction counts, the
        # sub-sector resolution and every scoring warning — i.e. everything
        # about step 2. The profile half was captured and the scoring half
        # was not, which made the file actively misleading rather than merely
        # incomplete.
        "fundos.assessment": {
            "handlers": ["generation_file", "console"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

CORS_ALLOW_ALL_ORIGINS = env("FUNDOS_CORS_ALLOW_ALL", default=True, cast=bool)

# Caches — Redis in uat/prod (idempotency store), overridden in dev
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# core.User enforces case-insensitive email uniqueness for LIVE rows via a
# partial unique index (soft delete keeps historical rows) — silence the
# blanket auth.E003 which cannot see partial constraints.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]
