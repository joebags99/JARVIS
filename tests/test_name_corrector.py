"""Tests for proper-name correction (app/name_corrector.py)."""

from __future__ import annotations

import pytest

from app import name_corrector


@pytest.fixture(autouse=True)
def _restore_cache():
    """Keep tests from leaking the glossary cache into one another."""
    saved = name_corrector._cache
    yield
    name_corrector._cache = saved


def _set_glossary(canon, aliases, stoplist=frozenset()):
    name_corrector._cache = (canon, aliases, frozenset(stoplist))


def test_no_glossary_returns_input_unchanged():
    _set_glossary([], {})
    assert name_corrector.normalize_names("Cailynn was here") == "Cailynn was here"


def test_exact_variant_replacement():
    _set_glossary(["Kailin"], {"kailin": "Kailin", "cailynn": "Kailin"})
    assert name_corrector.normalize_names("I met Cailynn today") == "I met Kailin today"


def test_replacement_matches_lowercase_case():
    _set_glossary(["Kailin"], {"kailin": "Kailin", "cailynn": "Kailin"})
    # a lowercased occurrence keeps a lowercased first letter
    assert name_corrector.normalize_names("hello cailynn") == "hello kailin"


def test_fuzzy_corrects_close_unlisted_variant():
    _set_glossary(["Marik"], {"marik": "Marik"})
    assert name_corrector.normalize_names("Marick arrives") == "Marik arrives"


def test_stoplisted_word_is_not_fuzzy_corrected():
    _set_glossary(["Marik"], {"marik": "Marik"}, stoplist={"mark"})
    assert name_corrector.normalize_names("Mark arrives") == "Mark arrives"


def test_short_tokens_are_not_fuzzy_corrected():
    _set_glossary(["Marik"], {"marik": "Marik"})
    # "Mar" is below MIN_FUZZY_LEN, so it's left alone
    assert name_corrector.normalize_names("Mar is short") == "Mar is short"


def test_hotwords_includes_canonicals():
    _set_glossary(["Kailin", "Adaria"], {"kailin": "Kailin", "adaria": "Adaria"})
    words = name_corrector.hotwords().split()
    assert "Kailin" in words and "Adaria" in words
