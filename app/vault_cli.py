"""Token-free CLI for the Obsidian vault — inspect, preview, and migrate.

Everything here runs locally and **never calls the Claude API**, so you can
confirm the vault is wired up correctly and convert your existing notes + memory
without spending any tokens. Run from the repo root:

    python -m app.vault_cli check                    # readiness report
    python -m app.vault_cli migrate --dry-run        # preview the import (no writes)
    python -m app.vault_cli migrate                  # do the import (idempotent, copies)
    python -m app.vault_cli reindex                  # rebuild the search index
    python -m app.vault_cli search "kitchen remodel" # FTS search, token-free
    python -m app.vault_cli list [folder]            # browse notes

The migration is the same one JARVIS runs automatically on first launch with the
vault enabled — running it here just lets you do it deliberately and preview it
first. Nothing is moved: your original notes/ and memory.db are copied, and a
marker makes a real import a one-time operation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import CONFIG


def _require_vault() -> bool:
    """Print a fix-it hint and return False unless the vault is configured."""
    if not CONFIG.obsidian_enabled:
        print("Obsidian vault is OFF. Set OBSIDIAN_ENABLED=true in .env.")
        return False
    if not CONFIG.obsidian_vault_path:
        print("No vault path. Set OBSIDIAN_VAULT_PATH=<folder> in .env.")
        return False
    return True


def _print_plan(plan: dict) -> None:
    print(f"Vault: {plan['vault_root']}")
    print(f"Notes → Imported/                : {len(plan['notes'])} file(s)")
    for _, dest in plan["notes"][:50]:
        print(f"    {dest}")
    if len(plan["notes"]) > 50:
        print(f"    … and {len(plan['notes']) - 50} more")
    print(f"Session summaries → Sessions/    : {len(plan['sessions'])} file(s)")
    for _, dest in plan["sessions"][:50]:
        print(f"    {dest}")
    print(f"Durable facts → Memory/Facts.md  : {plan['fact_count']}")


def cmd_check(_args) -> int:
    """Readiness report: config, vault scaffold, legacy sources, search index."""
    from integrations import obsidian

    print("JARVIS — Obsidian vault check")
    print("-" * 44)
    print(f"OBSIDIAN_ENABLED    : {CONFIG.obsidian_enabled}")
    print(f"OBSIDIAN_VAULT_PATH : {CONFIG.obsidian_vault_path or '(unset)'}")
    print(f"available           : {CONFIG.obsidian_available}")
    if not CONFIG.obsidian_available:
        print("\n→ Set BOTH OBSIDIAN_ENABLED=true and OBSIDIAN_VAULT_PATH in .env, "
              "then re-run.")
        return 1

    plan = obsidian.migration_plan()
    root = Path(plan["vault_root"]) if plan["vault_root"] else None
    exists = bool(root and root.exists())
    print(f"vault folder        : {root} ({'exists' if exists else 'will be created'})")
    print(f"already migrated    : {plan['already_migrated']}")

    if exists:
        folders = sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
        print(f"folders             : {', '.join(folders) or '(none yet — run migrate)'}")
        print(f"index.md            : "
              f"{'present' if (root / 'index.md').exists() else 'missing (run migrate)'}")

    print(f"legacy notes found  : {len(plan['notes']) + len(plan['sessions'])} file(s) in notes/")
    print(f"legacy facts found  : {plan['fact_count']} in memory.db")

    # The index is a rebuildable cache; rebuild it (only if the vault exists) so
    # the reported count is accurate and we prove indexing works end to end.
    indexed = obsidian.reindex() if exists else 0
    print(f"searchable notes    : {indexed} indexed")
    print("\nAll local — zero tokens. Preview an import with `migrate --dry-run`.")
    return 0


def cmd_migrate(args) -> int:
    """Import legacy notes/memory into the vault (or preview it with --dry-run)."""
    if not _require_vault():
        return 1
    from integrations import obsidian

    plan = obsidian.migration_plan()
    if args.dry_run:
        print("DRY RUN — nothing will be written.\n")
        _print_plan(plan)
        if plan["already_migrated"]:
            print("\nNote: a migration marker already exists, so a real run is a no-op.")
        else:
            print("\nRun without --dry-run to import. Originals are copied, not moved.")
        return 0

    if plan["already_migrated"]:
        print("Already migrated (marker present) — nothing to import. "
              "Use `reindex` to refresh search.")
        return 0
    obsidian.ensure_scaffold()
    count = obsidian.migrate_legacy()
    indexed = obsidian.reindex()
    print(f"Imported {count} legacy item(s); {indexed} note(s) now searchable.")
    print("Your original notes/ and memory.db were left untouched.")
    return 0


def cmd_reindex(_args) -> int:
    """Rebuild the FTS5 search index from the vault's markdown files."""
    if not _require_vault():
        return 1
    from integrations import obsidian

    print(f"Reindexed {obsidian.reindex()} note(s).")
    return 0


def cmd_search(args) -> int:
    """Token-free relevance search over the vault (the model's search_vault tool)."""
    if not _require_vault():
        return 1
    from integrations import obsidian

    hits = obsidian.search(args.query, tag=args.tag, folder=args.folder, limit=args.limit)
    if not hits:
        print("(no matches)")
        return 0
    for h in hits:
        tagline = f"  [tags: {h.tags}]" if h.tags else ""
        print(f"### {h.title or h.path}\n    {h.path}{tagline}\n    {h.snippet}\n")
    return 0


def cmd_list(args) -> int:
    """List note paths in the vault, optionally within a folder."""
    if not _require_vault():
        return 1
    from integrations import obsidian

    paths = obsidian.list_notes(folder=args.folder)
    if not paths:
        print("(no notes)")
        return 0
    print("\n".join(paths))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vault_cli",
        description="Token-free Obsidian vault tools for JARVIS (no Claude API calls).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Readiness report: config, scaffold, sources, index.").set_defaults(func=cmd_check)

    m = sub.add_parser("migrate", help="Import legacy notes + facts into the vault.")
    m.add_argument("--dry-run", action="store_true", help="Preview the import without writing.")
    m.set_defaults(func=cmd_migrate)

    sub.add_parser("reindex", help="Rebuild the search index from the vault.").set_defaults(func=cmd_reindex)

    s = sub.add_parser("search", help="FTS search the vault (token-free).")
    s.add_argument("query")
    s.add_argument("--tag", help="Filter by tag (with or without #).")
    s.add_argument("--folder", help="Scope to a folder, e.g. People.")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_search)

    ls = sub.add_parser("list", help="List notes in the vault.")
    ls.add_argument("folder", nargs="?", default=None)
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
