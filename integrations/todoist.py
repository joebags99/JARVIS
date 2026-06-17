"""Todoist integration.

Uses Todoist's unified API v1 (the old REST v2 was sunset Feb 2026) with a
personal API token — no OAuth, no browser flow. Set ``TODOIST_API_KEY`` in
``.env`` (Todoist Settings → Integrations → Developer → "API token").

Categories map 1:1 to Todoist projects (e.g. "Daedabyte", "General",
"Brightpoint"). Resolving a category looks up an existing project by name
(case-insensitive); if none exists yet, one is created on the fly so JARVIS
never blocks on missing setup.

Due dates are resolved locally with ``parsedatetime`` rather than trusting
Claude (or Todoist's own parser) with phrases like "next Friday" — LLMs are
unreliable at date arithmetic, so the tool layer asks Claude to pass the
user's words through unmodified and this module anchors them to the real
system clock instead.
"""

from __future__ import annotations

import datetime as dt
import re
import time

import parsedatetime
import requests

from app.config import CONFIG
from app.logging_setup import get_logger

log = get_logger("todoist")

BASE = "https://api.todoist.com/api/v1"
DEFAULT_FILTER = "overdue | today"

_CAL = parsedatetime.Calendar()
# Recurring phrases need Todoist's own parser to set up the recurrence rule —
# parsedatetime only resolves a single point in time, not "every Monday".
_RECURRING_HINTS = ("every", "each", "daily", "weekly", "biweekly", "monthly", "yearly", "annually")
# Match the hints as whole words only, so "everyone" / "delivery" / "weekly
# standup" are handled correctly ("weekly" still matches, "delivery" doesn't).
_RECURRING_RE = re.compile(
    r"\b(" + "|".join(re.escape(h) for h in _RECURRING_HINTS) + r")\b", re.IGNORECASE
)


def _resolve_due(phrase: str) -> dict:
    """Turn a natural-language due phrase into Todoist due_date/due_datetime fields.

    Falls back to passing the phrase through as due_string (Todoist's own
    parser) when it's a recurring pattern or parsedatetime can't parse it —
    e.g. "no date" to clear a due date.
    """
    if _RECURRING_RE.search(phrase):
        return {"due_string": phrase}

    parsed, status = _CAL.parseDT(phrase, sourceTime=dt.datetime.now())
    if status == 0:
        return {"due_string": phrase}
    if status & 2:  # time-of-day was specified
        return {"due_datetime": parsed.isoformat()}
    return {"due_date": parsed.date().isoformat()}


def _headers() -> dict:
    return {"Authorization": f"Bearer {CONFIG.todoist_api_key}"}


# Transient HTTP statuses worth retrying. 502/503/504 (gateway/unavailable) and
# 429 (rate limit) mean the request almost certainly wasn't applied, so they're
# safe to retry even for writes. 500 is ambiguous for a write (it may have taken
# effect), so it's only retried on GETs to avoid creating duplicate tasks.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds → 0.5s, 1.0s between attempts


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """HTTP to the Todoist API with auth, timeout, and transient-failure retries.

    Todoist occasionally returns a brief 503/502 or drops a connection; rather
    than fail the user's command on a momentary blip, retry a couple of times
    with exponential backoff. Non-transient errors (400/401/404…) raise at once.
    """
    url = path if path.startswith("http") else f"{BASE}{path}"
    kwargs.setdefault("timeout", 15)
    kwargs.setdefault("headers", _headers())
    is_get = method.upper() == "GET"

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            retryable = status in _RETRY_STATUSES and (status != 500 or is_get)
            if not retryable or attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc
        except requests.Timeout as exc:
            # A write may have been applied server-side before timing out, so
            # don't risk a duplicate — only retry timeouts on GETs.
            if not is_get or attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc
        except requests.ConnectionError as exc:
            # Never reached the server → always safe to retry.
            if attempt == _MAX_ATTEMPTS:
                raise
            last_exc = exc

        delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        log.warning(
            "Todoist %s %s failed (attempt %d/%d: %s); retrying in %.1fs",
            method, path, attempt, _MAX_ATTEMPTS, last_exc, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover — loop always returns or raises above


def _results(resp: requests.Response) -> list[dict]:
    """List endpoints on API v1 return {"results": [...], "next_cursor": ...}."""
    return resp.json().get("results", [])


def _get_projects() -> list[dict]:
    return _results(_request("GET", "/projects"))


def _resolve_project(category: str) -> tuple[str, str]:
    """Find a project ID by name, creating it if it doesn't exist yet.

    Returns (project_id, matched_name).
    """
    projects = _get_projects()
    for proj in projects:
        if proj.get("name", "").lower() == category.lower():
            return proj["id"], proj["name"]
    for proj in projects:
        if category.lower() in proj.get("name", "").lower():
            return proj["id"], proj["name"]

    created = _request("POST", "/projects", json={"name": category}).json()
    log.info("created new Todoist project '%s' (id=%s)", category, created["id"])
    return created["id"], created["name"]


def _format_task(task: dict, project_names: dict[str, str], indent: int = 0) -> str:
    due = task.get("due") or {}
    when = due.get("string") or due.get("date") or "no due date"
    project = project_names.get(task.get("project_id"), "?")
    priority = task.get("priority", 1)
    flag = " !" * (priority - 1) if priority > 1 else ""
    prefix = "  " * indent + "- "
    return f"{prefix}[{task['id']}] {task['content']} (due: {when}) [{project}]{flag}"


def _due_sort_key(task: dict) -> str:
    return (task.get("due") or {}).get("date") or "9999-99-99"


def list_tasks(filter_str: str | None = None) -> str:
    """Return a formatted list of tasks matching a Todoist filter query.

    Never raises — returns a readable error string on failure.
    """
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    query = filter_str or DEFAULT_FILTER
    try:
        tasks = _results(_request("GET", "/tasks", params={"filter": query}))
    except Exception as exc:  # noqa: BLE001
        log.error("list_tasks failed (filter=%r): %s", query, exc)
        return f"Error fetching Todoist tasks: {exc}"

    if not tasks:
        return f"(No Todoist tasks matching '{query}'.)"

    try:
        project_names = {p["id"]: p["name"] for p in _get_projects()}
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch Todoist projects for labeling: %s", exc)
        project_names = {}

    # Nest subtasks under their parent instead of listing them as confusing
    # flat siblings. A task whose parent didn't also match the filter (e.g.
    # the parent has no due date and the filter is date-based) is rendered
    # at the top level rather than dropped.
    ids = {t["id"] for t in tasks}
    children: dict[str, list[dict]] = {}
    top_level: list[dict] = []
    for t in tasks:
        parent_id = t.get("parent_id")
        if parent_id and parent_id in ids:
            children.setdefault(parent_id, []).append(t)
        else:
            top_level.append(t)

    def render(task: dict, indent: int) -> list[str]:
        lines = [_format_task(task, project_names, indent)]
        for child in sorted(children.get(task["id"], []), key=_due_sort_key):
            lines.extend(render(child, indent + 1))
        return lines

    lines: list[str] = []
    for t in sorted(top_level, key=_due_sort_key):
        lines.extend(render(t, 0))
    return "\n".join(lines)


def create_task(
    content: str,
    category: str,
    due_string: str | None = None,
    description: str | None = None,
    subtasks: list[str] | None = None,
) -> str:
    """Create a new Todoist task in the given category (project). Never raises.

    If *subtasks* is given, each string becomes a child task nested under the
    new task via Todoist's ``parent_id`` (the project is inherited from the
    parent, so subtasks don't need their own project_id). Partial subtask
    failures are reported in the summary rather than aborting — the parent
    task is already created by that point and shouldn't disappear over one
    failed child.
    """
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    try:
        project_id, matched_name = _resolve_project(category)
    except Exception as exc:  # noqa: BLE001
        log.error("create_task: could not resolve project '%s': %s", category, exc)
        return f"Error resolving Todoist category '{category}': {exc}"

    body: dict = {"content": content, "project_id": project_id}
    if due_string:
        body.update(_resolve_due(due_string))
    if description:
        body["description"] = description

    try:
        created = _request("POST", "/tasks", json=body).json()
    except Exception as exc:  # noqa: BLE001
        log.error("create_task failed: %s", exc)
        return f"Error creating Todoist task: {exc}"

    due = (created.get("due") or {}).get("string")
    when = f" (due {due})" if due else ""
    log.info("created task '%s' in '%s'%s (id=%s)", content, matched_name, when, created["id"])
    summary = f"Task '{content}' added to '{matched_name}'{when}."

    if subtasks:
        added = 0
        errors: list[str] = []
        for step in subtasks:
            try:
                _request("POST", "/tasks", json={"content": step, "parent_id": created["id"]})
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.error("create_task: subtask '%s' failed: %s", step, exc)
                errors.append(f"'{step}': {exc}")
        summary += f" Added {added}/{len(subtasks)} subtasks."
        if errors:
            summary += "\n\nSome subtasks failed:\n" + "\n".join(f"- {e}" for e in errors)

    return summary



def _find_task(content: str, due_hint: str | None = None) -> tuple[dict | None, str | None]:
    """Match an open task by text (and optional due-date hint).

    Returns (task, None) on a single match, or (None, message) when the
    caller should relay that message back to Claude (no match / ambiguous).
    """
    tasks = _results(_request("GET", "/tasks"))

    matches = [t for t in tasks if content.lower() in t.get("content", "").lower()]
    if not matches:
        return None, f"Error: no open task matching '{content}' found."

    if len(matches) > 1 and due_hint:
        hinted = [
            t for t in matches
            if due_hint.lower() in ((t.get("due") or {}).get("string") or "").lower()
        ]
        if hinted:
            matches = hinted

    if len(matches) > 1:
        options = "; ".join(
            f"'{t['content']}' (due: {(t.get('due') or {}).get('string', 'no date')})"
            for t in matches[:5]
        )
        return None, (
            f"Found {len(matches)} tasks matching '{content}'. "
            f"Specify which one with a due_hint. Options: {options}"
        )

    return matches[0], None


def complete_task(content: str, due_hint: str | None = None) -> str:
    """Find an active task by matching text and mark it complete. Never raises."""
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    try:
        target, error = _find_task(content, due_hint)
    except Exception as exc:  # noqa: BLE001
        log.error("complete_task: could not list tasks: %s", exc)
        return f"Error fetching Todoist tasks: {exc}"
    if error:
        return error

    try:
        _request("POST", f"/tasks/{target['id']}/close")
    except Exception as exc:  # noqa: BLE001
        log.error("complete_task failed (id=%s): %s", target["id"], exc)
        return f"Error completing Todoist task: {exc}"

    log.info("completed task '%s' (id=%s)", target["content"], target["id"])
    return f"Task '{target['content']}' marked complete."


def update_task(
    content: str,
    due_hint: str | None = None,
    new_content: str | None = None,
    new_due_string: str | None = None,
    new_description: str | None = None,
    new_category: str | None = None,
) -> str:
    """Find a task by text and patch only the supplied fields. Never raises.

    Changing the category moves the task to a different project via the
    dedicated /move endpoint — the regular task-update endpoint can't do it.
    """
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    try:
        target, error = _find_task(content, due_hint)
    except Exception as exc:  # noqa: BLE001
        log.error("update_task: could not list tasks: %s", exc)
        return f"Error fetching Todoist tasks: {exc}"
    if error:
        return error

    patch: dict = {}
    if new_content is not None:
        patch["content"] = new_content
    if new_due_string is not None:
        patch.update(_resolve_due(new_due_string))
    if new_description is not None:
        patch["description"] = new_description

    if not patch and new_category is None:
        return "Error: no fields to update were specified."

    if patch:
        try:
            _request("POST", f"/tasks/{target['id']}", json=patch)
        except Exception as exc:  # noqa: BLE001
            log.error("update_task failed (id=%s): %s", target["id"], exc)
            return f"Error updating Todoist task: {exc}"

    matched_category = None
    if new_category is not None:
        try:
            project_id, matched_category = _resolve_project(new_category)
            _request(
                "POST",
                f"/tasks/{target['id']}/move",
                json={"project_id": project_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.error("update_task: move to '%s' failed (id=%s): %s", new_category, target["id"], exc)
            return f"Error moving Todoist task to '{new_category}': {exc}"

    display_name = new_content or target["content"]
    log.info("updated task '%s' (id=%s)", display_name, target["id"])
    suffix = f" moved to '{matched_category}'." if matched_category else "."
    return f"Task '{display_name}' updated{suffix}"
