# Demo deployment runbook

Topology (see `DEMO_CONTRACT.md`):

- **Frontend**: Vue SPA, deployed as a Cloudflare Worker with static assets at `https://grocery-store.driptobhattacharyya.com`.
- **Backend**: Flask API on a Render free web service, `https://<name>.onrender.com`.
- **E-mail**: Resend, sending from `mail.driptobhattacharyya.com`.

Deploy the backend first. The frontend build needs its URL.

## 1. Backend on Render

1. In Render, choose **New + > Web Service**, connect the GitHub repo `driptobhattacharyya/Grocery-Store`, and pick the **Docker** runtime and the **Free** plan. `render.yaml` in the repo describes the same service if you prefer **New + > Blueprint**.
2. Set these environment variables:

   | Variable | Value |
   | --- | --- |
   | `APP_ENV` | `production` |
   | `SECRET_KEY` | generated (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
   | `JWT_SECRET_KEY` | generated, same method |
   | `CORS_ORIGINS` | `https://grocery-store.driptobhattacharyya.com` |
   | `REDIS_URL` | from a Render Key Value instance (free plan) |
   | `TRUSTED_PROXY_COUNT` | `1` |
   | `RESEND_API_KEY` | optional; without it the app runs in preview mode |
   | `TURNSTILE_SECRET_KEY` | secret key of the Turnstile widget (section 3); pairs with `VITE_TURNSTILE_SITE_KEY` in the frontend build |
   | `TURNSTILE_EXPECTED_HOSTNAME` | `grocery-store.driptobhattacharyya.com` (default; empty skips the check) |

   `ADMIN_PASSWORD` is not needed: in demo mode every sandbox has its own admin with the public password `admin1234`. The full list, with defaults, is in `.env.example`.
3. Create a **Key Value** instance on the free plan, **in the same region as the web service** (`render.yaml` pins both to `oregon`), set its max-memory policy to `noeviction` (it is the Celery broker; `render.yaml` sets this), and copy its internal connection URL into `REDIS_URL`.
4. Deploy. Health check: `https://<name>.onrender.com/api/health`.

## 2. Frontend on Cloudflare

Requirements:

- The zone `driptobhattacharyya.com` is on Cloudflare.
- An API token with **Account › Workers Scripts › Edit** and **Zone › Workers Routes › Edit** on `driptobhattacharyya.com`.
- Your account ID in `CLOUDFLARE_ACCOUNT_ID`.

Deploy:

```sh
cd frontend
npm ci
VITE_API_BASE_URL=https://<name>.onrender.com npm run build
CLOUDFLARE_ACCOUNT_ID=<account-id> npx wrangler deploy
```

`wrangler.jsonc` is an assets-only Worker: there is no Worker script. It serves `dist/` and attaches the custom domain.

If the Render URL changes, update `connect-src` in `frontend/public/_headers`, then rebuild and redeploy.

## 3. E-mail (Resend)

1. Sign up at Resend.
2. Add the domain `mail.driptobhattacharyya.com`.
3. Add the DNS records Resend lists in Cloudflare. Set each one to **DNS only** (grey cloud).
4. If `_dmarc.driptobhattacharyya.com` has no TXT record, add one:
   `v=DMARC1; p=none; rua=mailto:<owner>`
5. Create an API key with **sending access** only.
6. In Render, set:
   - `RESEND_API_KEY` to that key
   - `EMAIL_FROM` to `Grocery Store Demo <demo@mail.driptobhattacharyya.com>`

### Cloudflare Turnstile (required for real sending)

Turnstile is the human check on the e-mail form. Its two keys are a **pair**, and you need both:

| Key | Where it goes |
| --- | --- |
| Secret key | `TURNSTILE_SECRET_KEY` in the Render dashboard (`render.yaml` declares it with `sync: false`) |
| Site key | `VITE_TURNSTILE_SITE_KEY` in the environment of the **frontend build** (`frontend/.env.example`) |

To create the widget:

1. In the Cloudflare dashboard open **Turnstile** and choose **Add widget**.
2. Name it, then under **Hostname management** add `grocery-store.driptobhattacharyya.com`.
3. Pick the **Managed** widget mode and create it.
4. Copy the **site key** into the frontend build (`VITE_TURNSTILE_SITE_KEY=<site key> npm run build`, then redeploy the Worker) and the **secret key** into Render as `TURNSTILE_SECRET_KEY`.

The backend also checks that Cloudflare reports `TURNSTILE_EXPECTED_HOSTNAME` (default `grocery-store.driptobhattacharyya.com`) for the solved token. To test with Cloudflare's dummy keys (they report `example.com`), set `TURNSTILE_EXPECTED_HOSTNAME` to an empty value.

**Safety net.** You can deploy before Turnstile is set up. If `APP_ENV=production` and `RESEND_API_KEY` is set but `TURNSTILE_SECRET_KEY` is not, the app starts, logs a `WARNING` saying real sending is disabled, and runs in **preview mode** (jobs end in `preview`; `GET /api/demo/jobs` reports `email_enabled: false`). Set `ALLOW_EMAIL_WITHOUT_TURNSTILE=true` only if you accept that bots can make the demo e-mail strangers (the daily caps still apply).

### Other e-mail protections

- Counting is per mailbox, not per spelling: `+tag` is dropped for every domain, and for gmail.com/googlemail.com dots are ignored too. IPv6 clients are counted per /64.
- Before an address is accepted, its domain needs an MX record (or else an A/AAAA record). A domain without any is rejected with "That e-mail domain doesn't seem to receive mail." If DNS times out or fails, the address is **accepted** (fail open), because Turnstile and the caps still apply. Switch the check off with `EMAIL_DOMAIN_CHECK=false`.
- E-mails never contain text a visitor can edit: product names are rendered only if they equal the seeded catalog name, and the account only if it is a seeded demo account; otherwise the text is neutral ("a product you added", "your demo account").
- Jobs still `queued` or `running` after `JOB_STALE_SECONDS` (180) are reported as failed ("Timed out; please try again."). A task that Celery redelivers cannot send a second e-mail.

Without `RESEND_API_KEY` the app runs in **preview mode**: e-mails are rendered and stored in the sandbox outbox, and nothing is sent. Jobs then end in status `preview`.

Delivery uses Resend's HTTPS API (Render's free tier blocks outbound SMTP). Daily limits: 5 e-mails per recipient, 10 per client IP, and `EMAIL_DAILY_CAP` (default 80) in total. They are shared by all sandboxes. `EMAIL_ENABLED=false` is a kill switch: jobs fail with "e-mail disabled".

### Background jobs (Celery)

`render.yaml` creates a free Key Value instance (`maxmemoryPolicy: noeviction`, region `oregon`) and passes its internal connection string as `REDIS_URL`. With `REDIS_URL` set, the container's CMD (`supervisor.py`) runs gunicorn and one Celery worker (solo pool, concurrency 1). If either process dies, the container exits and Render restarts it. Without `REDIS_URL`, no worker starts and jobs run on a thread inside gunicorn. There is no Celery beat schedule: nothing runs periodically.

### If Redis is down

The site keeps working: the response cache is skipped, rate limits fall back to process memory, and every Redis call times out after 2 seconds instead of hanging. Only e-mail jobs answer 503, because their daily counters fail closed.

### Limits that protect the 512 MB instance

- SQL console: 20 queries per minute, 0.5 s per query, 256 KB per response, 2 KB per cell, 100 KB per value.
- `GET /api/demo/db` and `/api/demo/outbox` are size-bounded the same way (an e-mail body is cut at 64 KB).
- One sandbox database is capped at `SANDBOX_MAX_PAGES` (2000 pages, about 8 MB); beyond that, writes answer 507 `sandbox_full`. Worst case disk use is `SANDBOX_MAX` (200) times that.
- Text length caps: product name 60, unit 20, category 40, user name 60, address 200, e-mail 254, password 128.
- Mutating requests are limited per client to `RATELIMIT_DEFAULT` (300 per minute).

## 4. Optional keep-warm

Render free instances sleep after 15 minutes without traffic. To keep one awake, create an UptimeRobot monitor that sends a GET to `https://<name>.onrender.com/api/health` every 14 minutes.

A single always-on free service uses about 720–744 of the 750 free instance hours each month.

## 5. Smoke test

Run these against the live site after each deploy:

- [ ] The landing page loads at `https://grocery-store.driptobhattacharyya.com`.
- [ ] The wake indicator appears when the API is asleep, then clears.
- [ ] Each role (user, manager, admin) can sign in and see its pages.
- [ ] The DB drawer opens and shows the sandbox's data.
- [ ] The SQL console runs a query.
- [ ] Jobs appear and run.
- [ ] E-mail: a job sends a message, and the e-mail preview shows it.
