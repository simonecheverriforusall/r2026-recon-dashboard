-- Communications: plan/quarter state (eligibility, sponsors, DevRev send)

CREATE TABLE IF NOT EXISTS public.comm_plan_quarter (
  plan_id                  INT NOT NULL,
  quarter                  TEXT NOT NULL CHECK (quarter IN ('Q1', 'Q2', 'Q3', 'Q4')),
  ops_email                TEXT,
  ops_label                TEXT NOT NULL,
  symlink                  TEXT,
  ws_key                   TEXT,
  plan_name                TEXT,
  file_date                DATE,
  recon_complete           BOOLEAN NOT NULL DEFAULT FALSE,
  recon_detail             JSONB,
  jira_q_done              BOOLEAN NOT NULL DEFAULT FALSE,
  jira_task_key            TEXT,
  jira_status              TEXT,
  drive_ok                 BOOLEAN NOT NULL DEFAULT FALSE,
  drive_configured         BOOLEAN NOT NULL DEFAULT FALSE,
  drive_detail             TEXT,
  eligible                 BOOLEAN NOT NULL DEFAULT FALSE,
  gates_refreshed_at       TIMESTAMPTZ,
  sponsor_emails_default   TEXT[],
  sponsor_emails           TEXT[],
  sponsor_updated_at       TIMESTAMPTZ,
  sponsor_updated_by         TEXT,
  send_status              TEXT NOT NULL DEFAULT 'not_sent'
    CHECK (send_status IN ('not_sent', 'dry_run', 'sent', 'failed')),
  devrev_ticket_key        TEXT,
  sent_at                  TIMESTAMPTZ,
  sent_by                  TEXT,
  send_error               TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (plan_id, quarter)
);

CREATE INDEX IF NOT EXISTS idx_comm_plan_quarter_ops_quarter
  ON public.comm_plan_quarter (ops_label, quarter);

CREATE INDEX IF NOT EXISTS idx_comm_plan_quarter_gates_refreshed
  ON public.comm_plan_quarter (gates_refreshed_at DESC);

CREATE TABLE IF NOT EXISTS public.sync_jobs (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope            TEXT NOT NULL,
  ops_label        TEXT,
  quarter          TEXT,
  started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at      TIMESTAMPTZ,
  status           TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'success', 'failed')),
  plans_upserted   INT NOT NULL DEFAULT 0,
  error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_started
  ON public.sync_jobs (started_at DESC);

-- updated_at trigger
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS comm_plan_quarter_updated_at ON public.comm_plan_quarter;
CREATE TRIGGER comm_plan_quarter_updated_at
  BEFORE UPDATE ON public.comm_plan_quarter
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- RLS: service role only (app uses service key server-side)
ALTER TABLE public.comm_plan_quarter ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_jobs ENABLE ROW LEVEL SECURITY;
