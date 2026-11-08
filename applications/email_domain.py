"""Does an e-mail domain receive mail? (S4: avoid bounces from our sending domain.)

An MX record means yes; with no MX, an A or AAAA record is accepted (RFC 5321 implicit
MX). NXDOMAIN, no records at all, or a "null MX" (RFC 7505, target ".") mean no. A DNS
timeout or resolver failure *fails open* (the address is accepted): Turnstile and the
daily caps still apply, and a flaky resolver should not lock real visitors out.

Results are cached in memory for an hour (negative answers: 5 minutes); failures are not cached.
"""
import logging
import threading
import time

import dns.exception
import dns.resolver

log = logging.getLogger(__name__)

DNS_TIMEOUT = 2.0  # seconds, for the whole lookup of one record type
POSITIVE_TTL = 3600
NEGATIVE_TTL = 300
CACHE_MAX = 2000

_cache = {}  # domain -> (expires_at, bool)
_lock = threading.Lock()


def _resolver():
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT
    resolver.lifetime = DNS_TIMEOUT
    return resolver


def _lookup(domain):
    """True / False, or None when the answer is unknown (timeout, SERVFAIL, no resolver)."""
    resolver = _resolver()
    try:
        try:
            answer = resolver.resolve(domain, "MX")
            targets = [str(r.exchange).rstrip(".") for r in answer]
            return any(targets)  # only a null MX ("." target) leaves nothing
        except dns.resolver.NoAnswer:
            pass
        for rtype in ("A", "AAAA"):
            try:
                if len(resolver.resolve(domain, rtype)):
                    return True
            except dns.resolver.NoAnswer:
                continue
        return False
    except (dns.resolver.NXDOMAIN, dns.name.EmptyLabel, dns.name.LabelTooLong, dns.name.NameTooLong):
        return False
    except (dns.exception.DNSException, OSError):
        log.warning("DNS check for %s failed; accepting the address", domain, exc_info=True)
        return None


def domain_receives_mail(domain):
    """False only when we positively know the domain cannot receive mail."""
    domain = domain.strip().lower().rstrip(".")
    now = time.monotonic()
    with _lock:
        hit = _cache.get(domain)
        if hit and hit[0] > now:
            return hit[1]
    result = _lookup(domain)
    if result is None:
        return True  # fail open
    with _lock:
        if len(_cache) >= CACHE_MAX:
            for key in [k for k, (exp, _) in _cache.items() if exp <= now] or list(_cache)[: CACHE_MAX // 2]:
                _cache.pop(key, None)
        _cache[domain] = (now + (POSITIVE_TTL if result else NEGATIVE_TTL), result)
    return result


def clear_cache():
    with _lock:
        _cache.clear()
