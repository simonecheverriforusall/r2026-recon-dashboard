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

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

# ── env (must load before snowflake_client — it reads SNOWFLAKE_ENABLED at import) ─
load_dotenv(Path(__file__).parent / ".env")

import communications as comm
import comms_sync
import devrev_client as devrev
import snowflake_client as sf
import supabase_store as sb_store

JIRA_BASE  = os.environ.get("JIRA_BASE_URL", "https://forusall401k.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
PROJECT    = os.environ.get("JIRA_PROJECT_KEY", "R2026")

REQUIRE_AUTH         = os.environ.get("REQUIRE_AUTH", "true").lower() in ("1", "true", "yes")
SESSION_SECRET       = os.environ.get("SESSION_SECRET", "")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "forusall.com").lstrip("@")
APP_BASE_URL         = os.environ.get("APP_BASE_URL", "").rstrip("/")

COMMS_SYNC_SECRET = os.environ.get("COMMS_SYNC_SECRET", "").strip()

AUTH_PUBLIC_PATHS = {"/api/health", "/auth/login", "/auth/callback", "/api/communications/sync-all"}

# ── Jira custom fields (R2026) ────────────────────────────────────────────────
OPS_CF          = "customfield_11675"
PLAN_ID_CF      = "customfield_11661"
SYMLINK_CF      = "customfield_11662"
AUDIT_CF        = "customfield_11667"   # select: TRUE=11683 / FALSE=11684
RK_CF           = "customfield_11680"   # Record Keeper (text)
OFF_CALENDAR_CF = "customfield_11671"   # select: off-calendar Yes=11689 / No=11690

AUDIT_TRUE_ID     = "11683"
OFF_CAL_TRUE_ID   = "11689"

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
JIRA_HTTP_TIMEOUT = 120.0  # Render cold starts + large Jira pagination

log = logging.getLogger("uvicorn.error")


def _jira_error_response(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = f"Jira API error: {exc.response.status_code} {exc.response.text[:200]}"
    elif isinstance(exc, httpx.HTTPError):
        detail = f"Jira request failed: {type(exc).__name__}: {exc}"
    else:
        detail = f"Jira fetch failed: {exc}"
    return HTTPException(status_code=502, detail=detail)


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


def _build_dashboard(
    ops: str | None = None,
    record_keeper: str | None = None,
    calendar: str | None = None,
    quarter: str | None = None,
) -> dict:
    """Fetch Jira data (or use flow) and compute metrics with optional filters."""
    raw = _fetch_raw()
    return _compute_dashboard(
        raw["workstreams"],
        raw["tasks"],
        ops=ops,
        record_keeper=record_keeper,
        calendar=calendar,
        quarter=quarter,
    )


def _fetch_raw() -> dict:
    """Fetch and parse all workstreams and tasks from Jira (parallel requests)."""
    auth = (JIRA_EMAIL, JIRA_TOKEN)
    headers = {"Accept": "application/json"}
    ws_jql = f"project = {PROJECT} AND issuetype = Workstream ORDER BY created ASC"
    task_jql = f"project = {PROJECT} AND issuetype = Task ORDER BY created ASC"
    ws_fields = [
        "key", "summary", "status", "resolutiondate",
        OPS_CF, PLAN_ID_CF, SYMLINK_CF, AUDIT_CF, RK_CF, OFF_CALENDAR_CF,
    ]
    task_fields = ["key", "summary", "status", "parent", "resolutiondate"]

    def _fetch(jql: str, fields: list[str]) -> list[dict]:
        with httpx.Client(auth=auth, headers=headers, timeout=JIRA_HTTP_TIMEOUT) as client:
            return _search_all(client, jql, fields)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ws_future = pool.submit(_fetch, ws_jql, ws_fields)
        task_future = pool.submit(_fetch, task_jql, task_fields)
        ws_issues = ws_future.result()
        task_issues = task_future.result()

    workstreams: list[dict] = []
    for iss in ws_issues:
        f = iss["fields"]
        audit_raw = f.get(AUDIT_CF) or {}
        off_cal_raw = f.get(OFF_CALENDAR_CF) or {}
        workstreams.append({
            "key":            iss["key"],
            "plan_id":        str(f.get(PLAN_ID_CF) or "").strip(),
            "symlink":        str(f.get(SYMLINK_CF) or "").strip(),
            "ops":            str(f.get(OPS_CF) or "").strip(),
            "record_keeper":  str(f.get(RK_CF) or "").strip(),
            "off_calendar":   off_cal_raw.get("id") == OFF_CAL_TRUE_ID,
            "status":         (f.get("status") or {}).get("name", ""),
            "audit":          audit_raw.get("id") == AUDIT_TRUE_ID,
        })

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

    return {"workstreams": workstreams, "tasks": tasks}


def _ensure_jira_raw() -> dict:
    """Return cached Jira workstreams + tasks, refreshing if stale."""
    global _cache
    now = time.time()
    if not _cache.get("raw") or (now - _cache.get("ts", 0)) >= CACHE_TTL:
        try:
            _cache = {"raw": _fetch_raw(), "ts": now}
        except Exception as exc:
            raise _jira_error_response(exc) from exc
    return _cache["raw"]


def _warm_jira_cache() -> None:
    if not JIRA_EMAIL or not JIRA_TOKEN:
        return
    try:
        raw = _ensure_jira_raw()
        log.info(
            "Jira cache ready: %s workstreams, %s tasks",
            len(raw["workstreams"]),
            len(raw["tasks"]),
        )
    except HTTPException as exc:
        log.warning("Jira cache warm failed: %s", exc.detail)
    except Exception as exc:
        log.warning("Jira cache warm failed: %s", exc)


def _filter_options(workstreams: list[dict]) -> dict:
    ops = sorted({_ops_label(ws["ops"]) for ws in workstreams if ws.get("ops")}, key=str.lower)
    rk  = sorted({ws["record_keeper"] for ws in workstreams if ws.get("record_keeper")}, key=str.lower)
    return {
        "ops":            ops,
        "record_keeper":  rk,
        "calendar": [
            {"value": "on",  "label": "On calendar"},
            {"value": "off", "label": "Off calendar"},
        ],
        "quarters": ["Q1", "Q2", "Q3", "Q4"],
    }


def _matches_ws_filters(
    ws: dict,
    ops: str | None,
    record_keeper: str | None,
    calendar: str | None,
) -> bool:
    if ops and _ops_label(ws.get("ops", "")) != ops:
        return False
    if record_keeper and ws.get("record_keeper", "") != record_keeper:
        return False
    if calendar == "on" and ws.get("off_calendar"):
        return False
    if calendar == "off" and not ws.get("off_calendar"):
        return False
    return True


def _task_in_scope(t: dict, ws_keys: set[str], quarter: str | None) -> bool:
    if t["parent_key"] not in ws_keys:
        return False
    if not quarter:
        return True
    if t["summary"] in QUARTER_TASKS:
        return t["summary"] == quarter
    if t["summary"] in CENSUS_TASKS:
        return True
    return False


def _compute_dashboard(
    all_workstreams: list[dict],
    all_tasks: list[dict],
    ops: str | None = None,
    record_keeper: str | None = None,
    calendar: str | None = None,
    quarter: str | None = None,
) -> dict:
    """Compute dashboard metrics for a filtered subset of plans/tasks."""
    quarter = quarter if quarter in QUARTER_TASKS else None

    workstreams = [
        ws for ws in all_workstreams
        if _matches_ws_filters(ws, ops, record_keeper, calendar)
    ]
    ws_keys = {ws["key"] for ws in workstreams}
    tasks   = [t for t in all_tasks if _task_in_scope(t, ws_keys, quarter)]

    tasks_by_ws: dict[str, list[dict]] = defaultdict(list)
    for t in tasks:
        tasks_by_ws[t["parent_key"]].append(t)

    for ws in workstreams:
        ws_tasks = tasks_by_ws[ws["key"]]
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
    q_list = [quarter] if quarter else ["Q1", "Q2", "Q3", "Q4"]
    q_counters: dict[str, dict[str, int]] = {q: {"done": 0, "pending": 0} for q in q_list}
    for t in tasks:
        if t["summary"] in QUARTER_TASKS and t["summary"] in q_counters:
            key = "done" if t["done"] else "pending"
            q_counters[t["summary"]][key] += 1

    quarters = [
        {"quarter": q, "done": v["done"], "pending": v["pending"]}
        for q, v in q_counters.items()
    ]

    ws_by_key = {ws["key"]: ws for ws in workstreams}

    quarter_done_plans: dict[str, list[dict]] = {q: [] for q in q_list}
    for t in tasks:
        if t["summary"] not in QUARTER_TASKS or not t["done"]:
            continue
        if t["summary"] not in quarter_done_plans:
            continue
        ws = ws_by_key.get(t["parent_key"])
        if not ws:
            continue
        quarter_done_plans[t["summary"]].append({
            "plan_id":  ws["plan_id"],
            "symlink":  ws["symlink"],
            "ops":      _ops_label(ws["ops"]),
            "ws_key":   ws["key"],
            "task_key": t["key"],
        })
    for q in quarter_done_plans:
        quarter_done_plans[q].sort(
            key=lambda x: int(x["plan_id"]) if x["plan_id"].isdigit() else 0
        )

    # ── OPS breakdown (Q tasks only) ──────────────────────────────────────────
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
        for ops_name, status_dict in sorted(
            bucket.items(), key=lambda x: -sum(len(v) for v in x[1].values())
        ):
            row: dict = {"name": ops_name}
            for s in _STATUS_ORDER:
                items = status_dict.get(s, [])
                row[s] = {"count": len(items), "tasks": items}
            row["total"] = sum(row[s]["count"] for s in _STATUS_ORDER)
            rows.append(row)
        if rows:
            totals: dict = {"name": "__total__"}
            for s in _STATUS_ORDER:
                totals[s] = {"count": sum(r[s]["count"] for r in rows), "tasks": []}
            totals["total"] = sum(r["total"] for r in rows)
            rows.append(totals)
        return rows

    census_breakdown = {
        "audit":         _census_table(census_bucket[True]),
        "non_audit":     _census_table(census_bucket[False]),
        "status_labels": _STATUS_LABELS,
    }

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
            "all_plans":        len(all_workstreams),
        },
        "filters":         _filter_options(all_workstreams),
        "active_filters": {
            "ops":            ops or "",
            "record_keeper":  record_keeper or "",
            "calendar":       calendar or "",
            "quarter":        quarter or "",
        },
        "quarters":               quarters,
        "quarter_done_plans":     quarter_done_plans,
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


# ── Google OAuth (@forusall.com) ──────────────────────────────────────────────

oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _auth_configured() -> bool:
    return bool(SESSION_SECRET and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _email_allowed(email: str) -> bool:
    email = (email or "").strip().lower()
    return email.endswith(f"@{ALLOWED_EMAIL_DOMAIN.lower()}")


def _app_base_url(request: Request) -> str:
    if APP_BASE_URL:
        return APP_BASE_URL
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if JIRA_EMAIL and JIRA_TOKEN:
        threading.Thread(target=_warm_jira_cache, name="jira-cache-warm", daemon=True).start()
    yield


app = FastAPI(title="R2026 Reconciliation Dashboard API", lifespan=_lifespan)

_session_secret = SESSION_SECRET or secrets.token_urlsafe(32)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not REQUIRE_AUTH:
            return await call_next(request)

        path = request.url.path
        if path in AUTH_PUBLIC_PATHS:
            return await call_next(request)

        if not _auth_configured():
            return JSONResponse(
                status_code=503,
                content={"detail": "Auth not configured. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET."},
            )

        user = request.session.get("user")
        if user and _email_allowed(user.get("email", "")):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return RedirectResponse("/auth/login", status_code=302)


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=_session_secret)


@app.get("/auth/login")
async def auth_login(request: Request):
    if not _auth_configured():
        return HTMLResponse(
            "<h1>Auth not configured</h1><p>Set Google OAuth env vars on the server.</p>",
            status_code=503,
        )
    redirect_uri = f"{_app_base_url(request)}/auth/callback"
    return await oauth.google.authorize_redirect(
        request,
        redirect_uri,
        hd=ALLOWED_EMAIL_DOMAIN,
    )


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not _auth_configured():
        return RedirectResponse("/auth/login")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/auth/login")
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email", "")
    if not _email_allowed(email):
        request.session.clear()
        return HTMLResponse(
            f"<h1>Access denied</h1><p>Only @{ALLOWED_EMAIL_DOMAIN} accounts are allowed.</p>",
            status_code=403,
        )
    request.session["user"] = {
        "email": email,
        "name":  userinfo.get("name", ""),
    }
    return RedirectResponse("/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login")


@app.get("/api/dashboard")
def get_dashboard(
    refresh: bool = Query(False),
    ops: str | None = Query(None),
    record_keeper: str | None = Query(None),
    calendar: str | None = Query(None, description="on or off"),
    quarter: str | None = Query(None, description="Q1, Q2, Q3, or Q4"),
):
    """Return dashboard data (cached raw Jira fetch 5 min; filters applied per request)."""
    global _cache
    now = time.time()

    if not JIRA_EMAIL or not JIRA_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Jira credentials not configured. Check your .env file.",
        )

    calendar = calendar if calendar in ("on", "off") else None
    quarter  = quarter if quarter in QUARTER_TASKS else None

    try:
        if refresh:
            _cache = {}
        raw = _ensure_jira_raw()
        data = _compute_dashboard(
            raw["workstreams"],
            raw["tasks"],
            ops=ops or None,
            record_keeper=record_keeper or None,
            calendar=calendar,
            quarter=quarter,
        )
    except Exception as exc:
        raise _jira_error_response(exc) from exc

    return data


@app.get("/api/health")
def health():
    raw = _cache.get("raw")
    return {
        "status": "ok",
        "cached": bool(raw),
        "workstreams": len(raw["workstreams"]) if raw else 0,
        "tasks": len(raw["tasks"]) if raw else 0,
    }


@app.get("/api/snowflake/test")
def snowflake_test():
    """
    Read-only Snowflake connectivity check (requires auth when REQUIRE_AUTH=true).
    Set SNOWFLAKE_ENABLED=true and RSA keypair env vars — see README.
    """
    if not sf.is_enabled():
        return JSONResponse(
            status_code=200,
            content={"ok": False, "reason": "disabled", "hint": "Set SNOWFLAKE_ENABLED=true"},
        )

    missing = sf.validate_config()
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Snowflake not configured. Missing: {', '.join(missing)}",
        )

    try:
        row = sf.test_connection()
    except sf.SnowflakeKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sf.SnowflakeConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sf.SnowflakeConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"ok": True, **row}


def _snowflake_enabled_or_http():
    if not sf.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Snowflake disabled. Set SNOWFLAKE_ENABLED=true in .env",
        )
    missing = sf.validate_config()
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Snowflake not configured. Missing: {', '.join(missing)}",
        )


@app.get("/api/snowflake/plans-in-bucket")
def snowflake_plans_in_bucket(
    plan_id: int = Query(..., description="Plan ID (e.g. 92)"),
    ops: str | None = Query(None, description="Optional OPS email filter (matches EMAIL column)"),
):
    """
    Rows from RECON_PROJECT.CONTROL.PLANS_IN_BUCKET for one plan.
    Example: /api/snowflake/plans-in-bucket?plan_id=92
    With OPS filter: ?plan_id=92&ops=ana@forusall.com
    """
    _snowflake_enabled_or_http()
    try:
        rows = sf.fetch_plans_in_bucket(plan_id, ops_email=ops)
    except sf.SnowflakeKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sf.SnowflakeConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sf.SnowflakeConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "ok": True,
        "plan_id": plan_id,
        "ops": ops,
        "count": len(rows),
        "rows": rows,
    }


class CommunicationsSendBody(BaseModel):
    plan_id: str
    quarter: str
    ops: str


class SponsorEmailsBody(BaseModel):
    emails: list[str]


def _session_email(request: Request) -> str | None:
    user = request.session.get("user") or {}
    return user.get("email")


def _comms_plans_response(ops: str | None, quarter: str | None):
    if not ops or not quarter or quarter not in QUARTER_TASKS:
        return {
            "ok": True,
            "plans": [],
            "message": "Select OPS and quarter",
            "summary": {"total": 0, "eligible": 0, "pending": 0},
            "stale": False,
            "refreshed_at": None,
        }
    if not sb_store.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
        )
    try:
        plans, meta = sb_store.list_plans(ops, quarter)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "ops": ops,
        "quarter": quarter,
        "file_date": comm.quarter_file_date(quarter),
        "plans": plans,
        "summary": meta["summary"],
        "stale": meta["stale"],
        "refreshed_at": meta["refreshed_at"],
        "last_sync_job": sb_store.latest_sync_job(),
    }


@app.get("/api/communications/meta")
def communications_meta():
    """OPS list, quarters, and integration flags for Communications tab."""
    base = comm.meta_from_workstreams(_ensure_jira_raw()["workstreams"])
    if sb_store.is_configured():
        try:
            sb_ops = sb_store.distinct_ops_labels()
            if sb_ops:
                base["ops"] = sorted(
                    set(base.get("ops") or []) | set(sb_ops),
                    key=str.lower,
                )
        except Exception:
            pass
    base["supabase_configured"] = sb_store.is_configured()
    base["jira_base"] = JIRA_BASE
    return base


@app.get("/api/communications/plans")
def communications_plans(
    ops: str | None = Query(None),
    quarter: str | None = Query(None),
):
    return _comms_plans_response(ops, quarter)


@app.get("/api/communications/eligible")
def communications_eligible(
    ops: str | None = Query(None),
    quarter: str | None = Query(None),
):
    """Alias for /plans (Supabase-backed)."""
    return _comms_plans_response(ops, quarter)


@app.get("/api/communications/plan")
def communications_plan(
    plan_id: str = Query(...),
    ops: str | None = Query(None),
    quarter: str | None = Query(None),
):
    if not quarter or quarter not in QUARTER_TASKS:
        raise HTTPException(status_code=400, detail="quarter is required")
    if not sb_store.is_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    try:
        pid = int(plan_id)
        plan = sb_store.get_plan(pid, quarter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid plan_id") from exc
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if ops and plan.get("ops_label") != ops:
        raise HTTPException(status_code=404, detail="Plan not found for this OPS filter")
    return {"ok": True, "plan": plan}


@app.patch("/api/communications/plans/{plan_id}/{quarter}/sponsors")
def communications_update_sponsors(
    plan_id: int,
    quarter: str,
    body: SponsorEmailsBody,
    request: Request,
):
    if quarter not in QUARTER_TASKS:
        raise HTTPException(status_code=400, detail="Invalid quarter")
    if not sb_store.is_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    emails = [e.strip().lower() for e in body.emails if e.strip()]
    try:
        plan = sb_store.update_sponsors(plan_id, quarter, emails, _session_email(request))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Plan not found") from exc
    return {"ok": True, "plan": plan}


@app.post("/api/communications/refresh")
def communications_refresh(
    ops: str = Query(...),
    quarter: str = Query(...),
):
    if quarter not in QUARTER_TASKS:
        raise HTTPException(status_code=400, detail="Invalid quarter")
    if not sb_store.is_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    raw = _ensure_jira_raw()
    try:
        result = comms_sync.sync_ops_quarter(
            ops, quarter, raw["workstreams"], raw["tasks"]
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/communications/sync-all")
def communications_sync_all(secret: str = Query("")):
    if not COMMS_SYNC_SECRET or secret != COMMS_SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Invalid sync secret")
    if not sb_store.is_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")
    raw = _ensure_jira_raw()
    try:
        return comms_sync.sync_all(raw["workstreams"], raw["tasks"])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/communications/send")
def communications_send(request: Request, body: CommunicationsSendBody = Body(...)):
    quarter = body.quarter if body.quarter in QUARTER_TASKS else None
    if not quarter or not body.ops.strip():
        raise HTTPException(status_code=400, detail="ops and quarter are required")
    if not sb_store.is_configured():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        pid = int(body.plan_id)
        plan = sb_store.get_plan(pid, quarter)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid plan_id") from exc
    if not plan or plan.get("ops_label") != body.ops.strip():
        raise HTTPException(status_code=404, detail="Plan not found for this OPS filter")
    if not plan["eligible"]:
        raise HTTPException(
            status_code=400,
            detail="Plan is not eligible — all gates must pass before sending",
        )

    payload = {
        "plan_id": plan["plan_id"],
        "symlink": plan["symlink"],
        "quarter": quarter,
        "ops": body.ops.strip(),
        "ws_key": plan["ws_key"],
        "file_date": comm.quarter_file_date(quarter),
        "sponsor_emails": plan.get("sponsor_emails_effective") or [],
    }
    result = devrev.send_communication(payload)
    status = "dry_run" if result.get("dry_run") else ("sent" if result.get("ok") else "failed")
    ticket = result.get("ticket_key") or result.get("devrev_ticket_key")
    try:
        plan = sb_store.update_send_result(
            pid,
            quarter,
            send_status=status,
            devrev_ticket_key=ticket,
            sent_by=_session_email(request) if request else None,
            send_error=None if result.get("ok") else result.get("message"),
        )
    except LookupError:
        pass
    return {"ok": result.get("ok", False), "plan": plan, "send": result}


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
