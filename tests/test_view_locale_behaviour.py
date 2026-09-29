"""A French member's click must come back in French - on the real classes.

The companion to ``tests/test_view_locale_hygiene.py``. That one is structural:
it asks every dispatch root whether its check installs the clicker's locale.
This one is behavioural: it takes the two classes the defect was proven on -
``tools.paginator.Paginator`` and ``cogs.music.views.MusicController`` - builds
them for real, dispatches a real click through discord.py's own
``View._dispatch_item``, and reads back WHICH CATALOGUE served every ``_()`` the
click produced.

WHY A CATALOGUE SPY AND NOT A STRING COMPARISON. "Did French come out?" must not
depend on whether that particular msgid happens to be translated: the
paginator's footer, "Page {current}/{total}", is identical in French, so a test
comparing rendered text would have called the bug fixed while it was live. The
two stand-in catalogues installed here translate EVERY string and record the
locale in force at the moment of the call, so a miss can never be a coverage
gap. They are installed with ``monkeypatch.setitem`` on ``i18n.translations``,
which ``_`` (``use_current_gettext``) reads at call time - so it works even for
the many modules that did ``from tools.i18n import _`` at import.

EVERY ASSERTION HAS A CONTROL. Each test has a twin that puts the PRE-FIX
``interaction_check`` back on the same real class and runs the same click: the
twin must see ENGLISH. Without it, "the spy recorded fr" would also be the
result of a test that accidentally ran in a French context to begin with, and
"no French" would be indistinguishable from "no text at all" - which is exactly
how two of the scouting probes first misread ``_FavouriteActions``. A third
control pins that a click which produces no text at all is reported as neither.

No network, no database, no Discord, no Lavalink: the player, the cog, the
voice channel and the db_pool are in-memory stand-ins, and the dispatch is
discord.py's own.
"""

import asyncio
import contextlib
import types

import discord
import pytest

# music first: views.py imports from it at module level, so importing views on
# its own hits the package's documented circular-import order.
from cogs.music import music, vibes, views, voteskip  # noqa: F401
from tools import i18n, settings
from tools.paginator import Paginator, paginate_lines
from tools.views import LocaleLayoutView, LocaleView, PinnedRenderLocale

CLICKER_ID = 707_070_707_070_707_070
GUILD_ID = 606_060_606_060_606_060


# ---------------------------------------------------------------------------
# The spy catalogues
# ---------------------------------------------------------------------------


class _RecordingCatalog:
    """A gettext catalogue that translates everything and logs who asked."""

    def __init__(self, tag, log, prefix=""):
        self.tag = tag
        self.log = log
        self.prefix = prefix

    def gettext(self, message):
        self.log.append((self.tag, message))
        return self.prefix + message

    def ngettext(self, singular, plural, n):
        self.log.append((self.tag, singular))
        return self.prefix + (singular if n == 1 else plural)


@pytest.fixture
def spy(monkeypatch):
    """Install the spy catalogues; yield the shared (locale, msgid) log.

    English keeps its identity prefix so a view built before the click still
    renders findable English labels; French is marked so a stray French render
    is obvious in the log dump of a failure.
    """
    log = []
    monkeypatch.setitem(i18n.translations, "en", _RecordingCatalog("en", log))
    monkeypatch.setitem(i18n.translations, "fr", _RecordingCatalog("fr", log, "[FR]"))
    return log


def tags(log):
    """The set of locales that served this click. Empty means: no text at all."""
    return {tag for tag, _msgid in log}


# ---------------------------------------------------------------------------
# The click
# ---------------------------------------------------------------------------


class _Pool:
    async def fetchval(self, *args, **kwargs):
        return None


class _Response:
    def __init__(self):
        self.calls = []

    def is_done(self):
        return False

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send_message", content, kwargs))

    async def edit_message(self, content=None, **kwargs):
        self.calls.append(("edit_message", content, kwargs))

    async def defer(self, **kwargs):
        self.calls.append(("defer", None, kwargs))

    async def send_modal(self, modal):
        self.calls.append(("send_modal", None, {"modal": modal}))


class _Interaction:
    """Enough of ``discord.Interaction`` for a check and a callback to run."""

    def __init__(self, user, locale="fr"):
        self.client = types.SimpleNamespace(db_pool=_Pool())
        self.user = user
        self.guild_id = GUILD_ID
        self.locale = locale
        self.data = {}
        self.response = _Response()

    async def original_response(self):
        return types.SimpleNamespace(id=1, edit=self._noop)

    async def _noop(self, **kwargs):
        return None


class _Stranger:
    """A clicker who is not a ``discord.Member`` - i.e. not in the voice room."""

    def __init__(self, user_id=CLICKER_ID):
        self.id = user_id
        self.display_name = "stranger"


class _Listener(discord.Member):
    """A clicker who IS in the player's voice channel.

    A real ``discord.Member`` subclass because ``_ensure_in_voice`` decides with
    ``isinstance(user, discord.Member)``: a duck would be refused for the wrong
    reason and the "allowed" test would silently become a second refusal test.
    ``id`` and ``voice`` are re-declared because the base defines them as
    properties over gateway state this stand-in does not have.
    """

    def __init__(self, voice_channel, user_id=CLICKER_ID):
        self._probe_id = user_id
        self._probe_channel = voice_channel

    @property
    def id(self):
        return self._probe_id

    @property
    def voice(self):
        return types.SimpleNamespace(channel=self._probe_channel)


async def click(view, item, interaction):
    """Dispatch one click the way discord.py does, from an English context.

    ``View._dispatch_item`` spawns the callback with ``asyncio.create_task``,
    which copies the context it is called in - so pinning the locale to the
    default here reproduces the gateway task exactly. The whole point of the bug
    is that this copy says "en".
    """
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    task = view._dispatch_item(item, interaction)
    assert task is not None, "the view refused to dispatch - it is already stopped"
    await task


# ---------------------------------------------------------------------------
# Paginator
# ---------------------------------------------------------------------------


def _paginator():
    view = Paginator(
        paginate_lines([f"line {index}" for index in range(30)], title="Leaderboard"),
        author_id=CLICKER_ID,
    )
    return view


async def _page_click(spy_log):
    """Build a paginator, click Next, and return the log of that click alone."""
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    view = _paginator()          # construction renders the first footer...
    spy_log.clear()              # ...which is not part of the click
    await click(view, view.next_page, _Interaction(_Stranger(), locale="fr"))
    return view


async def test_a_french_member_pages_in_french(spy):
    """The real Paginator: clicking Next renders through the French catalogue."""
    await _page_click(spy)

    assert ("fr", "Page {current}/{total}") in spy, spy
    assert tags(spy) == {"fr"}, spy


async def test_control_the_same_paginator_click_is_english_without_the_fix(
    spy, monkeypatch
):
    """Put the pre-fix check back on the real class: the same click turns English.

    This is the control the French assertion above is worth nothing without.
    """
    from tools.i18n import _

    async def pre_fix_interaction_check(self, interaction):
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This menu isn't for you."), ephemeral=True
            )
            return False
        return True

    monkeypatch.setattr(Paginator, "interaction_check", pre_fix_interaction_check)

    await _page_click(spy)

    assert ("en", "Page {current}/{total}") in spy, spy
    assert tags(spy) == {"en"}, spy


async def test_control_a_click_that_renders_nothing_is_neither_language(spy):
    """"No French" and "no text at all" must not collapse into one answer.

    Without this pin, a test whose click silently did nothing would read as a
    passing English control and a failing French assertion for the same reason.
    """

    class Silent(LocaleView):
        def __init__(self):
            super().__init__(timeout=None)
            self.hit = False
            self.add_item(discord.ui.Button(label="x", custom_id="silent"))
            self.children[0].callback = self._callback

        async def _callback(self, interaction):
            self.hit = True

    view = Silent()
    spy.clear()
    await click(view, view.children[0], _Interaction(_Stranger(), locale="fr"))

    assert view.hit is True
    assert tags(spy) == set(), spy


# ---------------------------------------------------------------------------
# MusicController
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title="Song"):
        self.title = title
        self.author = "Artist"
        self.identifier = "id"
        self.uri = "https://example.test/song"
        self.length = 200000
        self.is_stream = False
        self.extras = types.SimpleNamespace(requester=None, radio=False)


class _Queue:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.mode = None


class _VoicePlayer:
    def __init__(self, voice_channel, tracks=()):
        self.current = _Track()
        self.queue = _Queue(tracks)
        self.paused = False
        self.volume = 50
        self.position = 0
        self.autoplay = None
        self.radio_genre = None
        self.channel = voice_channel
        self.home = None
        self.dj = None


def _controller():
    """A real MusicController on a real voice channel, built in English."""
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    voice_channel = types.SimpleNamespace(name="General", id=1234)
    player = _VoicePlayer(voice_channel, tracks=[_Track("Upcoming")])
    view = views.MusicController(types.SimpleNamespace(), player)
    return view, voice_channel


def _button(view, label):
    """The controller button whose English label is ``label``."""
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"no button labelled {label!r} on {type(view).__name__}")


async def test_a_french_member_outside_the_room_is_refused_in_french(spy):
    """The proven case: the room gate's refusal, which used to be English.

    ``_ensure_in_voice`` runs INSIDE ``interaction_check``, so this is the
    strictest possible ordering test - the locale has to be in place before the
    gate that the same check performs, not merely before the callback.
    """
    view, _channel = _controller()
    spy.clear()
    interaction = _Interaction(_Stranger(), locale="fr")

    await click(view, _button(view, "Queue"), interaction)

    assert ("fr", "You must be in my voice channel to use these controls.") in spy, spy
    assert tags(spy) == {"fr"}, spy
    # The gate itself is UNCHANGED: still one ephemeral refusal, still no panel.
    assert [call[0] for call in interaction.response.calls] == ["send_message"]
    assert interaction.response.calls[0][2]["ephemeral"] is True


async def test_control_the_same_refusal_is_english_without_the_super_hop(
    spy, monkeypatch
):
    """Put the pre-fix check back on the real MusicController: English returns."""

    async def pre_fix_interaction_check(self, interaction):
        return await views._ensure_in_voice(self.player, interaction)

    monkeypatch.setattr(
        views.MusicController, "interaction_check", pre_fix_interaction_check
    )

    view, _channel = _controller()
    spy.clear()

    await click(view, _button(view, "Queue"), _Interaction(_Stranger(), locale="fr"))

    assert ("en", "You must be in my voice channel to use these controls.") in spy, spy
    assert tags(spy) == {"en"}, spy


async def test_a_french_listener_gets_a_french_queue_from_the_controller(spy):
    """Past the gate: the CALLBACK's own text is French too.

    The Queue button's handler builds a whole ``QueueView`` and sends it, so
    this covers the case the refusal cannot - a click that is allowed through
    and renders a panel.
    """
    view, channel = _controller()
    spy.clear()
    interaction = _Interaction(_Listener(channel), locale="fr")

    await click(view, _button(view, "Queue"), interaction)

    assert ("fr", "### 🎶 Queue") in spy, spy
    assert tags(spy) == {"fr"}, spy
    # The gate itself is UNCHANGED: a member in the room is still let through,
    # and still gets the panel rather than a refusal.
    assert [call[0] for call in interaction.response.calls] == ["send_message"]
    assert isinstance(interaction.response.calls[0][2]["view"], views.QueueView)


async def test_control_the_same_queue_panel_is_english_without_the_super_hop(
    spy, monkeypatch
):
    """The allowed path's control: pre-fix check, same click, English panel."""

    async def pre_fix_interaction_check(self, interaction):
        return await views._ensure_in_voice(self.player, interaction)

    monkeypatch.setattr(
        views.MusicController, "interaction_check", pre_fix_interaction_check
    )

    view, channel = _controller()
    spy.clear()

    await click(
        view, _button(view, "Queue"), _Interaction(_Listener(channel), locale="fr")
    )

    assert ("en", "### 🎶 Queue") in spy, spy
    assert tags(spy) == {"en"}, spy


# ---------------------------------------------------------------------------
# The bases add a locale and NOTHING else
# ---------------------------------------------------------------------------
#
# The whole re-basing rests on one claim: a Locale* base never refuses anyone,
# so swapping discord.ui.View for LocaleView under a view that already had a
# same-voice or DJ gate cannot change who may click. Pin the claim directly.


async def test_no_locale_base_ever_refuses_a_clicker():
    """All four bases return True, for a user they have never heard of."""
    from tools.views import (
        LocaleDynamicItem,
        LocaleLayoutView,
        LocaleModal,
    )

    interaction = _Interaction(_Stranger(user_id=1), locale="fr")
    for base in (LocaleView, LocaleLayoutView, LocaleModal, LocaleDynamicItem):
        instance = object.__new__(base)
        # LocaleDynamicItem forwards the decision to the item it wraps, which
        # for every real one is a plain Button (permissive).
        object.__setattr__(instance, "item", discord.ui.Button(label="x"))
        assert await base.interaction_check(instance, interaction) is True, base.__name__


async def test_the_paginator_author_gate_is_unchanged(spy):
    """A non-author is still refused, with the same wording, in their language.

    Paginator's gate now runs through ``AuthorView`` instead of a local copy.
    Same decision, same registered deny literal - only the language changed.
    """
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    view = _paginator()          # author_id=CLICKER_ID
    spy.clear()
    interaction = _Interaction(_Stranger(user_id=CLICKER_ID + 1), locale="fr")

    await click(view, view.next_page, interaction)

    assert ("fr", "This menu isn't for you.") in spy, spy
    assert [call[0] for call in interaction.response.calls] == ["send_message"]
    assert interaction.response.calls[0][2]["ephemeral"] is True
    # Refused means refused: the page never turned.
    assert view.index == 0


async def test_a_public_paginator_still_lets_anyone_page(spy):
    """author_id=None keeps its looser gate - and gains the locale.

    The branch AuthorView cannot serve (its gate would compare every id against
    None and lock the whole room out), so it is the one place the apply is
    spelled out rather than inherited.
    """
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    view = Paginator(
        paginate_lines([f"line {index}" for index in range(30)]), author_id=None
    )
    spy.clear()
    interaction = _Interaction(_Stranger(user_id=CLICKER_ID + 2), locale="fr")

    await click(view, view.next_page, interaction)

    assert view.index == 1, "a public paginator must let a non-author page"
    assert ("fr", "Page {current}/{total}") in spy, spy
    assert tags(spy) == {"fr"}, spy


# ---------------------------------------------------------------------------
# The click really is dispatched from an English context
# ---------------------------------------------------------------------------


async def test_the_dispatch_context_starts_in_english():
    """Anti-vacuity: if the task already carried "fr" these tests prove nothing.

    Reproduces what ``View._dispatch_item`` does - ``asyncio.create_task`` from
    the caller's context - and pins that the task sees the English default, so
    every French verdict above is a change the check made, not an inheritance.
    """
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)

    async def body():
        return i18n.current_locale.get()

    assert await asyncio.create_task(body()) == i18n.DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# THE RENDER RULE: one public message, one language
# ---------------------------------------------------------------------------
#
# Installing the CLICKER's locale in a check is right for what only the clicker
# reads and wrong for the BODY of a public message, because that body is also
# re-rendered by people with other locales and by background tasks with none.
# Before tools.views.PinnedRenderLocale, one now-playing panel went
# "### Now Playing" (posted by a background task, no locale -> English) ->
# "### Lecture en cours" (a French member pressed Pause) -> "### Now Playing"
# (the 60s progress tick), on the same message, in front of everyone.
#
# Every test below reads WHICH CATALOGUE served the panel heading, so a verdict
# never depends on a msgid happening to be translated, and every one has a
# control that removes the pin from the real class and watches the flip-flop
# come back.


def _headings(log, msgid="### 🎵 Now Playing"):
    """The locales that served ``msgid``, and how many renders were seen.

    Returns ``(set_of_locales, count)``. The count is what keeps "only French"
    from also being the answer for "the panel never rendered at all".
    """
    hits = [tag for tag, message in log if message == msgid]
    return set(hits), len(hits)


class _Message:
    """A bound message that records its in-place edits."""

    def __init__(self):
        self.id = 4242
        self.edits = 0

    async def edit(self, **kwargs):
        self.edits += 1

    async def delete(self):
        return None


class _ControllablePlayer(_VoicePlayer):
    """A player the Pause button can actually drive."""

    def __init__(self, voice_channel, guild=None):
        super().__init__(voice_channel)
        self.guild = guild
        self.controller = None

    async def pause(self):
        self.paused = True

    async def resume(self):
        self.paused = False


class _Cog:
    """The two cog methods the Pause path calls, and nothing else."""

    def __init__(self):
        self.snapshots = 0

    async def _can_control(self, player, user):
        return True

    async def _snapshot(self, player):
        self.snapshots += 1


def _pause_button(view):
    """The Pause/Resume button, found by HANDLER, not by label.

    The spy catalogue rewrites every label, so matching on rendered text would
    make these tests depend on the very thing they measure.
    """
    for child in view.walk_children():
        if getattr(child, "_handler", None) == view._pause_resume:
            return child
    raise AssertionError("the controller has no Pause/Resume button")


def _pinned_controller(locale_code):
    """A real MusicController posted (and therefore pinned) in ``locale_code``."""
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    voice_channel = types.SimpleNamespace(name="General", id=1234)
    player = _ControllablePlayer(voice_channel)
    with i18n.locale(locale_code):
        view = views.MusicController(_Cog(), player)
    view.message = _Message()
    return view, voice_channel


def _unpin(monkeypatch):
    """Put the pre-fix render back on the real class: every build in the caller's locale.

    ``_compose`` IS the layout builder the class had before the mixin; binding it
    as ``_build`` reproduces the shipped behaviour byte for byte, which is what
    makes the controls below a mutation of the real code and not of a stand-in.
    """
    monkeypatch.setattr(
        views.MusicController, "_build", views.MusicController._compose
    )


async def test_a_public_panel_keeps_its_language_when_a_french_member_pauses(spy):
    """The MAJOR: the panel body must not follow the clicker, the reply must."""
    view, channel = _pinned_controller("en")
    spy.clear()
    interaction = _Interaction(_Listener(channel), locale="fr")

    await click(view, _pause_button(view), interaction)

    # The body was really re-rendered (one real edit), and stayed English.
    assert view.message.edits == 1
    assert _headings(spy) == ({"en"}, 1), spy
    # ...while the ephemeral confirmation, which only the clicker reads, is French.
    assert ("fr", "Paused.") in spy, spy


async def test_control_the_same_pause_flips_the_panel_to_french_without_the_pin(
    spy, monkeypatch
):
    """Remove the pin from the real class: the shipped defect comes straight back."""
    _unpin(monkeypatch)

    view, channel = _pinned_controller("en")
    spy.clear()

    await click(view, _pause_button(view), _Interaction(_Listener(channel), "fr"))

    assert view.message.edits == 1
    assert _headings(spy) == ({"fr"}, 1), spy


async def test_one_public_panel_speaks_one_language_to_all_three_writers(spy):
    """A French server's panel: background tick, French click, English click.

    The three writers of this one message carry three different locales - none,
    "fr" and "en" - which is exactly the flip-flop. All three renders must come
    out of the French catalogue.
    """
    view, channel = _pinned_controller("fr")
    spy.clear()

    # 1. The 60s idle tick, from a task with no locale of its own.
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    view.player.position = 100_000
    assert await view.refresh_progress() is True

    # 2. A French listener pauses. 3. An English listener resumes.
    await click(view, _pause_button(view), _Interaction(_Listener(channel), "fr"))
    await click(view, _pause_button(view), _Interaction(_Listener(channel), "en"))

    assert _headings(spy) == ({"fr"}, 3), spy


async def test_control_the_same_three_writers_produce_two_languages_unpinned(
    spy, monkeypatch
):
    """Without the pin the same sequence renders the same message in two languages."""
    _unpin(monkeypatch)

    view, channel = _pinned_controller("fr")
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    view.player.position = 100_000
    assert await view.refresh_progress() is True
    await click(view, _pause_button(view), _Interaction(_Listener(channel), "fr"))
    await click(view, _pause_button(view), _Interaction(_Listener(channel), "en"))

    locales, count = _headings(spy)
    assert count == 3, spy
    assert locales == {"en", "fr"}, spy


async def test_the_live_lyrics_card_does_not_change_language_between_ticks(spy):
    """The worst case: a poller rebuilds this public card every few seconds."""
    from cogs.music import lyrics

    card = _lyrics_card("fr")
    spy.clear()

    # The poller's edit path: set_state -> _build, from a task with no locale.
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    card.set_state(body="a line")
    card.set_state(body="another line")

    assert isinstance(card, lyrics.SyncedLyricsCard)
    assert _headings(spy, "### 🎤 Live Lyrics") == ({"fr"}, 2), spy


async def test_control_the_lyrics_card_follows_the_poller_without_the_pin(
    spy, monkeypatch
):
    """Unpinned, the same two ticks render the French card in English."""
    from cogs.music import lyrics

    monkeypatch.setattr(
        lyrics.SyncedLyricsCard, "_build", lyrics.SyncedLyricsCard._compose
    )

    card = _lyrics_card("fr")
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    card.set_state(body="a line")
    card.set_state(body="another line")

    assert _headings(spy, "### 🎤 Live Lyrics") == ({"en"}, 2), spy


def _lyrics_card(locale_code):
    """A real SyncedLyricsCard over a stand-in session, built in ``locale_code``."""
    from cogs.music import lyrics

    async def _noop(*args, **kwargs):
        return None

    session = types.SimpleNamespace(
        track=None,
        source="Test Provider",
        offset_ms=0,
        stop_from_interaction=_noop,
        shift_offset_from_interaction=_noop,
    )
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    with i18n.locale(locale_code):
        return lyrics.SyncedLyricsCard(session)


# ---------------------------------------------------------------------------
# ...and the background poster resolves the GUILD's language, not English
# ---------------------------------------------------------------------------


class _SendPool:
    """No stored preference for anything; records the controller-id persist."""

    def __init__(self):
        self.calls = []

    async def fetchval(self, *args, **kwargs):
        return None

    async def fetch(self, *args, **kwargs):
        return []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Home:
    """The player's home channel: records what was posted."""

    def __init__(self, guild):
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _Message()


class _Bot:
    """The two bot attributes the controller path uses.

    ``get_guild`` is how ``_send_controller`` reaches the guild object: the
    player's own ``guild`` property is unusable for that (see
    :class:`_DetachedPlayer`), so the poster looks the guild up from the id it
    already derived from the voice / home channels.
    """

    def __init__(self, guild):
        self.db_pool = _SendPool()
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == getattr(self._guild, "id", None) else None


class _DetachedPlayer(_VoicePlayer):
    """A player sonolink has not attached to a guild yet.

    sonolink makes ``Player.guild`` a PROPERTY that raises ``RuntimeError`` -
    NOT ``AttributeError`` - while its ``_guild`` is None (verified in the
    installed ``sonolink/gateway/player/_base.py``). ``getattr(player, "guild",
    None)`` only swallows ``AttributeError``, so it does not protect a caller
    from this at all: the RuntimeError comes straight out of the getattr.

    Nothing else about this player is unusual - it has a voice channel and a
    home channel, so its guild IS reachable, just not through that property.
    """

    @property
    def guild(self):
        raise RuntimeError("Player is not yet attached to a guild.")


async def _post_controller(preferred_locale, guild_id, *, detached=False):
    """Run the real ``Music._send_controller``; return (sent, player, cog).

    ``detached`` swaps in a player whose ``guild`` property raises, i.e. the
    state sonolink leaves a player in before it is attached to a guild.
    """
    guild = types.SimpleNamespace(id=guild_id, preferred_locale=preferred_locale)
    voice_channel = types.SimpleNamespace(name="General", id=99, guild=guild)
    if detached:
        player = _DetachedPlayer(voice_channel)
        player.controller = None
    else:
        player = _ControllablePlayer(voice_channel, guild=guild)
    player.home = _Home(guild)

    cog = music.Music.__new__(music.Music)
    cog.bot = _Bot(guild)
    cog._controllers = {}
    cog._controller_locks = {}

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await cog._send_controller(player)
    return player.home.sent, player, cog


async def test_the_background_poster_builds_the_panel_in_the_guild_locale(spy):
    """A French server's track_start must not post an English panel.

    ``_send_controller`` is the ONLY place a public controller is created, and it
    runs from track_start / the cold restore / a repost - none of which carries a
    locale. It therefore resolves the guild's own and builds inside it; the pin
    then holds that language for every later edit.
    """
    sent, _player, _cog = await _post_controller(
        "fr", guild_id=515_151_515_151_515_151
    )

    assert len(sent) == 1
    assert sent[0]["view"]._render_locale == "fr"
    assert _headings(spy) == ({"fr"}, 1), spy


async def test_control_an_english_server_still_gets_an_english_panel(spy):
    """The other half of the control: the resolve is real, not a hard-coded "fr"."""
    sent, _player, _cog = await _post_controller(
        "en-US", guild_id=525_252_525_252_525_252
    )

    assert len(sent) == 1
    assert sent[0]["view"]._render_locale == "en"
    assert _headings(spy) == ({"en"}, 1), spy


# ---------------------------------------------------------------------------
# ...and it reaches that guild WITHOUT touching player.guild
# ---------------------------------------------------------------------------
#
# sonolink's Player.guild raises RuntimeError before the player is attached to a
# guild, and getattr(player, "guild", None) does not catch a RuntimeError. The
# locale resolve above therefore has to reuse the guild_id _send_controller
# already derives from the voice / home channels - which the function does
# deliberately, for this exact reason - and look the guild up from it.


def test_the_detached_player_stand_in_really_raises_the_way_sonolink_does():
    """The witness for the test below: the probe is calibrated, not merely quiet.

    "The panel was posted" is only evidence if the player would genuinely have
    broken the old line. So assert the raise, and assert that ``getattr`` does
    NOT rescue it - if ``guild`` ever became a plain attribute or started raising
    AttributeError, this fails and says the test below has stopped proving
    anything.
    """
    player = _DetachedPlayer(types.SimpleNamespace(name="General", id=1))

    with pytest.raises(RuntimeError):
        player.guild
    with pytest.raises(RuntimeError):
        getattr(player, "guild", None)


async def test_a_player_with_no_guild_attached_still_gets_its_panel_posted(spy):
    """A track_start on a not-yet-attached player must still post a panel.

    This is the whole failure: the poster used to read ``player.guild`` through a
    ``getattr`` default that cannot catch a RuntimeError, so on this player the
    resolve raised INSIDE the per-guild lock and the room got no now-playing
    controller at all. The guild is reachable the whole time - it is on the voice
    channel - which is why the panel below also comes out in French rather than
    merely coming out.
    """
    sent, _player, _cog = await _post_controller(
        "fr", guild_id=535_353_535_353_535_353, detached=True
    )

    assert len(sent) == 1
    assert sent[0]["view"]._render_locale == "fr"
    assert _headings(spy) == ({"fr"}, 1), spy


# ---------------------------------------------------------------------------
# A guild that changes its language mid-session
# ---------------------------------------------------------------------------
#
# The controller is an EVENT surface, so its language IS the guild's. The pin
# must therefore follow a /language change - but only at a message boundary, or
# the flip-flop the pin exists to kill walks back in through the refresh door.
# A track change is that boundary: the whole body is redrawn there anyway.


class _PanelCog(_Cog):
    """The Pause path's two cog methods, plus the bot the re-pin resolves through."""

    def __init__(self, guild):
        super().__init__()
        self.bot = _Bot(guild)


def _guild_panel(locale_code, guild_id, preferred_locale="en-US", *, detached=False):
    """A real MusicController pinned to ``locale_code`` over a cog that has a bot.

    The bot is what :meth:`MusicController._repin_to_guild_locale` resolves the
    guild's language through, so this - unlike ``_pinned_controller`` above - is a
    panel that can actually move its pin.

    ``detached`` swaps in the player whose ``guild`` property raises, which is the
    state a freshly reconnected player is in when the track_start rebind hands it
    to ``_rerender_for_track``.
    """
    guild = types.SimpleNamespace(id=guild_id, preferred_locale=preferred_locale)
    voice_channel = types.SimpleNamespace(name="General", id=1234, guild=guild)
    if detached:
        player = _DetachedPlayer(voice_channel)
        player.controller = None
    else:
        player = _ControllablePlayer(voice_channel, guild=guild)
    cog = _PanelCog(guild)
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    with i18n.locale(locale_code):
        view = views.MusicController(cog, player)
    view.message = _Message()
    return view, player, cog


async def _set_guild_language(cog, guild_id, locale_code):
    """What ``/language`` does: write the guild's ``locale`` preference."""
    await settings.set_guild(cog.bot.db_pool, guild_id, "locale", locale_code)


async def test_a_language_change_reaches_the_panel_on_the_next_track(spy):
    """An admin runs /language: the next track redraws the panel in the new one."""
    guild_id = 545_454_545_454_545_454
    view, _player, cog = _guild_panel("en", guild_id)
    await _set_guild_language(cog, guild_id, "fr")
    spy.clear()

    # track_start: a gateway task with no locale of its own.
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    assert await view._rerender_for_track(_Track("Next Song")) is True

    assert view._render_locale == "fr"
    assert view.message.edits == 1
    assert _headings(spy) == ({"fr"}, 1), spy


async def test_control_without_the_repin_the_panel_keeps_the_stale_language(
    spy, monkeypatch
):
    """Null the re-pin on the real class: the panel keeps saying the old language.

    This is the shipped behaviour - the pin was taken once at the first render and
    never re-taken - so the same /language change never reaches this message.
    """

    async def _no_repin(self):
        return None

    monkeypatch.setattr(views.MusicController, "_repin_to_guild_locale", _no_repin)

    guild_id = 555_555_555_555_555_555
    view, _player, cog = _guild_panel("en", guild_id)
    await _set_guild_language(cog, guild_id, "fr")
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    assert await view._rerender_for_track(_Track("Next Song")) is True

    assert view._render_locale == "en"
    assert _headings(spy) == ({"en"}, 1), spy


async def test_no_click_or_tick_between_two_tracks_moves_the_pin(spy):
    """The anti-regression: the re-pin must be a track boundary, nothing else.

    Same guild-language change as above, but this time only a French member's
    click and the 60s progress tick touch the panel. Both must render the language
    the message already speaks: a re-pin on either of those paths is the
    two-languages-on-one-message defect coming back wearing a different hat.
    """
    guild_id = 565_656_565_656_565_656
    view, player, cog = _guild_panel("en", guild_id)
    await _set_guild_language(cog, guild_id, "fr")
    spy.clear()

    await click(view, _pause_button(view), _Interaction(_Listener(player.channel), "fr"))
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    player.position = 100_000
    assert await view.refresh_progress() is True

    assert view._render_locale == "en"
    assert _headings(spy) == ({"en"}, 2), spy


async def test_an_unresolvable_guild_leaves_the_pin_where_it_was(spy):
    """A panel whose guild is not in cache keeps its language, not English.

    ``resolve_guild_locale`` answers "en" for a None guild, so re-pinning from it
    unconditionally would turn a French panel English on the next track in exactly
    the situation where we know the least. The re-pin stands down instead.
    """
    guild_id = 575_757_575_757_575_757
    view, _player, cog = _guild_panel("fr", guild_id)
    cog.bot._guild = None  # the guild fell out of the bot's cache
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    assert await view._rerender_for_track(_Track("Next Song")) is True

    assert view._render_locale == "fr"
    assert _headings(spy) == ({"fr"}, 1), spy


async def test_the_repin_still_works_on_a_player_with_no_guild_attached(spy):
    """The witness for the re-pin's own guild lookup: it must not read player.guild.

    The re-pin runs on the track_start path, where ``_send_controller`` has just
    rebound the panel onto the player the event carried - and a player that came
    back from a reconnect is exactly the one whose ``guild`` property raises
    ``RuntimeError`` (see
    :func:`test_the_detached_player_stand_in_really_raises_the_way_sonolink_does`,
    which proves this stand-in raises the way sonolink does). There is no ``try``
    anywhere between here and the track_start handler, so a guild lookup that read
    that property would take the whole re-render down inside the per-guild
    controller lock and the panel would silently stop following track changes -
    the failure ``Music._send_controller`` was already fixed for, re-entering
    through the re-pin door.

    So this asserts BEHAVIOUR on that player, not the shape of the lookup: the
    guild's French still arrives. It fails both ways the lookup can go wrong - a
    raise (nothing is rendered at all) and a swallowed failure that gives up on
    the guild (the pin stays English).
    """
    guild_id = 585_858_585_858_585_858
    view, _player, cog = _guild_panel("en", guild_id, detached=True)
    await _set_guild_language(cog, guild_id, "fr")
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    assert await view._rerender_for_track(_Track("Next Song")) is True

    assert view._render_locale == "fr"
    assert view.message.edits == 1
    assert _headings(spy) == ({"fr"}, 1), spy


# ---------------------------------------------------------------------------
# The skip vote: one public message, four writers, one language
# ---------------------------------------------------------------------------
#
# The vote message is written by the member who opened it (a command or a
# controller click, so their language), by every later voter's click (theirs, on
# the button's count label), by the 30 s view timeout and by the track_start
# hook - and those last two carry no language at all. Unpinned, one message
# opened in French, relabelled itself in English and closed in English.


class _VoteCog:
    """The one cog method a passing vote calls."""

    def __init__(self, result=voteskip.SKIP_RESULT_ADVANCED):
        self._result = result

    async def _execute_skip(self, player):
        return self._result, None


def _skip_vote(locale_code, *, guild_id=1, humans=4, cog=None):
    """A real SkipVote constructed - and therefore pinned - in ``locale_code``.

    Returns ``(vote, registry, channel)``. Registered in a real
    :class:`~cogs.music.voteskip.SkipVotes` so the registry-driven finalise paths
    (``notify_track``, ``clear``) can be exercised as they really run.
    """
    track = _Track("Voted On")
    voice_channel = types.SimpleNamespace(
        name="General",
        id=77,
        members=[types.SimpleNamespace(bot=False, id=900 + n) for n in range(humans)],
    )
    player = types.SimpleNamespace(channel=voice_channel, current=track, home=None)
    channel = _VoteChannel()
    registry = voteskip.SkipVotes()
    initiator = types.SimpleNamespace(id=901, mention="<@901>")

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    with i18n.locale(locale_code):
        vote = voteskip.SkipVote(
            cog=cog if cog is not None else _VoteCog(),
            player=player,
            channel=channel,
            track=track,
            initiator=initiator,
            registry=registry,
            guild_id=guild_id,
        )
    registry._put(guild_id, vote)
    return vote, registry, channel


class _VoteChannel:
    """The text channel a vote posts into."""

    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _Message()


def _kill_the_pin(monkeypatch):
    """Put the pre-fix render back on the real SkipVote: no pin at all.

    Every render then happens in whatever language its caller is in, which is
    exactly what the shipped code did - the wording was even translated at the
    call site, in that caller's context.
    """
    monkeypatch.setattr(
        voteskip.SkipVote, "_pinned", lambda self: contextlib.nullcontext()
    )


async def _run_a_vote(locale_code):
    """Open a vote in ``locale_code``, then let the other three writers write.

    The sequence a real vote sees: the starter opens it in their own language, a
    second voter clicks (their language - English here), and 30 s later the view
    timeout finalises it from a task with NO language.
    """
    vote, _registry, _channel = _skip_vote(locale_code)
    with i18n.locale(locale_code):
        await vote.start()
    with i18n.locale("en"):
        await vote.apply(voteskip.VOTE_COUNTED)
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await vote.expire()
    return vote


async def test_one_skip_vote_message_speaks_one_language_to_all_four_writers(spy):
    """The MAJOR: open in French, an English vote, an expiry with no locale."""
    vote = await _run_a_vote("fr")

    assert vote.resolved
    # Every render of this one message came out of the French catalogue...
    assert {tag for tag, _msgid in spy} == {"fr"}, spy
    # ...and all four really happened, so "only French" is not "nothing rendered".
    assert ("fr", "{user} wants to skip **{title}**.") in spy, spy
    assert [msgid for _tag, msgid in spy].count("Vote skip ({count}/{needed})") == 2, spy
    assert ("fr", "Vote expired.") in spy, spy


async def test_control_the_same_vote_shows_three_writers_in_two_languages(
    spy, monkeypatch
):
    """Remove the pin from the real class and the shipped defect comes back."""
    _kill_the_pin(monkeypatch)

    await _run_a_vote("fr")

    assert {tag for tag, _msgid in spy} == {"en", "fr"}, spy
    # Opened in French...
    assert ("fr", "{user} wants to skip **{title}**.") in spy, spy
    # ...relabelled by an English voter, and closed in English.
    assert ("en", "Vote skip ({count}/{needed})") in spy, spy
    assert ("en", "Vote expired.") in spy, spy


@pytest.mark.parametrize(
    ("result", "msgid"),
    [
        (voteskip.SKIP_RESULT_ADVANCED, "Skipped by vote."),
        (voteskip.SKIP_RESULT_NONE, "There are no more tracks in the queue to skip to."),
    ],
)
async def test_a_passing_vote_announces_its_outcome_in_the_votes_language(
    spy, result, msgid
):
    """Both closing lines of a vote that reached its threshold, from a click."""
    vote, _registry, _channel = _skip_vote("fr", cog=_VoteCog(result))
    with i18n.locale("fr"):
        await vote.start()
    spy.clear()

    # The deciding click comes from an English member.
    with i18n.locale("en"):
        await vote.apply(voteskip.VOTE_PASSED)

    assert ("fr", msgid) in spy, spy
    assert {tag for tag, _msgid in spy} == {"fr"}, spy


async def test_control_the_outcome_follows_the_deciding_clicker_without_the_pin(
    spy, monkeypatch
):
    """The same deciding click writes English onto the French message, unpinned."""
    _kill_the_pin(monkeypatch)

    vote, _registry, _channel = _skip_vote("fr")
    with i18n.locale("fr"):
        await vote.start()
    spy.clear()

    with i18n.locale("en"):
        await vote.apply(voteskip.VOTE_PASSED)

    assert ("en", "Skipped by vote.") in spy, spy


async def test_a_track_change_closes_the_vote_in_the_votes_own_language(spy):
    """``notify_track`` runs from the track_start hook, which carries no locale."""
    guild_id = 4242
    vote, registry, _channel = _skip_vote("fr", guild_id=guild_id)
    with i18n.locale("fr"):
        await vote.start()
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await registry.notify_track(guild_id, "a-completely-different-track")

    assert vote.resolved
    assert ("fr", "This track already ended.") in spy, spy
    assert {tag for tag, _msgid in spy} == {"fr"}, spy


async def test_control_a_track_change_closes_it_in_english_without_the_pin(
    spy, monkeypatch
):
    """Unpinned, the same hook writes English onto the French vote message."""
    _kill_the_pin(monkeypatch)

    guild_id = 4243
    vote, registry, _channel = _skip_vote("fr", guild_id=guild_id)
    with i18n.locale("fr"):
        await vote.start()
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await registry.notify_track(guild_id, "a-completely-different-track")

    assert ("en", "This track already ended.") in spy, spy


# ---------------------------------------------------------------------------
# The join card's successor speaks the language of the card it replaces
# ---------------------------------------------------------------------------
#
# A bare /play outside voice posts the join card in the invoker's language. When
# they join, a VOICE-STATE LISTENER - a gateway task with no locale at all -
# edits that same message into the vibe card. Built there from scratch, the
# member watched their own card turn English.


def _armed_watch(locale_code, *, guild_id=6161, member_id=717_171):
    """A real JoinVoiceCard posted in ``locale_code`` with its watch armed."""
    cog = music.Music.__new__(music.Music)
    cog._pending_watches = vibes.PendingVoiceWatches()
    guild = types.SimpleNamespace(id=guild_id)
    member = types.SimpleNamespace(id=member_id, guild=guild)

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    with i18n.locale(locale_code):
        card = views.JoinVoiceCard(member_id, [])
    card.message = _Message()
    cog._pending_watches.add(guild_id, member_id, card)
    return cog, member, card


VIBE_HEADING = "## 🎧 Choose your vibe"


async def test_the_swapped_in_vibe_card_keeps_the_members_own_language(spy):
    """The join card was posted in French, so its successor is French too."""
    cog, member, card = _armed_watch("fr")
    spy.clear()

    # The listener's own context: no locale, i.e. the English default.
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await cog._fire_voice_watch(member)

    assert card.message.edits == 1
    assert _headings(spy, VIBE_HEADING) == ({"fr"}, 1), spy


async def test_control_an_english_join_card_swaps_into_an_english_vibe_card(spy):
    """The other half: the language is read off the card, not hard-coded French."""
    cog, member, card = _armed_watch("en")
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await cog._fire_voice_watch(member)

    assert card.message.edits == 1
    assert _headings(spy, VIBE_HEADING) == ({"en"}, 1), spy


async def test_control_a_card_that_captured_no_locale_swaps_into_english(spy):
    """The pre-fix card exactly: nothing captured, so the listener's English wins.

    ``render_locale`` is the whole fix - the card carried no language before it -
    so clearing it on a real French card reproduces the shipped defect.
    """
    cog, member, card = _armed_watch("fr")
    card.render_locale = None
    spy.clear()

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await cog._fire_voice_watch(member)

    assert card.message.edits == 1
    assert _headings(spy, VIBE_HEADING) == ({"en"}, 1), spy


# ---------------------------------------------------------------------------
# The re-pin door itself
# ---------------------------------------------------------------------------


class _PinnedProbe(PinnedRenderLocale, LocaleLayoutView):
    """The smallest real user of the mixin: one heading, pinned at construction."""

    def __init__(self):
        super().__init__(timeout=None)
        self._build()

    def _compose(self):
        self.clear_items()
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(i18n._("### 🎵 Now Playing")))
        self.add_item(container)


async def test_a_re_pin_with_no_language_leaves_the_message_where_it_was(spy):
    """``_repin_render_locale`` refuses a falsy language, and accepts a real one.

    ``MusicController._repin_to_guild_locale`` hands over whatever
    ``resolve_guild_locale`` returned, and the whole point of re-pinning is to
    keep a public message saying the right thing - so a caller that resolved
    nothing must not be able to drag a French panel to the English default. The
    second half is the control: the guard is "ignore nothing", not "ignore
    everything".
    """
    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    with i18n.locale("fr"):
        probe = _PinnedProbe()
    spy.clear()

    probe._repin_render_locale(None)
    probe._repin_render_locale("")
    probe._build()

    assert probe._render_locale == "fr"
    assert _headings(spy) == ({"fr"}, 1), spy

    probe._repin_render_locale("en")
    probe._build()

    assert probe._render_locale == "en"
    assert _headings(spy) == ({"fr", "en"}, 2), spy
