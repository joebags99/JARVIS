"""LLM-driven vault cleanup — tidy the migrated ``Imported/`` notes.

Unlike :mod:`app.vault_cli` (which never touches the API), this command sends one
request per note to the Claude API, so **it costs tokens**. It's a deliberate,
one-time tidy-up: each raw imported note is rewritten with proper frontmatter,
tags, and ``[[wikilinks]]`` and refiled into the right folder
(Sessions/People/Projects/Topics/...). Run from the repo root:

    python -m app.vault_organize                 # PREVIEW: propose a plan (calls the API), writes nothing
    python -m app.vault_organize --limit 5       # only the first 5 notes (good first run)
    python -m app.vault_organize --apply          # write the tidied notes + archive the originals
    python -m app.vault_organize --folder Imported/Daedabyte
    python -m app.vault_organize --model claude-haiku-4-5   # cheaper model

It is **non-destructive**: with ``--apply`` the tidied note is written to its new
home and the original is moved to ``Archive/`` (never deleted), so you can review
in Obsidian and delete ``Archive/`` once happy. A token-free way to see *which*
notes would be processed first is ``python -m app.vault_cli list Imported``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

from app.config import CONFIG

# Folders the cleanup may refile a note into. "Projects/<Name>" subfolders are
# allowed too; anything else falls back to Topics.
_ALLOWED_FOLDERS = ["Sessions", "Daily", "People", "Projects", "Topics", "Memory"]

_SYSTEM = (
    "You reorganize ONE note from the user's Obsidian knowledge vault so the vault "
    "stays tidy and interconnected. You are given a note's current path and its raw "
    "content. Rewrite it cleanly and decide where it belongs.\n\n"
    "Return ONLY a JSON object (no prose, no code fence) with these keys:\n"
    '  "folder": one of ' + ", ".join(_ALLOWED_FOLDERS) + ' — or a "Projects/<Name>" '
    "subfolder (e.g. \"Projects/Daedabyte\"). Pick the single best fit: meeting/recap "
    "notes → Sessions or Projects/<Name>; a person → People; a project → Projects/<Name>; "
    "reference/topic notes → Topics; durable facts → Memory.\n"
    '  "title": a clear human title for the note.\n'
    '  "tags": a list of lowercase tags (no "#"), e.g. ["meeting","daedabyte"].\n'
    '  "body": the cleaned markdown body. Do NOT include a frontmatter block or an '
    "H1 title (those are added automatically).\n\n"
    "Rules:\n"
    "- FAITHFUL cleanup, not summarization: preserve every fact, decision, action item, "
    "owner, date, number, and open question. Fix formatting and structure only.\n"
    "- Use clear ## section headers (e.g. Summary, Decisions, Action Items, Open Questions).\n"
    "- Connect people and projects with [[wikilinks]] in the body so the vault graph "
    "stays linked. Use consistent full names.\n"
    "- Never invent detail. If something is unknown, keep it as stated (e.g. 'TBD')."
)


@dataclass
class Proposal:
    """A tidied note the cleanup proposes for one source note."""

    source: str
    target: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)


def _loads_lenient(text: str) -> dict:
    """Parse a JSON object from a model reply, tolerating ```json fences/prose."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model reply")
    return json.loads(raw[start:end + 1])


def parse_proposal(rel: str, text: str) -> Proposal:
    """Turn a model JSON reply into a :class:`Proposal` (raises on bad output)."""
    from integrations import obsidian

    data = _loads_lenient(text)
    folder = str(data.get("folder") or "Topics").strip().strip("/") or "Topics"
    title = str(data.get("title") or obsidian._title_from_stem(rel.rsplit("/", 1)[-1][:-3])).strip()
    tags = [str(t).lstrip("#").strip() for t in (data.get("tags") or []) if str(t).strip()]
    body = str(data.get("body") or "").strip()
    if not body:
        raise ValueError("model returned an empty body")
    target = obsidian.path_for_title(title, folder)
    return Proposal(source=rel, target=target, title=title, body=body, tags=tags)


def _api_proposer(model: str):
    """Build a ``(rel, raw) -> Proposal`` function backed by the Claude API."""
    from anthropic import Anthropic

    client = Anthropic(api_key=CONFIG.anthropic_api_key)

    def propose(rel: str, raw: str) -> Proposal:
        msg = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Current path: {rel}\n\n<<<NOTE>>>\n{raw}\n<<<END NOTE>>>",
            }],
        )
        text = msg.content[0].text if msg.content else ""
        return parse_proposal(rel, text)

    return propose


def run(
    *, folder: str = "Imported", apply: bool = False, limit: int | None = None,
    model: str | None = None, proposer=None, on_result=None,
) -> dict:
    """Reorganize notes under *folder*. Preview by default; ``apply`` to commit.

    *proposer* is a ``(rel, raw) -> Proposal`` callable; it defaults to the Claude
    API one (injected as a fake in tests). On ``apply`` each tidied note is written
    to its new home (non-colliding) and the original is moved to ``Archive/``.
    Returns ``{"planned": [...], "applied": [...], "errors": [...]}``.
    """
    from integrations import obsidian

    proposer = proposer or _api_proposer(model or CONFIG.anthropic_model)
    root = obsidian.vault_root()
    rels = obsidian.list_notes(folder=folder)
    if limit is not None:
        rels = rels[:limit]

    out: dict = {"planned": [], "applied": [], "errors": []}
    for rel in rels:
        try:
            raw = (root / rel).read_text(encoding="utf-8", errors="replace")
            prop = proposer(rel, raw)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append((rel, str(exc)))
            continue
        out["planned"].append(prop)
        if apply:
            obsidian.write_note(
                prop.target, prop.body, title=prop.title, tags=prop.tags, overwrite=False
            )
            archived = obsidian.move_to_archive(rel)
            out["applied"].append((rel, prop.target, archived))
        if on_result:
            on_result(prop, apply)
    return out


def _print_report(out: dict, apply: bool) -> None:
    verb = "Refiled" if apply else "Would refile"
    for prop in out["planned"]:
        tags = f"  [tags: {', '.join(prop.tags)}]" if prop.tags else ""
        print(f"{verb}: {prop.source}  →  {prop.target}{tags}")
    for rel, err in out["errors"]:
        print(f"!! skipped {rel}: {err}")
    print(
        f"\n{len(out['planned'])} note(s) {'tidied' if apply else 'planned'}, "
        f"{len(out['errors'])} skipped."
    )
    if apply:
        print("Originals moved to Archive/ (delete it once you're happy).")
    else:
        print("Preview only — nothing written. Re-run with --apply to commit.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="vault_organize",
        description="Tidy the vault's Imported/ notes with the Claude API (costs tokens).",
    )
    p.add_argument("--apply", action="store_true",
                   help="Write the tidied notes and archive originals (default: preview).")
    p.add_argument("--folder", default="Imported", help="Folder to clean (default: Imported).")
    p.add_argument("--limit", type=int, default=None, help="Only process the first N notes.")
    p.add_argument("--model", default=None,
                   help="Model to use (default: ANTHROPIC_MODEL).")
    args = p.parse_args(argv)

    if not CONFIG.obsidian_available:
        print("Obsidian vault not configured. Set OBSIDIAN_ENABLED=true and "
              "OBSIDIAN_VAULT_PATH in .env.")
        return 1
    if not CONFIG.has_anthropic_key:
        print("This command calls the Claude API. Set ANTHROPIC_API_KEY in .env.")
        return 1

    print(f"{'Applying' if args.apply else 'Previewing'} cleanup of '{args.folder}' "
          f"(model={args.model or CONFIG.anthropic_model}) — this calls the API per note.\n")
    out = run(folder=args.folder, apply=args.apply, limit=args.limit, model=args.model)
    _print_report(out, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
