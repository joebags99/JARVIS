"""Tests for the persona voice dials (app/persona.py)."""

from __future__ import annotations

import pytest

from app import persona


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A Persona backed by a throwaway dials file (no real persona_dials.json)."""
    monkeypatch.setattr(persona, "DIALS_FILE", tmp_path / "dials.json")
    return persona.Persona()


def test_defaults_loaded(fresh):
    assert fresh.dials == persona.DEFAULTS


def test_describe_picks_band():
    # brevity 75 falls in the "be concise" band (<=80), not the terse <=100 band.
    assert "concise" in persona._describe("brevity", 75).lower()
    assert "terse" in persona._describe("brevity", 100).lower()


def test_adjust_set_to_clamps(fresh):
    fresh.adjust("humor", set_to=250)
    assert fresh.dials["humor"] == 100
    fresh.adjust("humor", set_to=-10)
    assert fresh.dials["humor"] == 0


def test_adjust_change_by_relative(fresh):
    start = fresh.dials["sarcasm"]
    fresh.adjust("sarcasm", change_by=-15)
    assert fresh.dials["sarcasm"] == max(0, start - 15)


def test_adjust_query_only_reports(fresh):
    before = dict(fresh.dials)
    msg = fresh.adjust("formality")  # no set_to/change_by → just reports
    assert "currently" in msg.lower()
    assert fresh.dials == before


def test_unknown_dial(fresh):
    msg = fresh.adjust("loudness", set_to=50)
    assert "unknown dial" in msg.lower()


def test_reset_alias_restores_defaults(fresh):
    fresh.adjust("humor", set_to=100)
    fresh.adjust("reset")
    assert fresh.dials == persona.DEFAULTS


def test_persist_writes_file(fresh):
    # `fresh` already points DIALS_FILE at a tmp file, so persist round-trips there.
    fresh.adjust("humor", set_to=42, persist=True)
    # A new Persona reading the same file should see the persisted default.
    reborn = persona.Persona()
    assert reborn.dials["humor"] == 42
