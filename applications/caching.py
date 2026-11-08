"""Response cache (Flask-Caching) that can never take an endpoint down.

The cache is an optimisation: when Redis is slow or unreachable every operation degrades
to "no cache" (a miss; writes and deletes are skipped) instead of raising. After a
failure the backend is skipped for ``COOLDOWN`` seconds, so a blackholed Redis costs one
socket timeout (2 s, see CACHE_OPTIONS) per cool-down, not one per request.

``safe_cache_clear()`` replaces ``cache.clear()`` after a database commit: the write has
already happened, so a cache failure must not turn it into a 500.
"""
import logging
import threading
import time

from flask_caching import Cache
from flask_caching.backends.rediscache import RedisCache

log = logging.getLogger(__name__)

cache = Cache()

COOLDOWN = 15.0  # seconds to skip Redis after an error


class ResilientRedisCache(RedisCache):
    """Redis cache whose operations swallow errors (see module docstring)."""

    _down_until = 0.0
    _guard = threading.Lock()

    def _call(self, name, default, *args, **kwargs):
        if time.monotonic() < self._down_until:
            return default
        try:
            return getattr(super(), name)(*args, **kwargs)
        except Exception as exc:
            with self._guard:
                first = time.monotonic() >= self._down_until
                self._down_until = time.monotonic() + COOLDOWN
            if first:
                log.warning("Cache backend unavailable (%s: %s); serving without cache for %.0f s",
                            type(exc).__name__, exc, COOLDOWN)
            return default

    def get(self, *a, **k):
        return self._call("get", None, *a, **k)

    def get_many(self, *keys):
        return self._call("get_many", [None] * len(keys), *keys)

    def set(self, *a, **k):
        return self._call("set", False, *a, **k)

    def set_many(self, *a, **k):
        return self._call("set_many", False, *a, **k)

    def add(self, *a, **k):
        return self._call("add", False, *a, **k)

    def has(self, *a, **k):
        return self._call("has", False, *a, **k)

    def delete(self, *a, **k):
        return self._call("delete", False, *a, **k)

    def delete_many(self, *a, **k):
        return self._call("delete_many", False, *a, **k)

    def clear(self):
        return self._call("clear", False)


def safe_cache_clear():
    """``cache.clear()`` that logs and swallows errors. Returns True if the cache was cleared."""
    try:
        cache.clear()
        return True
    except Exception:
        log.exception("Could not clear the response cache; entries expire on their own")
        return False
