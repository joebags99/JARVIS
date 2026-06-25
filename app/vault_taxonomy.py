"""The vault's taxonomy — folders, their note ``type``, entity status, and graph color.

This is the single, **config-driven** source of truth for how the vault is
organized, so adding a new category (Books, Recipes, Companies, Goals…) is a
data edit, not a code change. Everything keys off it: folder scaffolding, the
``type:`` stamped on notes, which folders hold de-duplicated *entities* (People,
Projects), and the colors the graph view paints each cluster.

Defaults below cover the built-in folders. Drop a ``vault_config.json`` at the
repo root to extend or override them, e.g.::

    {"folders": [
      {"folder": "Books",  "type": "book",  "entity": true,  "color": "#ff8a65"},
      {"folder": "Goals",  "type": "goal",  "entity": false, "color": "#7e57c2"}
    ]}

Fields per entry: ``folder`` (name), ``type`` (frontmatter ``type:`` value),
``entity`` (true → notes here are canonical, alias-de-duplicated identities), and
``color`` (hex for the graph). ``skip`` lists folders excluded from the index/graph.
"""

from __future__ import annotations

import json

from .config import ROOT_DIR
from .logging_setup import get_logger

log = get_logger("vault-taxonomy")

# folder · type · entity? · graph color
DEFAULT_TAXONOMY: list[dict] = [
    {"folder": "People", "type": "person", "entity": True, "color": "#4caf79"},
    {"folder": "Projects", "type": "project", "entity": True, "color": "#00bcd4"},
    {"folder": "Sessions", "type": "session", "entity": False, "color": "#e0a458"},
    {"folder": "Daily", "type": "daily", "entity": False, "color": "#9e9e9e"},
    {"folder": "Topics", "type": "topic", "entity": False, "color": "#b39ddb"},
    {"folder": "Ideas", "type": "idea", "entity": False, "color": "#f06292"},
    {"folder": "Maps", "type": "map", "entity": False, "color": "#ffd54f"},
    {"folder": "Memory", "type": "memory", "entity": False, "color": "#26a69a"},
    {"folder": "Imported", "type": "note", "entity": False, "color": "#607d8b"},
]
DEFAULT_SKIP = ["Archive"]
_CONFIG_FILE = ROOT_DIR / "vault_config.json"
_DEFAULT_ENTRY = {"type": "note", "entity": False, "color": "#607d8b"}

_cache: tuple[list[dict], list[str]] | None = None


def _load() -> tuple[list[dict], list[str]]:
    global _cache
    if _cache is not None:
        return _cache
    by_folder = {t["folder"]: dict(t) for t in DEFAULT_TAXONOMY}
    order = [t["folder"] for t in DEFAULT_TAXONOMY]
    skip = list(DEFAULT_SKIP)
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            for entry in data.get("folders", []) or []:
                folder = str(entry.get("folder", "")).strip()
                if not folder:
                    continue
                base = by_folder.get(folder, {"folder": folder, **_DEFAULT_ENTRY})
                by_folder[folder] = {**base, **entry, "folder": folder}
                if folder not in order:
                    order.append(folder)
            if "skip" in data:
                skip = [str(s).strip() for s in data["skip"] if str(s).strip()]
            log.info("loaded vault taxonomy overrides (%d folder entries)",
                     len(data.get("folders", []) or []))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read vault_config.json: %s", exc)
    _cache = ([by_folder[f] for f in order], skip)
    return _cache


def reload() -> None:
    """Drop the cached taxonomy so the next call re-reads vault_config.json."""
    global _cache
    _cache = None


def taxonomy() -> list[dict]:
    return _load()[0]


def folders() -> list[str]:
    return [t["folder"] for t in taxonomy()]


def entity_folders() -> list[str]:
    return [t["folder"] for t in taxonomy() if t.get("entity")]


def skip_folders() -> set[str]:
    return set(_load()[1])


def type_for_folder(folder: str) -> str:
    for t in taxonomy():
        if t["folder"] == folder:
            return t.get("type", "note")
    return "note"


def color_for_folder(folder: str) -> str | None:
    for t in taxonomy():
        if t["folder"] == folder:
            return t.get("color")
    return None


def color_groups() -> list[tuple[str, str]]:
    """``(folder, color)`` pairs for the graph config (folders that have a color)."""
    return [(t["folder"], t["color"]) for t in taxonomy() if t.get("color")]
