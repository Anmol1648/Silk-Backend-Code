"""
Single-instance (overlap) lock for periodic Celery tasks — ported from
Gyain `dbal/task_locks.py` (tried & tested) per Doc 6 §7.

Semantics:
  * acquire `SET key token NX EX ttl`; if not acquired ⇒ skip this tick
    silently (a second beat tick during a run must not double-run);
  * release only if the per-run token matches (compare-and-delete Lua) —
    never release a lock a later run re-acquired after expiry;
  * fail-open: if Redis is unreachable, log and run anyway (availability
    over strictness);
  * wrapper signature is (*args, **kwargs) with the lock name held in the
    closure — nothing in __kwdefaults__ (obfuscation-safety lesson).
"""
import functools
import logging
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def _redis_client():
    """Build a redis client from the Celery broker URL (broker is Redis)."""
    import redis
    url = getattr(settings, "CELERY_BROKER_URL", None) or getattr(
        settings, "REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)


def _default_ttl():
    return int(getattr(settings, "CELERY_TASK_TIME_LIMIT", 900) or 900) + 120


def single_instance(name=None, expire=None):
    """Decorator: only ONE copy of the wrapped task executes at a time.

        @single_instance(name="evaluate_alerts", expire=600)
        def evaluate_alerts(): ...
    """
    def decorator(func):
        lock_name = f"fundos:lock:{name or func.__name__}"
        ttl = int(expire or _default_ttl())

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            token = uuid.uuid4().hex
            client = None
            acquired = False
            try:
                client = _redis_client()
                acquired = bool(client.set(lock_name, token, nx=True, ex=ttl))
                if not acquired:
                    logger.info("LOCK: '%s' already running — skipping this tick.",
                                lock_name)
                    return None
            except Exception as e:
                # Fail-open: run anyway, overlap protection lost for this tick.
                logger.warning("LOCK: Redis unavailable for '%s' (%s) — "
                               "running without overlap protection.", lock_name, e)
                client = None
                acquired = False

            try:
                return func(*args, **kwargs)
            finally:
                if client is not None and acquired:
                    try:
                        client.eval(_RELEASE_LUA, 1, lock_name, token)
                    except Exception as e:
                        logger.warning("LOCK: release failed for '%s': %s",
                                       lock_name, e)

        wrapper._lock_name = lock_name  # readable by ops/tests
        return wrapper

    return decorator
