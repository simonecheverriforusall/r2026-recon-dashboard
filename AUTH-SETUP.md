# Google login (built into the app)

The dashboard now requires **Google sign-in** with an `@forusall.com` account on **every URL**, including `r2026-recon-dashboard.onrender.com`. No Cloudflare needed for auth.

Only `/api/health` stays public (Render health checks).

---

## 1. Create Google OAuth credentials

1. Open [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials).
2. **OAuth consent screen** → User type: **Internal** (Workspace only).
3. **Create credentials → OAuth client ID → Web application**.
4. **Authorized redirect URIs** (add all you use):

   ```
   https://r2026-recon-dashboard.onrender.com/auth/callback
   http://localhost:8000/auth/callback
   ```

   Add `https://r2026.forusall.com/auth/callback` later if you set up custom DNS.

5. Copy **Client ID** and **Client Secret**.

---

## 2. Set env vars on Render

[Render → r2026-recon-dashboard → Environment](https://dashboard.render.com/web/srv-d88c21i8qa3s73f5aeqg):

| Key | Value |
|-----|-------|
| `REQUIRE_AUTH` | `true` |
| `SESSION_SECRET` | long random string (e.g. `openssl rand -hex 32`) |
| `ALLOWED_EMAIL_DOMAIN` | `forusall.com` |
| `GOOGLE_CLIENT_ID` | from step 1 |
| `GOOGLE_CLIENT_SECRET` | from step 1 |

Save → Render redeploys automatically.

---

## 3. Local dev

In `.env`:

```env
REQUIRE_AUTH=false
```

Or use the same Google OAuth creds with redirect `http://localhost:8000/auth/callback`.

---

## 4. Test

1. Open https://r2026-recon-dashboard.onrender.com in incognito.
2. You should be redirected to Google login.
3. Sign in with `@forusall.com` → dashboard loads.
4. Personal Gmail → **Access denied**.
