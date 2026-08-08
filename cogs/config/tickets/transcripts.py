"""A closing ticket's conversation, rendered to a plain-text file IN MEMORY.

This module is the one place in the feature that ever touches what people typed,
and it is built so that touching it cannot turn into keeping it:

* it has NO database import and takes no pool. There is no code path from here
  to a table, and the ``tickets`` table has no column a transcript could go into
  anyway (schema.sql). PRIVACY.md's "ticket content is never stored" stays
  literally true because there is nothing here that could store it;
* it writes NO file to disk. The transcript exists as a ``BytesIO`` that is
  handed to one ``discord.File`` and then dropped when the close flow returns;
* it decides NOTHING about where the file goes. The caller
  (cogs/config/tickets/lifecycle.py) only builds one when a log channel is
  configured, and sends it there and nowhere else - never in the ticket, never
  in a DM, never to the opener.

A LEAF of the package: no i18n, no settings, no SQL, no knowledge of tickets as
a concept. The caller passes the header lines it wants (already translated) and
the filename; this module fetches, renders and wraps. That keeps it unit-testable
with plain stand-ins and keeps the localized wording in one module.

Two bounds, both hard, because a transcript is the only unbounded thing in the
feature - a busy ticket has as many messages as people typed:

* :data:`MAX_MESSAGES` caps the fetch, so the REST cost of a close is at most
  ten paginated history calls whatever the thread looks like;
* :data:`MAX_BYTES` caps the rendered text, so a thread of maximum-length
  messages cannot produce a buffer too large to upload (or too large to hold).

Both truncate from the END and say so in the file: the OLDEST messages are the
ones a support transcript is actually about (the request, the diagnosis), so
``oldest_first=True`` keeps the useful part and drops the tail rather than the
other way round.

Attachment URLs are recorded, not the attachments. Discord's CDN links are
signed and expire, so a transcript preserves what was sent and roughly when, not
the bytes - which is the honest trade for a file that must not become a data
store.

Reading an ARCHIVED thread works: ``Thread`` inherits ``history`` straight from
``discord.abc.Messageable`` with no archived guard (discord.py 2.7.1), and the
only archived restriction the library documents is on ``Thread.edit`` ("The
thread must be unarchived to be edited"). So the auto-archive close path fetches
the transcript from the archived thread as-is and never unarchives it.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import io
import logging

import discord

log = logging.getLogger(__name__)

# How many messages a transcript may contain. discord.py pages history 100 at a
# time, so this is at most ten REST calls - the bound that keeps "close a very
# old, very busy ticket" the same cost as closing a quiet one.
MAX_MESSAGES = 1000

# How large the rendered text may get. Two MiB is a quarter of the 8 MiB upload
# floor every guild has (so the file is never rejected for size, whatever the
# server's boost tier) and it is also the peak memory one close can hold, which
# is the number that matters when a thousand servers share one process.
MAX_BYTES = 2 * 1024 * 1024

# Written in place of the messages that did not fit, whichever bound stopped it.
TRUNCATION_NOTICE = "[transcript truncated: this ticket had more messages]"

# Timestamp format. Fixed, ASCII and UTC on purpose: a transcript is an archival
# record read next to other transcripts, not a UI surface to localize.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"


def format_message(message) -> str:
    """One message as transcript lines: who, when, what, and what came with it.

    Pure - it reads attributes and returns text. Content is kept VERBATIM,
    newlines included: a transcript that reflowed what somebody typed would be a
    worse record than an occasionally ragged one. Attachments become one
    indented URL line each and embeds an ``[embed]`` marker, so a message that
    carried something but said nothing still shows up as a line rather than
    vanishing.
    """
    created = getattr(message, "created_at", None)
    stamp = created.strftime(TIMESTAMP_FORMAT) if created is not None else "?"
    author = getattr(message, "author", None)
    who = "{name} ({ident})".format(
        name=str(author), ident=getattr(author, "id", "?")
    )

    content = str(getattr(message, "content", "") or "")
    extras = []
    for attachment in getattr(message, "attachments", ()) or ():
        url = getattr(attachment, "url", None)
        name = getattr(attachment, "filename", "file")
        extras.append("    [attachment: {name}] {url}".format(name=name, url=url))
    for _embed in getattr(message, "embeds", ()) or ():
        extras.append("    [embed]")

    if not content and not extras:
        content = "[no text content]"

    lines = ["[{stamp}] {who}: {content}".format(stamp=stamp, who=who, content=content)]
    lines.extend(extras)
    return "\n".join(lines)


def render(header, entries) -> str:
    """Header lines plus as many rendered messages as the byte budget allows.

    Pure. Stops at :data:`MAX_BYTES` and appends :data:`TRUNCATION_NOTICE`, so
    the file always states that it is partial instead of silently ending early.
    The header is never truncated - it is the caller's few lines of context, and
    a transcript with no header would be unreadable.
    """
    parts = list(header)
    used = sum(len(line.encode("utf-8")) + 1 for line in parts)
    truncated = False
    for entry in entries:
        size = len(entry.encode("utf-8")) + 1
        if used + size > MAX_BYTES:
            truncated = True
            break
        parts.append(entry)
        used += size
    if truncated:
        parts.append(TRUNCATION_NOTICE)
    return "\n".join(parts) + "\n"


async def collect(thread, *, limit=MAX_MESSAGES):
    """Fetch up to ``limit`` messages OLDEST FIRST and render each one.

    Returns ``(entries, truncated)``. ``truncated`` says the message cap was
    reached, which the caller does not have to act on - :func:`render` writes the
    notice - but which makes the two bounds testable apart.

    Deliberately fetches ``limit + 1``: asking for one more than we keep is how
    "there were exactly ``limit`` messages" is told apart from "there were more".
    """
    entries = []
    truncated = False
    async for message in thread.history(limit=limit + 1, oldest_first=True):
        if len(entries) >= limit:
            truncated = True
            break
        entries.append(format_message(message))
    return entries, truncated


async def build(thread, *, header, filename, limit=MAX_MESSAGES):
    """The transcript as a ``discord.File``, or ``None`` if it cannot be made.

    NEVER raises. A history fetch can fail for reasons the close flow must not
    inherit - the bot lost ``read_message_history``, the thread was deleted
    between the click and here, Discord is having a moment - and none of them is
    a reason to leave a ticket open. So a failure is logged and answered with
    ``None``, and the caller closes the ticket with a note that there is no
    transcript.
    """
    try:
        entries, truncated = await collect(thread, limit=limit)
    except discord.HTTPException:
        log.warning(
            "tickets: could not read the history of thread %s for a transcript",
            getattr(thread, "id", "?"),
            exc_info=True,
        )
        return None
    except Exception:
        log.exception("tickets: transcript collection failed")
        return None

    lines = list(header)
    if truncated:
        lines.append(
            "[only the first {limit} messages were read]".format(limit=limit)
        )
    text = render(lines, entries)
    buffer = io.BytesIO(text.encode("utf-8"))
    return discord.File(buffer, filename=filename)
