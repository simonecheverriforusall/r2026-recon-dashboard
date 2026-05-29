"""
Snowflake connectivity for recon-dashboard (RSA keypair auth).

Configure via environment variables; see .env.example.
"""
from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_REQUIRED_WHEN_ENABLED = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_WAREHOUSE")


class SnowflakeConfigError(Exception):
    """Missing or invalid Snowflake configuration."""


class SnowflakeKeyError(Exception):
    """Private key load or decrypt failed."""


class SnowflakeConnectionError(Exception):
    """Query or network failure talking to Snowflake."""


def is_enabled() -> bool:
    return os.environ.get("SNOWFLAKE_ENABLED", "false").lower() in ("1", "true", "yes")


def _pem_material() -> str:
    """Return PEM text from env PEM, base64 env, or optional file path."""
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY", "").strip()
    if pem:
        return pem.replace("\\n", "\n")

    b64 = os.environ.get("SNOWFLAKE_PRIVATE_KEY_BASE64", "").strip()
    if b64:
        return base64.b64decode(b64).decode("utf-8")

    path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
    if path:
        key_path = Path(path).expanduser()
        if not key_path.is_file():
            raise SnowflakeKeyError(f"SNOWFLAKE_PRIVATE_KEY_PATH not found: {key_path}")
        return key_path.read_text(encoding="utf-8")

    raise SnowflakeConfigError(
        "Set SNOWFLAKE_PRIVATE_KEY, SNOWFLAKE_PRIVATE_KEY_BASE64, or SNOWFLAKE_PRIVATE_KEY_PATH"
    )


def load_private_key_bytes() -> bytes:
    """DER PKCS8 bytes for snowflake.connector private_key=."""
    pem = _pem_material()
    passphrase = (
        os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "").strip()
        or os.environ.get("PRIVATE_KEY_PASSPHRASE", "").strip()  # snowflake_pass helper
        or None
    )
    try:
        p_key = serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=passphrase.encode("utf-8") if passphrase else None,
            backend=default_backend(),
        )
    except ValueError as exc:
        raise SnowflakeKeyError("Failed to decrypt private key — check SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") from exc
    except TypeError as exc:
        raise SnowflakeKeyError("Invalid private key PEM") from exc

    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def validate_config() -> list[str]:
    """Return list of missing required env var names (empty if OK)."""
    missing = [k for k in _REQUIRED_WHEN_ENABLED if not os.environ.get(k, "").strip()]
    if not missing:
        try:
            _pem_material()
        except SnowflakeConfigError:
            missing.append("SNOWFLAKE_PRIVATE_KEY (or _BASE64 / _PATH)")
    return missing


@contextmanager
def get_connection() -> Iterator[Any]:
    import snowflake.connector
    from snowflake.connector.errors import Error as SnowflakeError

    missing = validate_config()
    if missing:
        raise SnowflakeConfigError(f"Missing: {', '.join(missing)}")

    kwargs: dict[str, Any] = {
        "account":   os.environ["SNOWFLAKE_ACCOUNT"].strip(),
        "user":      os.environ["SNOWFLAKE_USER"].strip(),
        "private_key": load_private_key_bytes(),
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"].strip(),
        "login_timeout": 30,
        "network_timeout": 30,
    }
    for env_key, param in (
        ("SNOWFLAKE_ROLE", "role"),
        ("SNOWFLAKE_DATABASE", "database"),
        ("SNOWFLAKE_SCHEMA", "schema"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            kwargs[param] = val

    conn = None
    try:
        conn = snowflake.connector.connect(**kwargs)
        yield conn
    except SnowflakeError as exc:
        raise SnowflakeConnectionError(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()


def test_connection() -> dict[str, str]:
    """
    Run a read-only identity query. Returns user, role, warehouse, database.
    """
    sql = (
        "SELECT CURRENT_USER() AS user, CURRENT_ROLE() AS role, "
        "CURRENT_WAREHOUSE() AS warehouse, CURRENT_DATABASE() AS database"
    )
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            row = cur.fetchone()
            if not row:
                raise SnowflakeConnectionError("Snowflake returned no rows")
            cols = [d[0].lower() for d in cur.description]
            return dict(zip(cols, (str(v) if v is not None else "" for v in row)))
        finally:
            cur.close()


def _serialize_value(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val) if val % 1 else int(val)
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def _rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    cols = [d[0].lower() for d in cursor.description]
    return [
        {col: _serialize_value(val) for col, val in zip(cols, row)}
        for row in cursor.fetchall()
    ]


PLANS_IN_BUCKET_TABLE = "RECON_PROJECT.CONTROL.PLANS_IN_BUCKET"
RECON_COMPLETE_VIEW = "RECON_PROJECT.CONTROL.RECON_COMPLETE_DETAILS"


def fetch_recon_complete(plan_id: int, file_date: str) -> list[dict[str, Any]]:
    """Rows from RECON_COMPLETE_DETAILS for plan + quarter end date."""
    sql = (
        f"SELECT * FROM {RECON_COMPLETE_VIEW} "
        "WHERE TRUNC(PLAN_ID) = %s AND FILE_DATE = %s "
        "ORDER BY FILE_DATE DESC"
    )
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, (plan_id, file_date))
            return _rows_to_dicts(cur)
        finally:
            cur.close()


def recon_complete_exists(plan_id: int, file_date: str) -> bool:
    return len(fetch_recon_complete(plan_id, file_date)) > 0


def fetch_recon_complete_bulk(plan_ids: list[int], file_date: str) -> dict[int, list[dict[str, Any]]]:
    """All RECON_COMPLETE_DETAILS rows for quarter end date and plan IDs (one query)."""
    if not plan_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(plan_ids))
    sql = (
        f"SELECT * FROM {RECON_COMPLETE_VIEW} "
        f"WHERE FILE_DATE = %s AND TRUNC(PLAN_ID) IN ({placeholders})"
    )
    params: list[Any] = [file_date, *plan_ids]
    result: dict[int, list[dict[str, Any]]] = {}
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            for row in _rows_to_dicts(cur):
                pid = row.get("plan_id")
                if pid is None:
                    continue
                key = int(pid) if not isinstance(pid, int) else pid
                result.setdefault(key, []).append(row)
        finally:
            cur.close()
    return result


def fetch_plans_in_bucket(plan_id: int, ops_email: str | None = None) -> list[dict[str, Any]]:
    """
    Read rows from RECON_PROJECT.CONTROL.PLANS_IN_BUCKET for one plan.
    Optional ops_email filters on EMAIL (OPS contact on the row).
    """
    sql = f"SELECT * FROM {PLANS_IN_BUCKET_TABLE} WHERE PLAN_ID = %s"
    params: list[Any] = [plan_id]
    if ops_email:
        sql += " AND LOWER(EMAIL) = LOWER(%s)"
        params.append(ops_email.strip())
    sql += " ORDER BY FILE_DATE DESC NULLS LAST"

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return _rows_to_dicts(cur)
        finally:
            cur.close()
