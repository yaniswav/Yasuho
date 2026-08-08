"""Unit tests for ``cogs.config.tickets.transcripts`` (lot T2).

The one module in the feature that touches what people typed, so the tests are
about two things: that it renders a faithful record, and that it cannot become a
place where that record LIVES.

The privacy properties are asserted structurally, not by reading the docstring:
the module is imported and its namespace inspected for a database surface, and
its source is scanned for a filesystem write. A future edit that adds either has
to delete a test to land, which is the point.

Nothing here needs Discord, a database or the network: history is a scripted
async iterator and the messages are plain stand-ins.
"""

import ast
import datetime
import pathlib
import types

import discord
import pytest

from cogs.config.tickets import transcripts

WHEN = datetime.datetime(2026, 8, 8, 14, 30, 5, tzinfo=datetime.timezone.utc)
STAMP = "2026-08-08 14:30:05 UTC"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Author:
    def __init__(self, name="Kira", ident=77):
        self.name = name
        self.id = ident

    def __str__(self):
        return self.name


def _msg(content="hello", *, author=None, when=WHEN, attachments=(), embeds=()):
    return types.SimpleNamespace(
        created_at=when,
        author=author if author is not None else _Author(),
        content=content,
        attachments=list(attachments),
        embeds=list(embeds),
    )


def _attachment(filename="log.txt", url="https://cdn.test/log.txt"):
    return types.SimpleNamespace(filename=filename, url=url)


class _History:
    """A scripted ``thread.history(...)`` - a callable returning an aiterator."""

    def __init__(self, messages, error=None):
        self.messages = list(messages)
        self.error = error
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._iterate()

    async def _iterate(self):
        if self.error is not None:
            raise self.error
        for message in self.messages:
            yield message


class _Thread:
    def __init__(self, messages=(), error=None, thread_id=4242):
        self.id = thread_id
        self.history = _History(messages, error)


# ---------------------------------------------------------------------------
# One message on one line (plus whatever rode with it)
# ---------------------------------------------------------------------------


def test_a_message_renders_as_timestamp_author_and_content():
    line = transcripts.format_message(_msg("I need help with my role"))
    assert line == "[{stamp}] Kira (77): I need help with my role".format(stamp=STAMP)


def test_content_is_kept_verbatim_including_newlines():
    line = transcripts.format_message(_msg("first\nsecond"))
    assert line.endswith("first\nsecond")


def test_attachments_become_indented_url_lines():
    line = transcripts.format_message(
        _msg("see this", attachments=[_attachment("shot.png", "https://cdn.test/s.png")])
    )
    assert "    [attachment: shot.png] https://cdn.test/s.png" in line.splitlines()


def test_embeds_become_a_marker_not_their_contents():
    line = transcripts.format_message(
        _msg("", embeds=[object(), object()])
    )
    assert line.splitlines()[1:] == ["    [embed]", "    [embed]"]


def test_a_message_that_carried_nothing_still_shows_up():
    """An empty line would read as a gap in the conversation, which is a lie."""
    line = transcripts.format_message(_msg(""))
    assert line.endswith("[no text content]")


def test_a_message_with_no_timestamp_degrades_instead_of_raising():
    line = transcripts.format_message(_msg("x", when=None))
    assert line.startswith("[?] ")


# ---------------------------------------------------------------------------
# The two bounds
# ---------------------------------------------------------------------------


async def test_collect_reads_oldest_first_and_asks_for_one_more_than_it_keeps():
    thread = _Thread([_msg("a"), _msg("b")])

    entries, truncated = await transcripts.collect(thread, limit=10)

    assert thread.history.calls == [{"limit": 11, "oldest_first": True}]
    assert len(entries) == 2
    assert truncated is False


async def test_collect_stops_at_the_message_cap_and_says_so():
    thread = _Thread([_msg(str(i)) for i in range(6)])

    entries, truncated = await transcripts.collect(thread, limit=3)

    assert len(entries) == 3
    assert truncated is True


async def test_exactly_the_cap_is_not_truncation():
    thread = _Thread([_msg(str(i)) for i in range(3)])

    entries, truncated = await transcripts.collect(thread, limit=3)

    assert len(entries) == 3
    assert truncated is False


def test_render_stops_at_the_byte_budget_and_labels_the_file_partial():
    entry = "x" * 10_000
    entries = [entry] * 1000  # ~10 MB of text, five times the budget

    text = transcripts.render(["header"], entries)

    assert len(text.encode("utf-8")) <= transcripts.MAX_BYTES
    assert text.rstrip().endswith(transcripts.TRUNCATION_NOTICE)


def test_render_never_truncates_the_header():
    """A transcript with no context lines would be unreadable."""
    header = ["Transcript of ticket #4", "Server: Test (1)"]

    text = transcripts.render(header, ["y" * transcripts.MAX_BYTES])

    assert text.startswith("Transcript of ticket #4\nServer: Test (1)")
    assert transcripts.TRUNCATION_NOTICE in text


def test_a_short_transcript_carries_no_truncation_notice():
    text = transcripts.render(["header"], ["one", "two"])
    assert text == "header\none\ntwo\n"


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


async def test_build_returns_a_named_utf8_file():
    thread = _Thread([_msg("bonjour \N{LATIN SMALL LETTER E WITH ACUTE}")])

    file = await transcripts.build(
        thread, header=["Transcript"], filename="ticket-9-transcript.txt"
    )

    assert isinstance(file, discord.File)
    assert file.filename == "ticket-9-transcript.txt"
    body = file.fp.read().decode("utf-8")
    assert body.startswith("Transcript\n")
    assert "bonjour \N{LATIN SMALL LETTER E WITH ACUTE}" in body


async def test_build_notes_the_message_cap_in_the_header():
    thread = _Thread([_msg(str(i)) for i in range(5)])

    file = await transcripts.build(
        thread, header=["Transcript"], filename="t.txt", limit=2
    )

    body = file.fp.read().decode("utf-8")
    assert "[only the first 2 messages were read]" in body


async def test_build_answers_none_when_the_history_cannot_be_read():
    """A failed transcript must never be a reason to leave a ticket open."""
    thread = _Thread([], error=discord.HTTPException(_Response(403), "no history"))

    assert await transcripts.build(thread, header=[], filename="t.txt") is None


async def test_build_answers_none_on_any_unexpected_failure_too():
    thread = _Thread([], error=RuntimeError("boom"))

    assert await transcripts.build(thread, header=[], filename="t.txt") is None


class _Response:
    def __init__(self, status):
        self.status = status
        self.reason = "Forbidden"


# ---------------------------------------------------------------------------
# PRIVACY: this module cannot keep anything
# ---------------------------------------------------------------------------


def _imported_modules():
    """Top-level names this module imports, read from its AST (not its prose)."""
    source = pathlib.Path(transcripts.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    return names


def test_the_module_has_no_database_surface():
    """It imports exactly four things, and none of them can reach a table.

    Asserted as an exact set rather than a blacklist: adding ``storage``,
    ``asyncpg``, ``tools.settings`` or anything else with a pool behind it fails
    here, and so does an import nobody thought to forbid.
    """
    assert _imported_modules() == {"__future__", "io", "logging", "discord"}
    assert not hasattr(transcripts, "storage")


def test_the_module_never_writes_to_disk():
    source = pathlib.Path(transcripts.__file__).read_text(encoding="utf-8")
    calls = [
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "open" not in calls
    assert "io.BytesIO" in source, "the transcript must stay an in-memory buffer"


def test_the_bounds_are_real_numbers_and_stay_small():
    """Both caps exist so one close cannot cost unbounded REST calls or memory."""
    assert transcripts.MAX_MESSAGES == 1000
    assert transcripts.MAX_BYTES <= 8 * 1024 * 1024


@pytest.mark.parametrize("name", ["collect", "build", "render", "format_message"])
def test_the_public_surface_is_the_documented_four(name):
    assert callable(getattr(transcripts, name))
