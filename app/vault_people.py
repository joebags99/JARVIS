"""Back-compat shim — ``python -m app.vault_people`` consolidates *people* only.

The general command is :mod:`app.vault_entities`, which consolidates people **and**
companies/projects. This thin wrapper just forces ``--kind people`` so any existing
muscle memory / docs keep working.
"""

from __future__ import annotations

import sys

from app.vault_entities import main as _main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--kind" not in argv:
        argv += ["--kind", "people"]
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
