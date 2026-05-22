# Cloudflare Access setup — `r2026.forusall.com`

Protect the dashboard so only `@forusall.com` Google Workspace users can open it.

**Live Render URL (no auth):** https://r2026-recon-dashboard.onrender.com  
**Target URL (with auth):** https://r2026.forusall.com

---

## Overview (do in this order)

```
User → r2026.forusall.com (Cloudflare DNS, proxied)
     → Cloudflare Access (Google Workspace login, @forusall.com only)
     → r2026-recon-dashboard.onrender.com (Render)
```

---

## Step 1 — DNS record (required — this is why you see NXDOMAIN)

`DNS_PROBE_FINISHED_NXDOMAIN` means **no DNS record exists yet** for `r2026.forusall.com`.

`forusall.com` is hosted on **AWS Route 53** (not Cloudflare). You need someone with Route 53 access to add the record.

### Option A — Route 53 only (dashboard works, no Google login gate)

In **AWS Route 53** → hosted zone **forusall.com** → **Create record**:

| Field | Value |
|-------|-------|
| Record name | `r2026` |
| Type | `CNAME` |
| Value | `r2026-recon-dashboard.onrender.com` |
| TTL | 300 |

Save. Wait 5–15 minutes, then open https://r2026.forusall.com

> `recon.forusall.com` already exists in Route 53 (points elsewhere). Do **not** reuse that name.

### Option B — Cloudflare Access (Google login) + Route 53

Cloudflare Access only works if traffic passes through Cloudflare. That usually means either:

- **forusall.com** is on Cloudflare (proxied), **or**
- IT sets up a **Cloudflare for SaaS** / custom hostname for `r2026.forusall.com`

If your zone is only on Route 53, ask IT which approach they use. The Cloudflare steps below assume you can proxy `r2026.forusall.com` through Cloudflare.

**Cloudflare DNS** (if the zone is on Cloudflare):

| Field | Value |
|-------|-------|
| Type | `CNAME` |
| Name | `r2026` |
| Target | `r2026-recon-dashboard.onrender.com` |
| Proxy | **Proxied** (orange cloud ON) |

### Use the app now (no custom domain)

While DNS is pending, the dashboard already works at:

**https://r2026-recon-dashboard.onrender.com**

---

## Step 2 — Custom domain on Render (~2 min)

1. Open [Render service → Settings → Custom Domains](https://dashboard.render.com/web/srv-d88c21i8qa3s73f5aeqg/settings).
2. Confirm **`r2026.forusall.com`** is listed (already added via CLI).
3. Render will verify the CNAME once DNS propagates (usually a few minutes).
4. Wait until status shows **Verified** / certificate issued.

---

## Step 3 — Cloudflare Zero Trust team (~3 min)

If you don't have Zero Trust yet:

1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com/).
2. Create a team (free plan covers up to 50 users).
3. Note your **team name** — your login URL looks like:
   `https://<team-name>.cloudflareaccess.com`

---

## Step 4 — Google Workspace identity provider (~10 min)

Use **Google Workspace** (not generic “Google”) so only your company domain can sign in.

### A. Google Cloud OAuth client

1. Open [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create or select a project (e.g. `forusall-cloudflare-access`).
3. **APIs & Services → OAuth consent screen:**
   - User type: **Internal** (Workspace users only — blocks personal Gmail)
   - App name: `R2026 Recon Dashboard`
   - Support email: your `@forusall.com` address
   - Save
4. **Credentials → Create credentials → OAuth client ID:**
   - Application type: **Web application**
   - Name: `Cloudflare Access - R2026 Recon`
   - **Authorized JavaScript origins:**
     ```
     https://<your-team-name>.cloudflareaccess.com
     ```
   - **Authorized redirect URIs:**
     ```
     https://<your-team-name>.cloudflareaccess.com/cdn-cgi/access/callback
     ```
5. Copy the **Client ID** and **Client Secret**.

### B. Add IdP in Cloudflare

1. [Zero Trust → Integrations → Identity providers](https://one.dash.cloudflare.com/?to=/:account/access/identity)
2. **Add new identity provider** → **Google Workspace**
3. Fill in:
   - Client ID / Client Secret (from step A)
   - **Apps domain:** `forusall.com`
4. Save.

---

## Step 5 — Access application (~5 min)

1. [Zero Trust → Access → Applications](https://one.dash.cloudflare.com/?to=/:account/access/applications)
2. **Add an application** → **Self-hosted**
3. **Application name:** `R2026 Recon Dashboard`
4. **Session duration:** 24 hours (or your preference)
5. **Application domain:**
   - Subdomain: `r2026`
   - Domain: `forusall.com`
   - (Full hostname: `r2026.forusall.com`)
6. **Identity providers:** enable only **Google Workspace** (disable “Accept all available” if others are listed)
7. **Add a policy** (Policy name: `ForUsAll employees`):
   - **Action:** Allow
   - **Include** → **Emails ending in:** `@forusall.com`
   - *(Optional extra belt-and-suspenders with Workspace IdP + Internal OAuth)*
8. Save application.

---

## Step 6 — Test

1. Open an **incognito** window.
2. Go to https://r2026.forusall.com
3. You should see Cloudflare Access login → **Sign in with Google Workspace**.
4. Sign in with your `@forusall.com` account → dashboard loads.
5. Try a personal `@gmail.com` account → should be **denied**.

Direct Render URL (`r2026-recon-dashboard.onrender.com`) will still work **without** auth. Share only `r2026.forusall.com` with the team.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| NXDOMAIN / site can't be reached | No DNS record yet — add CNAME `r2026` in **Route 53** (see Step 1) |
| Redirect URI mismatch | Google OAuth redirect must exactly match `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback` |
| Gmail accounts can log in | Use **Google Workspace** IdP + **Internal** OAuth consent, not generic Google |
| 525 / SSL errors | Wait for Render to issue cert after DNS verifies |
| Slow first load | Render free tier cold start (~30–60s) — normal |

---

## Optional: block direct Render URL

To force everyone through Cloudflare Access, ask IT to add Render's outbound IPs to an allow-list, or accept that the `.onrender.com` URL is a semi-public fallback (don't share it).

---

## Quick links

- [Render service dashboard](https://dashboard.render.com/web/srv-d88c21i8qa3s73f5aeqg)
- [Cloudflare Zero Trust](https://one.dash.cloudflare.com/)
- [Google Cloud Credentials](https://console.cloud.google.com/apis/credentials)
- [Cloudflare Google Workspace IdP docs](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/google-workspace/)
