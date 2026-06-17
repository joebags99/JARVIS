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

# Ordinary English words and common given names are never fuzzy-corrected: they
# are correctly-spelled real words/people, not misspellings of a fantasy name,
# so "Mark" must stay "Mark" (not become "Marik") in a meeting note. Exact,
# explicitly-listed variants still apply — this only gates the guessy fuzzy
# pass. Users can extend it with an "ignore" list in name_corrections.json.
_COMMON_WORDS = frozenset("""
about above after again against also always another answer anyone area around
away back because been before being below best better between both bring call
came case change come could course current daily date days deal does done down
during each early else even ever every example feel find first follow form
found from full give goes going good great group hand have having held help
here high home hour house however idea info into item just keep kind know last
late later lead least left less life like line list little long look made make
many mark mean meet might more most move much must name need never next none
note nothing only open order other over part past place plan play point present
problem program provide question quite rather read real really result review
right room same says school seem seen send sent several should show side since
some soon sort still such sure take talk team tell than that them then there
these they thing think this those time today together took toward turn under
until upon used user very view want week well went were what when where which
while will with word work would year your
monday tuesday wednesday thursday friday saturday sunday
january february march april june july august september october november december
aaron adam adrian alan albert alex alexander alice amanda amber amy andrea
andrew angela anna anne anthony april ashley austin barbara becky ben benjamin
beth bill billy bob bobby brad bradley brandon brenda brian bruce bryan caleb
cameron carl carol caroline carrie casey catherine charles charlie cheryl chris
christian christina christine christopher cindy claire clara cody colin connor
craig crystal dan dana daniel danny darren dave david dawn dean deborah debra
denise dennis derek diana diane don donald donna doug douglas dylan eddie edward
elaine eleanor elizabeth ellen emily emma eric erica erin ethan eugene evan
evelyn frank fred gabriel gary george gerald gina glenn gloria grace greg
gregory hannah harold harry heather heidi helen henry holly howard ian isaac
jack jackie jacob jake james jamie jane janet janice jared jason jean jeff
jeffrey jennifer jenny jeremy jerry jesse jessica jill jimmy joan joanne joel
john johnny jonathan jordan joseph josh joshua joyce juan judith judy julia
julie justin kaitlyn karen kari karl kate katherine kathleen kathy katie kayla
keith kelly kenneth kevin kimberly kyle larry laura lauren laurie lawrence leon
leonard leslie linda lisa lloyd logan lori louis lucas luke lynn marc marcus
margaret maria marie marilyn martha martin mary mason matt matthew megan melissa
michael micheal michelle mike miranda mitchell molly nancy natalie nathan neil
nicholas nick nicole noah norman olivia oscar pamela patricia patrick paul paula
peggy peter philip phillip rachel ralph randy raymond rebecca regina renee
richard ricky robert roberta robin roger ronald rosa russell ruth ryan sam
samantha samuel sandra sara sarah scott sean seth shane shannon sharon shawn
sheila shirley sophia stacey stanley stephanie stephen steve steven susan tammy
tanya taylor teresa terry theresa thomas timothy tina todd tommy tony tracy
travis trevor troy tyler valerie vanessa victor victoria vincent virginia walter
wanda warren wayne wendy william willie zachary
""".split())

# Cache: (canonical_names, alias_map, fuzzy_stoplist). alias_map maps a
# lowercased variant OR lowercased canonical -> the canonical spelling.
# fuzzy_stoplist is _COMMON_WORDS plus the user's "ignore" list (lowercased).
# None means "not loaded yet".
_cache: tuple[list[str], dict[str, str], frozenset[str]] | None = None


def reload() -> None:
    """Drop the cached glossary so the next call re-reads the file."""
    global _cache
    _cache = None


def _load() -> tuple[list[str], dict[str, str], frozenset[str]]:
    global _cache
    if _cache is not None:
        return _cache

    path = ROOT_DIR / CONFIG.name_corrections_file
    canon: list[str] = []
    aliases: dict[str, str] = {}
    extra_ignore: set[str] = set()
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
            for word in data.get("ignore", []) or []:
                word = str(word).strip().lower()
                if word:
                    extra_ignore.add(word)
            log.info("loaded %d canonical name(s) for correction", len(canon))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", path.name, exc)

    # An explicitly-listed variant should still win over the stoplist, so never
    # let a known alias block its own fuzzy fallback.
    stoplist = frozenset((_COMMON_WORDS | extra_ignore) - set(aliases))
    _cache = (canon, aliases, stoplist)
    return _cache


def hotwords() -> str:
    """Space-joined canonical names (+ variants) to bias Whisper. "" if none."""
    canon, aliases, _ = _load()
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
    canon, aliases, stoplist = _load()
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
                or low in stoplist         # ordinary word/common name — leave it
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
