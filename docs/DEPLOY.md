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
   | `ADMIN_PASSWORD` | at least 8 characters, not `password` (required in production) |
   | `CORS_ORIGINS` | `https://grocery-store.driptobhattacharyya.com` |
   | `REDIS_URL` | from a Render Key Value instance (free plan) |
   | `TRUSTED_PROXY_COUNT` | `1` |
   | `RESEND_API_KEY` | optional; without it the app runs in preview mode |

   The full list, with defaults, is in `.env.example`.
3. Create a **Key Value** instance on the free plan and copy its internal connection URL into `REDIS_URL`.
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

> **Known gap:** the current backend does not read `RESEND_API_KEY` or `EMAIL_FROM`. `applications/mail_service.py` sends through SMTP using `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS` and `SENDER_EMAIL`. Until the code is changed, these steps do not enable e-mail. Check the code before relying on them.

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
