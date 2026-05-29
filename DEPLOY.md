# Deploy to Render (+ Cloudflare Access)

Host the dashboard for the whole company. Jira credentials stay on the server; users only need a `@forusall.com` Google account (via Cloudflare Access).

---

## Part 1 — Render

### 1. Push this folder to GitHub

Render deploys from a Git repo. Push `recon-dashboard/` (or the whole monorepo) to GitHub.

### 2. Connect GitHub + billing (required once)

Render needs both before it can create a service:

1. **GitHub app** — [Install Render on GitHub](https://github.com/apps/render/installations/new) and grant access to `r2026-recon-dashboard`.
   - The repo is **private**. On your GitHub user (`simonecheverriforusall`), open [Render app installations](https://github.com/apps/render/installations) → **Configure** → **Repository access** → include `r2026-recon-dashboard` (or “All repositories”).
   - If Render deploys fail with `404` / `unfetchable`, the app does not have access to this private repo yet.
2. **Billing** — [Add a card](https://dashboard.render.com/billing) (required even for the free plan; you won't be charged unless you upgrade).

### 3. Create a Render web service

1. Go to [render.com](https://render.com) and sign up / log in.
2. **New → Blueprint** (if using `render.yaml`) **or** **New → Web Service**.
3. Connect your GitHub repo.
4. If not using Blueprint, set:
   - **Root directory:** `recon-dashboard` (if the repo is the monorepo)
   - **Runtime:** Docker
   - **Plan:** Free
   - **Health check path:** `/api/health`

### 4. Set environment variables

In Render → your service → **Environment**, add:

| Key | Example |
|-----|---------|
| `JIRA_BASE_URL` | `https://forusall401k.atlassian.net` |
| `JIRA_EMAIL` | `your.name@forusall.com` |
| `JIRA_API_TOKEN` | *(from Atlassian API tokens)* |
| `JIRA_PROJECT_KEY` | `R2026` |

Optional — Snowflake connectivity test (`GET /api/snowflake/test`, requires app login):

| Key | Notes |
|-----|--------|
| `SNOWFLAKE_ENABLED` | `true` |
| `SNOWFLAKE_ACCOUNT` | Account locator from `~/.snowflake/connections.toml` |
| `SNOWFLAKE_USER` | Snowflake user |
| `SNOWFLAKE_WAREHOUSE` | Warehouse |
| `SNOWFLAKE_ROLE` | Optional read-only role |
| `SNOWFLAKE_PRIVATE_KEY_BASE64` | `base64 -i ~/.snowflake/keys/cli_key.p8 \| tr -d '\n'` |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | Key passphrase (secret) |

Do **not** commit `.env` or private keys to Git. `/api/health` does not call Snowflake.

**Supabase (Communications):** `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `COMMS_SYNC_SECRET`. See [SUPABASE.md](./SUPABASE.md).

**Render cron (6h):** Create a cron job that runs:

```bash
curl -sf -X POST "https://<your-host>/api/communications/sync-all?secret=$COMMS_SYNC_SECRET"
```

Schedule: `0 */6 * * *`

### 5. Deploy

Click **Deploy**. When it’s live, Render gives you a URL like:

`https://r2026-recon-dashboard.onrender.com`

Open it — you should see the dashboard (first load may take ~30s on the free tier if the service was sleeping).

---

## Part 2 — Cloudflare Access (Google login, @forusall.com only)

**→ Full click-by-click guide: [CLOUDFLARE-SETUP.md](./CLOUDFLARE-SETUP.md)**

Custom domain **`r2026.forusall.com`** is already registered on Render. You only need:

1. **Cloudflare DNS** — CNAME `r2026` → `r2026-recon-dashboard.onrender.com` (proxied)
2. **Google Workspace IdP** in Zero Trust (Internal OAuth, domain `forusall.com`)
3. **Access app** on `r2026.forusall.com` with allow policy for `@forusall.com`

Share **https://r2026.forusall.com** with the team (not the `.onrender.com` URL).

---

## Notes

- **Free tier cold starts:** Render sleeps after ~15 minutes of no traffic. The first request after that can take 30–60 seconds.
- **Secrets:** Rotate `JIRA_API_TOKEN` in Render if it’s ever exposed. Use a service account / shared ops Jira user if preferred.
- **Local dev:** unchanged — `python3 -m uvicorn server:app --reload --port 8000`

---

## Quick checklist

- [ ] Repo on GitHub with `recon-dashboard/`
- [ ] Render web service deployed (Docker, health check `/api/health`)
- [ ] Jira env vars set in Render
- [ ] Custom domain CNAME → Render
- [ ] Cloudflare Access app + `@forusall.com` allow policy
