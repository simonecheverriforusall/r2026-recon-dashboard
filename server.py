#!/usr/bin/env python3
"""
recon-dashboard/server.py
─────────────────────────
FastAPI backend for the R2026 Reconciliation Dashboard.

Fetches Workstreams and Tasks from Jira, computes all metrics,
and serves them as JSON.  The static/ folder holds the SPA frontend.

Run:
    uvicorn server:app --reload --port 8000

Then open http://localhost:8000
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ── env ───────────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

JIRA_BASE  = os.environ.get("JIRA_BASE_URL", "https://forusall401k.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
PROJECT    = os.environ.get("JIRA_PROJECT_KEY", "R2026")

# ── Jira custom fields (R2026) ────────────────────────────────────────────────
OPS_CF     = "customfield_11675"
PLAN_ID_CF = "customfield_11661"
SYMLINK_CF = "customfield_11662"
AUDIT_CF   = "customfield_11667"   # select field: TRUE=11683 / FALSE=11684

AUDIT_TRUE_ID = "11683"

PROJECT_START_WEEK = "2026-01-05"  # first Monday of 2026
QUARTER_TASKS = {"Q1", "Q2", "Q3", "Q4"}
CENSUS_TASKS  = {"Validate Employee Census", "Submit DMGY File"}

# Status normalisation (Jira status name → our key)
_STATUS_ORDER = ["in_progress", "done", "blocked", "to_do"]
_STATUS_LABELS = {
    "in_progress": "In Progress",
    "done":        "Done",
    "blocked":     "Blocked",
    "to_do":       "To Do",
}

def _norm_status(raw: str) -> str:
    s = raw.lower()
    if "done" in s:                        return "done"
    if "block" in s:                       return "blocked"
    if "progress" in s or "review" in s:   return "in_progress"
    return "to_do"


def _parse_jira_datetime(raw: str) -> datetime:
    """Parse Jira resolution/changelog timestamps (Z or ±HHMM offsets)."""
    s = raw.strip().replace("Z", "+00:00")
    # Jira Cloud often returns -0700 instead of ISO -07:00
    if len(s) >= 5 and s[-5] in "+-" and s[-4:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def _week_start(resolutiondate: str | None) -> str | None:
    if not resolutiondate:
        return None
    dt = _parse_jira_datetime(resolutiondate)
    week_start = dt - timedelta(days=dt.weekday())
    return week_start.strftime("%Y-%m-%d")


def _monday_of(dt: datetime) -> datetime:
    return dt - timedelta(days=dt.weekday())


def _week_range(start: str, end: str) -> list[str]:
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    weeks: list[str] = []
    while cur <= end_d:
        weeks.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=7)
    return weeks


def _format_weekly(counter: dict[str, int], start_week: str, end_week: str) -> list[dict]:
    return [
        {
            "week":       w,
            "week_label": datetime.strptime(w, "%Y-%m-%d").strftime("%b %d"),
            "count":      counter.get(w, 0),
        }
        for w in _week_range(start_week, end_week)
    ]

# ── simple in-process cache ───────────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL    = 300  # seconds


# ── Jira helpers ──────────────────────────────────────────────────────────────

def _search_all(client: httpx.Client, jql: str, fields: list[str]) -> list[dict]:
    """Cursor-paginated fetch for /rest/api/3/search/jql (Jira Cloud)."""
    results: list[dict] = []
    params: dict = {
        "jql":        jql,
        "maxResults": 100,
        "fields":     ",".join(fields),
    }
    while True:
        r = client.get(f"{JIRA_BASE}/rest/api/3/search/jql", params=params)
        r.raise_for_status()
        data  = r.json()
        batch = data.get("issues", [])
        results.extend(batch)
        if data.get("isLast", True) or not batch:
            break
        npt = data.get("nextPageToken")
        if not npt:
            break
        params["nextPageToken"] = npt
    return results


def _build_dashboard() -> dict:
    """Fetch Jira data and compute all dashboard metrics."""
    auth    = (JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    with httpx.Client(auth=auth, headers=headers, timeout=90.0) as client:
        ws_issues = _search_all(
            client,
            f"project = {PROJECT} AND issuetype = Workstream ORDER BY created ASC",
            ["key", "summary", "status", "resolutiondate", OPS_CF, PLAN_ID_CF, SYMLINK_CF, AUDIT_CF],
        )
        task_issues = _search_all(
            client,
            f"project = {PROJECT} AND issuetype = Task ORDER BY created ASC",
            ["key", "summary", "status", "parent", "resolutiondate"],
        )

    # ── parse workstreams ─────────────────────────────────────────────────────
    workstreams: list[dict] = []
    for iss in ws_issues:
        f = iss["fields"]
        audit_raw = f.get(AUDIT_CF) or {}
        workstreams.append({
            "key":     iss["key"],
            "plan_id": str(f.get(PLAN_ID_CF) or "").strip(),
            "symlink": str(f.get(SYMLINK_CF)  or "").strip(),
            "ops":     str(f.get(OPS_CF)       or "").strip(),
            "status":  (f.get("status") or {}).get("name", ""),
            "audit":   audit_raw.get("id") == AUDIT_TRUE_ID,
        })

    # ── parse tasks ───────────────────────────────────────────────────────────
    tasks: list[dict] = []
    for iss in task_issues:
        f      = iss["fields"]
        parent = f.get("parent") or {}
        tasks.append({
            "key":            iss["key"],
            "summary":        str(f.get("summary") or "").strip(),
            "parent_key":     parent.get("key", ""),
            "status":         (f.get("status") or {}).get("name", ""),
            "done":           (f.get("status") or {}).get("name", "").lower() == "done",
            "resolutiondate": f.get("resolutiondate"),
        })

    # ── enrich workstreams with per-ws task counts ────────────────────────────
    tasks_by_ws: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        tasks_by_ws[t["parent_key"]].append(t)

    for ws in workstreams:
        ws_tasks           = tasks_by_ws[ws["key"]]
        ws["total_tasks"]  = len(ws_tasks)
        ws["done_tasks"]   = sum(1 for t in ws_tasks if t["done"])
        ws["fully_reconciled"] = (
            ws["total_tasks"] > 0 and ws["total_tasks"] == ws["done_tasks"]
        )

    # ── summary KPIs ──────────────────────────────────────────────────────────
    total_plans  = len(workstreams)
    fully_rec    = sum(1 for ws in workstreams if ws["fully_reconciled"])
    in_progress  = sum(1 for ws in workstreams if 0 < ws["done_tasks"] < ws["total_tasks"])
    not_started  = sum(1 for ws in workstreams if ws["done_tasks"] == 0)
    total_tasks  = sum(ws["total_tasks"] for ws in workstreams)
    done_tasks   = sum(ws["done_tasks"]  for ws in workstreams)
    task_pct     = round(done_tasks / total_tasks * 100, 1) if total_tasks else 0.0
    census_done  = sum(1 for t in tasks if t["summary"] in CENSUS_TASKS and t["done"])

    # ── quarter breakdown ─────────────────────────────────────────────────────
    q_counters: dict[str, dict[str, int]] = {
        q: {"done": 0, "pending": 0} for q in ["Q1", "Q2", "Q3", "Q4"]
    }
    for t in tasks:
        if t["summary"] in QUARTER_TASKS:
            key = "done" if t["done"] else "pending"
            q_counters[t["summary"]][key] += 1

    quarters = [
        {"quarter": q, "done": v["done"], "pending": v["pending"]}
        for q, v in q_counters.items()
    ]

    # ── OPS breakdown (Q tasks only) ──────────────────────────────────────────
    ws_by_key   = {ws["key"]: ws for ws in workstreams}
    ops_counter: dict[str, dict[str, int]] = defaultdict(lambda: {"done": 0, "pending": 0})

    for t in tasks:
        if t["summary"] not in QUARTER_TASKS:
            continue
        ws    = ws_by_key.get(t["parent_key"])
        label = _ops_label(ws["ops"] if ws else "")
        ops_counter[label]["done" if t["done"] else "pending"] += 1

    ops_data = sorted(
        [{"name": k, **v, "total": v["done"] + v["pending"]} for k, v in ops_counter.items()],
        key=lambda x: x["pending"],
        reverse=True,
    )

    # ── weekly velocity (plans, Q tasks, census tasks) ────────────────────────
    weekly_plans_counter: dict[str, int] = defaultdict(int)
    for ws in workstreams:
        if not ws["fully_reconciled"]:
            continue
        dates = [
            t["resolutiondate"]
            for t in tasks_by_ws[ws["key"]]
            if t["done"] and t["resolutiondate"]
        ]
        if not dates:
            continue
        week = _week_start(max(dates))
        if week:
            weekly_plans_counter[week] += 1

    weekly_q_counter: dict[str, int] = defaultdict(int)
    weekly_census_counter: dict[str, int] = defaultdict(int)
    for t in tasks:
        if not t["done"]:
            continue
        week = _week_start(t["resolutiondate"])
        if not week:
            continue
        if t["summary"] in QUARTER_TASKS:
            weekly_q_counter[week] += 1
        elif t["summary"] in CENSUS_TASKS:
            weekly_census_counter[week] += 1

    # Shared calendar-week range: project start → current week (zeros filled in)
    current_week = _monday_of(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    all_weeks = (
        set(weekly_plans_counter)
        | set(weekly_q_counter)
        | set(weekly_census_counter)
    )
    if all_weeks:
        start_week = min(min(all_weeks), PROJECT_START_WEEK)
    else:
        start_week = PROJECT_START_WEEK
    if start_week > current_week:
        start_week = current_week

    weekly_data         = _format_weekly(weekly_plans_counter, start_week, current_week)
    weekly_q_data       = _format_weekly(weekly_q_counter, start_week, current_week)
    weekly_census_data  = _format_weekly(weekly_census_counter, start_week, current_week)

    # ── census breakdown by audit flag ───────────────────────────────────────
    # Structure: census_bucket[is_audit][ops_label][norm_status] = [task_detail, ...]
    # Each task_detail carries enough info to populate the drawer instantly.
    census_bucket: dict[bool, dict] = {
        True:  defaultdict(lambda: defaultdict(list)),
        False: defaultdict(lambda: defaultdict(list)),
    }
    for t in tasks:
        if t["summary"] not in CENSUS_TASKS:
            continue
        ws = ws_by_key.get(t["parent_key"])
        if not ws:
            continue
        label  = _ops_label(ws["ops"])
        status = _norm_status(t["status"])
        census_bucket[ws["audit"]][label][status].append({
            "task_key": t["key"],
            "ws_key":   ws["key"],
            "plan_id":  ws["plan_id"],
            "symlink":  ws["symlink"],
            "ops":      label,
        })

    def _census_table(bucket: dict) -> list[dict]:
        rows = []
        for ops, status_dict in sorted(bucket.items(),
                                        key=lambda x: -sum(len(v) for v in x[1].values())):
            row: dict = {"name": ops}
            for s in _STATUS_ORDER:
                items = status_dict.get(s, [])
                row[s] = {"count": len(items), "tasks": items}
            row["total"] = sum(row[s]["count"] for s in _STATUS_ORDER)
            rows.append(row)
        if rows:
            totals: dict = {"name": "__total__"}
            for s in _STATUS_ORDER:
                totals[s] = {
                    "count": sum(r[s]["count"] for r in rows),
                    "tasks": [],   # total row is not drillable
                }
            totals["total"] = sum(r["total"] for r in rows)
            rows.append(totals)
        return rows

    census_breakdown = {
        "audit":         _census_table(census_bucket[True]),
        "non_audit":     _census_table(census_bucket[False]),
        "status_labels": _STATUS_LABELS,
    }

    # ── fully reconciled list ─────────────────────────────────────────────────
    fr_plans = [
        {
            "plan_id": ws["plan_id"],
            "symlink": ws["symlink"],
            "ops":     _ops_label(ws["ops"]),
            "key":     ws["key"],
            "url":     f"{JIRA_BASE}/browse/{ws['key']}",
        }
        for ws in workstreams
        if ws["fully_reconciled"]
    ]
    fr_plans.sort(key=lambda x: int(x["plan_id"]) if x["plan_id"].isdigit() else 0)

    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%b %d, %Y %I:%M %p UTC"),
        "project":    PROJECT,
        "jira_base":  JIRA_BASE,
        "summary": {
            "total_plans":      total_plans,
            "fully_reconciled": fully_rec,
            "in_progress":      in_progress,
            "not_started":      not_started,
            "total_tasks":      total_tasks,
            "done_tasks":       done_tasks,
            "task_pct":         task_pct,
            "census_submitted": census_done,
        },
        "quarters":               quarters,
        "ops":                    ops_data,
        "weekly":                 weekly_data,
        "weekly_q_tasks":         weekly_q_data,
        "weekly_census_tasks":    weekly_census_data,
        "census_breakdown":       census_breakdown,
        "fully_reconciled_plans": fr_plans,
    }


def _adf_to_text(node: object) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    if not isinstance(node, dict):
        return ""
    t = node.get("text")
    if t:
        return str(t)
    content = node.get("content") or []
    parts   = [_adf_to_text(c) for c in content]
    joined  = " ".join(p for p in parts if p.strip())
    ntype   = node.get("type", "")
    if ntype in ("paragraph", "heading", "blockquote"):
        return joined + "\n"
    if ntype == "listItem":
        return "• " + joined
    return joined


def _ops_label(ops_email: str) -> str:
    if not ops_email:
        return "Unassigned"
    name = ops_email.split("@")[0].replace(".", " ")
    return " ".join(w.capitalize() for w in name.split())


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="R2026 Reconciliation Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/dashboard")
def get_dashboard(refresh: bool = Query(False)):
    """Return pre-computed dashboard data (cached 5 min)."""
    global _cache
    now = time.time()

    if not refresh and _cache.get("data") and (now - _cache.get("ts", 0)) < CACHE_TTL:
        return _cache["data"]

    if not JIRA_EMAIL or not JIRA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Jira credentials not configured. Check your .env file.",
        )

    try:
        data = _build_dashboard()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Jira API error: {exc}") from exc

    _cache = {"data": data, "ts": now}
    return data


@app.get("/api/health")
def health():
    return {"status": "ok", "cached": bool(_cache.get("data"))}


@app.get("/api/task-details")
def get_task_details(keys: str = Query(..., description="Comma-separated Jira task keys")):
    """
    Fetch Jira comments for a list of task keys.
    Comments are fetched in parallel (up to 8 threads) for speed.
    Returns: [{key, status, comments: [{author, text, date}]}]
    """
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    if not key_list:
        return []
    if len(key_list) > 100:
        raise HTTPException(status_code=400, detail="Max 100 keys per request")

    auth    = (JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}

    def _fetch_one(key: str) -> dict:
        with httpx.Client(auth=auth, headers=headers, timeout=15.0) as c:
            r = c.get(
                f"{JIRA_BASE}/rest/api/3/issue/{key}",
                params={"fields": "comment,status,summary"},
            )
            if r.status_code != 200:
                return {"key": key, "error": f"HTTP {r.status_code}", "comments": []}
            f = r.json().get("fields", {})
            raw_comments = (f.get("comment") or {}).get("comments", [])
            comments = []
            for c_obj in raw_comments:
                text = _adf_to_text(c_obj.get("body") or {}).strip()
                if text:
                    comments.append({
                        "author": (c_obj.get("author") or {}).get("displayName", ""),
                        "text":   text,
                        "date":   (c_obj.get("created") or "")[:10],
                    })
            return {
                "key":      key,
                "status":   (f.get("status") or {}).get("name", ""),
                "comments": comments,
            }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(key_list))) as ex:
        futures = {ex.submit(_fetch_one, k): k for k in key_list}
        for fut in as_completed(futures):
            results.append(fut.result())

    # Preserve original key order
    order = {k: i for i, k in enumerate(key_list)}
    results.sort(key=lambda x: order.get(x["key"], 999))
    return results


# Serve the SPA last so API routes take priority
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
