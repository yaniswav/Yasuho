"""A member who plays music before Lavalink is up gets an answer, not a crash.

THE INCIDENT (production, 2026-08-20 12:27:35). A member ran ``/play`` 35 seconds
after a restart, while the node was still loading its plugins, and got a RAW
TRACEBACK: sonolink's ``Player._ensure_node`` asks ``Client.get_best_node``,
which raises ``RuntimeError("No nodes are currently connected.")``, straight
through ``ctx.author.voice.channel.connect(cls=Player)``.

WHY THE EXISTING GUARD DID NOT CATCH IT. ``Music._nodes_available`` only asks
whether a node was REGISTERED, and ``create_node()`` registers one at startup
long before its websocket is up - the exact distinction ``_nodes_connected``
exists for. And pre-checking harder would still lose the race between the check
and the connect.

THE SEAM. Every fresh connect in the package goes through
``player.connect_player``, which turns both failures into a
:class:`~cogs.music.player.VoiceConnectFailed` carrying an already-translated
member-facing reason. These tests pin the translation of the failure, the
entry points that inherit it, and that no future entry point can call
``channel.connect(cls=Player)`` behind its back.
"""

import ast
import pathlib
import types

import discord
import pytest

from cogs.music import music, playlists_shared  # noqa: F401
from cogs.music import player as player_module

NO_NODE = RuntimeError("No nodes are currently connected.")


# --- Fakes ------------------------------------------------------------------


class _Channel:
    """A voice channel whose ``connect`` fails the way the incident did."""

    def __init__(self, error=None, player=None):
        self.error = error
        self.player = player
        self.calls = 0

    async def connect(self, cls=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.player


class _Ctx:
    def __init__(self, channel):
        self.author = types.SimpleNamespace(
            id=1, voice=types.SimpleNamespace(channel=channel)
        )
        self.channel = types.SimpleNamespace(id=2)
        self.guild = types.SimpleNamespace(id=3)
        self.voice_client = None
        self.sends = []

    async def send(self, content=None, **kwargs):
        self.sends.append(content)
        return content


# --- The seam ---------------------------------------------------------------


async def test_a_missing_node_becomes_a_friendly_wait_not_a_traceback():
    channel = _Channel(error=NO_NODE)

    with pytest.raises(player_module.VoiceConnectFailed) as caught:
        await player_module.connect_player(channel)

    assert "starting up" in caught.value.message
    assert "try again" in caught.value.message.lower()


async def test_discord_refusing_the_join_keeps_its_own_wording():
    channel = _Channel(error=discord.ClientException("already connected"))

    with pytest.raises(player_module.VoiceConnectFailed) as caught:
        await player_module.connect_player(channel)

    assert "voice channel" in caught.value.message


async def test_a_healthy_connect_returns_the_player_untouched():
    sentinel = object()
    channel = _Channel(player=sentinel)

    assert await player_module.connect_player(channel) is sentinel
    assert channel.calls == 1


async def test_an_unrelated_failure_is_not_swallowed():
    """Only the two known refusals are translated; anything else still raises."""

    channel = _Channel(error=ValueError("something else"))

    with pytest.raises(ValueError):
        await player_module.connect_player(channel)


# --- The entry points -------------------------------------------------------


async def test_play_query_answers_instead_of_raising():
    """The reported command: ``/play <query>`` (and its picker, the vibe card's
    search modal and every ``/music search`` pick, which all funnel here)."""

    cog = music.Music.__new__(music.Music)
    cog._nodes_available = lambda: True

    async def _guard_query(ctx, query):
        return True

    cog._guard_query = _guard_query
    ctx = _Ctx(_Channel(error=NO_NODE))

    await cog._play_query(ctx, "a song")

    assert ctx.sends and "starting up" in ctx.sends[-1]


async def test_the_playlist_connect_answers_and_queues_nothing():
    """``/serverplaylist play`` and the favourites card share this connect."""

    cog = playlists_shared.ServerPlaylistMixin.__new__(
        playlists_shared.ServerPlaylistMixin
    )
    ctx = _Ctx(_Channel(error=NO_NODE))

    assert await cog._connect_for_playlist(ctx) is None
    assert ctx.sends and "starting up" in ctx.sends[-1]


# --- Nothing may connect behind the seam ------------------------------------


def _music_modules():
    return sorted(pathlib.Path("cogs/music").glob("*.py"))


def test_only_the_seam_itself_ever_calls_channel_connect():
    """A new entry point that calls ``channel.connect(cls=Player)`` directly is
    a new way to hand a member a traceback."""

    offenders = []
    for path in _music_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "connect":
                continue
            if any(
                kw.arg == "cls" and getattr(kw.value, "id", None) == "Player"
                for kw in node.keywords
            ):
                offenders.append("{0}:{1}".format(path.name, node.lineno))

    assert offenders == ["player.py:{0}".format(_seam_lineno())]


def _seam_lineno():
    tree = ast.parse(pathlib.Path("cogs/music/player.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "connect_player":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "connect"
                ):
                    return inner.lineno
    raise AssertionError("connect_player no longer connects")


def _connect_callers():
    """``{(module, function): catches_VoiceConnectFailed}`` across the package."""

    found = {}
    for path in _music_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "connect_player"
                for inner in ast.walk(node)
            )
            if not calls:
                continue
            catches = any(
                getattr(handler.type, "id", None) == "VoiceConnectFailed"
                for inner in ast.walk(node)
                if isinstance(inner, ast.Try)
                for handler in inner.handlers
            )
            found[(path.stem, node.name)] = catches
    return found


def test_every_entry_point_that_starts_a_session_handles_the_refusal():
    """The coverage claim, spelled out: three member-facing entry points, plus
    the cold restore (whose caller isolates each guild's failure already)."""

    assert _connect_callers() == {
        ("music", "_play_query"): True,
        ("music", "_start_genre"): True,
        ("music", "_restore_one"): False,
        ("playlists_shared", "_connect_for_playlist"): True,
    }
