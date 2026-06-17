"""Fantasy/proper-name correction for transcription and typed input.

Whisper (and typing) routinely mangle proper names — "Kailin" → "Cailynn",
"Adaria" → "Ederia". A user-maintained glossary (``name_corrections.json``,
gitignored, same convention as ``knowledge_pools.json``) maps each canonical
name to its known variants:

    {"names": {"Kailin": ["Cailynn", "Caelyn"], "Edwin": ["Edwinn"]}}

Two uses:
  * ``hotwords()`` biases Whisper toward the canonical spellings at the source.
  * ``normalize_names(text)`` fixes the text after the fact — exact replacement
    of listed variants, plus a conservative fuzzy pass for new close variants.

Everything is best-effort and never raises: with no glossary file, ``hotwords``
is "" and ``normalize_names`` returns its input unchanged.
"""

from __future__ import annotations

import difflib
import json
import re

from .config import CONFIG, ROOT_DIR
from .logging_setup import get_logger

log = get_logger("names")

MIN_FUZZY_LEN = 4     # don't fuzzy-match very short tokens
                      # (fuzzy cutoff is CONFIG.name_fuzzy_cutoff)

# Cache: (canonical_names, alias_map). alias_map maps a lowercased variant OR
# lowercased canonical -> the canonical spelling. None means "not loaded yet".
_cache: tuple[list[str], dict[str, str]] | None = None


def reload() -> None:
    """Drop the cached glossary so the next call re-reads the file."""
    global _cache
    _cache = None


def _load() -> tuple[list[str], dict[str, str]]:
    global _cache
    if _cache is not None:
        return _cache

    path = ROOT_DIR / CONFIG.name_corrections_file
    canon: list[str] = []
    aliases: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names = data.get("names", {})
            for canonical, variants in names.items():
                canonical = str(canonical).strip()
                if not canonical:
                    continue
                canon.append(canonical)
                aliases[canonical.lower()] = canonical
                for variant in variants or []:
                    variant = str(variant).strip()
                    if variant:
                        aliases[variant.lower()] = canonical
            log.info("loaded %d canonical name(s) for correction", len(canon))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", path.name, exc)

    _cache = (canon, aliases)
    return _cache


def hotwords() -> str:
    """Space-joined canonical names (+ variants) to bias Whisper. "" if none."""
    canon, aliases = _load()
    if not canon:
        return ""
    # Include variants too — they're real tokens the model might lean toward.
    return " ".join(dict.fromkeys([*canon, *aliases.values()]))


def _match_case(canonical: str, original: str) -> str:
    """Render *canonical* using the leading-capital style of *original*."""
    if original[:1].isupper():
        return canonical
    return canonical[:1].lower() + canonical[1:]


def normalize_names(text: str) -> str:
    """Correct known/likely-misspelled names in *text*. Never raises."""
    if not text:
        return text
    canon, aliases = _load()
    if not aliases:
        return text

    try:
        result = text
        # ── Stage 1: exact replacement of listed variants/canonicals. Longest
        # phrases first so multi-word aliases ("Shadow Heart") win over parts.
        for alias in sorted(aliases, key=len, reverse=True):
            canonical = aliases[alias]
            pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)

            def _sub(m: re.Match, _c=canonical) -> str:
                return _match_case(_c, m.group(0))

            result = pattern.sub(_sub, result)

        # ── Stage 2: conservative fuzzy for unlisted close variants. Only
        # capitalized, reasonably long tokens, single best match above cutoff.
        canon_lower = {c.lower(): c for c in canon}

        def _fuzzy(m: re.Match) -> str:
            token = m.group(0)
            low = token.lower()
            if (
                len(token) < MIN_FUZZY_LEN
                or not token[:1].isupper()
                or low in aliases          # already canonical/known variant
            ):
                return token
            hit = difflib.get_close_matches(
                low, canon_lower.keys(), n=1, cutoff=CONFIG.name_fuzzy_cutoff
            )
            if hit:
                return _match_case(canon_lower[hit[0]], token)
            return token

        result = re.sub(r"[A-Za-z][A-Za-z'’]+", _fuzzy, result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("name normalization failed: %s", exc)
        return text
