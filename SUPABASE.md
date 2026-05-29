# Supabase setup (recon-dashboard project)

Communications data lives in Supabase (`comm_plan_quarter`, `sync_jobs`).

## 1. Link project (CLI)

```bash
cd recon-dashboard
supabase login
supabase link --project-ref <YOUR_PROJECT_REF>
```

Project ref: Supabase Dashboard → Project **recon-dashboard** → Settings → General → Reference ID.

## 2. Apply migrations

```bash
supabase db push
```

Migration file: `supabase/migrations/20260529120000_comm_plan_quarter.sql`

## 3. Environment variables

Copy from `.env.example` into `.env` (and Render secrets):

| Variable | Where |
|----------|--------|
| `SUPABASE_URL` | Project Settings → API → URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Project Settings → API → service_role (secret) |
| `COMMS_SYNC_SECRET` | Random string; used by Render cron |
| `COMMS_CACHE_TTL_HOURS` | `6` (stale UI hint) |

## 4. First sync

```bash
source ~/.zshrc && snowflake_pass
pip3 install -r requirements.txt
python3 -m uvicorn server:app --reload --port 8000
```

In the app: Communications → select OPS + quarter → **Sync gates**.

Or API:

```bash
curl -X POST "http://localhost:8000/api/communications/refresh?ops=Juliana%20Ramirez&quarter=Q1"
```

## 5. Render cron (every 6 hours)

Create a **Cron Job** on Render:

- Schedule: `0 */6 * * *`
- Command: `curl -sf -X POST "https://<your-host>/api/communications/sync-all?secret=$COMMS_SYNC_SECRET"`

Set `COMMS_SYNC_SECRET` on both the web service and cron job.
