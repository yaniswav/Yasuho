"""Tolerant Discord-id coercion for values read out of JSONB config blobs.

The Remix dashboard is a SEPARATE Node process writing the same Postgres rows
the bot reads, and JavaScript cannot hold a snowflake in a Number (2^53), so it
serialises every id as a STRING - which means a ``guild_settings`` blob may come
back with ``"123"`` where the bot expects ``123``. ``guild.get_channel("123")``
and ``guild.get_role("123")`` simply return ``None``, so the feature goes silent
with no error at all. Read seams run ids through :func:`coerce_id` so both
spellings resolve to the same channel/role.

Deliberately pure and dependency-free: no discord.py, no DB, no logging.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

__all__ = ["coerce_id", "coerce_ids"]


def coerce_id(value):
    """Return ``value`` as a positive int snowflake, or ``None`` when it is not one.

    Accepts a positive ``int`` and a clean ASCII decimal ``str`` (surrounding
    whitespace tolerated). Everything else is ``None``: ``bool`` (``True`` is an
    ``int`` in Python, and a stray toggle must never masquerade as id ``1``),
    ``float``, non-decimal or non-ASCII digit strings, ``0``, negatives, and any
    other type. ``None`` in, ``None`` out.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        # isascii() matters: "²".isdigit() is True but int() would raise, and
        # non-ASCII digits are never what the dashboard writes.
        if text.isascii() and text.isdigit():
            number = int(text)
            return number if number > 0 else None
    return None


def coerce_ids(values):
    """Return the valid snowflakes of ``values`` as a list of ints, order kept.

    Junk entries are dropped (each is passed through :func:`coerce_id`), so an
    id list out of a JSONB blob can be compared against live ``.id`` ints. A
    non-sequence (including ``None``, and a bare ``str``, which would otherwise
    iterate character by character) yields an empty list.
    """
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    out = []
    for value in values:
        coerced = coerce_id(value)
        if coerced is not None:
            out.append(coerced)
    return out
