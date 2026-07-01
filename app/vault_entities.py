"""Consolidate entity name variants — and reclassify misfiled notes — in the vault.

Two API-backed cleanup passes:

**Consolidate** (default): the same person, company, or project often appears under
several names ("Joe"/"Joe K"/"Joe Konkle", "Databyte"/"Daedabyte", "CCC"/"CCC
Legacy"), splitting them across separate ``[[links]]`` and notes. This asks Claude
to cluster the variants *per kind* (people / companies / projects), records the
canonical name + ``aliases`` on one note in the right folder, folds duplicate notes
into it, and rewrites every ``[[alias]]`` to the canonical name.

**Reclassify** (``--reclassify``): when notes land in the wrong folder (a meeting in
People/, a company under Projects/), this classifies every entity note by what it's
actually about and moves each to the folder that matches (merging into an existing
same-named entity instead of duplicating it).

Both **call the API** so they cost tokens. Preview-first and non-destructive —
duplicates go to ``Archive/`` (never deleted). Run from the repo root:

    python -m app.vault_entities                  # PREVIEW consolidation (writes nothing)
    python -m app.vault_entities --apply           # consolidate all kinds, rewrite links
    python -m app.vault_entities --kind companies  # just companies
    python -m app.vault_entities --reclassify      # PREVIEW folder fixes
    python -m app.vault_entities --reclassify --apply

Going forward JARVIS keeps things tidy on its own: the roster (people + companies +
projects) is in its prompt, writes are canonicalized, and meeting notes are routed
out of entity folders deterministically — so these are mainly one-time cleanups.
Edit a note's ``aliases:`` in Obsidian to teach a new nickname anytime.
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
    "companies": {
        "folder": "Companies",
        "label": "companies",
        "system": (
            "You are given names referenced in a personal Obsidian vault. Group the "
            "ones that refer to the SAME company, organization, or client (e.g. "
            "'Databyte'/'Daedabyte', 'CCC'/'CCC Legacy'). IGNORE names that are "
            "individual people, specific projects/products, or generic topics."
        ),
    },
    "projects": {
        "folder": "Projects",
        "label": "projects",
        "system": (
            "You are given names referenced in a personal Obsidian vault. Group the "
            "ones that refer to the SAME project, product, or initiative (e.g. "
            "'Be Cleaned', 'Brightpoint Campaign'). IGNORE names that are individual "
            "people, companies/organizations, or generic topics."
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
    # Drop meeting/session-style names so a meeting never becomes a People/Projects entity.
    return sorted(n for n in names if n.strip() and not obsidian.looks_like_meeting(n))


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


def _in_other_entity_folder(canonical: str, folder: str) -> bool:
    """True if *canonical* already has an entity note in a different entity folder.

    The guard that stops the people↔projects bleed: if "Felicity Kline" already
    lives in People/, the projects pass must not also create Projects/felicity_kline.
    """
    from integrations import obsidian

    return any(
        f != folder and obsidian.find_entity_note(canonical, f)
        for f in obsidian.ENTITY_FOLDERS
    )


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
    *, kinds: tuple[str, ...] = ("people", "companies", "projects"), apply: bool = False,
    model: str | None = None, clusterer=None,
) -> dict:
    """Cluster and (optionally) consolidate each kind. ``clusterer`` is injectable."""
    from integrations import obsidian

    clusterer = clusterer or _default_clusterer(model or CONFIG.anthropic_model)
    candidates = gather_candidates()
    out: dict = {"kinds": {}, "links_rewritten": 0, "skipped": []}
    any_applied = False
    claimed: set[str] = set()  # canonicals already filed by an earlier kind this run
    for kind in kinds:
        folder = _KINDS[kind]["folder"]
        clusters = clusterer(kind, candidates) if candidates else []
        applied = []
        if apply:
            for c in clusters:
                canonical = c["canonical"]
                # Never create the same entity in two folders (people↔projects bleed).
                if canonical.lower() in claimed or _in_other_entity_folder(canonical, folder):
                    out["skipped"].append((kind, canonical))
                    continue
                applied.append(_apply_cluster(c, folder))
                claimed.add(canonical.lower())
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
    if out.get("skipped"):
        print(f"Skipped {len(out['skipped'])} name(s) already filed in another folder "
              "(no cross-folder duplicates created).")
    if apply:
        print(f"Rewrote links in {out['links_rewritten']} note(s); "
              "duplicate notes moved to Archive/.")
    elif total:
        print("Preview only — nothing written. Re-run with --apply to commit.")


# ── Reclassify: move misfiled entity notes into the right folder (uses the API) ──
# Maps the model's label to the folder a note of that kind belongs in. "meeting"
# routes to Sessions/ so a meeting recap misfiled as a person/company lands home.
_LABEL_FOLDER = {
    "person": "People",
    "company": "Companies",
    "project": "Projects",
    "meeting": "Sessions",
}

_RECLASSIFY_SYSTEM = (
    "You are tidying a personal Obsidian vault whose folders have gotten mixed up. "
    "Each line is an existing note given as 'path — title'. Decide what each note is "
    "REALLY about, judging by the title (the current folder may be wrong):\n"
    "  - person  : an individual human being\n"
    "  - company : an organization, business, client, or team\n"
    "  - project : a project, product, campaign, or initiative\n"
    "  - meeting : a meeting, standup, call, or session recap\n\n"
    'Return ONLY a JSON object: {"classifications": [{"path": "<the exact path '
    'given>", "type": "person"|"company"|"project"|"meeting"}]}. Include every note.'
)


def gather_entity_notes() -> list[tuple[str, str]]:
    """``(rel, title)`` for every direct child note in an entity folder.

    These are the notes the reclassify pass judges — a meeting hiding in People/,
    a company filed under Projects/, etc. Notes nested in a subfolder are left out
    (they're deliberately scoped) as are non-entity folders.
    """
    from integrations import obsidian

    out: list[tuple[str, str]] = []
    for folder in obsidian.ENTITY_FOLDERS:
        for rel in obsidian.list_notes(folder=folder):
            if len(rel.split("/")) != 2:
                continue
            try:
                out.append((rel, obsidian.read_note(rel).title))
            except Exception:  # noqa: BLE001
                out.append((rel, obsidian._title_from_stem(Path(rel).stem)))
    return out


def parse_classifications(text: str) -> dict[str, str]:
    """Parse the model reply into ``{path: label}`` (unknown labels dropped)."""
    data = _loads_lenient(text)
    out: dict[str, str] = {}
    for c in data.get("classifications", []) or []:
        path = str(c.get("path") or "").strip()
        label = str(c.get("type") or "").strip().lower()
        if path and label in _LABEL_FOLDER:
            out[path] = label
    return out


def _api_classifier(model: str):
    """Return an ``items -> {path: label}`` classifier backed by the API."""
    from anthropic import Anthropic

    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    def classify(items: list[tuple[str, str]]) -> dict[str, str]:
        lines = [f"{rel} — {title}" for rel, title in items]
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_RECLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": "Notes:\n" + "\n".join(lines)}],
        )
        text = msg.content[0].text if msg.content else ""
        return parse_classifications(text)

    return classify


def reclassify(*, apply: bool = False, model: str | None = None, classifier=None) -> dict:
    """Move misfiled entity notes into the folder that matches what they're about.

    Every note in People/Companies/Projects is classified by the API
    (person/company/project/meeting); any whose folder disagrees is relocated with
    :func:`obsidian.relocate_note`, which merges into an existing same-named entity
    rather than duplicating it. Preview-first — ``apply=False`` writes nothing.
    ``classifier`` is injectable for tests.
    """
    from integrations import obsidian

    classifier = classifier or _api_classifier(model or CONFIG.anthropic_model)
    items = gather_entity_notes()
    labels = classifier(items) if items else {}
    moves: list[tuple[str, str, str]] = []  # (src_rel, label, dest_rel)
    for rel, _title in items:
        label = labels.get(rel)
        if not label:
            continue
        dest_folder = _LABEL_FOLDER[label]
        if rel.split("/", 1)[0] == dest_folder:
            continue  # already correctly filed
        dest_rel = (obsidian.relocate_note(rel, dest_folder) if apply
                    else f"{dest_folder}/{Path(rel).name}")
        moves.append((rel, label, dest_rel))
    out = {"moves": moves, "links_rewritten": 0}
    if apply and moves:
        out["links_rewritten"] = obsidian.recanonicalize_vault()
    return out


def _print_reclassify(out: dict, apply: bool) -> None:
    moves = out["moves"]
    if not moves:
        print("Nothing misfiled — every entity note is already in the right folder.")
        return
    verb = "Moved" if apply else "Would move"
    for rel, label, dest_rel in moves:
        print(f"[{label}] {verb}: {rel}  →  {dest_rel}")
    print(f"\n{len(moves)} note(s) reclassified.")
    if apply:
        print(f"Rewrote links in {out['links_rewritten']} note(s); "
              "duplicates merged into existing entities.")
    else:
        print("Preview only — nothing written. Re-run with --reclassify --apply to commit.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vault_entities",
        description="Consolidate people/company/project name variants, or reclassify "
                    "misfiled notes (both use the API).",
    )
    p.add_argument("--kind", choices=["people", "companies", "projects", "all"],
                   default="all", help="Which entities to consolidate (default: all).")
    p.add_argument("--apply", action="store_true",
                   help="Set aliases, merge duplicate notes, and rewrite links.")
    p.add_argument("--reclassify", action="store_true",
                   help="Instead of consolidating, move misfiled entity notes into "
                        "the correct folder (person/company/project/meeting).")
    p.add_argument("--model", default=None, help="Model to use (default: ANTHROPIC_MODEL).")
    args = p.parse_args(argv)

    if not CONFIG.obsidian_available:
        print("Obsidian vault not configured. Set OBSIDIAN_ENABLED=true and "
              "OBSIDIAN_VAULT_PATH in .env.")
        return 1
    if not CONFIG.has_anthropic_key:
        print("This command calls the Claude API. Set ANTHROPIC_API_KEY in .env.")
        return 1

    if args.reclassify:
        print(f"{'Applying' if args.apply else 'Previewing'} reclassification of misfiled "
              f"notes (model={args.model or CONFIG.anthropic_model}) — this calls the API.\n")
        _print_reclassify(reclassify(apply=args.apply, model=args.model), args.apply)
        return 0

    kinds = ("people", "companies", "projects") if args.kind == "all" else (args.kind,)
    print(f"{'Applying' if args.apply else 'Previewing'} consolidation of {', '.join(kinds)} "
          f"(model={args.model or CONFIG.anthropic_model}) — this calls the API.\n")
    _print_report(run(kinds=kinds, apply=args.apply, model=args.model), args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
