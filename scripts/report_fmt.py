"""Shared formatting for verification failure records and report rendering.

Three scripts agree on one data contract — import_check.py and parse_check.py
produce failure records, compare_failures.py consumes them — and one
Markdown-safety rule: error messages render as code spans so `__init__`/
`__future__` don't turn into bold on GitHub, with inner backticks downgraded
so they can't break the span. Both live here so the producers and the
consumer can't drift apart silently. (verify.sh's baseline-unavailable path
re-implements code_span in jq — `gsub("`"; "'")` — keep it in sync.)
"""

from __future__ import annotations


def failure(path: str, exc_class: str, msg: str) -> dict:
    """One verification failure, keyed exactly as compare_failures.py reads it."""
    return {"path": path, "exc_class": exc_class, "msg": msg}


def code_span(msg: str) -> str:
    return f"`{msg.replace(chr(96), chr(39))}`"
