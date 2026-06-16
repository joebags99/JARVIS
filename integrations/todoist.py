"""Todoist integration.

Uses Todoist's REST API (v2) with a personal API token — no OAuth, no browser
flow. Set ``TODOIST_API_KEY`` in ``.env`` (Todoist Settings → Integrations →
Developer → "API token").

Categories map 1:1 to Todoist projects (e.g. "Daedabyte", "General",
"Brightpoint"). Resolving a category looks up an existing project by name
(case-insensitive); if none exists yet, one is created on the fly so JARVIS
never blocks on missing setup.
"""

from __future__ import annotations

import requests

from app.config import CONFIG
from app.logging_setup import get_logger

log = get_logger("todoist")

BASE = "https://api.todoist.com/rest/v2"
DEFAULT_FILTER = "overdue | today"


def _headers() -> dict:
    return {"Authorization": f"Bearer {CONFIG.todoist_api_key}"}


def _get_projects() -> list[dict]:
    resp = requests.get(f"{BASE}/projects", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


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

    resp = requests.post(
        f"{BASE}/projects", headers=_headers(), json={"name": category}, timeout=15
    )
    resp.raise_for_status()
    created = resp.json()
    log.info("created new Todoist project '%s' (id=%s)", category, created["id"])
    return created["id"], created["name"]


def _format_task(task: dict, project_names: dict[str, str]) -> str:
    due = task.get("due") or {}
    when = due.get("string") or due.get("date") or "no due date"
    project = project_names.get(task.get("project_id"), "?")
    priority = task.get("priority", 1)
    flag = " !" * (priority - 1) if priority > 1 else ""
    return f"- [{task['id']}] {task['content']} (due: {when}) [{project}]{flag}"


def list_tasks(filter_str: str | None = None) -> str:
    """Return a formatted list of tasks matching a Todoist filter query.

    Never raises — returns a readable error string on failure.
    """
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    query = filter_str or DEFAULT_FILTER
    try:
        resp = requests.get(
            f"{BASE}/tasks", headers=_headers(), params={"filter": query}, timeout=15
        )
        resp.raise_for_status()
        tasks = resp.json()
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

    tasks.sort(key=lambda t: (t.get("due") or {}).get("date") or "9999-99-99")
    return "\n".join(_format_task(t, project_names) for t in tasks)


def create_task(
    content: str,
    category: str,
    due_string: str | None = None,
    description: str | None = None,
) -> str:
    """Create a new Todoist task in the given category (project). Never raises."""
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    try:
        project_id, matched_name = _resolve_project(category)
    except Exception as exc:  # noqa: BLE001
        log.error("create_task: could not resolve project '%s': %s", category, exc)
        return f"Error resolving Todoist category '{category}': {exc}"

    body: dict = {"content": content, "project_id": project_id}
    if due_string:
        body["due_string"] = due_string
    if description:
        body["description"] = description

    try:
        resp = requests.post(f"{BASE}/tasks", headers=_headers(), json=body, timeout=15)
        resp.raise_for_status()
        created = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("create_task failed: %s", exc)
        return f"Error creating Todoist task: {exc}"

    due = (created.get("due") or {}).get("string")
    when = f" (due {due})" if due else ""
    log.info("created task '%s' in '%s'%s (id=%s)", content, matched_name, when, created["id"])
    return f"Task '{content}' added to '{matched_name}'{when}."


def complete_task(content: str, due_hint: str | None = None) -> str:
    """Find an active task by matching text and mark it complete. Never raises."""
    if not CONFIG.todoist_enabled:
        return "Todoist is not configured. Add TODOIST_API_KEY to your .env file."

    try:
        resp = requests.get(f"{BASE}/tasks", headers=_headers(), timeout=15)
        resp.raise_for_status()
        tasks = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.error("complete_task: could not list tasks: %s", exc)
        return f"Error fetching Todoist tasks: {exc}"

    matches = [t for t in tasks if content.lower() in t.get("content", "").lower()]
    if not matches:
        return f"Error: no open task matching '{content}' found."

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
        return (
            f"Found {len(matches)} tasks matching '{content}'. "
            f"Specify which one with a due_hint. Options: {options}"
        )

    target = matches[0]
    try:
        resp = requests.post(
            f"{BASE}/tasks/{target['id']}/close", headers=_headers(), timeout=15
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("complete_task failed (id=%s): %s", target["id"], exc)
        return f"Error completing Todoist task: {exc}"

    log.info("completed task '%s' (id=%s)", target["content"], target["id"])
    return f"Task '{target['content']}' marked complete."
