"""LLM app Celery tasks (placeholder home for scheduled AI jobs; the adapter
itself is synchronous — generation endpoints enqueue via their own module
tasks)."""
from celery import shared_task

# Bumped whenever the probe payload changes. diagnose_config compares the
# value the WORKER returns against the value the WEB process has in code: a
# mismatch means the worker is running an older release, which is invisible
# in every other check and is a leading cause of "I fixed it and nothing
# changed".
PROBE_VERSION = 1


@shared_task(name="fundos.llm.tasks.registry_health_check")
def registry_health_check():
    from fundos.llm.adapter import registry_consistency_check
    return registry_consistency_check()


@shared_task(name="fundos.llm.tasks.config_probe")
def config_probe():
    """Report the WORKER's view of the configuration.

    The worker is a separate process with its own environment, its own copy
    of the code and its own working directory. Every check that runs in the
    web process describes the web process only — and the worker is what
    actually performs generation. An API key added to the environment file
    after the worker started is the classic case: the web process passes
    every check, the worker cannot authenticate, and the failure surfaces as
    an AI error with no configuration explanation.

    Returns plain JSON-safe types so it travels over the result backend
    without custom serialisers.
    """
    import os
    import socket
    import sys

    from django.conf import settings

    payload = {
        "probe_version": PROBE_VERSION,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "settings_module": getattr(settings, "SETTINGS_MODULE", ""),
        "cwd": os.getcwd(),
        "env_keys": {},
        "storage_root": str(getattr(settings, "FUNDOS_STORAGE_LOCAL_ROOT", "")),
        "storage_writable": None,
    }

    # Which credential env vars the worker can actually see. Names only —
    # never a value, because this payload is logged.
    try:
        from fundos.llm.models import LLMEndpoint
        for endpoint in LLMEndpoint.objects.filter(is_active=True):
            if endpoint.api_key_env_var:
                payload["env_keys"][endpoint.api_key_env_var] = bool(
                    os.environ.get(endpoint.api_key_env_var))
    except Exception as e:      # pragma: no cover - defensive
        payload["env_error"] = f"{type(e).__name__}: {e}"

    # The worker writes uploaded-document extracts; if its storage path is
    # not writable, documents are stored and never read.
    try:
        import tempfile
        root = payload["storage_root"]
        if root:
            os.makedirs(root, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root):
                pass
            payload["storage_writable"] = True
    except Exception as e:
        payload["storage_writable"] = False
        payload["storage_error"] = f"{type(e).__name__}: {e}"

    return payload
