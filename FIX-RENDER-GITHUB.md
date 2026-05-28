# Fix Render “could not read Username” / unable to clone

Render cannot clone **private** repos unless the **Render GitHub App** is installed on the **same GitHub account that owns the repo**.

Your repo lives under **`simonecheverriforusall`** (personal). If Render is only installed on **`forus-coop`** (org) with “All repositories”, that does **not** include your personal repos.

---

## Option A — Keep repo public (simplest)

The repo has **no secrets** (Jira/OAuth keys are only in Render env vars). Public is safe for source code.

```bash
gh repo edit simonecheverriforusall/r2026-recon-dashboard --visibility public --accept-visibility-change-consequences
```

Then in [Render](https://dashboard.render.com/web/srv-d88c21i8qa3s73f5aeqg) click **Manual Deploy → Deploy latest commit**.

---

## Option B — Keep repo private (correct GitHub setup)

### 1. Install Render on your **personal** GitHub user

1. Open https://github.com/apps/render/installations/new  
2. Choose **simonecheverriforusall** (not only forus-coop)  
3. **Repository access:** All repositories, or select `r2026-recon-dashboard`  
4. Save  

Confirm at https://github.com/settings/installations — you should see **Render** under your **personal** account.

### 2. Reconnect Git on Render

1. [Service Settings → Build & Deploy](https://dashboard.render.com/web/srv-d88c21i8qa3s73f5aeqg/settings)  
2. Under **Git** / **Git Credentials**, disconnect and reconnect GitHub  
3. Pick `simonecheverriforusall/r2026-recon-dashboard` again  
4. **Manual Deploy → Deploy latest commit**

---

## Verify

A successful deploy log starts with:

```
==> Cloning from https://github.com/simonecheverriforusall/r2026-recon-dashboard
```

—not `fatal: could not read Username`.
