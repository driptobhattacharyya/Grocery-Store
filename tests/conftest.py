"""Shared fixtures.

``app.py`` builds the Flask app at import time from environment variables, so
the test environment (throw-away SQLite files, rate limiting off, development
mode, demo mode with sandboxes) is set up here, *before* ``app`` is imported.

The app runs in demo mode: every API request needs ``X-Sandbox-Id``. Each test
gets its own fresh sandbox (a copy of the seeded template database) and the
``client`` fixture adds that header to every request automatically; pass
``headers={"X-Sandbox-Id": ...}`` to override it, or use ``client.sandbox_id``
(``None`` sends no header). Use ``sandbox_client()`` for extra clients (e.g.
threads) bound to the same sandbox, and ``new_sandbox()`` for a second,
independent sandbox. ``query``/``execute`` run SQL directly against the test's
sandbox file.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="grocery-tests-")

# Settings the app reads at import time. Anything inherited from the
# developer's shell that would change behaviour is removed.
for _key in ("REDIS_URL", "CORS_ORIGINS", "CONTENT_SECURITY_POLICY", "SECRET_KEY", "JWT_SECRET_KEY",
             "ADMIN_USERNAME", "ADMIN_PASSWORD", "TRUSTED_PROXY_COUNT", "AUTH_RATE_LIMIT", "DEMO_MODE",
             "DEMO_ADMIN_PASSWORD", "SANDBOX_TTL_MINUTES", "SANDBOX_MAX", "TEMPLATE_DB_PATH",
             "DEMO_SESSION_RATE_LIMIT", "DEMO_SQL_RATE_LIMIT"):
    os.environ.pop(_key, None)
os.environ.update({
    "APP_ENV": "development",
    "DEMO_MODE": "true",
    "DATABASE_PATH": os.path.join(_TMP, "single", "test.db"),
    "SANDBOX_DIR": os.path.join(_TMP, "sandboxes"),
    "CORS_ORIGINS": "https://frontend.example",
    "RATELIMIT_ENABLED": "false",
    "RATELIMIT_STORAGE_URI": "memory://",
    "FRONTEND_DIST": os.path.join(_TMP, "no-frontend"),
    "LOG_LEVEL": "WARNING",
})

from app import app as flask_app  # noqa: E402
from applications.caching import cache  # noqa: E402
from applications.sandbox import SANDBOX_HEADER, get_store  # noqa: E402
from helpers import ADMIN, DEMO_MANAGER, DEMO_USER  # noqa: E402
import helpers  # noqa: E402


class SandboxClient(FlaskClient):
    """Test client that sends ``X-Sandbox-Id: <self.sandbox_id>`` unless the
    request already sets the header (or ``sandbox_id`` is None)."""

    sandbox_id = None

    def open(self, *args, **kwargs):
        headers = Headers(kwargs.pop("headers", None) or {})
        if self.sandbox_id and SANDBOX_HEADER not in headers:
            headers[SANDBOX_HEADER] = self.sandbox_id
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


flask_app.test_client_class = SandboxClient


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(TESTING=True)
    yield flask_app
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def store(app):
    return get_store(app)


@pytest.fixture
def new_sandbox(store):
    """Create another sandbox; returns its id. Removed at the end of the test."""
    created = []

    def make():
        sandbox_id = store.create()
        created.append(sandbox_id)
        return sandbox_id

    yield make
    for sandbox_id in created:
        store.delete(sandbox_id)


@pytest.fixture
def sandbox_id(app, new_sandbox):
    """This test's own freshly seeded sandbox."""
    sid = new_sandbox()
    helpers.CURRENT_SANDBOX = sid
    cache.clear()
    yield sid
    helpers.CURRENT_SANDBOX = None
    cache.clear()


@pytest.fixture
def sandbox_client(app, sandbox_id):
    """Factory for clients bound to a sandbox (default: this test's)."""

    def make(sid=None):
        client = app.test_client()
        client.sandbox_id = sid or sandbox_id
        return client

    return make


@pytest.fixture
def client(sandbox_client):
    return sandbox_client()


@pytest.fixture
def sandbox_path(store, sandbox_id):
    return store.path_for(sandbox_id)


@pytest.fixture
def query(sandbox_path):
    """Run a read query directly against the test's sandbox database."""

    def run(sql, *params):
        conn = sqlite3.connect(sandbox_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    return run


@pytest.fixture
def execute(sandbox_path):
    """Run a write statement directly against the test's sandbox database."""

    def run(sql, *params):
        conn = sqlite3.connect(sandbox_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    return run


def _login(client, path, email, password):
    response = client.post(path, json={"email": email, "password": password})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["access_token"]


@pytest.fixture
def login(client):
    """Log a shopper/manager in and return the access token."""
    return lambda email, password: _login(client, "/api/login", email, password)


@pytest.fixture
def shopper_token(client):
    return _login(client, "/api/login", *DEMO_USER)


@pytest.fixture
def manager_token(client):
    return _login(client, "/api/login", *DEMO_MANAGER)


@pytest.fixture
def admin_token(client):
    return _login(client, "/api/admin/login", *ADMIN)


@pytest.fixture
def signup(client):
    """Create a shopper account and return its email."""

    def run(email="new@example.com", password="secret123", name="New Shopper", phone="1234567890"):
        response = client.post("/api/signup", json={
            "name": name, "email": email, "password": password, "phoneNo": phone, "address": "1 Test Road",
        })
        assert response.status_code == 201, response.get_json()
        return email

    return run
