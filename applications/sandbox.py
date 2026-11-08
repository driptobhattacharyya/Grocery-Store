"""Per-visitor sandboxes: one private SQLite database per demo visitor.

At start-up a *template* database (schema + seed) is built. Each visitor gets a
copy of it at ``<SANDBOX_DIR>/<sandbox_id>.db``. The id is the visitor's
capability, so it is random (``secrets.token_urlsafe(24)``) and validated
strictly before it is ever turned into a path.

Lifecycle: an idle TTL (refreshed on every request) and a hard cap with
least-recently-used eviction; expired sandboxes are removed opportunistically.
The registry is in memory (guarded by a lock: gunicorn runs several threads in
one worker). A sandbox file that exists on disk but is not in the registry
(e.g. after a restart on a persistent disk) is adopted if it is still within
its TTL, judged by file mtime.

Endpoints that are NOT sandboxed (``UNSANDBOXED_ENDPOINTS``): ``/api/health``,
``/api/ready`` (checks the template DB) and ``POST /api/demo/session``, which
creates a sandbox. Everything else needs a valid ``X-Sandbox-Id`` header.
"""
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import g, request

from applications.database import _connect, init_db
from applications.http_errors import ApiError

log = logging.getLogger(__name__)

SANDBOX_HEADER = "X-Sandbox-Id"
# token_urlsafe(24) is exactly 32 characters of this alphabet.
SANDBOX_ID_RE = re.compile(r"[A-Za-z0-9_-]{32}")
_SUFFIXES = ("", "-wal", "-shm", "-journal")

# Endpoint names (Flask) that work without a sandbox.
UNSANDBOXED_ENDPOINTS = {"health", "ready", "demo_session", "spa", "static"}


class SandboxExpired(ApiError):
    """Missing, unknown or expired sandbox: HTTP 410 with code ``sandbox_expired``."""

    status = 410
    code = "sandbox_expired"

    def __init__(self, message="This demo session has expired. Please start a new one."):
        super().__init__(message)


def is_valid_id(sandbox_id):
    return isinstance(sandbox_id, str) and SANDBOX_ID_RE.fullmatch(sandbox_id) is not None


def _remove_files(path):
    for suffix in _SUFFIXES:
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove %s", path + suffix)


def _file_mtime(path):
    times = []
    for suffix in _SUFFIXES:
        try:
            times.append(os.stat(path + suffix).st_mtime)
        except OSError:
            pass
    return max(times) if times else None


class SandboxStore:
    def __init__(self, directory, template_path, ttl_seconds, max_count, busy_timeout=15.0):
        self.directory = os.path.abspath(directory)
        self.template_path = os.path.abspath(template_path)
        self.ttl = ttl_seconds
        self.max = max(1, int(max_count))
        self.busy_timeout = busy_timeout
        self._lock = threading.RLock()
        self._last_used = OrderedDict()  # sandbox_id -> wall-clock time, LRU first
        self._last_cleanup = 0.0
        os.makedirs(self.directory, exist_ok=True)
        self._adopt_existing()

    # --- paths ----------------------------------------------------------------
    def path_for(self, sandbox_id):
        """File path of a sandbox. Raises ValueError unless the id is well-formed."""
        if not is_valid_id(sandbox_id):
            raise ValueError("malformed sandbox id")
        return os.path.join(self.directory, sandbox_id + ".db")

    # --- registry ---------------------------------------------------------------
    def _adopt_existing(self):
        now = time.time()
        found = []
        for entry in os.listdir(self.directory):
            name, ext = os.path.splitext(entry)
            if ext != ".db" or not is_valid_id(name):
                continue
            path = os.path.join(self.directory, entry)
            mtime = _file_mtime(path)
            if mtime is None or now - mtime > self.ttl:
                _remove_files(path)
            else:
                found.append((mtime, name))
        with self._lock:
            for mtime, name in sorted(found):
                self._last_used[name] = mtime
            while len(self._last_used) > self.max:
                self._evict_oldest()

    def _evict_oldest(self):
        sandbox_id, _ = self._last_used.popitem(last=False)
        _remove_files(os.path.join(self.directory, sandbox_id + ".db"))
        log.info("Evicted sandbox %s...", sandbox_id[:4])

    def _expired(self, last_used, now):
        return now - last_used > self.ttl

    def cleanup(self, now=None):
        """Drop every expired sandbox."""
        now = now or time.time()
        with self._lock:
            self._last_cleanup = now
            for sandbox_id in [i for i, t in self._last_used.items() if self._expired(t, now)]:
                del self._last_used[sandbox_id]
                _remove_files(os.path.join(self.directory, sandbox_id + ".db"))

    def _maybe_cleanup(self, now):
        if now - self._last_cleanup > 60:
            self.cleanup(now)

    def create(self):
        """Copy the template into a fresh sandbox and return its id."""
        with self._lock:
            now = time.time()
            self.cleanup(now)
            while len(self._last_used) >= self.max:
                self._evict_oldest()
            while True:
                sandbox_id = secrets.token_urlsafe(24)
                if sandbox_id not in self._last_used:
                    break
            path = self.path_for(sandbox_id)
            try:
                self._copy_template(path)
            except BaseException:
                _remove_files(path)
                raise
            self._last_used[sandbox_id] = now
            return sandbox_id

    def _copy_template(self, dst_path):
        # The SQLite backup API yields a consistent, self-contained file (no
        # WAL needed) even if something is writing to the source.
        src = sqlite3.connect(f"{Path(self.template_path).as_uri()}?mode=ro", uri=True, timeout=self.busy_timeout)
        dst = sqlite3.connect(dst_path, timeout=self.busy_timeout)
        try:
            src.backup(dst)
            dst.execute("UPDATE meta SET value = ? WHERE key = 'db_instance_id'", (secrets.token_hex(16),))
            dst.commit()
            dst.execute("PRAGMA journal_mode=WAL")
        finally:
            dst.close()
            src.close()

    def touch(self, sandbox_id):
        """Refresh the idle timer and return the sandbox's path, or None if it
        is unknown or has expired (in which case it is removed)."""
        if not is_valid_id(sandbox_id):
            return None
        path = self.path_for(sandbox_id)
        with self._lock:
            now = time.time()
            self._maybe_cleanup(now)
            last = self._last_used.get(sandbox_id)
            if last is None:
                # Not in this process's registry; adopt a file left on disk.
                mtime = _file_mtime(path)
                if mtime is None:
                    return None
                last = mtime
            if self._expired(last, now):
                self._last_used.pop(sandbox_id, None)
                _remove_files(path)
                return None
            if not os.path.exists(path):
                self._last_used.pop(sandbox_id, None)
                return None
            self._last_used[sandbox_id] = now
            self._last_used.move_to_end(sandbox_id)
            return path

    def delete(self, sandbox_id):
        with self._lock:
            self._last_used.pop(sandbox_id, None)
            if is_valid_id(sandbox_id):
                _remove_files(self.path_for(sandbox_id))

    def count(self):
        with self._lock:
            return len(self._last_used)

    def ids(self):
        with self._lock:
            return list(self._last_used)


# --- application wiring -------------------------------------------------------------

def build_template(app):
    """(Re)build the template database. Built under a temporary name and moved
    into place atomically, so a process that is copying it never sees a
    half-built file."""
    template = os.path.abspath(app.config["TEMPLATE_DB_PATH"])
    os.makedirs(os.path.dirname(template), exist_ok=True)
    tmp = f"{template}.{os.getpid()}.tmp"
    _remove_files(tmp)
    init_db(app, path=tmp, admin_password=app.config["DEMO_ADMIN_PASSWORD"])
    # The template is only ever read: a plain rollback-journal file leaves no
    # -wal/-shm files behind and is a single self-contained file.
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    _remove_files(template)
    os.replace(tmp, template)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(tmp + suffix)
        except FileNotFoundError:
            pass
    return template


def init_sandboxes(app):
    """Build the template and the store (demo mode only)."""
    template = build_template(app)
    store = SandboxStore(
        app.config["SANDBOX_DIR"], template, app.config["SANDBOX_TTL_MINUTES"] * 60,
        app.config["SANDBOX_MAX"], app.config["SQLITE_BUSY_TIMEOUT"],
    )
    app.extensions["sandbox_store"] = store
    log.info("Demo mode: template %s, sandboxes in %s (ttl %.0f min, max %d)",
             template, store.directory, app.config["SANDBOX_TTL_MINUTES"], store.max)
    return store


def get_store(app):
    return app.extensions["sandbox_store"]


def bind_sandbox(sandbox_id):
    """Use this sandbox for ``get_db()`` in the current app context (for code
    that runs outside a request, e.g. Celery tasks). Raises SandboxExpired."""
    from flask import current_app

    if get_store(current_app).touch(sandbox_id) is None:
        raise SandboxExpired()
    g.sandbox_id = sandbox_id


def current_sandbox_id():
    """The sandbox of this request/app context, or None (also in non-demo mode)."""
    return g.get("sandbox_id")


def open_connection(app):
    """Connection for ``get_db()``."""
    if not app.config["DEMO_MODE"]:
        return _connect(app.config["DATABASE_PATH"], app.config)
    sandbox_id = g.get("sandbox_id")
    if sandbox_id is None:
        raise RuntimeError("No sandbox bound to this request (missing X-Sandbox-Id / bind_sandbox)")
    path = get_store(app).path_for(sandbox_id)
    # mode=rw never creates a file: a sandbox deleted mid-request must not be
    # silently recreated as an empty database.
    try:
        conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=rw", uri=True,
                               timeout=app.config.get("SQLITE_BUSY_TIMEOUT", 15))
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError:
        raise SandboxExpired() from None
    return conn


def open_template(app):
    """Read-only connection to the template database (readiness probe)."""
    return sqlite3.connect(f"{Path(app.config['TEMPLATE_DB_PATH']).resolve().as_uri()}?mode=ro", uri=True,
                           timeout=app.config.get("SQLITE_BUSY_TIMEOUT", 15))


def resolve_request_sandbox(app):
    """``before_request`` hook: bind the sandbox named by the header to ``g``.
    Raises SandboxExpired when it is missing, unknown or expired."""
    if not app.config["DEMO_MODE"] or request.method == "OPTIONS":
        return None
    path = request.path
    if not (path == "/api" or path.startswith("/api/")):
        return None
    if request.endpoint is None or request.endpoint in UNSANDBOXED_ENDPOINTS:
        return None  # unknown route (404/405) or an endpoint that needs no sandbox
    sandbox_id = request.headers.get(SANDBOX_HEADER, "")
    if get_store(app).touch(sandbox_id) is None:
        raise SandboxExpired()
    g.sandbox_id = sandbox_id
    return None
