"""Pytest foundation for the Yasuho bot test suite.

This module runs at collection time, BEFORE any test imports ``cogs.music.music``
or ``core``. It does two import-time jobs and then exposes the shared fixtures:

1. sonolink stub - the music backend (sonolink -> Lavalink v4) needs Python
   3.12+, so it is absent on the 3.10 dev box. We only inject a fake when the
   real package cannot be imported, so real sonolink is exercised on 3.12+ CI
   while the stub keeps imports working locally. ``cogs/music/music.py`` has NO
   ``from __future__ import annotations`` and evaluates some annotations at
   ``def`` time (e.g. ``sonolink.models.Playable`` in signatures and
   ``TrackSourceType.YOUTUBE`` at module level), so every referenced attribute
   must exist on the stub.

2. config bootstrap - ``core`` and several tools read config at import time via
   the ``config_loader`` singleton. On a fresh CI checkout the real
   ``config/bot.ini`` / ``config/tokens.ini`` may be absent, so we copy them
   from their committed ``*.template.ini`` siblings (which contain every key
   read at import) before anything imports them.

Tests must NEVER touch the network, a database, Discord, or Lavalink. The
fixtures below provide in-memory stand-ins for exactly those boundaries.
"""

from __future__ import annotations

import os
import shutil
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Import-time environment setup (runs at collection, before test imports).
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")


def _install_sonolink_stub() -> None:
    """Inject a fake ``sonolink`` into ``sys.modules`` ONLY if the real one is absent.

    On Python 3.12+ CI the real package imports fine and this is a no-op. On the
    3.10 dev box the import fails and we register a minimal fake that satisfies
    every attribute ``cogs/music/music.py`` touches at import/def time.
    """
    try:
        import sonolink  # noqa: F401
        return
    except ImportError:
        pass

    sonolink = types.ModuleType("sonolink")
    models = types.ModuleType("sonolink.models")
    gateway = types.ModuleType("sonolink.gateway")
    rest = types.ModuleType("sonolink.rest")
    enums = types.ModuleType("sonolink.rest.enums")

    class Player:
        """Subclassable stand-in; music.py does ``class Player(sonolink.Player)``."""

        def __init__(self, *args, **kwargs):
            pass

    class Client:
        def __init__(self, *args, **kwargs):
            pass

    class QueueEmpty(Exception):
        pass

    class QueueMode:
        NORMAL = "normal"
        LOOP = "loop"
        LOOP_ALL = "loop_all"

    class Playable:
        pass

    class Playlist:
        pass

    class TrackStartEvent:
        pass

    class TrackExceptionEvent:
        pass

    class WebSocketClosedEvent:
        pass

    class TrackSourceType:
        YOUTUBE = "youtube"

    sonolink.Player = Player
    sonolink.Client = Client
    sonolink.QueueEmpty = QueueEmpty
    sonolink.QueueMode = QueueMode
    sonolink.models = models
    sonolink.gateway = gateway
    sonolink.rest = rest

    models.Playable = Playable
    models.Playlist = Playlist
    gateway.TrackStartEvent = TrackStartEvent
    gateway.TrackExceptionEvent = TrackExceptionEvent
    gateway.WebSocketClosedEvent = WebSocketClosedEvent
    rest.enums = enums
    enums.TrackSourceType = TrackSourceType

    sys.modules["sonolink"] = sonolink
    sys.modules["sonolink.models"] = models
    sys.modules["sonolink.gateway"] = gateway
    sys.modules["sonolink.rest"] = rest
    sys.modules["sonolink.rest.enums"] = enums


def _ensure_config() -> None:
    """Guarantee config/bot.ini and config/tokens.ini exist for import-time reads.

    If a file is missing, copy it from the committed ``*.template.ini`` sibling so
    the ``config_loader`` singleton finds every key it reads at import (Token,
    prefix, PostgreSQL URI, ...). Real local files are left untouched.
    """
    for name in ("bot.ini", "tokens.ini"):
        target = os.path.join(CONFIG_DIR, name)
        if os.path.exists(target):
            continue
        template = os.path.join(CONFIG_DIR, name.replace(".ini", ".template.ini"))
        if os.path.exists(template):
            shutil.copyfile(template, target)


_install_sonolink_stub()
_ensure_config()


# ---------------------------------------------------------------------------
# asyncpg-pool stand-in
# ---------------------------------------------------------------------------


class Record(dict):
    """Minimal asyncpg.Record stand-in: supports ``r['key']`` like a real Record.

    A plain dict already indexes by key, so tests can build rows as
    ``Record(case_number=1)`` and read ``row['case_number']``.
    """


class FakePool:
    """In-memory asyncpg pool stand-in.

    Every ``execute``/``fetch``/``fetchrow``/``fetchval`` call is appended to
    ``.calls`` as ``(method, query, args)`` for assertions. Return values are
    configurable per instance via the ``*_return`` attributes.
    """

    def __init__(self):
        self.calls = []
        self.execute_return = "INSERT 0 1"
        self.fetch_return = []
        self.fetchrow_return = None
        self.fetchval_return = None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return self.execute_return

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetch_return

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.fetchrow_return

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.fetchval_return


@pytest.fixture
def fake_pool():
    """A fresh :class:`FakePool` per test (see class docstring for configuration)."""
    return FakePool()


# ---------------------------------------------------------------------------
# discord.Interaction stand-in
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, done, parent):
        self._done = done
        self._parent = parent

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._parent.sent.append((args, kwargs))
        self._done = True

    async def edit_message(self, *args, **kwargs):
        self._parent.edits.append((args, kwargs))
        self._done = True

    async def defer(self, *args, **kwargs):
        self._parent.defers.append((args, kwargs))
        self._done = True


class _FakeFollowup:
    def __init__(self, parent):
        self._parent = parent

    async def send(self, *args, **kwargs):
        self._parent.followups.append((args, kwargs))


class _FakeMessage:
    def __init__(self, parent):
        self._parent = parent

    async def edit(self, *args, **kwargs):
        self._parent.message_edits.append((args, kwargs))


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f"<@{user_id}>"


class FakeInteraction:
    """A fake ``discord.Interaction`` recording every async call for assertions.

    Recorded lists: ``.sent`` (response.send_message), ``.edits``
    (response.edit_message), ``.defers`` (response.defer), ``.followups``
    (followup.send), ``.message_edits`` (message.edit).
    """

    def __init__(self, done=False, user_id=1, guild_id=None, locale="en"):
        self.sent = []
        self.edits = []
        self.defers = []
        self.followups = []
        self.message_edits = []
        self.response = _FakeResponse(done, self)
        self.followup = _FakeFollowup(self)
        self.message = _FakeMessage(self)
        self.user = _FakeUser(user_id)
        self.guild_id = guild_id
        self.locale = locale
        self.client = None


@pytest.fixture
def make_interaction():
    """Factory: ``make_interaction(done=False, user_id=1, guild_id=None, locale='en')``."""

    def _factory(done=False, user_id=1, guild_id=None, locale="en"):
        return FakeInteraction(
            done=done, user_id=user_id, guild_id=guild_id, locale=locale
        )

    return _factory


# ---------------------------------------------------------------------------
# commands.Context stand-in
# ---------------------------------------------------------------------------


class _FakeAuthor:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f"<@{user_id}>"


class FakeContext:
    """A fake ``commands.Context`` with a recorded async ``send``.

    ``.sends`` collects every ``send`` call as ``(args, kwargs)``.
    """

    def __init__(self, author_id=1, guild=None):
        self.sends = []
        self.author = _FakeAuthor(author_id)
        self.guild = guild
        self.interaction = None

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return _FakeMessage(self)


@pytest.fixture
def make_context():
    """Factory: ``make_context(author_id=1, guild=None)`` -> :class:`FakeContext`."""

    def _factory(author_id=1, guild=None):
        return FakeContext(author_id=author_id, guild=guild)

    return _factory


# ---------------------------------------------------------------------------
# crypto key
# ---------------------------------------------------------------------------


@pytest.fixture
def crypto_key():
    """Point ``tools.crypto`` at a fresh valid Fernet key for the test.

    Resets the module-level cache globals (``_fernet``/``_loaded``), installs a
    freshly generated key so ``encrypt``/``decrypt``/``is_configured`` all work,
    yields the raw key bytes, then restores the globals to ``None``/``False`` on
    teardown so no key state leaks between tests. Tests that need the
    "no key configured" path can reset ``_fernet=None``/``_loaded=False`` and
    leave the config key empty themselves.
    """
    from cryptography.fernet import Fernet

    from tools import crypto

    key = Fernet.generate_key()
    crypto._fernet = Fernet(key)
    crypto._loaded = True
    try:
        yield key
    finally:
        crypto._fernet = None
        crypto._loaded = False


# ---------------------------------------------------------------------------
# i18n ContextVar hygiene
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_locale():
    """Reset the i18n locale ContextVar to the default around every test.

    The active locale lives in a ContextVar; without this a test that sets a
    non-default locale would leak into the next one. Autouse so every test starts
    and ends on ``DEFAULT_LOCALE``.
    """
    from tools import i18n

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    try:
        yield
    finally:
        i18n.current_locale.set(i18n.DEFAULT_LOCALE)
