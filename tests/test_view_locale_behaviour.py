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
import types

import discord
import pytest

# music first: views.py imports from it at module level, so importing views on
# its own hits the package's documented circular-import order.
from cogs.music import music, views  # noqa: F401
from tools import i18n
from tools.paginator import Paginator, paginate_lines
from tools.views import LocaleView

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


async def _post_controller(preferred_locale, guild_id):
    """Run the real ``Music._send_controller`` and hand back the posted view."""
    cog = music.Music.__new__(music.Music)
    cog.bot = types.SimpleNamespace(db_pool=_SendPool())
    cog._controllers = {}
    cog._controller_locks = {}

    guild = types.SimpleNamespace(id=guild_id, preferred_locale=preferred_locale)
    voice_channel = types.SimpleNamespace(name="General", id=99, guild=guild)
    player = _ControllablePlayer(voice_channel, guild=guild)
    player.home = _Home(guild)

    i18n.current_locale.set(i18n.DEFAULT_LOCALE)
    await cog._send_controller(player)
    return player.home.sent


async def test_the_background_poster_builds_the_panel_in_the_guild_locale(spy):
    """A French server's track_start must not post an English panel.

    ``_send_controller`` is the ONLY place a public controller is created, and it
    runs from track_start / the cold restore / a repost - none of which carries a
    locale. It therefore resolves the guild's own and builds inside it; the pin
    then holds that language for every later edit.
    """
    sent = await _post_controller("fr", guild_id=515_151_515_151_515_151)

    assert len(sent) == 1
    assert sent[0]["view"]._render_locale == "fr"
    assert _headings(spy) == ({"fr"}, 1), spy


async def test_control_an_english_server_still_gets_an_english_panel(spy):
    """The other half of the control: the resolve is real, not a hard-coded "fr"."""
    sent = await _post_controller("en-US", guild_id=525_252_525_252_525_252)

    assert len(sent) == 1
    assert sent[0]["view"]._render_locale == "en"
    assert _headings(spy) == ({"en"}, 1), spy
