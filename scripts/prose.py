#!/usr/bin/env python3
"""Loader for ``messages.md`` — every sentence the club says to a participant.

Why a separate module and not part of ``messages.py``: ``messages.py`` imports
``state``, so ``state`` cannot import it back, and ``state``'s ``apply_*`` outcome
lines are participant-facing prose too. Both import this instead.

Why a markdown file and not Python string literals: the prose was being revised in two
files at once and drifting in voice. One catalogue, edited in the same language the
output is written in, means a wording change reads as a wording change in the diff and
needs no knowledge of Python to make.

**What is here and what is not.** Sentences and blocks live in ``messages.md``. Their
*assembly* — which block appears when, how a table is built row by row — stays in
``messages.py``, because that is logic wearing prose's clothes. Editing the catalogue
changes what is said, never when.

Every ``params`` constant is pre-seeded into the format context, so the catalogue can
write ``{DEADLINE_DAYS}`` or ``{SITE_URL}`` without the call site passing them.
"""
from __future__ import annotations

import re
from pathlib import Path

import params

CATALOGUE = Path(__file__).resolve().parent / "messages.md"

_HEADING = re.compile(r"^##\s+([a-z0-9_.]+)\s*$", re.MULTILINE)

# Constants any message may interpolate by name. Kept to plain scalars: a message that
# needs to *compute* something wants a call-site argument, not a smarter catalogue.
_DEFAULTS = {k: v for k, v in vars(params).items()
             if k.isupper() and isinstance(v, (str, int, float))}


def _load(text: str) -> dict[str, str]:
    """Parse `## key` sections into {key: body}. Anything before the first is preamble."""
    out, marks = {}, list(_HEADING.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip("\n")
        key = m.group(1)
        if key in out:
            raise ValueError(f"messages.md: duplicate key '{key}'")
        out[key] = body
    return out


TEXT = _load(CATALOGUE.read_text(encoding="utf-8"))


def t(key: str, **kw) -> str:
    """Render one catalogue entry.

    Loud on both failure modes, because the alternative is a half-rendered sentence on
    a participant's thread: an unknown key, and a placeholder the caller did not supply.
    """
    if key not in TEXT:
        raise KeyError(f"messages.md has no entry '{key}' "
                       f"(nearest: {', '.join(sorted(TEXT)[:3])}…)")
    try:
        return TEXT[key].format(**{**_DEFAULTS, **kw})
    except KeyError as exc:
        raise KeyError(f"messages.md entry '{key}' wants {exc} — "
                       f"pass it to prose.t(), or fix the placeholder") from None


def placeholders(key: str) -> set[str]:
    """The names an entry interpolates. Used by the tests to render everything."""
    return {m.group(1).split(".")[0].split("[")[0]
            for m in re.finditer(r"\{([a-zA-Z_][a-zA-Z0-9_.\[\]]*)\}", TEXT[key])}
