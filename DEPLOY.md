# Deploy to Render (+ Cloudflare Access)

Host the dashboard for the whole company. Jira credentials stay on the server; users only need a `@forusall.com` Google account (via Cloudflare Access).

---

## Part 1 — Render

### 1. Push this folder to GitHub

Render deploys from a Git repo. Push `recon-dashboard/` (or the whole monorepo) to GitHub.

### 2. Connect GitHub + billing (required once)

Render needs both before it can create a service:

1. **GitHub app** — [Install Render on GitHub](https://github.com/apps/render/installations/new) and grant access to `r2026-recon-dashboard`.
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

Do **not** commit `.env` to Git.

### 5. Deploy

Click **Deploy**. When it’s live, Render gives you a URL like:

`https://r2026-recon-dashboard.onrender.com`

Open it — you should see the dashboard (first load may take ~30s on the free tier if the service was sleeping).

---

## Part 2 — Cloudflare Access (Google login, @forusall.com only)

Put Cloudflare in front of Render so only ForUsAll Google accounts can open the site.

### 1. Add your domain to Cloudflare

If `forusall.com` (or a subdomain) isn’t on Cloudflare yet, add the zone in the Cloudflare dashboard.

### 2. Point a subdomain at Render

Create a DNS record:

| Type | Name | Target |
|------|------|--------|
| CNAME | `recon` (→ `recon.forusall.com`) | `r2026-recon-dashboard.onrender.com` |

Enable the **orange cloud** (proxied) so traffic goes through Cloudflare.

In Render → **Settings → Custom Domains**, add `recon.forusall.com` and follow Render’s verification steps.

### 3. Enable Zero Trust (free for up to 50 users)

1. [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → **Access → Applications**.
2. **Add an application** → **Self-hosted**.
3. **Application domain:** `recon.forusall.com` (or your chosen subdomain).
4. **Identity providers:** add **Google**. Use your company Google Workspace / OAuth client if IT provides one, or Cloudflare’s Google integration.
5. **Add a policy:**
   - **Action:** Allow
   - **Include:** Emails ending in `@forusall.com`  
     *(or: Login Methods → Google, plus an email domain rule)*

Save. Anyone visiting `https://recon.forusall.com` must sign in with Google; only `@forusall.com` addresses pass through to Render.

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
