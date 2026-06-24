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
