"""Google Docs/Drive integration for JARVIS knowledge pools.

Fetches Google Docs content (and folder listings) via the Drive API.
Uses a separate OAuth token file from the calendar integration so that
adding Docs access does not invalidate existing calendar tokens.

Token cache: tokens/google/{account}_docs.json
"""

from __future__ import annotations

import inspect

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger

log = get_logger("google_docs")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_DIR = ROOT_DIR / "tokens" / "google"

_MAX_CHARS_PER_DOC = 2000
_DEFAULT_POOL_MAX = 4000
_DEFAULT_FOLDER_FILES = 2

# Maps Google Workspace MIME types to their plain-text export formats.
_EXPORT_MIME: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

_SUPPORTED_MIMES = " or ".join(
    f"mimeType='{m}'" for m in _EXPORT_MIME
)


def _load_credentials(account_name: str):
    """Load Drive credentials for *account_name*; separate token file from calendar."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        log.warning("google api libraries not installed; knowledge pools unavailable")
        return None

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"{account_name}_docs.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not read docs token file: %s", account_name, exc)

    if creds and creds.valid:
        has_sufficient_scopes = (
            bool(creds.scopes) and set(SCOPES).issubset(set(creds.scopes))
        )
        if not has_sufficient_scopes:
            log.info("[%s] docs token missing required scopes; re-authorizing", account_name)
            creds = None
        else:
            log.debug("[%s] Google Docs credentials valid", account_name)
            return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            log.info("[%s] Google Docs credentials refreshed", account_name)
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] docs token refresh failed, re-authenticating: %s", account_name, exc
            )

    cred_file = ROOT_DIR / CONFIG.google_credentials_path
    if not cred_file.exists():
        log.info(
            "[%s] Google credentials file not found (%s); knowledge pools unavailable",
            account_name,
            cred_file.name,
        )
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
        sig = inspect.signature(flow.run_local_server)
        if "prompt" in sig.parameters:
            creds = flow.run_local_server(port=0, prompt="consent")
        else:
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        log.info(
            "[%s] Google Docs authorized; token cached to %s", account_name, token_path
        )
        return creds
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] Google Docs OAuth flow failed: %s", account_name, exc)
        return None


def fetch_doc_content(
    doc_id: str,
    account_name: str,
    max_chars: int = _MAX_CHARS_PER_DOC,
    mime_type: str | None = None,
) -> str:
    """Export a Google Doc or Sheet as text, truncated to max_chars.

    Docs export as plain text; Sheets export as CSV.
    Pass mime_type to skip the metadata lookup (e.g. when already known from a folder listing).
    """
    creds = _load_credentials(account_name)
    if creds is None:
        return "[Google Docs not authorized]"

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "[googleapiclient not installed]"

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        if mime_type is None:
            meta = service.files().get(fileId=doc_id, fields="mimeType").execute()
            mime_type = meta.get("mimeType", "")

        export_type = _EXPORT_MIME.get(mime_type)
        if export_type is None:
            return f"[Unsupported file type: {mime_type}]"

        content = (
            service.files()
            .export_media(fileId=doc_id, mimeType=export_type)
            .execute()
        )
        text = content.decode("utf-8").strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n…(truncated)"
        return text
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_doc_content failed for doc %s: %s", doc_id, exc)
        return f"[Error loading doc: {exc}]"


def list_folder_files(
    folder_id: str, account_name: str, max_files: int = _DEFAULT_FOLDER_FILES
) -> list[dict]:
    """Return the max_files most recently modified Google Docs and Sheets in a folder."""
    creds = _load_credentials(account_name)
    if creds is None:
        return []

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return []

    try:
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        query = (
            f"'{folder_id}' in parents "
            f"and ({_SUPPORTED_MIMES}) "
            "and trashed=false"
        )
        result = service.files().list(
            q=query,
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=max_files,
        ).execute()
        return result.get("files", [])
    except Exception as exc:  # noqa: BLE001
        log.error("list_folder_files failed for folder %s: %s", folder_id, exc)
        return []


def load_pool(pool_config: dict, account_name: str) -> str:
    """Fetch and assemble content for a knowledge pool.

    Returns a formatted string ready to be returned as a tool result.
    Enforces pool_config["max_chars"] across all sources combined.
    """
    pool_max = pool_config.get("max_chars", _DEFAULT_POOL_MAX)
    sources = pool_config.get("sources", [])
    parts: list[str] = []
    total_chars = 0

    for source in sources:
        if total_chars >= pool_max:
            break

        src_type = source.get("type", "file")
        src_name = source.get("name", "Document")
        remaining = pool_max - total_chars

        if src_type == "file":
            doc_id = source.get("id", "")
            if not doc_id:
                continue
            content = fetch_doc_content(doc_id, account_name, min(_MAX_CHARS_PER_DOC, remaining))
            parts.append(f"### {src_name}\n{content}")
            total_chars += len(content)

        elif src_type == "folder":
            folder_id = source.get("id", "")
            if not folder_id:
                continue
            max_files = source.get("max_files", _DEFAULT_FOLDER_FILES)
            files = list_folder_files(folder_id, account_name, max_files)
            folder_parts: list[str] = []
            for f in files:
                if total_chars >= pool_max:
                    break
                remaining = pool_max - total_chars
                content = fetch_doc_content(
                    f["id"], account_name, min(_MAX_CHARS_PER_DOC, remaining),
                    mime_type=f.get("mimeType"),
                )
                folder_parts.append(f"#### {f['name']}\n{content}")
                total_chars += len(content)
            if folder_parts:
                parts.append(f"### {src_name} (folder)\n" + "\n\n".join(folder_parts))

    if not parts:
        return "(No content could be loaded from this knowledge pool.)"

    return "\n\n".join(parts)
