# Demo mode: architecture and API contract

This is the shared contract for the portfolio demo. Backend and frontend are built in parallel against it. Change it only by agreement, and record every deviation in your report.

## Topology

- **Frontend**: the Vite SPA, deployed as a Cloudflare Worker with static assets at `https://grocery-store.driptobhattacharyya.com`. The SPA fallback is configured with `not_found_handling = "single-page-application"`.
- **Backend**: the Flask API on a Render free web service (`https://<service>.onrender.com`, optionally `api-grocery-store.driptobhattacharyya.com`). The backend no longer has to serve the SPA, but it keeps that ability for local and Docker use.
- The frontend calls the API **cross-origin**. The API base URL comes from `import.meta.env.VITE_API_BASE_URL`, and an empty value means same origin (local dev through the Vite proxy, and the Docker image).
- CORS: the backend allows the origins listed in `CORS_ORIGINS`. It must allow the request headers `Authorization`, `Content-Type` and `X-Sandbox-Id`, and expose `Content-Disposition` (needed for the CSV download).
- Render free instances sleep after 15 minutes of inactivity and take up to about 60 seconds to wake. While Render is waking a sleeping service, the first cross-origin requests can fail at the network or CORS level, or return 502/503.

## Sandboxes (one private database per visitor)

- At startup the backend builds a **template DB**: the current schema plus the demo seed data.
- Each visitor gets a **sandbox**: a copy of the template DB at `<SANDBOX_DIR>/<sandbox_id>.db`.
  - `sandbox_id` is `secrets.token_urlsafe(24)`. It is unguessable and works as a capability: knowing it grants access to that sandbox.
- Each sandbox contains three demo accounts:
  - shopper `demo@grocery.app`
  - manager `manager@grocery.app` (status `approved`)
  - admin `admin`
- Every sandboxed API request carries the header `X-Sandbox-Id: <id>`.
  - Missing, unknown or expired → **410** `{"error": "...", "code": "sandbox_expired"}`.
  - The frontend reacts to a 410 by telling the visitor the demo session expired and offering to start a new one.
- JWTs issued inside a sandbox carry the claim `sbx = <sandbox_id>`. A token whose `sbx` doesn't match `X-Sandbox-Id` gets 401.
- Expiry:
  - idle TTL `SANDBOX_TTL_MINUTES` (default 60), refreshed on every request;
  - hard cap `SANDBOX_MAX` (default 200); creating a new sandbox when full evicts the least recently used one;
  - expired sandboxes are cleaned up opportunistically.
  - Everything is also lost when Render restarts or the service spins down.
- Sandbox creation is rate-limited per IP (default `20 per hour`).
- Every existing endpoint (catalog, cart, checkout, manager, admin, signup, login, admin login) works **inside the sandbox named by `X-Sandbox-Id`**.
- Response caching must never mix sandboxes: every cache key includes the sandbox id.

## Demo endpoints

All bodies and responses are JSON. Errors are `{"error": "<message>"}`, plus `"code"` where noted.

### `GET /api/health`
Unchanged: `200 {"status":"ok"}`. Needs no sandbox. The frontend calls it on load to wake the server.

### `POST /api/demo/session`
Creates a new sandbox and logs in as the chosen role.
- Body: `{"role": "user" | "manager" | "admin"}`.
- 201:
  ```json
  {"sandbox_id": "...", "expires_in": 3600, "role": "manager",
   "access_token": "...", "name": "Demo Manager", "is_manager": true,
   "accounts": [
     {"role": "user", "email": "demo@grocery.app", "password": "demo1234"},
     {"role": "manager", "email": "manager@grocery.app", "password": "manager1234"},
     {"role": "admin", "username": "admin", "password": "<admin password>"}
   ]}
  ```
  - `accounts` exists so the UI can show the credentials.
  - The admin password is the demo-sandbox admin password (`DEMO_ADMIN_PASSWORD`, default `admin1234`), not the operator's `ADMIN_PASSWORD`.
- 400 for a bad role; 429 when rate-limited.

### `POST /api/demo/switch`
Switches role inside the current sandbox. Needs `X-Sandbox-Id`.
- Body: `{"role": "user" | "manager" | "admin"}`, or `{"email": "<an account in this sandbox>"}` to become a specific signed-up user. That second form is how a visitor who signed up and got approved as a manager can come back as themselves.
- 200 with the same shape as `/api/demo/session`, minus `accounts`.
- 404 for an unknown email; 410 when the sandbox has expired.

### `GET /api/demo/db`
Read-only snapshot of the sandbox database, for the DB panel. Needs `X-Sandbox-Id`.
- 200:
  ```json
  {"tables": [{"name": "products", "columns": ["productID", ...],
               "rows": [[...], ...], "row_count": 14, "truncated": false}, ...]}
  ```
- Covers every app table except `meta`, in a stable order: users, user_profiles, store_manager, managers, categories, products, inventory, cart, orders, order_items, coupons, jobs, outbox.
- Values in any password column are replaced with `"••••••"`.
- At most 200 rows per table, with `truncated` set to true when rows were cut.
- The frontend diffs consecutive snapshots to highlight changed rows. A row's identity is its first column.

### `POST /api/demo/sql`
Read-only SQL console. Needs `X-Sandbox-Id`.
- Body: `{"sql": "SELECT ..."}`.
- 200: `{"columns": [...], "rows": [[...]], "truncated": bool, "elapsed_ms": n}`.
- 400 `{"error": "...", "code": "sql_error"}` for invalid SQL or anything that isn't a read.
- Enforcement, done at the SQLite level:
  - a `mode=ro` URI connection;
  - `set_authorizer` that allows only SELECT, READ and FUNCTION, and denies ATTACH, PRAGMA and writes;
  - reads of password columns return NULL;
  - exactly one statement per request;
  - a 200-row cap;
  - a ~1 s time limit through `set_progress_handler`.
- Rate-limited (default `20 per minute`, 0.5 s time limit, responses capped at 256 KB with each cell cut to 2 KB; `truncated: true` when anything was cut).

### E-mail and background jobs (Celery)
All of these need `X-Sandbox-Id`. Jobs are stored in a sandbox table `jobs(id, kind, status, to_email, created_at, updated_at, detail)`:
- `status` ∈ `queued | running | sent | preview | failed`.
- `preview` means no e-mail provider is configured. The message was rendered and stored in `outbox`, not delivered.

Each rendered e-mail is also stored in the sandbox table `outbox(id, job_id, to_email, subject, html, created_at)`, so the UI can show it.

- `POST /api/demo/email`, body `{"email": "...", "turnstile_token": "..."}`.
  - Sets the visitor's real e-mail address for this sandbox.
  - `turnstile_token` is required only when `TURNSTILE_SECRET_KEY` is set.
  - 200 `{"email": "..."}`; 400 for an invalid address or a failed Turnstile check.
- `POST /api/demo/jobs`, body `{"kind": "monthly_report" | "daily_reminder"}`.
  - Enqueues a Celery job that e-mails the sandbox's visitor address.
  - 202 `{"job": {...}}`; 400 when no visitor e-mail is set; 429 when a limit is hit.
- `GET /api/demo/jobs` → `200 {"jobs": [...newest first], "email": "<visitor email or null>", "email_enabled": bool}`.
  - `email_enabled` is false in preview mode.
- `GET /api/demo/outbox` → `200 {"messages": [...newest first]}`.
- A successful checkout also enqueues an `order_confirmation` job when a visitor e-mail is set.
- Limits:
  - per recipient: 5 e-mails/day;
  - per IP: 10/day;
  - global: `EMAIL_DAILY_CAP` (default 80);
  - `EMAIL_ENABLED=false` is a kill switch.
- Templates are fixed: no visitor-supplied text is ever included except product names from the sandbox, which are HTML-escaped. Every e-mail carries a demo footer.

## Frontend behaviour
- **Landing page** (`/`):
  - Pick a role: Shopper, Store Manager or Admin, each with a one-line description of what it can do.
  - On load, call `/api/health` with retry and backoff until it succeeds, showing "Waking the demo server… (free hosting, up to ~1 min)". The role buttons stay usable throughout, and a click waits for the server to be ready.
- **Session**: keep `sandbox_id` and the token in sessionStorage, and send `X-Sandbox-Id` and `Authorization` on every API call.
- **Demo toolbar**, present on every page:
  - shows the current role;
  - a role switcher that calls `/api/demo/switch`;
  - "Database" opens the DB panel;
  - "Emails & jobs" opens the jobs panel;
  - "Reset demo" starts a new session;
  - a "Demo data resets after an hour of inactivity" note.
- **DB panel**: a drawer with table tabs; rows changed since the previous snapshot are highlighted; it refreshes after every mutating API call. It also hosts the read-only SQL console with a few example queries.
- **Jobs panel**: the e-mail address form (with Turnstile when `VITE_TURNSTILE_SITE_KEY` is set), buttons for the monthly report and the daily reminder, a live list of jobs that polls every 2 s while any job is queued or running, and the outbox preview.
- **Signup inside the demo**: a visitor can sign up as a new user, apply to become a manager, switch to admin and approve themselves, then use "switch to my account" to become their own manager account.
