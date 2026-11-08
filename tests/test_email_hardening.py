"""S2 (Turnstile gating and hostname), S3 (canonical counting, /64 IPs), S4 (MX check)."""
import logging

import dns.exception
import dns.resolver
import pytest

import applications.email_domain as email_domain
import applications.jobs_routes as jobs_routes
from applications.client_key import reduce_ip
from applications.email_limits import canonical_recipient
from applications.mail_service import backend_name, delivery_configured, email_enabled_for_ui
from helpers import assert_error


def post_email(client, address, **kwargs):
    return client.post("/api/demo/email", json={"email": address}, **kwargs)


def post_job(client, **kwargs):
    return client.post("/api/demo/jobs", json={"kind": "daily_reminder"}, **kwargs)


# --- S3: canonical recipient ----------------------------------------------------------------

@pytest.mark.parametrize("raw,canon", [
    ("Victim@Example.com", "victim@example.com"),
    ("victim+1@example.com", "victim@example.com"),
    ("v.i.c+tim@example.com", "v.i.c@example.com"),       # dots only matter for Gmail
    ("v.i.c.t.i.m+x@gmail.com", "victim@gmail.com"),
    ("victim@googlemail.com", "victim@gmail.com"),
    ("v.ictim+a+b@GoogleMail.com", "victim@gmail.com"),
])
def test_canonical_recipient(raw, canon):
    assert canonical_recipient(raw) == canon


def test_gmail_variants_share_one_recipient_counter(client, sandbox_client, new_sandbox):
    variants = ["victim@gmail.com", "v.ictim@gmail.com", "victim+a@gmail.com", "vic.tim+b@googlemail.com",
                "VICTIM+c@gmail.com", "v.i.c.t.i.m@gmail.com"]
    codes = []
    for v in variants:
        c = sandbox_client(new_sandbox())
        assert post_email(c, v).status_code == 200
        codes.append(post_job(c).status_code)
    assert codes == [202] * 5 + [429]


def test_plus_tags_share_a_counter_for_every_domain(sandbox_client, new_sandbox):
    codes = []
    for i in range(6):
        c = sandbox_client(new_sandbox())
        post_email(c, f"victim+{i}@example.com")
        codes.append(post_job(c).status_code)
    assert codes == [202] * 5 + [429]


def test_the_address_is_stored_as_typed_not_canonical(client, query):
    post_email(client, "Vic.Tim+1@gmail.com")
    assert query("SELECT value FROM meta WHERE key = 'visitor_email'") == [("vic.tim+1@gmail.com",)]


@pytest.mark.parametrize("bad", ["a!b@example.com", "a'b@example.com", "a/b@example.com", "a=b@example.com",
                                 "a{b}@example.com", "a|b@example.com", "a..b@example.com", ".ab@example.com",
                                 "ab.@example.com", "+tag@example.com", "a b@example.com", "ä@example.com"])
def test_local_part_is_restricted(client, bad):
    assert_error(post_email(client, bad), 400)


@pytest.mark.parametrize("good", ["a.b@example.com", "a_b-c+d@example.com", "a1@sub.example.co.uk", "x@example.io"])
def test_ordinary_local_parts_still_work(client, good):
    assert post_email(client, good).status_code == 200


# --- S3: IPv6 /64 ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,key", [
    ("203.0.113.9", "203.0.113.9"),
    ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("fe80::1%eth0", "fe80::/64"),
    ("not-an-ip", "not-an-ip"),
    (None, None),
])
def test_reduce_ip(raw, key):
    assert reduce_ip(raw) == key


def test_one_ipv6_prefix_is_one_client_for_the_email_cap(sandbox_client, new_sandbox):
    codes = []
    for i in range(11):
        c = sandbox_client(new_sandbox())
        post_email(c, f"p{i}@example.com")
        codes.append(post_job(c, environ_overrides={"REMOTE_ADDR": f"2001:db8:0:7:{i:x}::{i + 1:x}"}).status_code)
    assert codes == [202] * 10 + [429]
    c = sandbox_client(new_sandbox())
    post_email(c, "other@example.com")
    assert post_job(c, environ_overrides={"REMOTE_ADDR": "2001:db8:0:8::1"}).status_code == 202


def test_flask_limiter_uses_the_same_key(app):
    from app import limiter, client_key
    assert limiter._key_func is client_key


# --- S4: MX check ---------------------------------------------------------------------------

class FakeRR:
    def __init__(self, exchange):
        self.exchange = exchange


@pytest.fixture
def dns_on(app, monkeypatch):
    """EMAIL_DOMAIN_CHECK on, resolver replaced: ``records[(domain, type)]`` is a list or an exception."""
    monkeypatch.setitem(app.config, "EMAIL_DOMAIN_CHECK", True)
    email_domain.clear_cache()
    records, calls = {}, []

    class FakeResolver:
        timeout = lifetime = None

        def resolve(self, domain, rtype):
            calls.append((domain, rtype))
            value = records.get((domain, rtype), dns.resolver.NoAnswer())
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(email_domain, "_resolver", lambda: FakeResolver())
    yield records, calls
    email_domain.clear_cache()


def test_domain_with_mx_is_accepted(client, dns_on):
    records, _ = dns_on
    records[("example.com", "MX")] = [FakeRR("mx.example.com.")]
    assert post_email(client, "a@example.com").status_code == 200


def test_domain_with_only_an_a_record_is_accepted(client, dns_on):
    records, calls = dns_on
    records[("example.com", "A")] = ["192.0.2.1"]
    assert post_email(client, "a@example.com").status_code == 200
    assert ("example.com", "MX") in calls and ("example.com", "A") in calls


def test_domain_with_only_aaaa_is_accepted(client, dns_on):
    records, _ = dns_on
    records[("example.com", "AAAA")] = ["2001:db8::1"]
    assert post_email(client, "a@example.com").status_code == 200


def test_nxdomain_is_rejected(client, dns_on):
    records, _ = dns_on
    records[("nope.example", "MX")] = dns.resolver.NXDOMAIN()
    assert assert_error(post_email(client, "a@nope.example"), 400) == "That e-mail domain doesn't seem to receive mail."


def test_no_records_at_all_is_rejected(client, dns_on):
    assert_error(post_email(client, "a@norecords.example"), 400)  # NoAnswer for MX, A and AAAA


def test_null_mx_is_rejected(client, dns_on):
    records, _ = dns_on
    records[("example.com", "MX")] = [FakeRR(".")]
    assert_error(post_email(client, "a@example.com"), 400)


@pytest.mark.parametrize("failure", [dns.exception.Timeout(), dns.resolver.NoNameservers(), OSError("no resolver")])
def test_dns_failures_fail_open(client, dns_on, failure):
    records, _ = dns_on
    records[("example.com", "MX")] = failure
    assert post_email(client, "a@example.com").status_code == 200


def test_results_are_cached(client, dns_on):
    records, calls = dns_on
    records[("example.com", "MX")] = [FakeRR("mx.example.com.")]
    post_email(client, "a@example.com")
    post_email(client, "b@example.com")
    assert calls.count(("example.com", "MX")) == 1


def test_failures_are_not_cached(client, dns_on):
    records, calls = dns_on
    records[("example.com", "MX")] = dns.exception.Timeout()
    post_email(client, "a@example.com")
    records[("example.com", "MX")] = [FakeRR("mx.example.com.")]
    post_email(client, "a@example.com")
    assert calls.count(("example.com", "MX")) == 2


def test_bad_domain_stores_nothing(client, dns_on, query):
    post_email(client, "a@norecords.example")
    assert query("SELECT * FROM meta WHERE key = 'visitor_email'") == []


def test_the_check_can_be_switched_off(app, client, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_DOMAIN_CHECK", False)
    monkeypatch.setattr(email_domain, "_resolver", lambda: (_ for _ in ()).throw(AssertionError("no DNS")))
    assert post_email(client, "a@nonexistent-domain-xyz.example").status_code == 200


def test_the_test_suite_default_is_off(app):
    assert app.config["EMAIL_DOMAIN_CHECK"] is False


# --- S2: Turnstile gating and hostname ---------------------------------------------------------

@pytest.fixture
def production(app, monkeypatch):
    monkeypatch.setitem(app.config, "IS_PRODUCTION", True)
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "re_key")
    monkeypatch.setitem(app.config, "TURNSTILE_SECRET_KEY", None)
    monkeypatch.setitem(app.config, "ALLOW_EMAIL_WITHOUT_TURNSTILE", False)


def test_production_resend_without_turnstile_falls_back_to_preview(app, production):
    assert backend_name(app.config) == "preview"
    assert not delivery_configured(app.config) and not email_enabled_for_ui(app.config)


def test_production_resend_with_turnstile_sends(app, production, monkeypatch):
    monkeypatch.setitem(app.config, "TURNSTILE_SECRET_KEY", "s")
    assert backend_name(app.config) == "resend" and email_enabled_for_ui(app.config)


def test_explicit_override_allows_sending_without_turnstile(app, production, monkeypatch):
    monkeypatch.setitem(app.config, "ALLOW_EMAIL_WITHOUT_TURNSTILE", True)
    assert backend_name(app.config) == "resend"


def test_development_is_not_gated(app, monkeypatch):
    monkeypatch.setitem(app.config, "IS_PRODUCTION", False)
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "re_key")
    monkeypatch.setitem(app.config, "TURNSTILE_SECRET_KEY", None)
    assert backend_name(app.config) == "resend"


def test_a_job_in_gated_production_is_a_preview_and_sends_nothing(app, client, production, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr("applications.mail_service.http_post_json", boom)
    post_email(client, "a@example.com")
    assert post_job(client).status_code == 202
    jobs = client.get("/api/demo/jobs").get_json()
    assert jobs["jobs"][0]["status"] == "preview" and jobs["email_enabled"] is False


def test_startup_warning_text_is_in_app_py():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert "TURNSTILE_SECRET_KEY is not" in src and "ALLOW_EMAIL_WITHOUT_TURNSTILE" in src


def test_startup_logs_a_loud_warning_in_production(tmp_path):
    import os, subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS", "JOBS", "TURNSTILE"))}
    env.update(APP_ENV="production", SECRET_KEY="s" * 40, JWT_SECRET_KEY="j" * 40, RESEND_API_KEY="re_x",
               SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="WARNING", JOBS_EXECUTION="inline")
    out = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr          # does not refuse to start
    text = out.stdout + out.stderr
    assert "[WARNING]" in text and "REAL E-MAIL SENDING IS DISABLED" in text
    env["TURNSTILE_SECRET_KEY"] = "ts"
    out = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert "E-MAIL SENDING IS DISABLED" not in out.stdout + out.stderr


@pytest.fixture
def siteverify(app, monkeypatch):
    result = {"body": {"success": True, "hostname": "grocery-store.driptobhattacharyya.com"}}
    monkeypatch.setitem(app.config, "TURNSTILE_SECRET_KEY", "s")
    monkeypatch.setattr(jobs_routes, "http_post_json", lambda *a, **k: (200, result["body"]))
    return result


def test_hostname_must_match(app, client, siteverify, monkeypatch):
    monkeypatch.setitem(app.config, "TURNSTILE_EXPECTED_HOSTNAME", "grocery-store.driptobhattacharyya.com")
    ok = client.post("/api/demo/email", json={"email": "a@example.com", "turnstile_token": "t"})
    assert ok.status_code == 200
    siteverify["body"] = {"success": True, "hostname": "evil.example"}
    assert_error(client.post("/api/demo/email", json={"email": "a@example.com", "turnstile_token": "t"}), 400)
    siteverify["body"] = {"success": True}
    assert_error(client.post("/api/demo/email", json={"email": "a@example.com", "turnstile_token": "t"}), 400)


def test_hostname_check_is_case_insensitive_and_skippable(app, client, siteverify, monkeypatch):
    monkeypatch.setitem(app.config, "TURNSTILE_EXPECTED_HOSTNAME", "Grocery-Store.Driptobhattacharyya.com")
    assert client.post("/api/demo/email", json={"email": "a@example.com", "turnstile_token": "t"}).status_code == 200
    siteverify["body"] = {"success": True, "hostname": "example.com"}
    monkeypatch.setitem(app.config, "TURNSTILE_EXPECTED_HOSTNAME", "")
    assert client.post("/api/demo/email", json={"email": "a@example.com", "turnstile_token": "t"}).status_code == 200


def test_hostname_default_and_empty_override(tmp_path):
    import os, subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = "from applications.config import Config; print(repr(Config.TURNSTILE_EXPECTED_HOSTNAME))"
    base = {k: v for k, v in os.environ.items() if k != "TURNSTILE_EXPECTED_HOSTNAME"}
    run = lambda env: subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True).stdout.strip()
    assert run(base) == "'grocery-store.driptobhattacharyya.com'"
    assert run({**base, "TURNSTILE_EXPECTED_HOSTNAME": ""}) == "''"
