"""Communications eligibility: Snowflake + Jira Q + Drive gates."""
from __future__ import annotations

import os
from typing import Any

import drive_client as drive
import devrev_client as devrev
import snowflake_client as sf

QUARTER_TASKS = frozenset({"Q1", "Q2", "Q3", "Q4"})

RECON_YEAR = int(os.environ.get("RECON_YEAR", "2026"))

QUARTER_END_DATES = {
    "Q1": f"{RECON_YEAR}-03-31",
    "Q2": f"{RECON_YEAR}-06-30",
    "Q3": f"{RECON_YEAR}-09-30",
    "Q4": f"{RECON_YEAR}-12-31",
}


def ops_label(ops_email: str) -> str:
    if not ops_email:
        return "Unassigned"
    name = ops_email.split("@")[0].replace(".", " ")
    return " ".join(w.capitalize() for w in name.split())


def quarter_file_date(quarter: str) -> str | None:
    return QUARTER_END_DATES.get(quarter) if quarter in QUARTER_TASKS else None


def filter_workstreams_for_ops(workstreams: list[dict], ops_label_value: str) -> list[dict]:
    return [ws for ws in workstreams if ops_label(ws.get("ops", "")) == ops_label_value]


def _jira_q_check(ws_key: str, quarter: str, tasks: list[dict]) -> dict[str, Any]:
    for t in tasks:
        if t["parent_key"] == ws_key and t["summary"] == quarter:
            return {
                "ok": t["done"],
                "task_key": t["key"],
                "status": t["status"],
                "detail": "Done" if t["done"] else f"Status: {t['status']}",
            }
    return {"ok": False, "task_key": None, "status": None, "detail": f"No {quarter} task found"}


def _recon_complete_check(plan_id: str, file_date: str) -> dict[str, Any]:
    if not sf.is_enabled():
        return {
            "ok": False,
            "file_date": file_date,
            "rows": [],
            "detail": "Snowflake disabled",
        }
    try:
        pid = int(plan_id)
        rows = sf.fetch_recon_complete(pid, file_date)
        return {
            "ok": bool(rows),
            "file_date": file_date,
            "rows": rows,
            "detail": "Recon complete in Snowflake" if rows else "No row in RECON_COMPLETE_DETAILS",
        }
    except (ValueError, sf.SnowflakeConfigError, sf.SnowflakeKeyError, sf.SnowflakeConnectionError) as exc:
        return {
            "ok": False,
            "file_date": file_date,
            "rows": [],
            "detail": str(exc),
        }


def _drive_check(plan_id: str, symlink: str) -> dict[str, Any]:
    result = drive.required_comm_file_present(plan_id, symlink)
    configured = result["configured"]
    ok = result["found"] if configured else False
    return {
        "ok": ok,
        "configured": configured,
        "detail": result["detail"],
        "folder": result.get("folder"),
        "matched_files": result.get("matched_files", []),
    }


def _plan_eligible(checks: dict[str, dict]) -> bool:
    return all(c.get("ok") for c in checks.values())


def build_plan_eligibility(
    ws: dict,
    quarter: str,
    tasks: list[dict],
    file_date: str,
) -> dict[str, Any]:
    plan_id = ws["plan_id"]
    symlink = ws["symlink"]
    checks = {
        "recon_complete": _recon_complete_check(plan_id, file_date),
        "jira_q_done": _jira_q_check(ws["key"], quarter, tasks),
        "drive_file": _drive_check(plan_id, symlink),
    }
    return {
        "plan_id": plan_id,
        "symlink": symlink,
        "ws_key": ws["key"],
        "ops_email": ws.get("ops", ""),
        "ops_label": ops_label(ws.get("ops", "")),
        "quarter": quarter,
        "checks": checks,
        "eligible": _plan_eligible(checks),
    }


def build_eligibility(
    ops_label_value: str,
    quarter: str,
    workstreams: list[dict],
    tasks: list[dict],
) -> list[dict]:
    file_date = quarter_file_date(quarter)
    if not file_date:
        return []

    filtered = filter_workstreams_for_ops(workstreams, ops_label_value)
    filtered.sort(key=lambda ws: int(ws["plan_id"]) if ws["plan_id"].isdigit() else 0)

    return [
        build_plan_eligibility(ws, quarter, tasks, file_date)
        for ws in filtered
        if ws.get("plan_id")
    ]


def find_plan_eligibility(
    plan_id: str,
    ops_label_value: str,
    quarter: str,
    workstreams: list[dict],
    tasks: list[dict],
) -> dict | None:
    file_date = quarter_file_date(quarter)
    if not file_date:
        return None
    for ws in filter_workstreams_for_ops(workstreams, ops_label_value):
        if ws["plan_id"] == str(plan_id).strip():
            return build_plan_eligibility(ws, quarter, tasks, file_date)
    return None


def meta_from_workstreams(workstreams: list[dict]) -> dict[str, Any]:
    ops_list = sorted(
        {ops_label(ws["ops"]) for ws in workstreams if ws.get("ops")},
        key=str.lower,
    )
    return {
        "quarters": list(QUARTER_TASKS),
        "ops": ops_list,
        "recon_year": RECON_YEAR,
        "quarter_end_dates": QUARTER_END_DATES,
        "snowflake_enabled": sf.is_enabled(),
        "drive_read_ready": drive.drive_read_ready(),
        "drive_pattern_configured": drive.comm_pattern_configured(),
        "devrev_configured": devrev.is_configured(),
        "communications_dry_run": devrev.COMMUNICATIONS_DRY_RUN,
    }


def summary_counts(plans: list[dict]) -> dict[str, int]:
    eligible = sum(1 for p in plans if p["eligible"])
    return {
        "total": len(plans),
        "eligible": eligible,
        "pending": len(plans) - eligible,
    }
