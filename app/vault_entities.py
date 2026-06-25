"""Consolidate entity name variants (people + companies/projects) in the vault.

The same person, company, or project often appears under several names
("Joe"/"Joe K"/"Joe Konkle", "Databyte"/"Daedabyte", "CCC"/"CCC Legacy"), which
splits them across separate ``[[links]]`` and notes. This command asks the Claude
API to cluster the variants *per kind*, records the canonical name + ``aliases``
on one note in the right folder (People/ or Projects/), folds duplicate notes into
it, and rewrites every ``[[alias]]`` across the vault to the canonical name.

Like :mod:`app.vault_organize` it **calls the API** (one clustering request per
kind), so it costs tokens. Preview-first and non-destructive — duplicate notes are
moved to ``Archive/`` (never deleted). Run from the repo root:

    python -m app.vault_entities                 # PREVIEW people + projects (writes nothing)
    python -m app.vault_entities --apply          # consolidate both, rewrite links
    python -m app.vault_entities --kind projects  # just companies/projects
    python -m app.vault_entities --kind people --apply

Going forward JARVIS keeps things consolidated on its own: the roster (people +
companies/projects) is in its prompt and writes are canonicalized, so this is
mainly a one-time cleanup. Edit a note's ``aliases:`` in Obsidian to teach a new
nickname anytime.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import CONFIG
from app.vault_organize import _loads_lenient

# Each kind maps to the folder its canonical notes live in, a human label, and the
# clustering instruction that tells Claude which names to group (and which to skip).
_KINDS: dict[str, dict] = {
    "people": {
        "folder": "People",
        "label": "people",
        "system": (
            "You are given names referenced in a personal Obsidian vault. Group the "
            "ones that refer to the SAME PERSON (e.g. 'Joe', 'Joe K', 'Joe Konkle'). "
            "IGNORE names that are not individual people (companies, clients, "
            "products, projects, topics)."
        ),
    },
    "projects": {
        "folder": "Projects",
        "label": "companies & projects",
        "system": (
            "You are given names referenced in a personal Obsidian vault. Group the "
            "ones that refer to the SAME company, client, or project (e.g. "
            "'Databyte'/'Daedabyte', 'CCC'/'CCC Legacy', 'Be Cleaned'). IGNORE names "
            "that are individual people or generic topics."
        ),
    },
}

_SYSTEM_TAIL = (
    "\n\nReturn ONLY a JSON object: {\"clusters\": [{\"canonical\": <fullest correct "
    "name>, \"aliases\": [<other variants>]}]}. Only include a cluster when two or "
    "more listed names refer to one entity; omit names that already appear once. The "
    "canonical name is the most complete/correct form; aliases are the other "
    "spellings exactly as they appear."
)


def gather_candidates() -> list[str]:
    """Distinct names worth clustering: entity-note names + all wikilink targets."""
    from integrations import obsidian

    names: set[str] = set()
    for folder in obsidian.ENTITY_FOLDERS:
        for rel in obsidian.list_notes(folder=folder):
            names.add(obsidian._title_from_stem(Path(rel).stem))
    for p in obsidian.iter_markdown():
        names.update(obsidian.extract_wikilinks(p.read_text(encoding="utf-8", errors="replace")))
    return sorted(n for n in names if n.strip())


def parse_clusters(text: str) -> list[dict]:
    """Parse the model reply into ``[{canonical, aliases}]`` (drops empty groups)."""
    data = _loads_lenient(text)
    out: list[dict] = []
    for c in data.get("clusters", []) or []:
        canonical = str(c.get("canonical") or "").strip()
        aliases = [str(a).strip() for a in (c.get("aliases") or []) if str(a).strip()]
        aliases = [a for a in aliases if a.lower() != canonical.lower()]
        if canonical and aliases:
            out.append({"canonical": canonical, "aliases": aliases})
    return out


def _default_clusterer(model: str):
    """Return a ``(kind, candidates) -> clusters`` function backed by the API."""
    from anthropic import Anthropic

    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    def cluster(kind: str, candidates: list[str]) -> list[dict]:
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_KINDS[kind]["system"] + _SYSTEM_TAIL,
            messages=[{"role": "user", "content": "Names:\n" + "\n".join(candidates)}],
        )
        text = msg.content[0].text if msg.content else ""
        return parse_clusters(text)

    return cluster


def _apply_cluster(c: dict, folder: str) -> tuple[str, list[str], list[str]]:
    from integrations import obsidian

    canonical, aliases = c["canonical"], c["aliases"]
    obsidian.set_aliases(canonical, aliases, folder=folder)
    canon_rel = obsidian.find_entity_note(canonical, folder)
    merged = []
    for a in aliases:
        a_rel = obsidian.find_entity_note(a, folder)
        if a_rel and a_rel != canon_rel and obsidian.merge_note_into(a_rel, canonical, folder=folder):
            merged.append(a_rel)
    return canonical, aliases, merged


def run(
    *, kinds: tuple[str, ...] = ("people", "projects"), apply: bool = False,
    model: str | None = None, clusterer=None,
) -> dict:
    """Cluster and (optionally) consolidate each kind. ``clusterer`` is injectable."""
    from integrations import obsidian

    clusterer = clusterer or _default_clusterer(model or CONFIG.anthropic_model)
    candidates = gather_candidates()
    out: dict = {"kinds": {}, "links_rewritten": 0}
    any_applied = False
    for kind in kinds:
        folder = _KINDS[kind]["folder"]
        clusters = clusterer(kind, candidates) if candidates else []
        applied = []
        if apply:
            applied = [_apply_cluster(c, folder) for c in clusters]
            any_applied = any_applied or bool(applied)
        out["kinds"][kind] = {"clusters": clusters, "applied": applied}
    if any_applied:
        out["links_rewritten"] = obsidian.recanonicalize_vault()
    return out


def _print_report(out: dict, apply: bool) -> None:
    total = 0
    for kind, res in out["kinds"].items():
        label = _KINDS[kind]["label"]
        if not res["clusters"]:
            print(f"[{label}] nothing to consolidate.")
            continue
        verb = "Merged" if apply else "Would merge"
        for c in res["clusters"]:
            print(f"[{label}] {verb}: {', '.join(c['aliases'])}  →  {c['canonical']}")
        total += len(res["clusters"])
    print(f"\n{total} entity(ies) consolidated.")
    if apply:
        print(f"Rewrote links in {out['links_rewritten']} note(s); "
              "duplicate notes moved to Archive/.")
    elif total:
        print("Preview only — nothing written. Re-run with --apply to commit.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vault_entities",
        description="Consolidate people + company/project name variants (uses the API).",
    )
    p.add_argument("--kind", choices=["people", "projects", "all"], default="all",
                   help="Which entities to consolidate (default: all).")
    p.add_argument("--apply", action="store_true",
                   help="Set aliases, merge duplicate notes, and rewrite links.")
    p.add_argument("--model", default=None, help="Model to use (default: ANTHROPIC_MODEL).")
    args = p.parse_args(argv)

    if not CONFIG.obsidian_available:
        print("Obsidian vault not configured. Set OBSIDIAN_ENABLED=true and "
              "OBSIDIAN_VAULT_PATH in .env.")
        return 1
    if not CONFIG.has_anthropic_key:
        print("This command calls the Claude API. Set ANTHROPIC_API_KEY in .env.")
        return 1

    kinds = ("people", "projects") if args.kind == "all" else (args.kind,)
    print(f"{'Applying' if args.apply else 'Previewing'} consolidation of {', '.join(kinds)} "
          f"(model={args.model or CONFIG.anthropic_model}) — this calls the API.\n")
    _print_report(run(kinds=kinds, apply=args.apply, model=args.model), args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
