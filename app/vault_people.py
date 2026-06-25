"""One-time people-consolidation pass — collapse name variants into one identity.

Migrated/meeting notes often refer to the same person several ways ("Joe",
"Joe K", "Joe Konkle"), which splits them across separate ``[[links]]`` and
notes. This command asks the Claude API to cluster those variants, then records
the canonical name + ``aliases`` on a single ``People/<Name>.md`` note, merges any
duplicate person notes into it, and rewrites every ``[[alias]]`` across the vault
to the canonical name.

Like :mod:`app.vault_organize`, it **calls the API** (one clustering request), so
it costs tokens. It is preview-first and non-destructive — duplicate notes are
moved to ``Archive/`` (never deleted). Run from the repo root:

    python -m app.vault_people            # PREVIEW the proposed clusters (writes nothing)
    python -m app.vault_people --apply    # set aliases, merge dup notes, rewrite links

Going forward, JARVIS links people by their canonical name automatically (the
roster is in its prompt, and writes are canonicalized), so this is mainly a
one-time cleanup of the existing vault.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import CONFIG
from app.vault_organize import _loads_lenient

_SYSTEM = (
    "You are given a list of names referenced in a personal Obsidian vault. Group "
    "the ones that refer to the SAME PERSON (e.g. 'Joe', 'Joe K', 'Joe Konkle'). "
    "Ignore names that are NOT people (companies, projects, products, topics).\n\n"
    "Return ONLY a JSON object: {\"clusters\": [{\"canonical\": <fullest real name>, "
    "\"aliases\": [<other variants>]}]}. Only include a cluster when two or more of "
    "the listed names refer to one person; omit names that already appear once and "
    "need no merging. The canonical name must be the most complete/correct form; "
    "aliases are the other spellings exactly as they appear in the list."
)


def gather_candidates() -> list[str]:
    """Distinct names worth clustering: People note names + all wikilink targets."""
    from integrations import obsidian

    names: set[str] = set()
    for rel in obsidian.list_notes(folder="People"):
        names.add(obsidian._title_from_stem(Path(rel).stem))
    for p in obsidian.iter_markdown():
        body = p.read_text(encoding="utf-8", errors="replace")
        names.update(obsidian.extract_wikilinks(body))
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


def _api_clusterer(model: str):
    from anthropic import Anthropic

    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    def cluster(candidates: list[str]) -> list[dict]:
        msg = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_SYSTEM,
            messages=[{"role": "user", "content": "Names:\n" + "\n".join(candidates)}],
        )
        text = msg.content[0].text if msg.content else ""
        return parse_clusters(text)

    return cluster


def run(*, apply: bool = False, model: str | None = None, clusterer=None) -> dict:
    """Cluster name variants and (optionally) consolidate them in the vault."""
    from integrations import obsidian

    clusterer = clusterer or _api_clusterer(model or CONFIG.anthropic_model)
    candidates = gather_candidates()
    clusters = clusterer(candidates) if candidates else []

    out: dict = {"clusters": clusters, "applied": [], "links_rewritten": 0}
    if apply and clusters:
        for c in clusters:
            canonical, aliases = c["canonical"], c["aliases"]
            obsidian.set_aliases(canonical, aliases)
            canon_rel = obsidian.find_person_note(canonical)
            merged = []
            for a in aliases:
                a_rel = obsidian.find_person_note(a)
                if a_rel and a_rel != canon_rel and obsidian.merge_note_into(a_rel, canonical):
                    merged.append(a_rel)
            out["applied"].append((canonical, aliases, merged))
        out["links_rewritten"] = obsidian.recanonicalize_vault()
    return out


def _print_report(out: dict, apply: bool) -> None:
    if not out["clusters"]:
        print("No name variants to consolidate — everyone's already one identity.")
        return
    verb = "Merged" if apply else "Would merge"
    for c in out["clusters"]:
        print(f"{verb}: {', '.join(c['aliases'])}  →  {c['canonical']}")
    print(f"\n{len(out['clusters'])} person(s) consolidated.")
    if apply:
        print(f"Rewrote links in {out['links_rewritten']} note(s); "
              "duplicate notes moved to Archive/.")
    else:
        print("Preview only — nothing written. Re-run with --apply to commit.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vault_people",
        description="Consolidate person name variants in the vault (uses the API).",
    )
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

    print(f"{'Applying' if args.apply else 'Previewing'} people consolidation "
          f"(model={args.model or CONFIG.anthropic_model}) — this calls the API.\n")
    _print_report(run(apply=args.apply, model=args.model), args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
