"""
python manage.py diagnose_config

Checks every setting the Company Profile pipeline depends on, END TO END, and
reports each as PASS / FAIL / WARN with the exact remedy. Run it after every
deployment and before investigating any "the profile is empty" report — most
such reports are a setting rather than a fault, and this command finds those
in seconds.

    --strict            exit non-zero if any WARN is present too
    --json              machine-readable output
    --scope profile     only the checks Company Profile depends on
    --no-worker-probe   skip the round trip to the Celery worker
    --probe-timeout N   seconds to wait for the worker probe (default 10)

Exit code is 1 if any CRITICAL check fails, so it can gate a deploy script.

WHAT CHANGED IN THIS VERSION, AND WHY
-------------------------------------
The previous version reported PASS on a system that could not generate a
profile. Four blind spots caused that, and each is now a check:

  1. ONE role binding was verified out of the nine Company Profile uses.
     `llm_generate` raises LLMUnavailable when a role has no binding — it does
     not fall back — so eight unbound roles produced a green report and an
     empty profile.
  2. The API KEY was never read. Endpoint rows were counted; the environment
     was not consulted, in either process.
  3. Only the ADVANCED tier was resolved. Simple and Judgement could still be
     pointing at another vendor's inactive profile.
  4. Model RETIREMENT was invisible. A model with a published end date in the
     catalog notes passed silently until the day it stopped answering.

Plus the failure the guide warns about and nothing checked: the Celery worker
is a separate process with its own environment and its own copy of the code.
It is the process that actually generates. `--no-worker-probe` turns that
round trip off.
"""
import datetime as dt
import json
import re

from django.core.management.base import BaseCommand

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"

# Ordering for the remediation list. Fix in this order and each fix removes
# the cause of the ones below it.
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

# The severity attached to a check is its CEILING — what it means when the
# check outright fails. A WARN from the same check is real but less urgent, so
# it is reported one step down. Without this a warning prints as CRITICAL and
# the remediation list stops telling you what to do first.
DOWNGRADE = {"CRITICAL": "HIGH", "HIGH": "MEDIUM", "MEDIUM": "LOW",
             "LOW": "LOW"}

# The roles Company Profile calls, and what each contributes. A role with no
# binding is not degraded — it raises, and that part of the profile is never
# produced.
#
# ESSENTIAL means a full generation cannot complete without it. Only the two
# pipeline roles qualify: a run is ten research calls and one synthesis call,
# and nothing else is on that path. The per-section roles below are RECOMMENDED
# rather than ESSENTIAL because they serve the Regenerate button on a single
# section — losing one costs that button, not the profile.
PROFILE_ROLES = [
    ("profile_research_batch", "ESSENTIAL",
     "the search-grounded research batches that gather the evidence"),
    ("profile_synthesis", "ESSENTIAL",
     "turns the gathered dossier into the structured profile"),
    ("company_profile_section", "RECOMMENDED",
     "regenerates a single narrative section on request"),
    ("company_profile_records", "RECOMMENDED",
     "regenerates founders, competitors, funding rounds, news"),
    ("company_profile_structured", "RECOMMENDED",
     "regenerates a structured numeric form"),
    ("company_profile_field", "RECOMMENDED", "fills a single field on request"),
    ("founder_profile", "RECOMMENDED", "builds founder background"),
    ("profile_qa", "OPTIONAL", "answers questions about a generated profile"),
    ("readiness_summary", "OPTIONAL", "produces the readiness summary"),
]
ESSENTIAL_ROLES = [r for r, p, _ in PROFILE_ROLES if p == "ESSENTIAL"]

# Declared, bound and prompted, but on no live call path. Kept so an install
# that rolls back to an earlier release still resolves them, and so their
# prompts survive as reference — but a diagnostic must not report them as
# healthy infrastructure, because nothing would notice if they broke.
RETIRED_ROLES = {
    "company_profile_deep_extract": (
        "superseded by profile_research_batch + profile_synthesis"),
    "company_profile_judgment": (
        "superseded by the single synthesis call over the dossier"),
}

# Research adapters that currently return fixtures rather than live data.
# Their being "enabled" says nothing about research quality, and reporting a
# source count without saying so overstates what is configured.
STUB_SOURCES = {"market", "competitor", "industry", "public_funding",
                "patents", "news", "awards", "hiring"}

RETIREMENT_RE = re.compile(
    r"RETIR\w*\s+(?:ON\s+)?(\d{1,2})[-\s]([A-Za-z]{3})[-\s](\d{4})",
    re.IGNORECASE)


class Command(BaseCommand):
    help = "Verify the configuration the profile pipeline depends on."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true",
                            help="Exit non-zero if any WARN is present too.")
        parser.add_argument("--json", action="store_true",
                            help="Emit JSON instead of the report.")
        parser.add_argument("--scope", choices=["profile", "all"],
                            default="all",
                            help="'profile' omits checks unrelated to "
                                 "Company Profile generation.")
        parser.add_argument("--no-worker-probe", action="store_true",
                            help="Do not round-trip a task to the worker.")
        parser.add_argument("--probe-timeout", type=int, default=10)

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        self.opts = opts
        self.results = []

        # (group, name, severity, fn, profile_scoped)
        checks = [
            ("Runtime", "Settings module", "LOW", self._settings, True),
            ("Runtime", "Database migrations", "CRITICAL",
             self._migrations, True),
            ("Runtime", "Background worker", "CRITICAL", self._celery, True),
            ("Runtime", "Worker environment", "CRITICAL",
             self._worker_probe, True),
            ("Runtime", "File storage", "CRITICAL", self._storage, True),
            ("Runtime", "Diagnostic log", "MEDIUM", self._logfile, True),
            ("Runtime", "Fuzzy matching library", "HIGH",
             self._rapidfuzz, False),

            ("AI provider", "AI mode", "CRITICAL", self._ai_mocked, True),
            ("AI provider", "Endpoints", "CRITICAL", self._endpoints, True),
            ("AI provider", "API credentials", "CRITICAL",
             self._credentials, True),
            ("AI provider", "Endpoint timeout", "MEDIUM",
             self._timeout, True),
            ("AI provider", "Profile role bindings", "CRITICAL",
             self._profile_bindings, True),
            ("AI provider", "Other role bindings", "MEDIUM",
             self._other_bindings, False),
            ("AI provider", "Output token ceilings", "HIGH",
             self._output_ceilings, True),

            ("Model chain", "Advanced tier (research)", "CRITICAL",
             lambda: self._tier_chain("advanced"), True),
            ("Model chain", "Simple tier (extraction)", "CRITICAL",
             lambda: self._tier_chain("simple"), True),
            ("Model chain", "Judgement tier (reasoning)", "HIGH",
             lambda: self._tier_chain("judgment"), True),
            ("Model chain", "Search capability", "CRITICAL",
             self._search_capability, True),
            ("Model chain", "Thinking budget", "HIGH",
             self._thinking, True),
            ("Model chain", "Model retirement", "HIGH",
             self._retirement, True),
            ("Model chain", "Global tier rows", "HIGH",
             self._duplicate_tiers, True),

            ("Profile pipeline", "Profile section registry", "CRITICAL",
             self._sections, True),
            ("Profile pipeline", "Prompt library", "HIGH",
             self._prompts, True),
            ("Profile pipeline", "Prompt tier overrides", "HIGH",
             self._prompt_tiers, True),
            ("Profile pipeline", "Research sources", "CRITICAL",
             self._research, True),
            ("Profile pipeline", "Question bank & run settings", "HIGH",
             self._flags, True),

            ("Other modules", "Investor database", "HIGH",
             self._investors, False),
        ]

        for group, name, severity, fn, in_profile_scope in checks:
            if opts["scope"] == "profile" and not in_profile_scope:
                continue
            self._run(group, name, severity, fn)

        if opts["json"]:
            self.stdout.write(json.dumps(self.results, indent=2, default=str))
        else:
            self._report()

        critical_failed = any(r["status"] == FAIL and r["severity"] == "CRITICAL"
                              for r in self.results)
        problems = [r for r in self.results if r["status"] in (FAIL, WARN)]
        if critical_failed or (opts["strict"] and problems):
            raise SystemExit(1)

    def _run(self, group, name, severity, fn):
        try:
            status, detail, remedy = fn()
        except Exception as e:
            status, detail, remedy = (
                FAIL, f"check failed: {type(e).__name__}: {e}",
                "Report this to the development team.")
        impact = severity if status == FAIL else DOWNGRADE.get(severity, severity)
        self.results.append({"group": group, "name": name,
                             "severity": severity, "impact": impact,
                             "status": status, "detail": detail,
                             "remedy": remedy})

    # ------------------------------------------------------------------
    def _report(self):
        width = 30
        out = self.stdout.write
        out("")
        out("=" * 78)
        out("SILK AI — CONFIGURATION CHECK")
        out("=" * 78)

        last_group = None
        for r in self.results:
            if r["group"] != last_group:
                out("")
                out(f"  {r['group'].upper()}")
                last_group = r["group"]
            out(f"  [{r['status']}] {r['name']:<{width}} {r['detail']}")
        out("")

        problems = [r for r in self.results if r["status"] in (FAIL, WARN)]
        if not problems:
            out("-" * 78)
            out("All checks passed. The pipeline is configured to run fully.")
            out("")
            out("Next: prove it end to end on one real company —")
            out("  python manage.py diagnose_profile --company \"Name\" "
                "--generate")
            out("")
            return

        problems.sort(key=lambda r: (r["status"] != FAIL,
                                     SEVERITY_RANK.get(r["impact"], 9)))
        out("-" * 78)
        out("WHAT TO FIX — in this order")
        out("-" * 78)
        for i, r in enumerate(problems, 1):
            out(f"  {i}. [{r['impact']}/{r['status']}] {r['name']}")
            out(f"     Problem : {r['detail']}")
            lines = [ln for ln in (r["remedy"] or "").split("\n") if ln.strip()]
            for n, line in enumerate(lines):
                label = "Fix     : " if n == 0 else "          "
                out(f"     {label}{line}")
            out("")

        blocking = [r for r in problems
                    if r["status"] == FAIL and r["severity"] == "CRITICAL"]
        if blocking:
            out("-" * 78)
            out(f"{len(blocking)} CRITICAL failure(s). Company Profile "
                "generation will not complete correctly until these are "
                "resolved.")
        else:
            out("-" * 78)
            out("No CRITICAL failures. Generation should run; the items above "
                "affect cost, quality or future reliability.")
        out("")

    # ==================================================================
    # Runtime
    # ==================================================================
    def _settings(self):
        from django.conf import settings
        module = getattr(settings, "SETTINGS_MODULE", "(unknown)")
        debug = bool(getattr(settings, "DEBUG", False))
        if debug and "dev" not in module:
            return (WARN, f"{module} with DEBUG on",
                    "DEBUG should be off outside development.")
        return (PASS, f"{module} (DEBUG {'on' if debug else 'off'})", "")

    def _migrations(self):
        """An unapplied migration is a silent source of impossible errors."""
        from django.db import connections
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connections["default"])
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        if plan:
            names = ", ".join(f"{m.app_label}.{m.name}" for m, _ in plan[:5])
            return (FAIL, f"{len(plan)} migration(s) not applied: {names}",
                    "Run: python manage.py migrate")
        return (PASS, f"{len(targets)} app(s) up to date", "")

    def _celery(self):
        from django.conf import settings
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return (PASS, "tasks run inline (development mode)", "")
        try:
            from fundos.celeryapp import app
            replies = app.control.ping(timeout=2.0)
        except Exception as e:
            self._worker_ok = False
            return (FAIL, f"could not reach the task queue: {e}",
                    "Start Redis and the Celery worker. Uploaded documents "
                    "are read by that worker; without it they are stored but "
                    "never used.")
        if not replies:
            self._worker_ok = False
            return (FAIL, "no worker responded",
                    "Start the Celery worker, and restart it after every "
                    "deployment so it runs the current code.")
        self._worker_count = len(replies)
        return (PASS, f"{len(replies)} worker(s) responding", "")

    def _worker_probe(self):
        """Ask the WORKER what it can see.

        Everything else in this command describes the web process. The worker
        performs generation, holds its own copy of the environment, and is not
        restarted by a code deployment on its own. A key added after the
        worker started is invisible to every other check here and fails at
        call time as an AI error.
        """
        from django.conf import settings

        if self.opts.get("no_worker_probe"):
            return (INFO, "skipped (--no-worker-probe)", "")
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return (INFO, "not applicable — tasks run inline", "")
        if not getattr(self, "_worker_ok", True):
            # No point waiting out the timeout for a process that just failed
            # to answer a ping.
            return (INFO, "skipped — no worker responded to the ping", "")

        try:
            from fundos.llm.tasks import PROBE_VERSION, config_probe
            result = config_probe.apply_async(expires=60)
            data = result.get(timeout=self.opts.get("probe_timeout", 10))
        except Exception as e:
            return (WARN,
                    f"the worker did not answer the probe ({type(e).__name__})",
                    "Most often this means the worker is running an OLDER "
                    "release that does not have this task, or the result "
                    "backend is unreachable. Restart the worker:\n"
                    "  sudo systemctl restart fundos-worker\n"
                    "Re-run this check afterwards. Until it answers, the "
                    "worker's own view of the configuration is unverified.")

        if not isinstance(data, dict):
            return (WARN, "the worker returned an unexpected payload",
                    "Restart the worker so it runs the current code.")

        problems = []
        if data.get("probe_version") != PROBE_VERSION:
            problems.append(
                f"worker code version {data.get('probe_version')} != web "
                f"{PROBE_VERSION}")
        missing = [name for name, present in (data.get("env_keys") or {}).items()
                   if not present]
        if missing:
            problems.append("worker cannot see " + ", ".join(sorted(missing)))
        if data.get("storage_writable") is False:
            problems.append(
                f"worker cannot write to {data.get('storage_root')}")

        host = data.get("hostname", "?")
        if problems:
            return (FAIL, "; ".join(problems),
                    "The worker is a separate process. It reads the "
                    "environment ONCE, at start.\n"
                    "  sudo systemctl restart fundos-worker\n"
                    "If the key is set for the web process but not the "
                    "worker, check that the worker unit file loads the same "
                    "environment file (EnvironmentFile= in "
                    "deploy/systemd/fundos-worker.service).")
        return (PASS,
                f"{host} sees the same code and credentials as the web app",
                "")

    def _storage(self):
        """Where uploaded documents are written."""
        import os
        import tempfile
        from django.conf import settings

        backend = getattr(settings, "FUNDOS_STORAGE_BACKEND", "") or ""
        if backend and backend != "local":
            bucket = getattr(settings, "FUNDOS_STORAGE_BUCKET", "") or ""
            if not bucket:
                return (FAIL, f"backend={backend} but no bucket is set",
                        "Set FUNDOS_STORAGE_BUCKET, or switch "
                        "FUNDOS_STORAGE_BACKEND to local.")
            return (PASS, f"{backend} bucket {bucket}", "")

        root = getattr(settings, "FUNDOS_STORAGE_LOCAL_ROOT", "") or ""
        if not root:
            return (FAIL, "local storage with no root directory set",
                    "Set FUNDOS_STORAGE_LOCAL_ROOT to a durable path outside "
                    "the release directory.")

        ephemeral = _looks_ephemeral(root)
        try:
            os.makedirs(root, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root):
                pass
        except Exception as e:
            return (FAIL, f"{root} is not writable: {e}",
                    "Grant the service account write access to this path.")

        if ephemeral:
            return (FAIL, f"{root} is inside the deployment directory",
                    "Uploaded documents written here are ORPHANED by the next "
                    "deployment. Set FUNDOS_STORAGE_LOCAL_ROOT to a durable "
                    "path, move existing files there, restart app and worker.")
        return (PASS, f"local {root}", "")

    def _logfile(self):
        from django.conf import settings

        log_dir = getattr(settings, "LOG_DIR", None)
        if not log_dir:
            return (WARN, "no diagnostic log directory configured",
                    "Set FUNDOS_LOG_DIR.")
        path = log_dir / "silk_generation.log"
        if _looks_ephemeral(str(log_dir)):
            return (WARN, f"{log_dir} is inside the deployment directory",
                    "Diagnostic history is lost at the next deployment. Set "
                    "FUNDOS_LOG_DIR to a durable path.")
        # Confirm the HANDLER is attached, not merely that a file exists.
        # The file is created when the process starts; whether anything ever
        # reaches it depends on the logging configuration, and a settings
        # module that replaces the LOGGING dict instead of extending it drops
        # the handler while leaving the file in place.
        import logging.handlers
        attached = any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in logging.getLogger("fundos.generation").handlers)
        if not attached:
            return (FAIL, "the fundos.generation logger has no file handler",
                    "Trace lines go to stdout only and no generation log is "
                    "kept. This settings module replaces the LOGGING dict "
                    "from base.py instead of extending it — the "
                    "'generation_file' handler and the four fundos.* logger "
                    "definitions are being dropped.")
        if not path.exists():
            return (WARN, f"{path} not created yet (written on first run)",
                    "No action needed before the first generation.")
        size = path.stat().st_size
        if size == 0:
            return (PASS,
                    f"{path} — handler attached, empty (nothing generated "
                    "since the last restart)", "")
        return (PASS, f"{path} ({size // 1024} KB)", "")

    def _rapidfuzz(self):
        try:
            import rapidfuzz
            return (PASS, f"available ({rapidfuzz.__version__})", "")
        except ImportError:
            return (FAIL, "not installed",
                    "Run: pip install -r requirements.txt. Without it, "
                    "investor name matching silently degrades to exact "
                    "matches only.")

    # ==================================================================
    # AI provider
    # ==================================================================
    def _ai_mocked(self):
        from django.conf import settings
        from fundos.config.feature_flags import ai_mocked
        if ai_mocked():
            return (FAIL,
                    "demonstration mode is ON — no real AI model is called",
                    "Admin -> Application Configuration -> switch OFF "
                    "'AI mocked'. On a development server this is expected "
                    f"(current settings module: {settings.SETTINGS_MODULE}).")
        return (PASS, "live (demonstration mode is off)", "")

    def _endpoints(self):
        from fundos.llm.models import LLMEndpoint
        total = LLMEndpoint.objects.count()
        active = list(LLMEndpoint.objects.filter(is_active=True))
        if total == 0:
            return (FAIL, "no AI endpoint is configured",
                    "Run: python manage.py seed_platform_config")
        if not active:
            return (FAIL, f"{total} endpoint(s) configured, none active",
                    "Admin -> LLM Integration: mark an endpoint active. An "
                    "inactive endpoint is skipped silently.")
        codes = ", ".join(e.code for e in active)
        kinds = {e.provider_kind for e in active}
        if len(kinds) > 1:
            return (WARN,
                    f"{len(active)} of {total} active across providers: {codes}",
                    "More than one provider is active. That is legal, but a "
                    "fallback can send one vendor's model string to another "
                    "vendor's API. Deactivate the endpoints you are not "
                    "using, or make sure no binding crosses providers.")
        return (PASS, f"{len(active)} of {total} active ({codes})", "")

    def _endpoints_in_use(self):
        """Endpoint codes a Company Profile call can actually reach.

        The three tier profiles supply the endpoint when they name one; the
        role bindings supply it otherwise. An active endpoint that neither
        names is idle — worth a warning, not a failure.
        """
        from fundos.llm.models import (LLMConfigProfile, LLMRoleBinding,
                                       TenantLLMTier)
        codes = set()
        for tier in ("simple", "advanced", "judgment"):
            code = TenantLLMTier.resolve_code(tier, None)
            if not code:
                continue
            profile = LLMConfigProfile.objects.filter(code=code).first()
            if profile and profile.endpoint_id:
                codes.add(profile.endpoint_id)
        roles = [r for r, _, _ in PROFILE_ROLES]
        for b in LLMRoleBinding.objects.filter(
                role__in=roles).select_related("primary_endpoint"):
            if b.primary_endpoint:
                codes.add(b.primary_endpoint.code)
        return codes

    def _credentials(self):
        """Endpoint rows record the NAME of an env var. Read the value."""
        from fundos.llm.models import KINDS_REQUIRING_KEY, LLMEndpoint

        active = LLMEndpoint.objects.filter(is_active=True)
        if not active.exists():
            return (FAIL, "no active endpoint to check", "See 'Endpoints'.")

        in_use = self._endpoints_in_use()
        missing, idle, unset_name, ok = [], [], [], []
        for endpoint in active:
            if endpoint.provider_kind not in KINDS_REQUIRING_KEY:
                continue
            if not endpoint.api_key_env_var:
                unset_name.append(endpoint.code)
            elif not endpoint.has_api_key_in_env():
                target = missing if endpoint.code in in_use else idle
                target.append(f"{endpoint.code} needs "
                              f"${endpoint.api_key_env_var}")
            else:
                ok.append(f"{endpoint.code}/${endpoint.api_key_env_var}")

        if unset_name:
            return (FAIL,
                    f"no API key env var named on: {', '.join(unset_name)}",
                    "Admin -> LLM Integration -> Endpoints: set 'API key env "
                    "var' to the NAME of the variable, e.g. GEMINI_API_KEY.")
        if missing:
            return (FAIL, "; ".join(missing),
                    "The endpoint names an environment variable that is not "
                    "set in this process. Add it to /etc/fundos/fundos.env "
                    "(or your equivalent) and restart BOTH the web "
                    "application and the Celery worker:\n"
                    "  sudo systemctl restart fundos-web fundos-worker\n"
                    "Then re-run: python manage.py seed_platform_config\n"
                    "(seeding binds roles and selects the provider only when "
                    "a key is present, so it does nothing without one).")
        if idle:
            return (WARN,
                    "active but keyless and unused: " + "; ".join(idle),
                    "No profile call routes to these, so nothing is broken "
                    "today — but an endpoint that is active and cannot "
                    "authenticate is a trap for whoever repoints a tier next. "
                    "Admin -> LLM Integration -> Endpoints: untick 'is "
                    "active' on the providers you are not using.")
        if not ok:
            return (PASS, "no keyed provider active", "")
        return (PASS, "present for " + ", ".join(ok), "")

    def _timeout(self):
        """A search call legitimately runs for one to three minutes."""
        from fundos.llm.models import LLMEndpoint
        tight = [f"{e.code} ({e.timeout_seconds}s)"
                 for e in LLMEndpoint.objects.filter(is_active=True)
                 if (e.timeout_seconds or 0) < 120]
        if tight:
            return (WARN, "below 120s: " + ", ".join(tight),
                    "A research batch performs live web searches and can run "
                    "for one to three minutes; the synthesis call reads the "
                    "whole dossier and is slower still. A tight timeout "
                    "appears in the log as an AI failure and reads like a "
                    "broken integration. Admin -> Endpoints: set 120 seconds "
                    "or more.")
        return (PASS, "120s or more on every active endpoint", "")

    def _profile_bindings(self):
        """Every role Company Profile can call, not just the ones a run uses.

        `llm_generate` raises LLMUnavailable for an unbound role rather than
        falling back, so an unbound role is not a degraded section — it is a
        missing one. Retired roles (see RETIRED_ROLES) are deliberately not
        checked: nothing calls them, so a broken binding on one is not a fault
        to report, and reporting it would bury the ones that matter.
        """
        from fundos.llm.models import LLMRoleBinding

        bindings = {b.role: b for b in LLMRoleBinding.objects
                    .select_related("primary_endpoint", "fallback_endpoint")}

        missing_essential, missing_other, broken, mocked, crossed = [], [], [], [], []
        for role, priority, _purpose in PROFILE_ROLES:
            b = bindings.get(role)
            if b is None:
                (missing_essential if priority == "ESSENTIAL"
                 else missing_other).append(role)
                continue
            if b.is_mocked:
                mocked.append(role)
            ep = b.primary_endpoint
            if ep is None or not ep.is_active:
                broken.append(f"{role} (endpoint inactive/missing)")
            elif not ep.has_api_key_in_env():
                broken.append(f"{role} ({ep.code} has no key in env)")
            fb = b.fallback_endpoint
            if fb is not None and ep is not None and \
                    fb.provider_kind != ep.provider_kind:
                crossed.append(f"{role} ({ep.code}->{fb.code})")

        seed_hint = (
            "The seeder creates a binding for every role automatically, but "
            "ONLY when an endpoint has a usable API key. Set the key first, "
            "then:\n"
            "  sudo systemctl restart fundos-web fundos-worker\n"
            "  python manage.py seed_platform_config\n"
            "Or bind them by hand: Admin -> LLM Integration -> Role bindings, "
            "primary endpoint = your provider, 'is mocked' UNTICKED.")

        if missing_essential:
            return (FAIL,
                    f"{len(missing_essential)} essential role(s) unbound: "
                    + ", ".join(missing_essential),
                    "A role with no binding raises an error — that part of "
                    "the profile is never generated, and the page loads "
                    "empty.\n" + seed_hint)
        if mocked:
            return (FAIL, "flagged mocked: " + ", ".join(mocked),
                    "A mocked role returns canned sample content regardless "
                    "of the global AI setting, and it is indistinguishable "
                    "from real output downstream. Admin -> Role bindings: "
                    "untick 'is mocked'.")
        if broken:
            return (FAIL, "; ".join(broken),
                    "Point these bindings at an active endpoint whose API key "
                    "is set.\n" + seed_hint)
        if missing_other:
            return (WARN,
                    f"{len(missing_other)} non-essential role(s) unbound: "
                    + ", ".join(missing_other),
                    "Generation will complete, but these features fail when "
                    "used.\n" + seed_hint)
        if crossed:
            return (WARN, "cross-provider fallback: " + ", ".join(crossed),
                    "A fallback on a different provider would receive the "
                    "primary's model string. Clear the fallback, or set one "
                    "of the same provider.")
        return (PASS, f"all {len(PROFILE_ROLES)} profile roles bound, "
                      "none mocked", "")

    def _other_bindings(self):
        """Roles outside Company Profile — same failure mode, wider blast.

        Retired roles are excluded. They are still declared so an install that
        rolls back keeps resolving them, but nothing calls them, and counting
        them as "unbound" would raise a warning no one can act on.
        """
        from fundos.llm.models import LLM_ROLES, LLMRoleBinding
        bound = set(LLMRoleBinding.objects.values_list("role", flat=True))
        skip = {r for r, _, _ in PROFILE_ROLES} | set(RETIRED_ROLES)
        others = [r for r in LLM_ROLES if r not in skip]
        missing = [r for r in others if r not in bound]
        if missing:
            return (WARN,
                    f"{len(missing)} of {len(others)} unbound "
                    f"(e.g. {', '.join(missing[:4])})",
                    "These drive strategy, materials, assessment and investor "
                    "features rather than Company Profile. Each raises "
                    "LLMUnavailable when used. Re-running "
                    "seed_platform_config binds them all.")
        return (PASS, f"all {len(others)} other roles bound", "")

    def _output_ceilings(self):
        """The role cap only ever LOWERS the binding's allowance.

        A binding that states no ceiling now uses the role's designed budget,
        so this check only fires where an administrator has actively capped a
        role below it. The symptom of a cap that is too low is a truncated
        JSON body: either a JSONDecodeError ("Unterminated string"), or a
        repaired fragment that populates two fields out of four and reads as
        "the model returned nothing for this block".
        """
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS
        from fundos.llm.models import LLMRoleBinding

        # Every role with a DESIGNED cap, not just the profile ones. A role
        # appears in ROLE_MAX_OUTPUT_TOKENS precisely because someone sized its
        # output deliberately, and a binding that undercuts that truncates the
        # response silently — which is as bad for a teaser or an IM as it is
        # for a profile. Severity still comes from whether a profile-essential
        # role is among them, so widening the net does not escalate everything.
        short = []
        for b in LLMRoleBinding.objects.filter(
                role__in=list(ROLE_MAX_OUTPUT_TOKENS)):
            designed = ROLE_MAX_OUTPUT_TOKENS.get(b.role)
            if not designed:
                continue
            allowed = b.max_output_tokens or 0
            if allowed and allowed < designed:
                short.append((b.role, allowed, designed))

        if not short:
            return (PASS, "every role can use its designed output budget", "")
        detail = "; ".join(f"{role}: {allowed} of {designed}"
                           for role, allowed, designed in short)
        essential = [r for r, _, _ in short if r in ESSENTIAL_ROLES]
        remedy = (
            "The effective ceiling is the LOWER of the role binding's max "
            "output tokens and the role's designed cap. A response cut off "
            "at the ceiling is not reported as an error — it is parsed, "
            "repaired if possible, and logged as a successful call with a "
            "thin result. Clearing the binding value restores the designed "
            "budget:\n"
            "  python manage.py shell -c \"\n"
            "from fundos.llm.models import LLMRoleBinding as B\n"
            "from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS as C\n"
            "B.objects.filter(role__in=list(C)).update("
            "max_output_tokens=None)\n"
            "print('cleared — each role now uses its designed budget')\"\n"
            "Or blank the field per role in Admin -> LLM Integration -> "
            "Role bindings.")
        return (FAIL if essential else WARN, detail, remedy)

    # ==================================================================
    # Model chain
    # ==================================================================
    def _tier_chain(self, tier):
        """Resolve one tier end to end: tier -> profile -> model -> endpoint.

        A binding can exist and still be unusable. The model actually used
        comes from the config profile for the tier, not from the binding, so a
        healthy-looking binding can sit in front of a retired model name, a
        profile with web search off, or an endpoint belonging to a different
        vendor. Each of those fails only when the call is made.
        """
        from fundos.llm.models import (LLMConfigProfile, LLMEndpoint,
                                       LLMModelCatalog, TenantLLMTier)

        # Deliberately NOT "and is the right role on this tier?" — that is
        # `_prompt_tiers`' job. Asking it here made a role-to-tier mismatch
        # short-circuit the chain resolution below, so a retired model or a
        # cross-provider endpoint went unreported behind it. One check, one
        # question.
        code = TenantLLMTier.resolve_code(tier, None)
        if not code:
            return (FAIL, f"no config profile resolves for the {tier} tier",
                    f"Admin -> Tenant LLM tiers: point the global '{tier}' "
                    "row (blank tenant) at a config profile.")
        profile = LLMConfigProfile.objects.filter(code=code).first()
        if profile is None:
            return (FAIL, f"config profile {code!r} does not exist",
                    "Admin -> Tenant LLM tiers: point the row at a profile "
                    "that exists.")
        if not profile.is_active:
            return (FAIL, f"config profile {code} is inactive",
                    "Admin -> Config profiles: activate it, and deactivate "
                    "the competing profile for the same tier.")

        # Web search is part of the CHAIN for the advanced tier, not a
        # refinement of it: a tier whose entire purpose is retrieval, with
        # retrieval switched off, generates an empty-looking profile while
        # every other line of this report stays green.
        if tier == "advanced" and not profile.web_search:
            return (FAIL, f"{code} has web search OFF",
                    "Admin -> Config profiles: switch web search ON. Without "
                    "it the model cannot research anything and the profile "
                    "generates almost empty.")

        model = profile.model_string or ""
        if not model:
            return (FAIL, f"{code} has no model string",
                    "Admin -> Config profiles: choose a model.")

        endpoint = None
        if profile.endpoint_id:
            endpoint = LLMEndpoint.objects.filter(
                code=profile.endpoint_id).first()
            if endpoint is None:
                return (FAIL,
                        f"{code} points at endpoint {profile.endpoint_id!r}, "
                        "which does not exist",
                        "Admin -> Config profiles: set the endpoint to one "
                        "that exists, matching the profile's provider.")
            if endpoint.provider_kind != profile.provider:
                return (FAIL,
                        f"{code} is provider {profile.provider} but endpoint "
                        f"{endpoint.code} is {endpoint.provider_kind}",
                        "The model string would be sent to the wrong vendor's "
                        "API. Point the profile at an endpoint of the same "
                        "provider.")
            if not endpoint.is_active:
                return (FAIL, f"{code} points at inactive endpoint "
                              f"{endpoint.code}",
                        "Admin -> Endpoints: activate it, or repoint the "
                        "profile.")

        known = LLMModelCatalog.objects.filter(
            provider=profile.provider, model_string=model).first()
        if known is None:
            available = list(
                LLMModelCatalog.objects
                .filter(provider=profile.provider, is_active=True)
                .values_list("model_string", flat=True)[:6])
            return (FAIL,
                    f"model {model!r} is not in the catalog for "
                    f"{profile.provider}",
                    "This is usually a retired or mistyped model name and "
                    "will fail at call time with a 404 that reads like a "
                    f"broken integration. Set {code} to one of: "
                    f"{', '.join(available) or '(catalog empty)'}.")
        if not known.is_active:
            return (WARN,
                    f"{code} -> {model} is hidden/retired in the catalog",
                    f"Choose a current model. Catalog note: {known.notes[:140]}")

        detail = (f"{tier} -> {code} -> {model} ({profile.provider}"
                  f"{'/' + endpoint.code if endpoint else ''}"
                  f"{', search on' if profile.web_search else ''})")
        return (PASS, detail, "")

    def _model_chain(self):
        """Backwards-compatible alias for the advanced-tier resolution.

        Kept because existing tests and runbooks call it by this name; the
        advanced tier is the one the research call depends on.
        """
        return self._tier_chain("advanced")

    def _search_capability(self):
        """The advanced tier exists to search. Confirm it can, and is bounded."""
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        code = TenantLLMTier.resolve_code("advanced", None)
        profile = LLMConfigProfile.objects.filter(code=code).first() if code \
            else None
        if profile is None:
            return (FAIL, "the advanced tier does not resolve",
                    "See 'Advanced tier (research)'.")

        # v27.3 — the advanced tier's search is now a CONTRACT, not a
        # checkbox: capabilities_payload() forces web_search on for this tier
        # regardless of the profile's own toggle, and clean() refuses to save
        # a profile bound to a tier it cannot honour. So the checkbox is no
        # longer worth checking; what matters is whether the capability
        # actually survives to the wire.
        caps = profile.capabilities_payload()
        if "web_search" not in caps:
            return (FAIL,
                    f"web search is ticked but provider {profile.provider} "
                    "does not expose it through this profile",
                    "The capability is dropped before the call. Choose a "
                    "provider/profile combination that supports search "
                    "grounding.")

        notes = []
        # Gemini's google_search grounding has no max_uses parameter, so the
        # cap is communicated in the prompt. With no cap set, nothing is said
        # and nothing is reserved against the budget.
        from fundos.llm.adapter import search_enforcement_for
        enforcement = None
        if profile.endpoint_id:
            from fundos.llm.models import LLMEndpoint
            ep = LLMEndpoint.objects.filter(code=profile.endpoint_id).first()
            if ep:
                enforcement = search_enforcement_for(ep.provider_kind)
        if not profile.max_uses:
            notes.append(
                "no max_uses set — searches are unbounded"
                + (" and this provider only enforces the cap by stating it in "
                   "the prompt, so nothing is stated at all"
                   if enforcement == "advisory" else ""))
        # responseSchema alongside google_search — UNRESOLVED, and the two
        # halves of this codebase used to assert opposite things about it:
        # this check called the combination rejected, while the adapter's
        # no-search warning told operators the combination was supported and
        # not to disable it. Both were written from reasoning, neither from
        # a measurement, and an operator following one was contradicted by
        # the other. Stated as an open question until the experiment settles
        # it -- see tools/diagnose_gemini_search.py, which tests exactly this
        # against the live API in eight calls.
        if profile.structured_output and profile.output_schema and \
                profile.provider == "gemini":
            notes.append(
                "a response schema is set alongside search — if this tier "
                "reports searches=0 while caps_sent contains web_search, run "
                "tools/diagnose_gemini_search.py to test whether the schema "
                "is suppressing the tool on this model")

        # Thinking is what lets a model DECIDE to search on Gemini, where
        # google_search is model-decided. The v27.3 contract supplies a
        # budget automatically, so a zero here means something overrode it.
        if (caps.get("thinking") or {}).get("budget_tokens") == 0:
            notes.append(
                "thinking budget is 0 on a searching tier — google_search is "
                "model-decided, so a model with no deliberation budget has "
                "no step in which to choose to call it")

        if notes:
            return (WARN, "; ".join(notes),
                    "Admin -> Config profiles -> the advanced profile: set "
                    "max_uses (6 is a reasonable default for a dossier) and "
                    "leave the thinking budget non-zero.")
        return (PASS,
                f"search on, capped at {profile.max_uses or 'default'} "
                f"searches, thinking budget "
                f"{(caps.get('thinking') or {}).get('budget_tokens')}", "")

    def _thinking(self):
        """Gemini reasons by default and bills the thoughts to the output.

        On Gemini 2.5 and later, thinking is ON unless a budget says
        otherwise, and thought tokens are deducted from maxOutputTokens
        WITHOUT appearing in the completion-token count. A tier left at the
        provider default can therefore spend its whole allowance thinking and
        return a truncated body that reports zero completion tokens.
        """
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        unstated = []
        for tier in ("simple", "advanced", "judgment"):
            code = TenantLLMTier.resolve_code(tier, None)
            if not code:
                continue
            profile = LLMConfigProfile.objects.filter(code=code).first()
            if profile is None or profile.provider != "gemini":
                continue
            caps = profile.capabilities_payload()
            thinking = caps.get("thinking")
            if not isinstance(thinking, dict) or \
                    thinking.get("budget_tokens") is None:
                unstated.append(f"{tier}={profile.model_string}")

        if not unstated:
            return (PASS, "stated explicitly on every Gemini tier", "")
        return (WARN,
                "provider default (thinking ON) on: " + ", ".join(unstated),
                "Set 'thinking mode' on each Gemini config profile: 'none' "
                "for the simple and advanced tiers, 'tokens' with a budget "
                "for judgement. Leaving it unstated is not the same as "
                "leaving it off — Gemini's default is on, and the thoughts "
                "come out of the same budget as the answer.")

    def _retirement(self):
        """Catalog notes carry published end dates. Read them.

        A model with a retirement date passes every other check until the day
        it stops answering, at which point the failure looks like a broken
        integration rather than a scheduled change that was in the database
        the whole time.
        """
        from fundos.llm.models import (LLMConfigProfile, LLMModelCatalog,
                                       TenantLLMTier)

        today = dt.date.today()
        findings = []
        for tier in ("simple", "advanced", "judgment"):
            code = TenantLLMTier.resolve_code(tier, None)
            if not code:
                continue
            profile = LLMConfigProfile.objects.filter(code=code).first()
            if profile is None:
                continue
            row = LLMModelCatalog.objects.filter(
                provider=profile.provider,
                model_string=profile.model_string).first()
            if row is None:
                continue
            when = _retirement_date(row.notes)
            if when is None:
                continue
            days = (when - today).days
            findings.append((days, tier, profile.model_string, when))

        if not findings:
            return (PASS, "no tier is on a model with a published end date", "")

        findings.sort()
        soonest = findings[0]
        summary = "; ".join(
            f"{tier}={model} ends {when:%d-%b-%Y} ({days}d)"
            for days, tier, model, when in findings)
        remedy = (
            "The catalog records a retirement date for these models. Move "
            "each tier to a current model before the date:\n"
            "  python manage.py shell -c \"from fundos.llm.models import "
            "LLMConfigProfile as P; "
            "P.objects.filter(code='tier.advanced.<provider>')"
            ".update(model_string='<current-model>')\"\n"
            "Admin -> Config profiles shows the alternatives, and the catalog "
            "notes carry the recommended replacement for each.")
        if soonest[0] < 0:
            return (FAIL, f"PAST its retirement date: {summary}", remedy)
        if soonest[0] <= 120:
            return (WARN, summary, remedy)
        return (PASS, f"earliest end date is {soonest[0]} days away "
                      f"({soonest[2]})", "")

    def _duplicate_tiers(self):
        """Duplicate global rows broke seeding on earlier builds."""
        from collections import Counter
        from fundos.llm.models import TenantLLMTier

        rows = TenantLLMTier.objects.filter(tenant_id=None)
        counts = Counter(rows.values_list("tier", flat=True))
        dupes = [t for t, n in counts.items() if n > 1]
        missing = [t for t in ("simple", "advanced", "judgment")
                   if counts.get(t, 0) == 0]
        if dupes:
            return (FAIL, f"duplicate global rows for: {', '.join(dupes)}",
                    "Two global rows for one tier means resolution is "
                    "arbitrary. Delete the extras in Admin -> Tenant LLM "
                    "tiers, then run: python manage.py migrate")
        if missing:
            return (FAIL, f"no global row for: {', '.join(missing)}",
                    "Every tenant without its own rows falls back to these. "
                    "Run: python manage.py seed_platform_config")
        # A row that exists but is switched off resolves to None exactly as a
        # missing row does, while still appearing in the admin list — which
        # reads as "configured". This check counted rows without regard to
        # is_active, so it passed while the tier it describes failed.
        inactive = sorted(rows.filter(is_active=False)
                          .values_list("tier", flat=True))
        if inactive:
            return (FAIL,
                    f"global row is switched OFF for: {', '.join(inactive)}",
                    "An inactive row resolves to nothing, and a role on that "
                    "tier raises LLMUnavailable rather than falling back. "
                    "Admin -> Tenant LLM tiers: tick 'is active' on the "
                    "global row, or run: python manage.py "
                    "seed_platform_config")
        return (PASS, "one active global row for each of the three tiers", "")

    # ==================================================================
    # Profile pipeline
    # ==================================================================
    def _sections(self):
        """Compared against the SHIPPED contract, never a hardcoded count.

        This used to read `if count < 22`, a number taken from the section set
        of the day. When the generated-profile contract moved to seventeen
        sections, a correctly seeded install started reporting its registry as
        stale — a check that fails on the right answer is worse than no check,
        because the remedy it prints has already been run.

        Comparing key sets instead of counts also says WHICH sections are
        missing, which is the actionable part, and cannot go stale: the shipped
        contract is the same constant the generator builds its prompt from.
        """
        from fundos.platformcfg.services import profile_sections
        from fundos.profile import schema

        registered = {s.section_key for s in profile_sections()}
        if not registered:
            return (FAIL, "no profile sections registered",
                    "Run: python manage.py seed_platform_config")

        # Storage keys, because that is what ProfileSectionConfig rows carry.
        expected = {s["storage_key"] for s in schema.SHIPPED_SECTIONS}
        missing = sorted(expected - registered)
        if missing:
            return (FAIL,
                    f"{len(missing)} section(s) not registered: "
                    + ", ".join(missing),
                    "The generator writes these and the API serves them, so "
                    "until they exist they cannot be saved and the profile "
                    "reads short. Run: python manage.py seed_platform_config")

        # More rows than the contract is normal, not a fault: sections retired
        # from the generated profile are deactivated rather than deleted, and
        # an administrator may have added their own.
        extra = len(registered) - len(expected)
        detail = f"{len(registered)} sections registered"
        if extra > 0:
            detail += f" ({len(expected)} from the shipped contract, {extra} additional)"
        return (PASS, detail, "")

    def _prompts(self):
        from fundos.platformcfg.models import PromptTemplate
        active = PromptTemplate.objects.filter(is_active=True).count()
        if active == 0:
            return (WARN, "no admin prompt rows — using shipped defaults",
                    "This works, but nothing is editable without a code "
                    "change. Run: python manage.py seed_platform_config")
        if active < 28:
            return (WARN, f"{active} active prompts (28 expected)",
                    "Roles without a row fall back to the shipped default, "
                    "so this is safe but not editable. Run: "
                    "python manage.py seed_platform_config")
        # A DB row WINS over the shipped default, so a prompt improved in
        # code has no effect until the row is refreshed. That has now caused
        # two silent regressions: a deep-extract prompt rewritten to ask the
        # model to search kept producing searches=0, because the row still
        # carried the old text and nothing said so.
        stale = []
        try:
            from fundos.llm.default_prompts import DEFAULT_PROMPTS
            for row in PromptTemplate.objects.filter(is_active=True):
                role = getattr(row, "role", "") or ""
                shipped = DEFAULT_PROMPTS.get(role)
                if not shipped:
                    continue
                if (shipped.get("system") or "").strip() != \
                        (getattr(row, "system_prompt", "") or "").strip():
                    stale.append(role)
        except Exception:
            stale = []
        if stale:
            shown = ", ".join(sorted(stale)[:4])
            more = f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
            return (WARN,
                    f"{active} installed, {len(stale)} differ from the "
                    f"shipped text: {shown}{more}",
                    "A database prompt row overrides the shipped default "
                    "entirely, so an improvement made in code does not reach "
                    "these roles. This is EXPECTED if you edited them "
                    "deliberately. If not, take the new text with:\n"
                    "  python manage.py seed_platform_config --reset-prompts\n"
                    "which overwrites every prompt row with the shipped "
                    "version — review your edits first.")
        return (PASS, f"{active} prompts installed", "")

    def _prompt_tiers(self):
        """A tier override changes cost and capability, not just wording.

        Moving a prompt from simple to advanced switches web search on for
        every call of that role. Moving deep-extract to simple switches it
        off, and research stops happening while generation still succeeds.
        """
        from fundos.llm.default_prompts import get_tier
        from fundos.platformcfg.models import PromptTemplate

        drift = []
        for row in PromptTemplate.objects.filter(is_active=True):
            override = (row.tier or "").strip().lower()
            if not override:
                continue
            shipped = (get_tier(row.role) or "simple").strip().lower()
            if override != shipped:
                drift.append(f"{row.role}: {shipped} -> {override}")

        if not drift:
            return (PASS, "no prompt overrides the shipped tier", "")
        costly = [d for d in drift if d.endswith("-> advanced")]
        if costly:
            # v28 — the schema consequence, stated. Since v27.5 an advanced
            # tier drops responseSchema, because a schema suppresses
            # google_search on Gemini (measured 19 Aug 2026: 0/4 searched
            # with a schema, 4/4 without). An extraction role moved to
            # advanced therefore loses its structured output, which is a
            # correctness problem and not merely a cost one — and it is
            # invisible from the admin screen, which still shows the tick.
            return (WARN, "; ".join(drift),
                    "Every role moved TO advanced runs web search on every "
                    "call AND LOSES ITS responseSchema — a schema suppresses "
                    "google_search on Gemini, so the two cannot both be on. "
                    "If any of these roles parses structured output "
                    "(company_profile_section, _records, _structured, "
                    "_field), the override is degrading it, not just adding "
                    "cost. Admin -> AI Prompts: blank the tier field "
                    "to return to the shipped default.")
        downgraded = [d for d in drift if d.endswith("-> simple")]
        if downgraded:
            return (WARN, "; ".join(drift),
                    "A role moved TO simple gets a thinking budget of 0. For "
                    "a judgement role that means reasoning with the "
                    "reasoning switched off: fast, cheap, confident and "
                    "shallow, with nothing downstream indicating it was not "
                    "allowed to think. Admin -> AI Prompts: blank the tier "
                    "field to restore the shipped default.")
        return (WARN, "; ".join(drift),
                "Admin -> AI Prompts: blank the tier field to return a role "
                "to its shipped default. Record why in the Purpose field if "
                "the override is deliberate.")

    def _research(self):
        from fundos.config.feature_flags import enable_research_extensions
        from fundos.research.adapters.registry import enabled_sources

        enabled = list(enabled_sources())
        wanted = {"website", "market", "competitor", "industry",
                  "public_funding"}
        missing = sorted(wanted - set(enabled))
        if not enabled:
            return (FAIL, "every research source is switched off",
                    "Admin -> Research Sources: enable website, market, "
                    "competitor, industry and public funding.")
        if missing:
            return (WARN,
                    f"enabled: {', '.join(enabled)}; off: {', '.join(missing)}",
                    "Admin -> Research Sources: enable the sources listed as "
                    "off, unless that is deliberate.")

        # Honesty about what "enabled" buys today. Most adapters return
        # fixtures pending feed ratification; the real research is the
        # advanced-tier search grounding inside the deep-extract call.
        live = [s for s in enabled if s not in STUB_SOURCES]
        stubs = [s for s in enabled if s in STUB_SOURCES]
        detail = f"{len(enabled)} enabled ({len(live)} live, {len(stubs)} fixture)"
        if enable_research_extensions():
            return (WARN, detail + " — extensions on",
                    "The patents/news/awards/hiring adapters currently return "
                    "fixtures pending feed ratification, so the extensions add "
                    "run time without adding facts. Admin -> Application "
                    "Configuration: switch ENABLE_RESEARCH_EXTENSIONS off "
                    "unless you are testing them. Actual research comes from "
                    "the advanced tier's search grounding.")
        return (PASS, detail, "")

    def _flags(self):
        """The pipeline's dials, reported as the values a run will actually use.

        Reports what is CONFIGURED and what is EFFECTIVE, because they differ:
        concurrency is clamped to what the database can take, and on SQLite
        that clamp is 1. An operator who set concurrency to 5 and sees five
        batches running one after another needs to be told why here rather
        than deducing it from timings.
        """
        from fundos.profile.pipeline import questions
        from fundos.profile.pipeline import settings as pipeline_settings

        try:
            batches = questions.build_batches("Example Co", "")
        except Exception as exc:
            return (FAIL, f"the research question bank is unreadable: {exc}",
                    "Admin -> Research batches. Every run depends on this; "
                    "re-seed with `python manage.py seed_platform_config`.")

        total_questions = sum(len(b.questions) for b in batches)
        if not batches:
            return (FAIL, "no active research batches — nothing will be asked",
                    "Admin -> Research batches: activate at least one. With "
                    "none active a run has no web research to ground on and "
                    "will be refused unless documents supply the evidence.")

        configured = pipeline_settings.configured_research_concurrency()
        effective = pipeline_settings.research_concurrency()
        detail = (f"{len(batches)} batches / {total_questions} questions, "
                  f"concurrency {effective}")

        if effective < configured:
            return (WARN, detail + f" (configured {configured}, clamped)",
                    "This database cannot run the batches concurrently — "
                    "SQLite serialises writers and every batch writes, so "
                    "they are run one at a time instead. Correct, but slower "
                    "by roughly the number of batches. PostgreSQL lifts it.")
        if not pipeline_settings.document_model_enabled():
            return (WARN, detail + ", document model off",
                    "Scanned PDFs and image-only slides will contribute "
                    "nothing to the profile. Admin -> Application "
                    "Configuration: switch PROFILE_OCR_ENABLED on unless the "
                    "token cost is deliberate.")
        return (PASS, detail + ", document model on", "")

    # ==================================================================
    # Other modules
    # ==================================================================
    def _investors(self):
        try:
            from fundos.investors.models import Investor
            count = Investor.objects.count()
        except Exception as e:
            return (WARN, f"could not read the investor table: {e}", "")
        if count == 0:
            return (WARN,
                    "no investors imported (investor discovery only — does "
                    "NOT affect Company Profile)",
                    "The investor corpus is a supplied Excel workbook, not "
                    "something the system generates. Import it when you reach "
                    "investor discovery:\n"
                    "  python manage.py seed_investor_config\n"
                    "  python manage.py import_investor_workbook "
                    "/path/to/workbook.xlsx\n"
                    "The workbook must contain a sheet named 'Consol - "
                    "Investors' with headers on row 2. Expect roughly 7,777 "
                    "rows across 3,450 investors.")
        return (PASS, f"{count} investors loaded", "")


# ----------------------------------------------------------------------
def _looks_ephemeral(path):
    """True when a path sits inside a per-deployment directory."""
    text = str(path).replace("\\", "/").lower()
    markers = ("/releases/", "/release/", "/current/", "/versions/")
    return any(m in text for m in markers)


def _retirement_date(notes):
    """Parse a published end date out of a catalog note, or None.

    The catalog carries these as free text ('RETIRING 16-Oct-2026') rather
    than a column, so no migration is required to make them actionable.
    """
    if not notes:
        return None
    match = RETIREMENT_RE.search(notes)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return dt.datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date()
    except ValueError:
        return None
