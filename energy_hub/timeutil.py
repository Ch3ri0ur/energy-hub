"""Shared time helpers."""

from __future__ import annotations

from datetime import datetime


def parse_iso_epoch(value: object) -> float | None:
    """Parse an ISO-8601 string into a unix epoch.

    Naive strings (no offset — what Tasmota sends) are interpreted in the
    host's local timezone.  A trailing ``Z`` is treated as UTC.  Returns
    ``None`` for empty input or anything that does not parse.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
