"""Daily e-mail limits shared by every sandbox (a visitor can make new sandboxes).

Counted when a job is enqueued, per UTC day: per recipient, per client IP and
globally (the global cap only counts when a real provider is configured).
Counters live in Redis when ``REDIS_URL`` is set, else in this process
(thread-safe; fine with the single gunicorn worker).
"""
import hashlib
import threading
import time

from applications.http_errors import ApiError


class EmailLimitReached(ApiError):
    status = 429
    code = "email_limit"


def _today():
    return time.strftime("%Y%m%d", time.gmtime())


def _seconds_to_midnight():
    now = time.gmtime()
    return 86400 - (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec) + 3600


class MemoryCounters:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}

    def incr(self, key):
        with self._lock:
            day = key.split(":", 2)[1]
            for old in [k for k in self._counts if k.split(":", 2)[1] != day]:
                del self._counts[old]
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def decr(self, key):
        with self._lock:
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1

    def reset(self):
        with self._lock:
            self._counts.clear()


class RedisCounters:
    def __init__(self, url):
        import redis

        self.client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)

    def incr(self, key):
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, _seconds_to_midnight())
        return int(pipe.execute()[0])

    def decr(self, key):
        self.client.decr(key)

    def reset(self):
        for key in self.client.scan_iter("emaillimit:*"):
            self.client.delete(key)


def get_counters(app):
    counters = app.extensions.get("email_counters")
    if counters is None:
        url = app.config.get("REDIS_URL")
        counters = RedisCounters(url) if url else MemoryCounters()
        app.extensions["email_counters"] = counters
    return counters


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]  # keeps addresses/IPs out of Redis keys


def reserve(app, recipient, ip, count_global):
    """Count one e-mail against every limit, or raise EmailLimitReached (counting nothing).
    Returns a token for ``release``."""
    counters = get_counters(app)
    day = _today()
    plan = [
        (f"emaillimit:{day}:rcpt:{_digest(recipient)}", app.config["EMAIL_RECIPIENT_DAILY_LIMIT"],
         "This address has reached its limit of {n} demo e-mails per day."),
        (f"emaillimit:{day}:ip:{_digest(ip or '-')}", app.config["EMAIL_IP_DAILY_LIMIT"],
         "You have reached the limit of {n} demo e-mails per day. Try again tomorrow."),
    ]
    if count_global:
        plan.append((f"emaillimit:{day}:global", app.config["EMAIL_DAILY_CAP"],
                     "The demo has reached its daily e-mail cap ({n}). Please try again tomorrow."))
    taken = []
    for key, limit, message in plan:
        taken.append(key)
        if counters.incr(key) > limit:
            for done in taken:
                counters.decr(done)
            raise EmailLimitReached(message.format(n=limit))
    return taken


def release(app, taken):
    counters = get_counters(app)
    for key in taken:
        counters.decr(key)
