"""S5 (cache/limiter/counters when Redis is unavailable), N3 (429 body) and S6 (stuck jobs)."""
import os
import subprocess
import sys
import textwrap

import pytest

import applications.caching as caching
from helpers import assert_error, bearer


def run_script(script, tmp_path, **env_over):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS", "JOBS", "REDIS", "CACHE"))}
    env.update(SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="ERROR", APP_ENV="development", JOBS_EXECUTION="inline")
    env.update(env_over)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=root, env=env, capture_output=True,
                          text=True, timeout=180)


# --- N3: 429 body and CORS ---------------------------------------------------------------

def test_429_is_generic_with_retry_after_and_exposed_header(tmp_path):
    result = run_script("""
        import app
        c = app.app.test_client()
        h = {"Origin": "https://frontend.example"}
        rs = [c.post("/api/demo/session", json={"role": "user"}, headers=h) for _ in range(3)]
        r = rs[2]
        assert r.status_code == 429, r.status_code
        body = r.get_json()
        assert body["error"] == "Too many requests. Please try again later.", body
        assert body["code"] == "rate_limited" and 1 <= body["retry_after"] <= 60, body
        assert int(r.headers["Retry-After"]) >= 1
        exposed = r.headers["Access-Control-Expose-Headers"]
        assert "Retry-After" in exposed, exposed
        print("ok")
    """, tmp_path, RATELIMIT_ENABLED="true", DEMO_SESSION_RATE_LIMIT="2 per minute", CORS_ORIGINS="https://frontend.example")
    assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr


# --- S5: configuration ------------------------------------------------------------------------

def test_limiter_and_cache_are_configured_to_survive_redis_outages(tmp_path):
    result = run_script("""
        import app
        c = app.app.config
        assert c["RATELIMIT_SWALLOW_ERRORS"] is True and c["RATELIMIT_IN_MEMORY_FALLBACK_ENABLED"] is True
        assert c["RATELIMIT_STORAGE_OPTIONS"] == {"socket_connect_timeout": 2, "socket_timeout": 2}
        assert c["CACHE_TYPE"] == "applications.caching.ResilientRedisCache"
        assert c["CACHE_KEY_PREFIX"] == "grocery_cache_"
        assert app.limiter._swallow_errors and app.limiter._in_memory_fallback_enabled
        print("ok")
    """, tmp_path, REDIS_URL="redis://127.0.0.1:1/0", JOBS_EXECUTION="thread")
    assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr


# --- S5: safe_cache_clear and the resilient cache -----------------------------------------------

def test_safe_cache_clear_swallows_errors(monkeypatch, caplog):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(caching.cache, "clear", boom)
    assert caching.safe_cache_clear() is False
    assert "Could not clear the response cache" in caplog.text


def test_safe_cache_clear_returns_true_when_it_works():
    assert caching.safe_cache_clear() is True


def test_writes_survive_a_failing_cache_clear(client, admin_token, monkeypatch, query):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(caching.cache, "clear", boom)
    body = {"product_name": "Kiwi", "category": "Dairy", "unit": "kg", "quantity": 1, "price": 1,
            "manufacture_date": "2026-01-01", "expiry_date": "2027-01-01"}
    r = client.post("/api/manager/add_product", json=body, headers=bearer(admin_token))
    assert r.status_code == 201
    assert query("SELECT COUNT(*) FROM products WHERE name = 'Kiwi'") == [(1,)]


def test_no_code_calls_cache_clear_directly():
    import glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in glob.glob(os.path.join(root, "applications", "*.py")):
        if path.endswith("caching.py"):
            continue
        assert "cache.clear()" not in open(path).read().replace("_cache.clear()", ""), path


class DeadRedis:
    """Stands in for a Redis whose every call raises."""

    calls = 0

    def __getattr__(self, name):
        def fail(*a, **k):
            DeadRedis.calls += 1
            raise ConnectionError("down")
        return fail


def test_resilient_cache_degrades_to_misses_and_backs_off(monkeypatch):
    from cachelib import RedisCache as Base
    cache = caching.ResilientRedisCache(host=DeadRedis(), key_prefix="t_")
    DeadRedis.calls = 0
    assert cache.get("k") is None
    first = DeadRedis.calls
    assert first >= 1
    assert cache.set("k", 1) is False and cache.get("k") is None and cache.delete("k") is False and cache.clear() is False
    assert DeadRedis.calls == first  # in the cool-down: Redis is not even tried
    cache._down_until = 0  # cool-down over: tried again
    cache.get("k")
    assert DeadRedis.calls > first


def test_cached_endpoints_work_with_a_dead_cache(app, client, monkeypatch):
    cache = caching.ResilientRedisCache(host=DeadRedis(), key_prefix="t_")
    monkeypatch.setitem(app.extensions["cache"], next(iter(app.extensions["cache"])), cache)
    assert client.get("/api/getCategories").status_code == 200
    assert client.get("/api/user_dashboard").status_code == 200


def test_email_counters_stay_fail_closed(app, client, monkeypatch):
    """A broken counter store must not let e-mails through unlimited (503, not 202)."""
    from applications.email_limits import get_counters

    client.post("/api/demo/email", json={"email": "a@example.com"})

    def boom(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(get_counters(app), "incr", boom)
    r = client.post("/api/demo/jobs", json={"kind": "daily_reminder"})
    assert r.status_code == 503 and r.get_json()["error"]
