"""DevRev integration stub for Communications send."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

DEVREV_API_TOKEN = os.environ.get("DEVREV_API_TOKEN", "").strip()
DEVREV_ENABLED = os.environ.get("DEVREV_ENABLED", "false").lower() in ("1", "true", "yes")
COMMUNICATIONS_DRY_RUN = os.environ.get("COMMUNICATIONS_DRY_RUN", "true").lower() in ("1", "true", "yes")


def is_configured() -> bool:
    return DEVREV_ENABLED and bool(DEVREV_API_TOKEN)


def send_communication(payload: dict, *, dry_run: bool | None = None) -> dict:
    """
    Send sponsor communication via DevRev (stub until template/API defined).
    """
    if dry_run is None:
        dry_run = COMMUNICATIONS_DRY_RUN

    if dry_run or not is_configured():
        logger.info("communications dry-run payload=%s", {k: payload.get(k) for k in payload})
        return {
            "ok": True,
            "dry_run": True,
            "message": "Communication not sent (dry run or DevRev not enabled)",
            "payload": payload,
        }

    # Future: httpx POST to DevRev API with DEVREV_API_TOKEN
    return {
        "ok": False,
        "dry_run": False,
        "message": "DevRev send not implemented yet — template required",
        "payload": payload,
    }
