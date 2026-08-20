from __future__ import annotations

import datetime
import random
from typing import Any, Optional, Sequence

import discord

# How much of a quoted-back argument a public message shows. Long enough for any
# real name (playlist names cap at 60, a voice channel name at 100), short enough
# that a 2000-character argument can neither blow the 2000-char message limit nor
# wall a channel.
ECHO_LIMIT = 80


def random_colour() -> int:
    """Return a random embed colour spanning the full RGB range."""
    return random.randint(0x000000, 0xFFFFFF)


class plural:
    def __init__(self, value: int):
        self.value: int = value

    def __format__(self, format_spec: str) -> str:
        v = self.value
        singular, sep, plural = format_spec.partition('|')
        plural = plural or f'{singular}s'
        if abs(v) != 1:
            return f'{v} {plural}'
        return f'{v} {singular}'


def human_join(seq: Sequence[str], delim: str = ', ', final: str = 'or') -> str:
    size = len(seq)
    if size == 0:
        return ''

    if size == 1:
        return seq[0]

    if size == 2:
        return f'{seq[0]} {final} {seq[1]}'

    return delim.join(seq[:-1]) + f' {final} {seq[-1]}'


def format_dt(dt: datetime.datetime, style: Optional[str] = None) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    if style is None:
        return f'<t:{int(dt.timestamp())}>'
    return f'<t:{int(dt.timestamp())}:{style}>'


# ---------------------------------------------------------------------------
# Quoting somebody else's text into a message the bot signs
# ---------------------------------------------------------------------------
# Third-party text (a track title, a playlist name, a channel name a moderator
# typed) is markup until it is made inert. These two live HERE, and not in one
# package, because three of them already need the same rule: cogs/music's
# safetext, cogs/community/profile's presence card and cogs/moderation. A rule
# with three copies is a rule that drifts.


def one_line(text: Any) -> str:
    """Collapse every run of whitespace, newlines included, into single spaces.

    Markdown structure (``#``, ``-#``, ``>``) only means anything at the START of
    a line, so a value with no interior newline left cannot forge a heading or a
    quote block inside a message someone else's text is embedded in.
    """
    return " ".join(str(text).split())


def public_echo(text: Any, *, limit: int = ECHO_LIMIT) -> str:
    """A user-typed value made inert for quoting back into a PUBLIC message.

    Flattened, clipped to ``limit``, then markdown-escaped. The escape is
    ``discord.utils.escape_markdown``, which neutralises ``* _ ~ | `` ` `` \\``,
    a line-leading ``>`` and a complete ``[text](url)`` - so a name like
    ``[click here](https://evil.example)`` is quoted as visible text instead of
    becoming a link the bot appears to endorse. Clipping happens BEFORE the
    escape so the cap governs what the member wrote, not how many backslashes it
    took to defuse it (and so the clip can never cut a ``\\x`` pair in half).

    This is only half the guard: it stops the MARKUP. The PINGS are stopped at
    the send, by passing ``allowed_mentions=discord.AllowedMentions.none()`` -
    the two always travel together.
    """
    flattened = one_line(text)
    if len(flattened) > limit:
        flattened = flattened[: max(0, limit - 3)] + "..."
    return discord.utils.escape_markdown(flattened)
