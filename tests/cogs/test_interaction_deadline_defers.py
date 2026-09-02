"""The commands that now acknowledge BEFORE their round trip, and the idiom itself.

The structural sweep lives in tests/test_interaction_deadline_hygiene.py; this
file runs the callbacks and asserts the ORDER of what actually happens, so a
removed ``ctx.defer()`` fails here even if someone teaches the AST walker to
look elsewhere.

Every test records a single ordered list of events off one fake Context, then
asserts where ``defer`` sits in it. Nothing here touches Discord, the DB, the
network or Lavalink.
"""

import types
from unittest import mock

import discord
import pytest
from discord.ext import commands

from cogs.anilist.account import AccountMixin
from cogs.community.leveling.level_config_ui import LevelConfigUI
from cogs.config.rolemenus import RoleMenuSelect
from cogs.config.rooms_panels import _RoomRenameModal
from cogs.config.welcome import Welcome
from cogs.moderation import moderation
from cogs.system import errors
from tools import interactions


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Recorder:
    """One ordered log of everything the command did, shared by every fake."""

    def __init__(self):
        self.events = []

    def add(self, name, **kwargs):
        self.events.append((name, kwargs))

    @property
    def names(self):
        return [name for name, _kwargs in self.events]

    def kwargs_of(self, name):
        return next(kw for n, kw in self.events if n == name)


class _Author:
    def __init__(self, log, user_id=7):
        self.id = user_id
        self.name = "someone"
        self._log = log

    async def send(self, *args, **kwargs):
        # THE round trip: a DM needs the DM channel opened first, then the send.
        self._log.add("author.send")
        return types.SimpleNamespace(id=1)

    async def add_roles(self, *args, **kwargs):
        self._log.add("add_roles")

    async def remove_roles(self, *args, **kwargs):
        self._log.add("remove_roles")


class _Typing:
    def __init__(self, log):
        self._log = log

    async def __aenter__(self):
        self._log.add("typing")
        return self

    async def __aexit__(self, *exc):
        return False


_SLASH = object()


class _Ctx:
    """A hybrid Context stand-in. ``interaction=None`` models the prefix path."""

    def __init__(self, log, *, interaction=_SLASH, guild=True):
        self._log = log
        if interaction is _SLASH:
            # ``extras`` is the real discord.py Interaction attribute
            # (interactions.py: ``self.extras: Dict[Any, Any] = {}``); the
            # ephemeral-flow marker is recorded there.
            interaction = types.SimpleNamespace(extras={})
        self.interaction = interaction
        self.author = _Author(log)
        self.guild = types.SimpleNamespace(id=42, name="guild") if guild else None
        self.message = None
        self.command = None

    async def defer(self, *, ephemeral=False):
        self._log.add("defer", ephemeral=ephemeral)

    async def send(self, *args, **kwargs):
        self._log.add("send", ephemeral=kwargs.get("ephemeral"))
        return types.SimpleNamespace(id=2)

    def typing(self, **_kwargs):
        return _Typing(self._log)


@pytest.fixture
def log():
    return _Recorder()


# ---------------------------------------------------------------------------
# The idiom: ctx.defer() is inert on the prefix path
# ---------------------------------------------------------------------------
async def test_context_defer_does_nothing_without_an_interaction():
    """Proof against the INSTALLED discord.py, not against a belief about it.

    ``Context.defer`` is guarded by ``if self.interaction:``. A prefix
    invocation has none, so every ``await ctx.defer()`` added in this lot is a
    no-op there - the prefix command behaves exactly as it did before.
    """

    prefix_ctx = types.SimpleNamespace(interaction=None)

    # Must not raise, must not touch anything: there is nothing to acknowledge.
    await commands.Context.defer(prefix_ctx, ephemeral=True)


async def test_context_defer_acknowledges_when_there_is_an_interaction():
    calls = []

    class _Response:
        async def defer(self, *, ephemeral=False):
            calls.append(ephemeral)

    slash_ctx = types.SimpleNamespace(
        interaction=types.SimpleNamespace(response=_Response())
    )

    await commands.Context.defer(slash_ctx, ephemeral=True)
    await commands.Context.defer(slash_ctx)

    assert calls == [True, False], "the ephemeral flag is passed straight through"


# ---------------------------------------------------------------------------
# The ephemeral-flow marker: an ephemeral defer that anything LATER can read
# ---------------------------------------------------------------------------
async def test_defer_ephemeral_defers_and_records_the_choice():
    """Driven through the INSTALLED ``commands.Context.defer``, not a stand-in.

    discord.py keeps no local trace of an ephemeral defer (the flag goes into
    the request payload; only ``_response_type`` is stored), so a later sender
    cannot read the privacy choice back. ``tools.interactions`` records it on
    ``Interaction.extras`` - the dictionary discord.py documents for exactly
    this - and that is what keeps a crash report private.
    """

    calls = []

    class _Response:
        async def defer(self, *, ephemeral=False):
            calls.append(ephemeral)

    interaction = types.SimpleNamespace(response=_Response(), extras={})
    ctx = types.SimpleNamespace(interaction=interaction)
    ctx.defer = lambda **kwargs: commands.Context.defer(ctx, **kwargs)

    await interactions.defer_ephemeral(ctx)

    assert calls == [True]
    assert interactions.prefers_ephemeral(ctx) is True


async def test_defer_ephemeral_is_inert_on_the_prefix_path():
    """No interaction: nothing deferred, nothing marked, nothing mutated."""

    ctx = types.SimpleNamespace(interaction=None)
    ctx.defer = lambda **kwargs: commands.Context.defer(ctx, **kwargs)

    await interactions.defer_ephemeral(ctx)  # must not raise

    assert interactions.prefers_ephemeral(ctx) is False
    assert vars(ctx).keys() == {"interaction", "defer"}


async def test_an_unmarked_interaction_is_not_an_ephemeral_flow():
    """The default is public: only an explicit mark makes a flow private."""

    ctx = types.SimpleNamespace(interaction=types.SimpleNamespace(extras={}))

    assert interactions.prefers_ephemeral(ctx) is False


async def test_the_marker_never_breaks_a_command_it_cannot_mark():
    """Bookkeeping must not be the thing that raises inside a command body."""

    ctx = types.SimpleNamespace(interaction=object())

    interactions.mark_ephemeral(ctx)  # no ``extras`` mapping: silently skipped

    assert interactions.prefers_ephemeral(ctx) is False


# ---------------------------------------------------------------------------
# /anilist login - the case with production proof
# ---------------------------------------------------------------------------
def _anilist_cog(**overrides):
    cog = types.SimpleNamespace(
        _login_available=lambda: True,
        _login_instructions=lambda: "click here",
    )
    for key, value in overrides.items():
        setattr(cog, key, value)
    return cog


async def test_anilist_login_defers_before_it_opens_the_dm(log, monkeypatch):
    monkeypatch.setattr(
        "cogs.anilist.account.LoginView", lambda cog, author_id: types.SimpleNamespace()
    )
    ctx = _Ctx(log)

    await AccountMixin.anilist_login.callback(_anilist_cog(), ctx)

    assert log.names == ["defer", "author.send", "send"], log.names
    assert log.names.index("defer") < log.names.index("author.send")


async def test_anilist_login_defers_ephemerally_and_answers_ephemerally(log, monkeypatch):
    """The defer's privacy has to match every send that follows it.

    A PUBLIC defer with an ephemeral follow-up strands Discord's "thinking"
    placeholder in the channel forever (the trap documented in
    cogs/community/votes.py). Both are ephemeral here.
    """

    monkeypatch.setattr(
        "cogs.anilist.account.LoginView", lambda cog, author_id: types.SimpleNamespace()
    )
    ctx = _Ctx(log)

    await AccountMixin.anilist_login.callback(_anilist_cog(), ctx)

    assert log.kwargs_of("defer")["ephemeral"] is True
    assert log.kwargs_of("send")["ephemeral"] is True


async def test_anilist_login_stays_public_on_the_prefix_path(log, monkeypatch):
    """No interaction: the ack is a plain channel message, exactly as before."""

    monkeypatch.setattr(
        "cogs.anilist.account.LoginView", lambda cog, author_id: types.SimpleNamespace()
    )
    ctx = _Ctx(log, interaction=None)

    await AccountMixin.anilist_login.callback(_anilist_cog(), ctx)

    assert log.kwargs_of("send")["ephemeral"] is False


async def test_anilist_login_answers_first_even_when_the_dm_is_refused(log, monkeypatch):
    import discord

    async def _forbidden(*_args, **_kwargs):
        log.add("author.send")
        raise discord.Forbidden(
            types.SimpleNamespace(status=403, reason="closed DMs"), "cannot DM"
        )

    monkeypatch.setattr(
        "cogs.anilist.account.LoginView", lambda cog, author_id: types.SimpleNamespace()
    )
    ctx = _Ctx(log)
    ctx.author.send = _forbidden

    await AccountMixin.anilist_login.callback(_anilist_cog(), ctx)

    assert log.names == ["defer", "author.send", "send"]
    assert log.kwargs_of("send")["ephemeral"] is True


@pytest.mark.parametrize(
    "command,kwargs",
    [("anilist_login", {}), ("anilist_code", {"code": "1234"})],
)
async def test_a_crash_after_an_ephemeral_defer_is_reported_ephemerally(
    log, monkeypatch, command, kwargs
):
    """THE cross-lot seam: the defers meet the global error reporter.

    The error handler answers a crashed command without ever seeing its body,
    and ``Context.send`` defaults ``ephemeral`` to False - so an ephemerally
    deferred command would have its crash report dropped into the channel, in
    public, on the two commands that handle an OAuth link and a PIN. The defer
    marks the flow; ``_safe_send`` reads the mark.
    """

    monkeypatch.setattr(
        "cogs.anilist.account.LoginView", lambda cog, author_id: types.SimpleNamespace()
    )

    async def _exchange(user_id, code):
        return "Yasuho"

    ctx = _Ctx(log)
    await getattr(AccountMixin, command).callback(
        _anilist_cog(_exchange_code=_exchange), ctx, **kwargs
    )

    await errors._safe_send(ctx, "something broke", surface="unit")

    assert log.events[-1] == ("send", {"ephemeral": True})


# ---------------------------------------------------------------------------
# /anilist code - a third-party HTTP call, plus the synthetic-message delete
# ---------------------------------------------------------------------------
async def test_anilist_code_defers_before_the_anilist_exchange(log):
    async def _exchange(user_id, code):
        log.add("anilist.http")
        return "Yasuho"

    ctx = _Ctx(log)
    await AccountMixin.anilist_code.callback(
        _anilist_cog(_exchange_code=_exchange), ctx, code="1234"
    )

    assert log.names == ["defer", "anilist.http", "send"], log.names
    assert log.kwargs_of("defer")["ephemeral"] is True


async def test_anilist_code_defers_before_deleting_the_pin_message(log):
    """The delete is a REST call on BOTH paths - ctx.message is synthetic on slash."""

    async def _exchange(user_id, code):
        log.add("anilist.http")
        return "Yasuho"

    async def _delete():
        log.add("message.delete")

    ctx = _Ctx(log)
    ctx.message = types.SimpleNamespace(delete=_delete)

    await AccountMixin.anilist_code.callback(
        _anilist_cog(_exchange_code=_exchange), ctx, code="1234"
    )

    assert log.names == ["defer", "message.delete", "anilist.http", "send"], log.names


# ---------------------------------------------------------------------------
# welcome test - a Pillow render behind the shared image executor
# ---------------------------------------------------------------------------
async def test_welcome_test_defers_before_rendering_the_card(log):
    async def _get_config(guild_id):
        return {"card": True}

    async def _render(member):
        log.add("render")
        return None

    cog = types.SimpleNamespace(
        get_config=_get_config,
        _compose=lambda config, member: ("hello", object()),
        _render_card_file=_render,
    )
    ctx = _Ctx(log)

    await Welcome.welcome_test.callback(cog, ctx)

    assert log.names == ["defer", "typing", "render", "send"], log.names
    assert log.kwargs_of("defer")["ephemeral"] is False, "the preview is public"


async def test_welcome_test_still_defers_when_the_card_is_off(log):
    """Deferring unconditionally is the point: it costs one thinking state and
    removes the branch where somebody turns the card back on and forgets."""

    async def _get_config(guild_id):
        return {"card": False}

    cog = types.SimpleNamespace(
        get_config=_get_config,
        _compose=lambda config, member: (None, object()),
        _render_card_file=None,
    )
    ctx = _Ctx(log)

    await Welcome.welcome_test.callback(cog, ctx)

    assert log.names == ["defer", "typing", "send"]


# ---------------------------------------------------------------------------
# addrole / removerole - MemberConverter waits on a GATEWAY member chunk
# ---------------------------------------------------------------------------
def _member_converter_recording(log, member):
    class _Converter:
        async def convert(self, ctx, argument):
            # discord.py: on a cache miss this is a gateway query_members under
            # asyncio.wait_for(..., timeout=30.0).
            log.add("member_query")
            return member

    return _Converter


@pytest.mark.parametrize(
    "command,role_event",
    [("addrole", "add_roles"), ("removerole", "remove_roles")],
)
async def test_role_commands_defer_before_the_member_query(
    log, monkeypatch, command, role_event
):
    monkeypatch.setattr(moderation.modchecks, "role_hierarchy_error", lambda ctx, role: None)
    ctx = _Ctx(log)
    monkeypatch.setattr(
        moderation, "MemberConverter", _member_converter_recording(log, ctx.author)
    )
    role = types.SimpleNamespace(id=3, name="staff")

    callback = getattr(moderation.Moderation, command).callback
    await callback(types.SimpleNamespace(), ctx, "someone", role)

    assert log.names == ["defer", "member_query", role_event, "send"], log.names
    assert log.kwargs_of("defer")["ephemeral"] is False


async def test_the_mass_role_path_is_untouched(log, monkeypatch):
    """``-all`` already answered with its confirmation prompt, so it must NOT
    gain a defer in front of that prompt."""

    monkeypatch.setattr(moderation.modchecks, "role_hierarchy_error", lambda ctx, role: None)
    ctx = _Ctx(log)

    async def _confirm(_ctx, _embed):
        log.add("confirm")
        return False

    async def _edit_confirm(_ctx, **_kwargs):
        log.add("edit_confirm")

    cog = types.SimpleNamespace(_confirm=_confirm, _edit_confirm=_edit_confirm)
    role = types.SimpleNamespace(id=3, name="staff")

    await moderation.Moderation.addrole.callback(cog, ctx, "-all", role)

    assert log.names == ["confirm", "edit_confirm"], log.names
    assert "defer" not in log.names


# ---------------------------------------------------------------------------
# /levelconfig xp give|take|set - reward-role REST loop, then a DM announce
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command,seam",
    [
        ("levelconfig_xp_give", "cmd_give"),
        ("levelconfig_xp_take", "cmd_take"),
        ("levelconfig_xp_set", "cmd_set"),
    ],
)
async def test_xp_admin_commands_defer_before_the_reward_routing(log, command, seam):
    async def _body(ctx, member, amount):
        log.add(seam)

    admin_cog = types.SimpleNamespace(**{seam: _body})

    async def _require(ctx, name):
        return admin_cog

    cog = types.SimpleNamespace(_require=_require)
    ctx = _Ctx(log)

    callback = getattr(LevelConfigUI, command).callback
    await callback(cog, ctx, types.SimpleNamespace(id=9), 50)

    assert log.names == ["defer", seam], log.names
    assert log.kwargs_of("defer")["ephemeral"] is False


async def test_xp_admin_does_not_defer_when_the_sibling_cog_is_missing(log):
    """A refusal is instant; nothing slow follows it, so it needs no defer."""

    async def _require(ctx, name):
        await ctx.send("not loaded")
        return None

    cog = types.SimpleNamespace(_require=_require)
    ctx = _Ctx(log)

    await LevelConfigUI.levelconfig_xp_give.callback(
        cog, ctx, types.SimpleNamespace(id=9), 50
    )

    assert log.names == ["send"]


# ---------------------------------------------------------------------------
# Component callbacks - the surface with no Context at all
# ---------------------------------------------------------------------------
# A button, select or modal callback is handed the raw Interaction, so it cannot
# use ``ctx.defer()``; it goes through ``tools.interactions.defer``, which takes
# TWO flags that the structural sweep in
# tests/test_interaction_deadline_hygiene.py cannot judge. That sweep classifies
# ``interactions.defer`` as "the interaction was answered" without reading its
# kwargs, so it proves a defer EXISTS and nothing about whether it matches the
# answer underneath it. Both flags are load-bearing:
#
# * ``ephemeral=False`` under an ephemeral follow-up strands Discord's public
#   "thinking" placeholder in the channel forever - the trap already pinned for
#   the command path by the anilist-login pair above.
# * ``thinking=False`` on a component or modal interaction is a DIFFERENT
#   response type: discord.py 2.7 sends DEFERRED_UPDATE_MESSAGE, which shows the
#   member nothing at all while the slow work runs, and drops the ephemeral flag
#   with it (``data = {'flags': 64}`` is built only under ``thinking and
#   ephemeral``). The token survives; the feedback does not.
#
# So both are asserted here, by RUNNING the callbacks.


class _ItxResponse:
    def __init__(self, log, done=False):
        self._log = log
        self._done = done

    def is_done(self):
        return self._done

    async def send_message(self, *args, **kwargs):
        self._log.add("response.send_message", ephemeral=kwargs.get("ephemeral"))
        self._done = True

    async def defer(self, *, ephemeral=False, thinking=False):
        self._log.add("defer", ephemeral=ephemeral, thinking=thinking)
        self._done = True


class _ItxFollowup:
    def __init__(self, log):
        self._log = log

    async def send(self, *args, **kwargs):
        self._log.add("followup.send", ephemeral=kwargs.get("ephemeral"))


class _Itx:
    """A raw ``discord.Interaction`` stand-in writing into the shared order log."""

    def __init__(self, log, *, guild=None, user=None):
        self.extras = {}  # the real attribute the ephemeral marker is stored on
        self.locale = "en"
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.user = user
        self.message = None
        # apply_interaction_locale reads this and swallows anything it raises,
        # so a stand-in with no bot behind it resolves to the default locale.
        self.client = types.SimpleNamespace(get_cog=lambda _name: None)
        self.response = _ItxResponse(log)
        self.followup = _ItxFollowup(log)


class _Role:
    def __init__(self, role_id, position, managed=False):
        self.id = role_id
        self.position = position
        self.managed = managed
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other):
        return self.position >= other.position


def _fake_member(log, roles):
    """A stand-in that passes ``isinstance(member, discord.Member)``.

    The role-menu callback guards on that isinstance, so a SimpleNamespace would
    silently take the "not in a server" arm and the test would prove nothing.
    """

    member = mock.MagicMock(spec=discord.Member)
    member.id = 7
    member.roles = roles

    async def _add(role, **_kwargs):
        log.add("add_roles", role_id=role.id)

    async def _remove(role, **_kwargs):
        log.add("remove_roles", role_id=role.id)

    member.add_roles = _add
    member.remove_roles = _remove
    return member


@pytest.fixture
def role_menu(log):
    """A two-role "any" menu where the member gains one role and loses another.

    That is the production shape from 2026-08-31 19:03: one pick, TWO REST calls
    in the loop, and the summary only after them.
    """

    colour = _Role(10, position=1)
    ping = _Role(20, position=1)
    bot_top = _Role(99, position=50)
    by_id = {r.id: r for r in (colour, ping, bot_top)}
    guild = types.SimpleNamespace(
        id=42,
        me=types.SimpleNamespace(top_role=bot_top),
        get_role=by_id.get,
    )
    member = _fake_member(log, [ping])
    return types.SimpleNamespace(
        interaction=_Itx(log, guild=guild, user=member),
        select=types.SimpleNamespace(
            config={"options": [{"role_id": 10}, {"role_id": 20}], "exclusive": False},
            values=["10"],
        ),
    )


async def test_the_role_menu_defers_before_the_role_loop(log, role_menu):
    await RoleMenuSelect.callback(role_menu.select, role_menu.interaction)

    assert log.names == ["defer", "add_roles", "remove_roles", "followup.send"], log.names


async def test_the_role_menu_defers_ephemerally_with_a_thinking_state(log, role_menu):
    """The defer's two flags have to match the ephemeral summary below it."""

    await RoleMenuSelect.callback(role_menu.select, role_menu.interaction)

    assert log.kwargs_of("defer") == {"ephemeral": True, "thinking": True}
    assert log.kwargs_of("followup.send")["ephemeral"] is True


async def test_the_role_menu_guard_answers_without_deferring(log):
    """Outside a server nothing slow follows, so the guard still answers plainly."""

    interaction = _Itx(log, guild=None, user=None)
    select = types.SimpleNamespace(config={}, values=[])

    await RoleMenuSelect.callback(select, interaction)

    assert log.names == ["response.send_message"], log.names
    assert log.kwargs_of("response.send_message")["ephemeral"] is True


def _rename_modal(log, channel):
    """``_RoomRenameModal`` without discord.py's Modal machinery behind it."""

    return types.SimpleNamespace(
        _owner=types.SimpleNamespace(_channel=lambda: channel),
        name_input=types.SimpleNamespace(value="  quiet corner  "),
    )


async def test_the_room_rename_defers_before_the_channel_edit(log):
    class _Channel:
        async def edit(self, **kwargs):
            # Discord caps channel name edits at two per ten minutes and
            # discord.py sleeps out the 429 inside the request: minutes, on a
            # three-second token.
            log.add("channel.edit", new_name=kwargs.get("name"))

    interaction = _Itx(log)

    await _RoomRenameModal.on_submit(_rename_modal(log, _Channel()), interaction)

    assert log.names == ["defer", "channel.edit", "followup.send"], log.names
    assert log.kwargs_of("channel.edit")["new_name"] == "quiet corner"


async def test_the_room_rename_defers_ephemerally_with_a_thinking_state(log):
    class _Channel:
        async def edit(self, **kwargs):
            log.add("channel.edit", new_name=kwargs.get("name"))

    interaction = _Itx(log)

    await _RoomRenameModal.on_submit(_rename_modal(log, _Channel()), interaction)

    assert log.kwargs_of("defer") == {"ephemeral": True, "thinking": True}
    assert log.kwargs_of("followup.send")["ephemeral"] is True


async def test_the_room_rename_failure_stays_ephemeral_after_the_defer(log):
    """The error answer has to clear the thinking state too, and stay private."""

    class _Channel:
        async def edit(self, **kwargs):
            log.add("channel.edit", new_name=kwargs.get("name"))
            raise discord.HTTPException(
                types.SimpleNamespace(status=500, reason="nope"), "boom"
            )

    interaction = _Itx(log)

    await _RoomRenameModal.on_submit(_rename_modal(log, _Channel()), interaction)

    assert log.names == ["defer", "channel.edit", "followup.send"], log.names
    assert log.kwargs_of("followup.send")["ephemeral"] is True


@pytest.mark.parametrize("value", ["", "   "])
async def test_the_room_rename_guards_answer_without_deferring(log, value):
    """A cache-only refusal is instant, so it must NOT gain a thinking state."""

    modal = _rename_modal(log, object())
    modal.name_input.value = value

    await _RoomRenameModal.on_submit(modal, _Itx(log))

    assert log.names == ["response.send_message"], log.names
    assert "defer" not in log.names


async def test_the_room_rename_guard_on_a_dead_room_answers_without_deferring(log):
    modal = _rename_modal(log, None)

    await _RoomRenameModal.on_submit(modal, _Itx(log))

    assert log.names == ["response.send_message"], log.names
