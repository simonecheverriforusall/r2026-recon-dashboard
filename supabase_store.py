"""Supabase persistence for Communications."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
JIRA_BASE = os.environ.get("JIRA_BASE_URL", "https://forusall401k.atlassian.net").rstrip("/")
COMMS_USE_SUPABASE = os.environ.get("COMMS_USE_SUPABASE", "true").lower() in ("1", "true", "yes")
COMMS_CACHE_TTL_HOURS = float(os.environ.get("COMMS_CACHE_TTL_HOURS", "6"))


def is_configured() -> bool:
    return COMMS_USE_SUPABASE and bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _import_create_client():
    """Load create_client from PyPI; ./supabase (CLI) must not shadow the SDK on sys.path."""
    import sys
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent
    app_dir_s = str(app_dir)

    for name in list(sys.modules):
        if name == "supabase" or name.startswith("supabase."):
            del sys.modules[name]

    trimmed = [p for p in sys.path if Path(p).resolve() != app_dir]
    old_path = sys.path
    sys.path = trimmed
    try:
        from supabase import create_client
    except ImportError as e:
        raise ImportError(
            "supabase package not installed. Run: pip install -r requirements.txt"
        ) from e
    finally:
        sys.path = old_path
    return create_client


def _client():
    if not is_configured():
        raise RuntimeError("Supabase not configured (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)")
    create_client = _import_create_client()
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _parse_ts(val: str | None) -> datetime | None:
    """Parse ISO timestamps from Supabase/Postgres (fractional sec length varies)."""
    if not val:
        return None
    s = str(val).strip().replace("Z", "+00:00")
    # Postgres may return e.g. .68598 (5 digits); Python 3.10 needs 6 for fromisoformat.
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:?\d{2})$",
        s,
    )
    if m:
        base, frac, tz = m.groups()
        frac = (frac + "000000")[:6]
        s = f"{base}.{frac}{tz}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _jira_issue_url(task_key: str | None, ws_key: str | None) -> str | None:
    key = (task_key or "").strip() or (ws_key or "").strip()
    return f"{JIRA_BASE}/browse/{key}" if key else None


def _drive_folder_url(row: dict) -> str | None:
    url = (row.get("drive_folder_url") or "").strip()
    if url:
        return url
    plan_id = row.get("plan_id")
    symlink = (row.get("symlink") or "").strip()
    if plan_id and symlink:
        return f"https://drive.google.com/drive/search?q={quote(f'{plan_id}-{symlink}')}"
    return None


def row_to_plan(row: dict) -> dict[str, Any]:
    """API/UI shape from comm_plan_quarter row."""
    default = row.get("sponsor_emails_default") or []
    override = row.get("sponsor_emails") or []
    effective = override if override else default
    jira_task_key = row.get("jira_task_key")
    ws_key = row.get("ws_key") or ""
    jira_url = _jira_issue_url(jira_task_key, ws_key)
    drive_url = _drive_folder_url(row)
    return {
        "plan_id": str(row["plan_id"]),
        "symlink": row.get("symlink") or "",
        "ws_key": row.get("ws_key") or "",
        "plan_name": row.get("plan_name") or "",
        "ops_email": row.get("ops_email") or "",
        "ops_label": row.get("ops_label") or "",
        "quarter": row.get("quarter"),
        "file_date": str(row["file_date"]) if row.get("file_date") else None,
        "checks": {
            "recon_complete": {
                "ok": bool(row.get("recon_complete")),
                "file_date": str(row["file_date"]) if row.get("file_date") else None,
                "rows": row.get("recon_detail") or [],
                "detail": (
                    "Recon complete in Snowflake" if row.get("recon_complete")
                    else "No row in RECON_COMPLETE_DETAILS"
                ),
            },
            "jira_q_done": {
                "ok": bool(row.get("jira_q_done")),
                "task_key": jira_task_key,
                "ws_key": ws_key,
                "url": jira_url,
                "status": row.get("jira_status"),
                "detail": "Done" if row.get("jira_q_done") else f"Status: {row.get('jira_status') or '—'}",
            },
            "drive_file": {
                "ok": bool(row.get("drive_ok")),
                "configured": bool(row.get("drive_configured")),
                "detail": row.get("drive_detail") or "",
                "folder_url": drive_url,
                "folder": None,
                "matched_files": [],
            },
        },
        "eligible": bool(row.get("eligible")),
        "sponsor_emails_default": default,
        "sponsor_emails": override,
        "sponsor_emails_effective": effective,
        "send_status": row.get("send_status") or "not_sent",
        "devrev_ticket_key": row.get("devrev_ticket_key"),
        "sent_at": row.get("sent_at"),
        "sent_by": row.get("sent_by"),
        "gates_refreshed_at": row.get("gates_refreshed_at"),
    }


def list_plans(ops_label: str, quarter: str) -> tuple[list[dict], dict[str, Any]]:
    sb = _client()
    resp = (
        sb.table("comm_plan_quarter")
        .select("*")
        .eq("ops_label", ops_label)
        .eq("quarter", quarter)
        .order("plan_id")
        .execute()
    )
    rows = resp.data or []
    plans = [row_to_plan(r) for r in rows]

    latest = None
    if rows:
        refreshed = [
            t
            for r in rows
            if (t := _parse_ts(r.get("gates_refreshed_at")))
        ]
        if refreshed:
            latest = max(refreshed)
    stale = False
    if latest:
        age_h = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
        stale = age_h > COMMS_CACHE_TTL_HOURS

    eligible = sum(1 for p in plans if p["eligible"])
    meta = {
        "refreshed_at": latest.isoformat() if latest else None,
        "stale": stale,
        "summary": {"total": len(plans), "eligible": eligible, "pending": len(plans) - eligible},
    }
    return plans, meta


def distinct_ops_labels() -> list[str]:
    """All OPS labels in cache (PostgREST defaults to 1000 rows per request)."""
    sb = _client()
    labels: set[str] = set()
    page_size = 1000
    offset = 0
    while True:
        resp = (
            sb.table("comm_plan_quarter")
            .select("ops_label")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        if not batch:
            break
        labels.update(r["ops_label"] for r in batch if r.get("ops_label"))
        if len(batch) < page_size:
            break
        offset += page_size
    return sorted(labels, key=str.lower)


def get_plan(plan_id: int, quarter: str) -> dict | None:
    sb = _client()
    resp = (
        sb.table("comm_plan_quarter")
        .select("*")
        .eq("plan_id", plan_id)
        .eq("quarter", quarter)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None
    return row_to_plan(resp.data[0])


def update_sponsors(
    plan_id: int,
    quarter: str,
    emails: list[str],
    updated_by: str | None,
) -> dict:
    sb = _client()
    payload = {
        "sponsor_emails": emails,
        "sponsor_updated_at": datetime.now(timezone.utc).isoformat(),
        "sponsor_updated_by": updated_by,
    }
    resp = (
        sb.table("comm_plan_quarter")
        .update(payload)
        .eq("plan_id", plan_id)
        .eq("quarter", quarter)
        .execute()
    )
    if not resp.data:
        raise LookupError("Plan not found")
    return row_to_plan(resp.data[0])


def update_send_result(
    plan_id: int,
    quarter: str,
    *,
    send_status: str,
    devrev_ticket_key: str | None = None,
    sent_by: str | None = None,
    send_error: str | None = None,
) -> dict:
    sb = _client()
    payload: dict[str, Any] = {"send_status": send_status, "send_error": send_error}
    if devrev_ticket_key:
        payload["devrev_ticket_key"] = devrev_ticket_key
    if send_status in ("sent", "dry_run"):
        payload["sent_at"] = datetime.now(timezone.utc).isoformat()
        payload["sent_by"] = sent_by
    resp = (
        sb.table("comm_plan_quarter")
        .update(payload)
        .eq("plan_id", plan_id)
        .eq("quarter", quarter)
        .execute()
    )
    if not resp.data:
        raise LookupError("Plan not found")
    return row_to_plan(resp.data[0])


def start_sync_job(scope: str, ops_label: str | None = None, quarter: str | None = None) -> str:
    sb = _client()
    resp = (
        sb.table("sync_jobs")
        .insert({
            "scope": scope,
            "ops_label": ops_label,
            "quarter": quarter,
            "status": "running",
        })
        .execute()
    )
    return resp.data[0]["id"]


def finish_sync_job(
    job_id: str,
    *,
    status: str,
    plans_upserted: int = 0,
    error_message: str | None = None,
) -> None:
    sb = _client()
    sb.table("sync_jobs").update({
        "status": status,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "plans_upserted": plans_upserted,
        "error_message": error_message,
    }).eq("id", job_id).execute()


def latest_sync_job() -> dict | None:
    sb = _client()
    resp = (
        sb.table("sync_jobs")
        .select("*")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None
