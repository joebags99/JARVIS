"""Per-type note templates — one consistent structure for each kind of note.

Every note JARVIS creates follows the skeleton for its ``type`` so the vault stays
uniform and readable: a person note always has the same sections, a meeting note
always has Summary / Decisions / Action Items, etc. The skeleton is applied when a
note is *created* (a fresh stub or a thin note with no structure); notes that
already carry their own headings are left as the author wrote them.

The same section lists are surfaced to JARVIS in the system prompt, so its
free-form writing matches the templates too. Templates are keyed by the taxonomy
``type`` (see :mod:`app.vault_taxonomy`), so a new category can declare its own.
"""

from __future__ import annotations

# type → ordered section headers (rendered as `## Header`). Empty = no sections.
_SECTIONS: dict[str, list[str]] = {
    "person": ["Facts", "Projects", "Notes"],
    "company": ["Overview", "People", "Projects", "Notes"],
    "project": ["Overview", "People", "Decisions", "Meetings", "Open Questions"],
    "session": ["Summary", "Attendees", "Decisions", "Action Items", "Open Questions"],
    "topic": ["Notes"],
    "daily": ["Log"],
    "idea": [],
    "memory": [],
    "map": [],
    "note": ["Notes"],
}


def sections_for(note_type: str) -> list[str]:
    """The section headers for *note_type* (empty list if it has no template)."""
    return _SECTIONS.get(note_type or "note", [])


def has_template(note_type: str) -> bool:
    return bool(sections_for(note_type))


def scaffold(note_type: str, title: str | None = None) -> str:
    """Render an empty note body for *note_type*: an ``# H1`` + its blank sections."""
    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")
    for header in sections_for(note_type):
        parts.append(f"## {header}\n")
    return "\n".join(parts).strip() + ("\n" if parts else "")


def prompt_summary() -> str:
    """A compact 'type → sections' description for JARVIS's system prompt."""
    lines = []
    for note_type, secs in _SECTIONS.items():
        if secs:
            lines.append(f"  - {note_type}: {', '.join(secs)}")
    return "\n".join(lines)
