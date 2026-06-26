"""Tests for the token-free vault CLI (app/vault_cli.py)."""

from __future__ import annotations

import pytest

from app import vault_cli, vault_index
from app.config import CONFIG
from app.vault_index import VaultIndex


@pytest.fixture
def cli_vault(tmp_path, monkeypatch):
    """A temp vault + redirected index, with legacy sources pointed at empties."""
    root = tmp_path / "vault"
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", str(root))
    monkeypatch.setattr(CONFIG, "obsidian_enabled", True)
    monkeypatch.setattr(vault_index, "_INDEX", VaultIndex(tmp_path / "idx.db"))
    # Keep migration from touching the real repo notes/ + memory.db.
    monkeypatch.setattr("integrations.obsidian.NOTES_DIR", tmp_path / "notes")
    monkeypatch.setattr("integrations.obsidian.MEMORY_DB_PATH", tmp_path / "memory.db")
    return root


def test_check_reports_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(CONFIG, "obsidian_enabled", False)
    monkeypatch.setattr(CONFIG, "obsidian_vault_path", "")
    rc = vault_cli.main(["check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "OBSIDIAN_ENABLED" in out


def test_check_runs_when_configured(cli_vault, capsys):
    rc = vault_cli.main(["check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Obsidian vault check" in out
    assert "zero tokens" in out


def test_migrate_dry_run_writes_nothing(cli_vault, tmp_path, capsys):
    notes = tmp_path / "notes"
    (notes / "General").mkdir(parents=True)
    (notes / "General" / "n.md").write_text("hi", encoding="utf-8")

    rc = vault_cli.main(["migrate", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert "Imported/General/n.md" in out
    assert not cli_vault.exists()  # preview created nothing


def test_migrate_then_search_roundtrip(cli_vault, capsys):
    from integrations import obsidian

    assert vault_cli.main(["migrate"]) == 0
    obsidian.write_note("Topics/Jazz.md", "I love jazz music", title="Jazz")
    assert vault_cli.main(["search", "jazz"]) == 0
    out = capsys.readouterr().out
    assert "Jazz" in out
    # idempotent: a second migrate is a no-op
    assert vault_cli.main(["migrate"]) == 0
    assert "Already migrated" in capsys.readouterr().out


def test_list(cli_vault, capsys):
    from integrations import obsidian

    obsidian.ensure_scaffold()
    obsidian.write_note("People/Sam.md", "x", title="Sam")
    assert vault_cli.main(["list", "People"]) == 0
    assert "People/Sam.md" in capsys.readouterr().out


def test_idea_graph_moc_doctor(cli_vault, capsys):
    root = cli_vault
    from integrations import obsidian

    obsidian.ensure_scaffold()
    obsidian.write_note("People/Sam.md", "see [[Ghost]]", title="Sam", canonicalize=False)

    assert vault_cli.main(["idea", "build", "a", "wake", "word"]) == 0
    assert "Ideas/Inbox.md" in capsys.readouterr().out

    assert vault_cli.main(["graph"]) == 0
    assert "graph colors" in capsys.readouterr().out.lower()
    assert (root / ".obsidian" / "graph.json").exists()

    assert vault_cli.main(["moc"]) == 0
    assert "map(s)" in capsys.readouterr().out

    assert vault_cli.main(["doctor"]) == 0
    out = capsys.readouterr().out.lower()
    assert "vault health" in out and "dangling" in out  # the [[Ghost]] link is flagged


def test_refile_moves_misfiled_meetings(cli_vault, capsys):
    from integrations import obsidian

    obsidian.ensure_scaffold()
    # write_note routes meetings away now, so drop the misfiled note on disk directly.
    (cli_vault / "People").mkdir(parents=True, exist_ok=True)
    (cli_vault / "People" / "team_meeting_june.md").write_text(
        "---\ntitle: TM\ntype: person\n---\n\n# TM\n\nx\n", encoding="utf-8")
    assert vault_cli.main(["refile"]) == 0          # preview
    assert "Would move" in capsys.readouterr().out
    assert vault_cli.main(["refile", "--apply"]) == 0
    assert "moved to sessions" in capsys.readouterr().out.lower()
    assert vault_cli.main(["refile"]) == 0          # nothing left
    assert "No misfiled" in capsys.readouterr().out


def test_dedupe_merges_cross_folder_duplicates(cli_vault, capsys):
    from integrations import obsidian

    obsidian.ensure_scaffold()
    obsidian.write_note("People/Joe Konkle.md", "person", title="Joe Konkle", canonicalize=False)
    obsidian.write_note("Projects/joe_konkle.md", "dup", title="Joe Konkle", canonicalize=False)
    assert vault_cli.main(["dedupe"]) == 0                 # preview
    assert "Would merge" in capsys.readouterr().out
    assert vault_cli.main(["dedupe", "--apply"]) == 0
    assert "merged" in capsys.readouterr().out.lower()
    assert vault_cli.main(["dedupe"]) == 0                 # nothing left
    assert "No cross-folder" in capsys.readouterr().out


def test_upgrade_modernizes_old_notes(cli_vault, capsys):
    from integrations import obsidian

    obsidian.ensure_scaffold()
    obsidian.set_aliases("Joe Konkle", ["Joe"])
    obsidian.write_note("Sessions/old.md", "Met Joe.", title="Old")  # bare mention
    assert vault_cli.main(["upgrade"]) == 0
    assert "Upgraded existing notes" in capsys.readouterr().out
    assert "[[Joe Konkle|Joe]]" in obsidian.read_note("Sessions/old.md").body
