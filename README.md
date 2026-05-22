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
