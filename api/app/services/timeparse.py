"""Robust ISO-8601 timestamp parsing for values read back from Supabase/Postgres.

Python 3.9's ``datetime.fromisoformat`` is strict: it only accepts a literal
``+HH:MM`` offset (not ``Z``) and a fractional second of exactly 3 or 6 digits.
Postgres ``TIMESTAMPTZ`` values come back with trailing zeros trimmed — e.g.
``2036-06-09T19:43:58.61511+00:00`` (5 digits) — which would otherwise raise an
``Invalid isoformat string`` error *intermittently*, only when the stored
microseconds happen to end in zero. Some APIs also use a trailing ``Z``.
"""
from __future__ import annotations

import re
from datetime import datetime

# Match the fractional-seconds group ONLY when it directly precedes a ±HH:MM
# offset, so the offset itself is never altered.
_ISO_FRAC = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}$)")


def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp robustly under Python 3.9's strict rules.

    Normalizes a trailing ``Z`` to ``+00:00`` and pads/truncates the fractional
    second to 6 digits, so a valid Postgres/Supabase timestamp never raises.
    """
    s = ts.strip().replace("Z", "+00:00")
    s = _ISO_FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s)
    return datetime.fromisoformat(s)
