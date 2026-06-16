"""Gmail integration — read recent mail and draft replies.

Reuses the same Google ``credentials.json`` as the calendar integration but
keeps its own OAuth consent and token store (``tokens/google_mail/{account}.json``)
because it needs mail-specific scopes. It is opt-in via ``GMAIL_ENABLED=true``
so the extra consent only happens for users who actually want email.

Scopes are deliberately narrow:
  * ``gmail.readonly`` — list and read messages
  * ``gmail.compose``  — create drafts (NOT auto-send)

JARVIS never sends mail on its own — it only writes a draft for the user to
review and send themselves, since sending is hard to reverse. Every function
degrades gracefully: a missing/invalid credential returns a readable string
for Claude instead of raising.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText

from app.config import ROOT_DIR, CONFIG
from app.logging_setup import get_logger

log = get_logger("gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
TOKEN_DIR = ROOT_DIR / "tokens" / "google_mail"


def _load_credentials(account_name: str):
    """Load cached mail creds for *account_name*, refreshing or re-consenting as needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        log.warning("google api libraries not installed; skipping Gmail")
        return None

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"{account_name}.json"

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not read mail token file: %s", account_name, exc)

    if creds and creds.valid:
        has_sufficient_scopes = (
            bool(creds.scopes) and set(SCOPES).issubset(set(creds.scopes))
        )
        if has_sufficient_scopes:
            return creds
        log.info("[%s] mail token missing required scopes; re-authorizing", account_name)
        creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            log.info("[%s] Gmail credentials refreshed", account_name)
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] mail token refresh failed, re-authenticating: %s", account_name, exc)

    cred_file = ROOT_DIR / CONFIG.google_credentials_path
    if not cred_file.exists():
        log.info("[%s] Google credentials file not found (%s); skipping Gmail",
                 account_name, cred_file.name)
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
        creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        log.info("[%s] Gmail authorized; token cached to %s", account_name, token_path)
        return creds
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] Gmail OAuth flow failed: %s", account_name, exc)
        return None


def _first_account() -> str:
    accounts = getattr(CONFIG, "google_accounts", None) or ["default"]
    return accounts[0]


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def list_emails(query: str | None = None, max_results: int = 10) -> str:
    """List recent emails (sender, subject, date, snippet). Never raises.

    *query* is a Gmail search string (e.g. 'is:unread', 'from:sam newer_than:7d').
    Defaults to the inbox. Reads from the first configured Google account.
    """
    if not CONFIG.gmail_available:
        return (
            "Gmail is not configured. Set GMAIL_ENABLED=true and make sure "
            "Google credentials.json is present."
        )

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "Error: Google API libraries not installed."

    account = _first_account()
    creds = _load_credentials(account)
    if creds is None:
        return f"Error: no valid Gmail credentials for account '{account}'."

    q = query or "in:inbox"
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        listing = (
            service.users().messages()
            .list(userId="me", q=q, maxResults=max(1, min(max_results, 25)))
            .execute()
        )
        message_ids = [m["id"] for m in listing.get("messages", [])]
        if not message_ids:
            return f"(No emails matching '{q}'.)"

        blocks = []
        for mid in message_ids:
            msg = (
                service.users().messages()
                .get(userId="me", id=mid, format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = msg.get("payload", {}).get("headers", [])
            sender = _header(headers, "From")
            subject = _header(headers, "Subject") or "(no subject)"
            date = _header(headers, "Date")
            snippet = msg.get("snippet", "").strip()
            unread = "UNREAD" in msg.get("labelIds", [])
            flag = " [unread]" if unread else ""
            blocks.append(
                f"- From: {sender}{flag}\n  Subject: {subject}\n  Date: {date}\n  {snippet}"
            )
        log.info("[%s] listed %d emails (q=%r)", account, len(blocks), q)
        return "\n".join(blocks)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] list_emails failed (q=%r): %s", account, q, exc)
        return f"Error fetching emails: {exc}"


def create_draft(to: str, subject: str, body: str, cc: str | None = None) -> str:
    """Create a Gmail draft (never auto-sends). Returns a status string. Never raises."""
    if not CONFIG.gmail_available:
        return (
            "Gmail is not configured. Set GMAIL_ENABLED=true and make sure "
            "Google credentials.json is present."
        )

    try:
        from googleapiclient.discovery import build
    except ImportError:
        return "Error: Google API libraries not installed."

    account = _first_account()
    creds = _load_credentials(account)
    if creds is None:
        return f"Error: no valid Gmail credentials for account '{account}'."

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = (
            service.users().drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        log.info("[%s] created draft to %s (id=%s)", account, to, draft.get("id"))
        return (
            f"Draft saved to Gmail (to {to}, subject '{subject}'). "
            "It was NOT sent — review and send it yourself from Gmail."
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] create_draft failed: %s", account, exc)
        return f"Error creating draft: {exc}"
