#!/usr/bin/env python3
"""Sync recon-dashboard .env to Render service env vars (full PUT)."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import dotenv_values

SERVICE_ID = "srv-d88c21i8qa3s73f5aeqg"
ROOT = Path(__file__).resolve().parent.parent


def snowflake_passphrase(env: dict) -> str:
    p = (env.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or "").strip()
    if p:
        return p
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                "snowflake_cli_pass",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def snowflake_key_b64(env: dict) -> str:
    path = env.get("SNOWFLAKE_PRIVATE_KEY_PATH", "").strip()
    if not path:
        return env.get("SNOWFLAKE_PRIVATE_KEY_BASE64", "").strip()
    p = Path(path).expanduser()
    if not p.is_file():
        return ""
    return base64.b64encode(p.read_bytes()).decode("ascii")


def build_payload(env: dict) -> list[dict[str, str]]:
    sf_b64 = snowflake_key_b64(env)
    sf_pass = snowflake_passphrase(env)
    entries = {
        "JIRA_BASE_URL": env.get("JIRA_BASE_URL", ""),
        "JIRA_EMAIL": env.get("JIRA_EMAIL", ""),
        "JIRA_API_TOKEN": env.get("JIRA_API_TOKEN", ""),
        "JIRA_PROJECT_KEY": env.get("JIRA_PROJECT_KEY", "R2026"),
        "JIRA_TEMPLATE_ISSUE_KEY": env.get("JIRA_TEMPLATE_ISSUE_KEY", "R2026-2"),
        "JIRA_ISSUE_TYPE_NAME": env.get("JIRA_ISSUE_TYPE_NAME", "Workstream"),
        "REQUIRE_AUTH": "true",
        "ALLOWED_EMAIL_DOMAIN": env.get("ALLOWED_EMAIL_DOMAIN", "forusall.com"),
        "SESSION_SECRET": env.get("SESSION_SECRET", ""),
        "GOOGLE_CLIENT_ID": env.get("GOOGLE_CLIENT_ID", ""),
        "GOOGLE_CLIENT_SECRET": env.get("GOOGLE_CLIENT_SECRET", ""),
        "SNOWFLAKE_ENABLED": env.get("SNOWFLAKE_ENABLED", "false"),
        "SNOWFLAKE_ACCOUNT": env.get("SNOWFLAKE_ACCOUNT", ""),
        "SNOWFLAKE_USER": env.get("SNOWFLAKE_USER", ""),
        "SNOWFLAKE_WAREHOUSE": env.get("SNOWFLAKE_WAREHOUSE", ""),
        "SNOWFLAKE_ROLE": env.get("SNOWFLAKE_ROLE", ""),
        "SNOWFLAKE_DATABASE": env.get("SNOWFLAKE_DATABASE", ""),
        "SNOWFLAKE_SCHEMA": env.get("SNOWFLAKE_SCHEMA", ""),
        "SNOWFLAKE_PRIVATE_KEY_BASE64": sf_b64,
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE": sf_pass,
        "DRIVE_API_KEY": env.get("DRIVE_API_KEY", ""),
        "DRIVE_PARENT_FOLDER_ID": env.get("DRIVE_PARENT_FOLDER_ID", ""),
        "DRIVE_COMM_REQUIRED_PATTERN": env.get("DRIVE_COMM_REQUIRED_PATTERN", ""),
        "RECON_YEAR": env.get("RECON_YEAR", "2026"),
        "COMMUNICATIONS_DRY_RUN": env.get("COMMUNICATIONS_DRY_RUN", "true"),
        "DEVREV_ENABLED": env.get("DEVREV_ENABLED", "false"),
        "DEVREV_API_TOKEN": env.get("DEVREV_API_TOKEN", ""),
        "SUPABASE_URL": env.get("SUPABASE_URL", ""),
        "SUPABASE_SERVICE_ROLE_KEY": env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        "COMMS_USE_SUPABASE": env.get("COMMS_USE_SUPABASE", "true"),
        "COMMS_CACHE_TTL_HOURS": env.get("COMMS_CACHE_TTL_HOURS", "6"),
        "COMMS_SYNC_SECRET": env.get("COMMS_SYNC_SECRET", ""),
    }
    return [{"key": k, "value": str(v)} for k, v in entries.items()]


def main() -> int:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        print(f"Missing {env_file}", file=sys.stderr)
        return 1

    env = dotenv_values(env_file)
    sf_pass = snowflake_passphrase(env)
    cli_path = Path.home() / ".render/cli.yaml"
    api_key = yaml.safe_load(cli_path.read_text())["api"]["key"]
    payload = build_payload(env)

    body = json.dumps(payload)
    proc = subprocess.run(
        [
            "curl", "-sfS", "-X", "PUT",
            "-H", f"Authorization: Bearer {api_key}",
            "-H", "Content-Type: application/json",
            "-d", body,
            f"https://api.render.com/v1/services/{SERVICE_ID}/env-vars",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode

    result = json.loads(proc.stdout)
    keys = sorted(x["envVar"]["key"] for x in result)
    print(f"Set {len(keys)} env vars on Render ({SERVICE_ID})")
    for k in keys:
        print(f"  - {k}")

    if not snowflake_key_b64(env):
        print("WARN: SNOWFLAKE_PRIVATE_KEY_BASE64 is empty", file=sys.stderr)
    if not sf_pass:
        print(
            "WARN: SNOWFLAKE_PRIVATE_KEY_PASSPHRASE missing (.env and Keychain snowflake_cli_pass)",
            file=sys.stderr,
        )
    else:
        print("Snowflake passphrase loaded from Keychain", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
