"""A pool of interchangeable API keys for one endpoint, rotated per call.

Why this exists
---------------
``LLMEndpoint.api_key`` read one environment variable and returned its value
verbatim. That is correct for a single key and wrong for the way this pipeline
actually runs: a profile generation dispatches ten search-grounded calls, up to
five of them concurrently, and a provider's per-minute quota is per *key*. One
key therefore rate-limits a run that a handful of keys would complete — and a
429 from Gemini is not retryable by waiting a few hundred milliseconds, it is a
minute-scale window.

The prototype this pipeline was ported from had key rotation and it was
deliberately not carried over, on the grounds that the adapter already provided
endpoint resolution, retry, a breaker and a cost ledger. That reasoning held for
everything except this: rotation is not duplicated machinery, it is the one
piece the adapter never had.

What it does
------------
* Reads the pool from the endpoint's configured variable, and falls back to the
  plural sibling (``GEMINI_API_KEY`` → ``GEMINI_API_KEYS``) so a pool can be
  supplied without editing the seeded endpoint row.
* Accepts a list separated by commas, newlines, semicolons or whitespace.
* Hands out keys round-robin, so concurrent workers do not all take the first.
* Puts a key that reported exhaustion into a cooldown and skips it until it
  expires — a rate-limited key is not a broken key, it is a key with a time on
  it.

What it deliberately does not do
--------------------------------
No persistence and no cross-process coordination. The cooldown is per process,
which is the right scope: it is an optimisation to avoid re-hitting a key that
just said no, not a quota ledger. A second worker learning that lesson
independently costs one wasted call, whereas a shared store would put a
database round-trip in front of every LLM call to save it.

Keys are never logged. :func:`describe` exists so diagnostics can report pool
size and health without a key reaching a log line, a run record or a support
bundle.
"""
import logging
import os
import re
import threading
import time

logger = logging.getLogger("fundos.llm")

# A key that reported exhaustion is skipped for this long. Sized to a
# provider's per-minute quota window: long enough that the next call does not
# immediately re-hit the same wall, short enough that a pool of one recovers
# on its own rather than failing the run outright.
COOLDOWN_SECONDS = 65.0

# Splits on comma, semicolon, newline or run of whitespace. A key itself
# contains none of these.
_SPLIT_RE = re.compile(r"[,;\s]+")

# Obvious non-keys that appear in .env files as placeholders. Treated as unset
# rather than sent to the provider, because "your-key-here" produces a 400 that
# reads as a code fault instead of a configuration one.
_PLACEHOLDERS = {
    "", "changeme", "change-me", "todo", "none", "null", "unset",
    "your-key-here", "your_api_key", "xxx", "placeholder",
}

_lock = threading.Lock()
# {env_var: {"keys": [...], "raw": str, "cursor": int, "cooldown": {key: ts}}}
_pools = {}


def _plural(name):
    """``GEMINI_API_KEY`` → ``GEMINI_API_KEYS``. Empty when already plural."""
    if not name or name.endswith("S"):
        return ""
    return f"{name}S"


def _read_raw(env_var):
    """The configured variable's value, or its plural sibling's."""
    raw = os.environ.get(env_var) or ""
    if raw.strip():
        return raw, env_var
    sibling = _plural(env_var)
    if sibling:
        raw = os.environ.get(sibling) or ""
        if raw.strip():
            return raw, sibling
    return "", env_var


def _parse(raw):
    """Split a raw env value into a de-duplicated, order-preserving key list."""
    out, seen = [], set()
    for candidate in _SPLIT_RE.split(raw or ""):
        key = candidate.strip().strip("\"'")
        if not key or key.lower() in _PLACEHOLDERS:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _pool(env_var):
    """The live pool for ``env_var``, rebuilt when the environment changes.

    Re-reading rather than caching at import: the tests set and clear these
    variables per case, a management command may load an env file after import,
    and a cached empty pool would then be wrong for the life of the process.
    Comparing the raw string makes the rebuild cheap and exact.
    """
    raw, source = _read_raw(env_var)
    pool = _pools.get(env_var)
    if pool is None or pool["raw"] != raw:
        keys = _parse(raw)
        pool = {"keys": keys, "raw": raw, "cursor": 0, "cooldown": {},
                "source": source}
        _pools[env_var] = pool
        if keys:
            logger.info("LLM: %s provides a pool of %d key(s)%s",
                        source, len(keys),
                        "" if source == env_var else f" (read via {source})")
    return pool


def next_key(env_var):
    """One key from the pool, or ``None`` when the pool is empty.

    Prefers a key not in cooldown. If every key is cooling down, returns the one
    whose cooldown expires soonest rather than ``None``: the call may still fail
    with a 429, and letting the adapter's own retry and circuit breaker see that
    is better than manufacturing a "no key configured" error, which would send
    an operator to check their environment when their environment is fine.
    """
    if not env_var:
        return None
    now = time.monotonic()
    with _lock:
        pool = _pool(env_var)
        keys = pool["keys"]
        if not keys:
            return None
        if len(keys) == 1:
            return keys[0]

        cooldown = pool["cooldown"]
        count = len(keys)
        start = pool["cursor"]
        for offset in range(count):
            index = (start + offset) % count
            key = keys[index]
            until = cooldown.get(key)
            if until is not None and until <= now:
                del cooldown[key]
                until = None
            if until is None:
                pool["cursor"] = (index + 1) % count
                return key

        # Everything is cooling down — take the one that frees up first.
        soonest = min(keys, key=lambda k: cooldown.get(k, 0.0))
        logger.warning(
            "LLM: every key in the %s pool (%d) is in cooldown after reporting "
            "quota exhaustion; using the one that frees up soonest. Runs will "
            "be slow or fail until a quota window resets.",
            pool["source"], count)
        return soonest


def penalise(env_var, key, *, seconds=COOLDOWN_SECONDS, reason=""):
    """Mark ``key`` exhausted so the next call skips it.

    Called when a provider reports a rate limit or quota exhaustion for this
    specific key. A no-op for a single-key pool: there is nothing to rotate to,
    and skipping the only key would turn a retryable throttle into an outage.
    """
    if not env_var or not key:
        return
    with _lock:
        pool = _pool(env_var)
        if len(pool["keys"]) < 2:
            return
        pool["cooldown"][key] = time.monotonic() + max(1.0, float(seconds))
        available = len(pool["keys"]) - len(pool["cooldown"])
    logger.warning(
        "LLM: a key in the %s pool reported exhaustion%s — cooling it for %ss; "
        "%d of %d key(s) still available.",
        env_var, f" ({reason})" if reason else "", int(seconds),
        max(0, available), len(pool["keys"]))


def pool_size(env_var):
    """How many keys are configured. Zero means unset."""
    if not env_var:
        return 0
    with _lock:
        return len(_pool(env_var)["keys"])


def describe(env_var):
    """Pool health for diagnostics. **Never includes a key.**

    Diagnostics output reaches logs, admin pages and support bundles, so this
    returns counts and a fingerprint rather than material. The fingerprint is
    the last four characters — enough for an operator to confirm which key they
    pasted, useless to anyone who obtains the output.
    """
    if not env_var:
        return {"configured": False, "count": 0, "available": 0,
                "source": "", "tails": []}
    now = time.monotonic()
    with _lock:
        pool = _pool(env_var)
        keys = pool["keys"]
        cooling = sum(1 for k in keys
                      if pool["cooldown"].get(k, 0.0) > now)
        return {
            "configured": bool(keys),
            "count": len(keys),
            "available": len(keys) - cooling,
            "cooling_down": cooling,
            "source": pool["source"],
            "tails": [f"…{k[-4:]}" for k in keys[:25]],
        }


def reset(env_var=None):
    """Drop cached pools. For tests and for a config reload."""
    with _lock:
        if env_var is None:
            _pools.clear()
        else:
            _pools.pop(env_var, None)
