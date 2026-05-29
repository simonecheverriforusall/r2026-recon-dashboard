"""Batch sync Communications eligibility into Supabase."""
from __future__ import annotations

import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import communications as comm
import drive_client as drive
import snowflake_client as sf
import supabase_store as store

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACKER = ROOT / "reconciliation" / "Year_end_comm_tracker - DB.csv"

QUARTERS = ["Q1", "Q2", "Q3", "Q4"]


def _norm_plan_id(v: str | None) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(float(str(v).strip()))
    except ValueError:
        return None


def load_tracker_sponsors(path: Path | None = None) -> dict[int, list[str]]:
    """plan_id -> list of sponsor emails from Primary Contact Email."""
    p = path or Path(
        __import__("os").environ.get("COMMS_TRACKER_CSV", str(DEFAULT_TRACKER))
    )
    if not p.is_file():
        return {}

    out: dict[int, list[str]] = {}
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = _norm_plan_id(row.get("Plan ID") or row.get("plan_id"))
            if pid is None:
                continue
            raw = (row.get("Primary Contact Email") or "").strip()
            if not raw or raw.upper() in ("#N/A", "N/A", "NA"):
                continue
            emails = [e.strip().lower() for e in re.split(r"[,;]", raw) if e.strip()]
            if emails:
                out[pid] = emails
    return out


def _parse_emails(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [e.strip().lower() for e in re.split(r"[,;]", raw) if e.strip()]


def _jira_q_check(ws_key: str, quarter: str, tasks: list[dict]) -> dict[str, Any]:
    for t in tasks:
        if t["parent_key"] == ws_key and t["summary"] == quarter:
            return {
                "ok": t["done"],
                "task_key": t["key"],
                "status": t["status"],
            }
    return {"ok": False, "task_key": None, "status": None}


def _drive_check(plan_id: str, symlink: str) -> dict[str, Any]:
    r = drive.required_comm_file_present(plan_id, symlink)
    configured = r["configured"]
    return {
        "ok": r["found"] if configured else False,
        "configured": configured,
        "detail": r["detail"],
    }


def _build_row(
    ws: dict,
    quarter: str,
    file_date: str,
    tasks: list[dict],
    sf_map: dict[int, list[dict]],
    tracker: dict[int, list[str]],
    now: str,
) -> dict[str, Any]:
    plan_id = int(ws["plan_id"]) if ws["plan_id"].isdigit() else None
    if plan_id is None:
        return {}

    sf_rows = sf_map.get(plan_id, [])
    jq = _jira_q_check(ws["key"], quarter, tasks)
    dr = _drive_check(ws["plan_id"], ws["symlink"])
    plan_name = (sf_rows[0].get("plan_name") if sf_rows else None) or ws.get("symlink") or ""

    eligible = bool(sf_rows) and jq["ok"] and dr["ok"]

    return {
        "plan_id": plan_id,
        "quarter": quarter,
        "ops_email": ws.get("ops") or "",
        "ops_label": comm.ops_label(ws.get("ops", "")),
        "symlink": ws.get("symlink") or "",
        "ws_key": ws.get("key") or "",
        "plan_name": plan_name,
        "file_date": file_date,
        "recon_complete": bool(sf_rows),
        "recon_detail": sf_rows,
        "jira_q_done": jq["ok"],
        "jira_task_key": jq["task_key"],
        "jira_status": jq["status"],
        "drive_ok": dr["ok"],
        "drive_configured": dr["configured"],
        "drive_detail": dr["detail"],
        "drive_folder_url": (dr.get("folder") or {}).get("url"),
        "eligible": eligible,
        "gates_refreshed_at": now,
        "sponsor_emails_default": tracker.get(plan_id, []),
    }


def _upsert_rows(rows: list[dict]) -> int:
    """Upsert plans; preserve sponsor_emails and send fields when row exists."""
    if not rows:
        return 0
    sb = store._client()
    count = 0
    for row in rows:
        pid, q = row["plan_id"], row["quarter"]
        existing = (
            sb.table("comm_plan_quarter")
            .select("sponsor_emails, send_status, devrev_ticket_key, sent_at, sent_by, send_error")
            .eq("plan_id", pid)
            .eq("quarter", q)
            .limit(1)
            .execute()
        )
        payload = dict(row)
        if existing.data:
            ex = existing.data[0]
            if ex.get("sponsor_emails"):
                payload["sponsor_emails"] = ex["sponsor_emails"]
            for k in ("send_status", "devrev_ticket_key", "sent_at", "sent_by", "send_error"):
                if ex.get(k) is not None:
                    payload[k] = ex[k]
        sb.table("comm_plan_quarter").upsert(payload, on_conflict="plan_id,quarter").execute()
        count += 1
    return count


def sync_ops_quarter(
    ops_label: str,
    quarter: str,
    workstreams: list[dict],
    tasks: list[dict],
    *,
    scope: str = "ops_quarter",
    manage_job: bool = True,
) -> dict[str, Any]:
    file_date = comm.quarter_file_date(quarter)
    if not file_date:
        return {"ok": False, "error": "Invalid quarter"}

    job_id = store.start_sync_job(scope, ops_label, quarter) if manage_job else None
    try:
        filtered = comm.filter_workstreams_for_ops(workstreams, ops_label)
        plan_ids = [int(ws["plan_id"]) for ws in filtered if ws.get("plan_id", "").isdigit()]

        sf_map: dict[int, list[dict]] = {}
        if sf.is_enabled() and plan_ids:
            sf_map = sf.fetch_recon_complete_bulk(plan_ids, file_date)

        tracker = load_tracker_sponsors()
        now = datetime.now(timezone.utc).isoformat()

        rows: list[dict] = []
        if drive.comm_pattern_configured() and filtered:
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {
                    ex.submit(_build_row, ws, quarter, file_date, tasks, sf_map, tracker, now): ws
                    for ws in filtered
                    if ws.get("plan_id")
                }
                for fut in as_completed(futures):
                    r = fut.result()
                    if r:
                        rows.append(r)
        else:
            for ws in filtered:
                if not ws.get("plan_id"):
                    continue
                r = _build_row(ws, quarter, file_date, tasks, sf_map, tracker, now)
                if r:
                    rows.append(r)

        rows.sort(key=lambda x: x["plan_id"])
        n = _upsert_rows(rows)
        if job_id:
            store.finish_sync_job(job_id, status="success", plans_upserted=n)
        return {"ok": True, "plans_upserted": n, "refreshed_at": now}
    except Exception as exc:
        if job_id:
            store.finish_sync_job(job_id, status="failed", error_message=str(exc))
        raise


def sync_all(workstreams: list[dict], tasks: list[dict]) -> dict[str, Any]:
    """Refresh all OPS × quarters present in Jira."""
    ops_labels = sorted(
        {comm.ops_label(ws["ops"]) for ws in workstreams if ws.get("ops")},
        key=str.lower,
    )
    job_id = store.start_sync_job("all")
    total = 0
    errors: list[str] = []
    try:
        for ops_label in ops_labels:
            for quarter in QUARTERS:
                try:
                    r = sync_ops_quarter(
                        ops_label, quarter, workstreams, tasks,
                        scope="all", manage_job=False,
                    )
                    total += r.get("plans_upserted", 0)
                except Exception as exc:
                    errors.append(f"{ops_label}/{quarter}: {exc}")
        status = "success" if not errors else "failed"
        store.finish_sync_job(
            job_id,
            status=status,
            plans_upserted=total,
            error_message="; ".join(errors[:5]) if errors else None,
        )
        return {"ok": not errors, "plans_upserted": total, "errors": errors}
    except Exception as exc:
        store.finish_sync_job(job_id, status="failed", error_message=str(exc))
        raise
