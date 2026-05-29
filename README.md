# R2026 Reconciliation Dashboard

A local, real-time dashboard for the **R2026 Annual Reconciliation** project. It pulls live data directly from Jira and presents it in a clean, interactive UI — no Jira logins or slow default gadgets required.

---

## What it shows

| Section | Description |
|---|---|
| **KPI cards** | Total plans · Fully Reconciled · Task Progress % · Census Submitted to RK |
| **Progress by Quarter** | Q1–Q4 Done vs Pending across all plans |
| **Whole Project ring** | Overall task completion donut with percentage |
| **OPS — Pending Q Tasks** | Per-analyst breakdown of Done vs Pending quarter tasks |
| **Plans Reconciled by Week** | Velocity chart — how many plans fully completed each week |
| **Submit Census File to RK** | Two side-by-side tables (Audit Plans / Non-Audit Plans) showing census task status per OPS analyst |
| **Plans Fully Reconciled** | Searchable table of every plan with all tasks done, with direct Jira links |

### Drilldown
Click any **non-zero number** in the census tables to open a side drawer with:
- Every plan behind that number (Plan ID, Symlink, Jira link)
- Jira comments on each task, loaded in parallel

---

## Requirements

- Python 3.9+
- A Jira Cloud account with API access to the R2026 project

---

## Setup

### 1. Clone / get the folder

```bash
cd recon-dashboard
```

### 2. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Configure credentials

Copy the example env file and fill it in:

```bash
cp .env.example .env
```

Edit `.env`:

```env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=your.email@company.com
JIRA_API_TOKEN=your_jira_api_token_here
JIRA_PROJECT_KEY=R2026
```

**Getting a Jira API token:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **Create API token**
3. Copy the token into `JIRA_API_TOKEN`

### 4. Run

```bash
python3 -m uvicorn server:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

---

## Deploy (company-wide access)

See **[DEPLOY.md](./DEPLOY.md)** for Render + Cloudflare Access setup (`@forusall.com` Google login).

---

## Project structure

```
recon-dashboard/
├── .env                  ← your credentials (never commit this)
├── .env.example          ← template to share with teammates
├── DEPLOY.md             ← Render + Cloudflare Access deploy guide
├── Dockerfile            ← container for Render
├── render.yaml           ← Render Blueprint
├── requirements.txt      ← Python dependencies
├── server.py             ← FastAPI backend: fetches Jira, computes metrics
└── static/
    └── index.html        ← Single-page frontend (Tailwind + Chart.js)
```

---

## How it works

```
Browser → GET /api/dashboard
              ↓
         server.py fetches all R2026 Workstreams + Tasks from Jira
         (cursor-paginated, handles 400+ issues automatically)
              ↓
         Computes: KPIs · Quarter breakdown · OPS breakdown ·
                   Weekly velocity · Census tables · Fully reconciled list
              ↓
         Returns JSON (cached 5 minutes)
              ↓
Browser renders charts and tables with Chart.js + Tailwind
```

When you click a number in the census table:
```
Browser → GET /api/task-details?keys=R2026-X,R2026-Y,...
              ↓
         server.py fetches comments for each task in parallel (up to 8 threads)
              ↓
         Returns comments per task key
```

### Data cache

Results are cached in-memory for **5 minutes**. Click **🔄 Refresh** in the header to force a reload from Jira.

---

## Custom field reference (R2026)

| Field | Jira custom field | Notes |
|---|---|---|
| Plan ID | `customfield_11661` | Used to identify plans |
| Symlink | `customfield_11662` | |
| OPS | `customfield_11675` | Assignee email |
| Record Keeper | `customfield_11680` | |
| Off calendar | `customfield_11671` | Yes = off-calendar plan |
| Audit | `customfield_11667` | TRUE = `11683`, FALSE = `11684` |

### Task name classification

| Task name | Type |
|---|---|
| Q1, Q2, Q3, Q4 | Quarter tasks (tracked in OPS chart) |
| Validate Employee Census | Census task (non-LT Trust plans) |
| Submit DMGY File | Census task (LT Trust plans) |

---

## Sharing with teammates

1. Share the `recon-dashboard/` folder (or push it to a repo)
2. Teammate copies `.env.example` → `.env` and fills in their credentials
3. `pip3 install -r requirements.txt`
4. `python3 -m uvicorn server:app --port 8000`

No build steps, no Node, no databases — just Python and a browser.

---

## Snowflake connectivity test

The dashboard can verify Snowflake access via **`GET /api/snowflake/test`** (same RSA keypair as the org `snow` CLI). This does not change dashboard metrics yet — Jira remains the data source for charts.

### Credentials (RSA keypair)

| Variable | Required | Notes |
|----------|----------|-------|
| `SNOWFLAKE_ENABLED` | Yes | `true` to enable the test endpoint |
| `SNOWFLAKE_ACCOUNT` | Yes | From `~/.snowflake/connections.toml` profile `payroll_node` |
| `SNOWFLAKE_USER` | Yes | Snowflake username |
| `SNOWFLAKE_WAREHOUSE` | Yes | Warehouse name |
| `SNOWFLAKE_PRIVATE_KEY` | One of three | Full PEM text (use `\n` for newlines in `.env`) |
| `SNOWFLAKE_PRIVATE_KEY_BASE64` | One of three | Base64 of PEM file — preferred on **Render** |
| `SNOWFLAKE_PRIVATE_KEY_PATH` | One of three | e.g. `~/.snowflake/keys/cli_key.p8` — **local dev only** |
| `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE` | If key encrypted | Same as Keychain / `snowflake_pass` (local: `PRIVATE_KEY_PASSPHRASE` also works after `snowflake_pass`) |
| `SNOWFLAKE_ROLE` | No | e.g. read-only role |
| `SNOWFLAKE_DATABASE` | No | e.g. `FORUS_WEB` |
| `SNOWFLAKE_SCHEMA` | No | e.g. `PUBLIC` |

Copy from `.env.example` and fill in `.env`. Never commit keys or passphrases.

### Local test

1. Confirm CLI works (optional baseline):

   ```bash
   source ~/.zshrc && snowflake_pass
   snow sql -c payroll_node --query "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()" --format JSON
   ```

2. In `recon-dashboard/.env` set `SNOWFLAKE_ENABLED=true` and either:
   - `SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/keys/cli_key.p8` + account/user/warehouse/passphrase, or
   - paste PEM into `SNOWFLAKE_PRIVATE_KEY`.

3. For curl without login: `REQUIRE_AUTH=false`.

4. Start the server and open:

   ```
   http://localhost:8000/api/snowflake/test
   ```

   Success:

   ```json
   { "ok": true, "user": "...", "role": "...", "warehouse": "...", "database": "..." }
   ```

   With `SNOWFLAKE_ENABLED=false`:

   ```json
   { "ok": false, "reason": "disabled", "hint": "Set SNOWFLAKE_ENABLED=true" }
   ```

When `REQUIRE_AUTH=true` (production), you must be logged in with Google (`@forusall.com`) before calling this URL.

### Plans in bucket (per plan)

`GET /api/snowflake/plans-in-bucket?plan_id=92` returns all rows from `RECON_PROJECT.CONTROL.PLANS_IN_BUCKET` for that plan (file dates, recon status, payroll/RK file names, etc.).

Optional OPS filter (matches `EMAIL` on the row):

```
/api/snowflake/plans-in-bucket?plan_id=92&ops=ana@forusall.com
```

Requires `SNOWFLAKE_DATABASE=RECON_PROJECT`, `SNOWFLAKE_SCHEMA=CONTROL`, and role with read access (e.g. `OPS_DEV` from the `recon_project` CLI profile).

### Render production

Add secret env vars on the `r2026-recon-dashboard` service (see `render.yaml` keys). Recommended:

- `SNOWFLAKE_ENABLED=true`
- `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`
- `SNOWFLAKE_PRIVATE_KEY_BASE64` — `base64 -i ~/.snowflake/keys/cli_key.p8 | tr -d '\n'`
- `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`

Redeploy, log in to the app, then visit `/api/snowflake/test`. `/api/health` stays Jira-only so deploy health checks are unaffected.

Future work: join [reconciliation/sql/recon_dashboard_plans.sql](../reconciliation/sql/recon_dashboard_plans.sql) into dashboard metrics.

---

## Communications tab

OPS users send sponsor emails (via **DevRev**, template TBD) only when a plan passes three gates for the selected quarter:

| Gate | Source |
|------|--------|
| Recon complete | Row in `RECON_PROJECT.CONTROL.RECON_COMPLETE_DETAILS` for `plan_id` + quarter end `FILE_DATE` (e.g. Q1 → `2026-03-31`) |
| Jira Q done | R2026 workstream child task `Q1`–`Q4` in Done status |
| Drive file | File matching `DRIVE_COMM_REQUIRED_PATTERN` in plan folder `{plan_id} - {symlink}` |

**Filtering:** Communications starts empty. Select **OPS** and **quarter** to load plans (any user can view any OPS’s book). Dashboard filters pre-fill Communications when you switch tabs.

**API:**

- `GET /api/communications/meta` — OPS list, config flags
- `GET /api/communications/eligible?ops=...&quarter=Q1` — plan checklist
- `POST /api/communications/send` — DevRev stub (dry-run by default)

**Env:** `RECON_YEAR`, `COMMUNICATIONS_DRY_RUN`, `DEVREV_API_TOKEN`, `DEVREV_ENABLED`, `DRIVE_COMM_REQUIRED_PATTERN`, `DRIVE_API_KEY`, `DRIVE_PARENT_FOLDER_ID`.

**Supabase (required for Communications):** See **[SUPABASE.md](./SUPABASE.md)** — `supabase link` + `supabase db push`, then set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

- **Load** — read cached rows from Supabase (fast).
- **Sync gates** — batch refresh Jira + Snowflake + Drive into Supabase for one OPS+quarter.
- **Cron** — `POST /api/communications/sync-all?secret=...` every 6 hours on Render.
- Edit sponsor emails in the table (saved to Supabase); ticket key after send.