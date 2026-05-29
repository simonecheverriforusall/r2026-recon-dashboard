"""Google Drive read helpers for recon-dashboard Communications."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import requests as _requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

SCOPES = ["https://www.googleapis.com/auth/drive"]

DRIVE_API_KEY = os.environ.get("DRIVE_API_KEY", "")
DRIVE_PARENT_FOLDER = os.environ.get("DRIVE_PARENT_FOLDER_ID", "")
DRIVE_COMM_PATTERN = os.environ.get("DRIVE_COMM_REQUIRED_PATTERN", "").strip()

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_TOKEN = _ROOT / "reconciliation" / "drive_token.json"
TOKEN_FILE = Path(os.environ.get("DRIVE_TOKEN_PATH", str(_DEFAULT_TOKEN))).expanduser()


def _oauth_creds():
    if not TOKEN_FILE.is_file():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        if creds and creds.valid:
            return creds
    except Exception:
        pass
    return None


def drive_read_ready() -> bool:
    return bool(DRIVE_PARENT_FOLDER) and (bool(_oauth_creds()) or bool(DRIVE_API_KEY))


def comm_pattern_configured() -> bool:
    return bool(DRIVE_COMM_PATTERN)


def _folder_name(plan_id: str, symlink: str) -> str:
    return f"{plan_id} - {symlink}"


def _drive_service():
    creds = _oauth_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_plan_folder(plan_id: str, symlink: str) -> dict | None:
    if not DRIVE_PARENT_FOLDER:
        return None

    name = _folder_name(plan_id, symlink)
    name_safe = name.replace("'", "\\'")
    q = (
        f"name = '{name_safe}' "
        f"and '{DRIVE_PARENT_FOLDER}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )

    service = _drive_service()
    if service:
        try:
            result = service.files().list(
                q=q,
                fields="files(id,name)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            ).execute()
            files = result.get("files", [])
            if files:
                fid = files[0]["id"]
                return {
                    "id": fid,
                    "name": files[0]["name"],
                    "url": f"https://drive.google.com/drive/folders/{fid}",
                }
        except Exception:
            pass

    if not DRIVE_API_KEY:
        return None
    try:
        resp = _requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": q,
                "key": DRIVE_API_KEY,
                "fields": "files(id,name)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        files = resp.json().get("files", [])
        if not files:
            return None
        fid = files[0]["id"]
        return {
            "id": fid,
            "name": files[0]["name"],
            "url": f"https://drive.google.com/drive/folders/{fid}",
        }
    except Exception:
        return None


def list_folder_files(folder_id: str) -> list[dict]:
    """List non-folder files in a Drive folder."""
    service = _drive_service()
    q = f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    files: list[dict] = []

    if service:
        try:
            page_token = None
            while True:
                result = service.files().list(
                    q=q,
                    fields="nextPageToken, files(id, name, mimeType)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageToken=page_token,
                ).execute()
                for f in result.get("files", []):
                    files.append({
                        "id": f["id"],
                        "name": f["name"],
                        "mimeType": f.get("mimeType", ""),
                        "url": f"https://drive.google.com/file/d/{f['id']}/view",
                    })
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
            return files
        except Exception:
            pass

    if not DRIVE_API_KEY:
        return []
    try:
        resp = _requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": q,
                "key": DRIVE_API_KEY,
                "fields": "files(id,name,mimeType)",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        for f in resp.json().get("files", []):
            files.append({
                "id": f["id"],
                "name": f["name"],
                "mimeType": f.get("mimeType", ""),
                "url": f"https://drive.google.com/file/d/{f['id']}/view",
            })
    except Exception:
        pass
    return files


def list_plan_folder_files(plan_id: str, symlink: str) -> list[dict]:
    folder = find_plan_folder(plan_id, symlink)
    if not folder:
        return []
    return list_folder_files(folder["id"])


def _name_matches(name: str, pattern: str) -> bool:
    pat = pattern.lower()
    nm = name.lower()
    if "*" in pat or "?" in pat:
        return fnmatch.fnmatch(nm, pat)
    return pat in nm


def required_comm_file_present(plan_id: str, symlink: str) -> dict:
    """
    Check Drive for required communication file.
    configured=false when DRIVE_COMM_REQUIRED_PATTERN is unset.
    """
    if not DRIVE_COMM_PATTERN:
        return {
            "configured": False,
            "found": False,
            "detail": "Drive rule not configured (set DRIVE_COMM_REQUIRED_PATTERN)",
            "folder": None,
            "matched_files": [],
        }

    folder = find_plan_folder(plan_id, symlink)
    if not folder:
        return {
            "configured": True,
            "found": False,
            "detail": "Plan folder not found on Drive",
            "folder": None,
            "matched_files": [],
        }

    all_files = list_folder_files(folder["id"])
    matched = [f for f in all_files if _name_matches(f["name"], DRIVE_COMM_PATTERN)]
    return {
        "configured": True,
        "found": bool(matched),
        "detail": "File found" if matched else f"No file matching '{DRIVE_COMM_PATTERN}'",
        "folder": folder,
        "matched_files": matched,
    }
