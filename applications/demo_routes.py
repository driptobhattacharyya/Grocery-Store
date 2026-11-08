"""Demo endpoints: sandbox sessions, role switching, the database snapshot and
the read-only SQL console. Registered on `current_app` at import time, like the
other route modules (rate limits are applied in app.py).
"""
import sqlite3
import time
from pathlib import Path

from flask import abort, current_app as app, g, jsonify, request
from flask_jwt_extended import create_access_token

from applications.database import DEMO_MANAGER, DEMO_USER, get_db
from applications.http_errors import ApiError, BadRequest, NotFound, api_errors, json_body
from applications.login_manager import normalize_email
from applications.sandbox import SandboxExpired, get_store
from applications.store_manager import manager_check
from applications.user_info import fetch_user_info

@app.before_request
def _demo_endpoints_need_demo_mode():
    if not app.config["DEMO_MODE"] and (request.endpoint or "").startswith("demo_"):
        abort(404)


ROLES = ("user", "manager", "admin")
REDACTED = "••••••"  # six bullets
MAX_ROWS = 200
# Response-size bounds (the free tier has 512 MB of RAM): every displayed cell is cut to
# MAX_CELL_CHARS and rows are only fetched while the running byte budget lasts.
MAX_CELL_CHARS = 2048
TRUNCATED_MARK = "…(truncated)"
SQL_RESPONSE_BUDGET = 256 * 1024     # bytes of cell data in one /api/demo/sql response
DB_TABLE_BUDGET = 64 * 1024          # ... per table in /api/demo/db
OUTBOX_HTML_MAX = 64 * 1024          # one e-mail body in /api/demo/outbox
OUTBOX_RESPONSE_BUDGET = 256 * 1024  # all e-mail bodies together

# Tables shown in the DB panel (everything except `meta`), in a fixed order.
# `used_coupons` is not in the contract's list but is an app table too.
DB_TABLES = ("users", "user_profiles", "store_manager", "managers", "categories", "products",
             "inventory", "cart", "orders", "order_items", "coupons", "used_coupons", "jobs", "outbox")


def _is_secret_column(name):
    return "password" in (name or "").lower()


def _display_cell(value, limit=MAX_CELL_CHARS):
    """JSON-safe, size-bounded form of one cell. Returns ``(value, approx_bytes)``."""
    if isinstance(value, (bytes, bytearray)):
        value = f"<blob {len(value)} bytes>"
    elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        value = str(value)
    if isinstance(value, str):
        if len(value) > limit:
            value = value[:limit] + TRUNCATED_MARK
        return value, len(value)
    return value, 8


def _take_rows(cursor, max_rows, budget, transform=None):
    """Fetch rows one at a time while the row count and the byte budget allow.
    Returns ``(rows, truncated)``; ``truncated`` is true if more rows existed."""
    rows, used = [], 0
    while True:
        row = cursor.fetchone()
        if row is None:
            return rows, False
        if len(rows) >= max_rows or used >= budget:
            return rows, True
        out = []
        for index, value in enumerate(row):
            value, size = transform(index, value) if transform else _display_cell(value)
            used += size
            out.append(value)
        rows.append(out)


# --- sessions ----------------------------------------------------------------------

def _identity_for_role(role):
    """(email/username, display name, is_manager) of the demo account for a role."""
    if role == "user":
        email = DEMO_USER[0]
    elif role == "manager":
        email = DEMO_MANAGER[0]
    else:
        return app.config["ADMIN_USERNAME"], "Administrator", True
    info = fetch_user_info(email)
    if info is None:
        raise NotFound("Demo account not found in this sandbox")
    return email, info["name"], role == "manager"


def _session_payload(role, email, name, is_manager):
    token_role = "admin" if role == "admin" else ("manager" if is_manager else "user")
    token = create_access_token(identity=email, additional_claims={"role": token_role, "sbx": g.sandbox_id})
    return {
        "sandbox_id": g.sandbox_id,
        "expires_in": int(get_store(app).ttl),
        "role": role,
        "access_token": token,
        "name": name,
        "is_manager": is_manager,
    }


def _accounts():
    return [
        {"role": "user", "email": DEMO_USER[0], "password": DEMO_USER[1]},
        {"role": "manager", "email": DEMO_MANAGER[0], "password": DEMO_MANAGER[1]},
        {"role": "admin", "username": app.config["ADMIN_USERNAME"], "password": app.config["DEMO_ADMIN_PASSWORD"]},
    ]


def _requested_role(data):
    role = data.get("role")
    if role not in ROLES:
        raise BadRequest('role must be "user", "manager" or "admin"')
    return role


@app.post("/api/demo/session")
@api_errors
def demo_session():
    """Create a sandbox and log in as the chosen role. Not itself sandboxed."""
    role = _requested_role(json_body())
    g.sandbox_id = get_store(app).create()
    email, name, is_manager = _identity_for_role(role)
    payload = _session_payload(role, email, name, is_manager)
    payload["accounts"] = _accounts()
    return jsonify(payload), 201


@app.post("/api/demo/switch")
@api_errors
def demo_switch():
    """Become another account of the current sandbox: by role, or by e-mail."""
    data = json_body()
    if data.get("email") not in (None, ""):
        email = data["email"]
        if not isinstance(email, str):
            raise BadRequest("email must be text")
        email = normalize_email(email)
        if email == app.config["ADMIN_USERNAME"]:
            role = "admin"
            email, name, is_manager = _identity_for_role("admin")
        else:
            info = fetch_user_info(email)
            if info is None:
                raise NotFound("No account with this email in this demo session")
            is_manager = manager_check(email)
            role = "manager" if is_manager else "user"
            name = info["name"]
    else:
        role = _requested_role(data)
        email, name, is_manager = _identity_for_role(role)
    return jsonify(_session_payload(role, email, name, is_manager))


# --- database snapshot ---------------------------------------------------------------

@app.get("/api/demo/db")
@api_errors
def demo_db():
    db = get_db()
    tables = []
    for table in DB_TABLES:
        columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            continue
        secret = [_is_secret_column(c) for c in columns]
        quoted = ", ".join(f'"{c}"' for c in columns)
        cursor = db.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid')

        def transform(index, value, secret=secret):
            return (REDACTED, 6) if secret[index] and value is not None else _display_cell(value)

        rows, truncated = _take_rows(cursor, MAX_ROWS, DB_TABLE_BUDGET, transform)
        cursor.close()
        count = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        tables.append({"name": table, "columns": columns, "rows": rows, "row_count": count,
                       "truncated": truncated})
    return jsonify({"tables": tables})


# --- read-only SQL console -------------------------------------------------------------

MAX_SQL_LENGTH = 5000
SQL_TIME_LIMIT = 0.5  # seconds
# SQLite refuses to build any string/blob/row above this size (e.g. printf('%.*c', 900000, 'a')).
SQL_MAX_VALUE_BYTES = 100_000

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension"}


class SqlError(ApiError):
    status = 400
    code = "sql_error"


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_READ:
        # arg1 = table, arg2 = column. SQLITE_IGNORE makes the column read as NULL.
        return sqlite3.SQLITE_IGNORE if _is_secret_column(arg2) else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY  # ATTACH, PRAGMA, writes, DDL, transactions, triggers, temp objects...


def _check_single_statement(sql):
    """Reject anything after the first statement. (SQLite's own binding also
    refuses it, and the connection is read-only; this just gives a clear error.)"""
    for i, char in enumerate(sql):
        if char != ";" or not sqlite3.complete_statement(sql[: i + 1]):
            continue
        rest = sql[i + 1:].replace(";", " ").strip()
        if rest and not rest.startswith(("--", "/*")):
            raise SqlError("Only one statement can be run at a time")
        return


def _open_readonly(path):
    try:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=2,
                               isolation_level=None)
    except sqlite3.Error:  # the sandbox file vanished (evicted / expired) since the request began
        raise SandboxExpired() from None
    conn.set_authorizer(_authorizer)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_LENGTH)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    try:
        conn.enable_load_extension(False)
    except AttributeError:
        pass
    return conn


@app.post("/api/demo/sql")
@api_errors
def demo_sql():
    data = json_body()
    sql = data.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise SqlError("sql is required")
    if len(sql) > MAX_SQL_LENGTH:
        raise SqlError(f"Query is too long (at most {MAX_SQL_LENGTH} characters)")
    _check_single_statement(sql)

    path = get_store(app).path_for(g.sandbox_id)
    started = time.monotonic()
    deadline = started + SQL_TIME_LIMIT
    conn = _open_readonly(path)
    try:
        # Called every 1000 SQLite VM instructions; returning non-zero aborts the query.
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            if not columns:
                raise SqlError("Only SELECT queries are allowed")
            rows, truncated = _take_rows(cursor, MAX_ROWS, SQL_RESPONSE_BUDGET)
        except (sqlite3.Error, sqlite3.Warning) as exc:
            if "unable to open" in str(exc):
                raise SandboxExpired() from None
            raise SqlError(_sql_message(exc, time.monotonic() > deadline)) from None
    finally:
        conn.close()
    return jsonify({"columns": columns, "rows": rows, "truncated": truncated,
                    "elapsed_ms": int((time.monotonic() - started) * 1000)})


def _sql_message(exc, timed_out):
    text = str(exc)
    if timed_out or "interrupted" in text:
        return f"Query took too long (limit: {SQL_TIME_LIMIT:g} seconds)"
    if "not authorized" in text or "prohibited" in text or "readonly" in text.lower():
        return "This console is read-only: only a single SELECT statement is allowed"
    if "too big" in text:
        return "A value in the result is too large (limit: 100 KB)"
    if "one statement" in text:
        return "Only one statement can be run at a time"
    return text
