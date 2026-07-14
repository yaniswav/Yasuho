"""Unit tests for the redesigned AutoMod surface (Lot A1).

Covers the naming redesign (``/automod links|invites|spam`` with ``on``/``off``
plus the old ``anti*`` prefix aliases), the single control panel and its
components, and the shared display catalog. The engine (regex scanning, native
API, DB) is unchanged and exercised only through the cog seams the panel uses.

Drives against the conftest fakes: ``fake_pool`` (records every DB call) and
``make_interaction``. The real :class:`AutoMod` cog is constructed with a fake
bot so ``set_custom_rule`` / ``set_native_rule`` run for real against the pool.
"""

import types

import discord

from cogs.moderation.automod import AutoMod
from cogs.moderation.automod_panel import (
    ACTION_CHOICES,
    CONTENT_FILTERS,
    DEFAULT_ACTION,
    NATIVE_FILTERS,
    VALID_ACTIONS,
    AutoModPanel,
    _ActionSelect,
    _ExemptChannelSelect,
    _ExemptRoleSelect,
    _FilterToggle,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeGuild:
    def __init__(self, guild_id=1, name="guild", channels=(), roles=()):
        self.id = guild_id
        self.name = name
        self._channels = {c.id: c for c in channels}
        self._roles = {r.id: r for r in roles}

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return self._roles.get(rid)

    async def fetch_automod_rules(self):
        # No managed native rules -> native_state resolves every key to False.
        return []


class _Ctx:
    def __init__(self, guild=None, author_id=1):
        self.guild = guild or _FakeGuild()
        self.author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
        self.invoked_subcommand = None
        self.sends = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return types.SimpleNamespace(id=999)


def _make_cog(fake_pool):
    bot = types.SimpleNamespace(db_pool=fake_pool, user=types.SimpleNamespace(id=7))
    return AutoMod(bot)


def _default_state():
    return {
        "link": False,
        "invite": False,
        "spam": False,
        "kw": False,
        "nspam": False,
        "nmention": False,
        "action": DEFAULT_ACTION,
        "exempt_roles": [],
        "exempt_channels": [],
    }


# ---------------------------------------------------------------------------
# Catalog invariants (guard against engine/UI vocabulary drift)
# ---------------------------------------------------------------------------
def test_valid_actions_are_derived_from_the_catalog():
    assert VALID_ACTIONS == {value for value, *_ in ACTION_CHOICES}
    assert DEFAULT_ACTION in VALID_ACTIONS


def test_native_filter_keys_match_the_engine_rule_names():
    assert {key for key, *_ in NATIVE_FILTERS} == set(AutoMod.NATIVE_RULE_NAMES)


def test_content_filter_keys_are_the_three_custom_filters():
    assert {key for key, *_ in CONTENT_FILTERS} == {"link", "invite", "spam"}


# ---------------------------------------------------------------------------
# Naming: /automod links|invites|spam with on/off + anti* prefix aliases
# ---------------------------------------------------------------------------
def test_subcommands_and_aliases():
    subs = {c.name: c for c in AutoMod.automod.commands}
    assert set(subs) == {"links", "invites", "spam", "panel"}
    assert subs["links"].aliases == ["antilink"]
    assert subs["invites"].aliases == ["antiinvite"]
    assert subs["spam"].aliases == ["antispam"]


async def test_links_on_writes_true_and_confirms(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(guild=_FakeGuild(guild_id=5))
    await cog.automod_links.callback(cog, ctx, "on")

    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    upsert = next(c for c in execs if "INSERT INTO automod" in c[1])
    assert "antilink" in upsert[1]
    assert upsert[2] == (5, True)
    embed = ctx.sends[0][1]["embed"]
    assert isinstance(embed, discord.Embed)
    assert "enabled" in embed.description


async def test_spam_off_writes_false(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(guild=_FakeGuild(guild_id=6))
    await cog.automod_spam.callback(cog, ctx, "off")

    upsert = next(
        c for c in fake_pool.calls if c[0] == "execute" and "antispam" in c[1]
    )
    assert upsert[2] == (6, False)
    assert "disabled" in ctx.sends[0][1]["embed"].description


async def test_invites_on_routes_through_settings_blob(fake_pool):
    # antiinvite lives in the guild_settings JSONB blob, not the automod table.
    cog = _make_cog(fake_pool)
    ctx = _Ctx(guild=_FakeGuild(guild_id=4242))
    await cog.automod_invites.callback(cog, ctx, "on")

    execs = [c for c in fake_pool.calls if c[0] == "execute"]
    assert any("guild_settings" in c[1] for c in execs)
    assert not any("INSERT INTO automod" in c[1] for c in execs)
    assert "enabled" in ctx.sends[0][1]["embed"].description


# ---------------------------------------------------------------------------
# Bare group + panel command both open the panel
# ---------------------------------------------------------------------------
async def test_bare_group_opens_the_panel(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    ctx.invoked_subcommand = None
    await cog.automod.callback(cog, ctx)
    assert len(ctx.sends) == 1
    assert isinstance(ctx.sends[0][1]["view"], AutoModPanel)


async def test_panel_command_opens_the_panel(fake_pool):
    cog = _make_cog(fake_pool)
    ctx = _Ctx(guild=_FakeGuild(guild_id=1))
    await cog.automod_panel.callback(cog, ctx)
    assert isinstance(ctx.sends[0][1]["view"], AutoModPanel)


# ---------------------------------------------------------------------------
# Panel builds across states (presentational; assert it assembles cleanly and
# stays inside the CV2 text budget rather than pinning exact copy).
# ---------------------------------------------------------------------------
def _text_chars(view):
    total = 0

    def walk(item):
        nonlocal total
        content = getattr(item, "content", None)
        if isinstance(content, str):
            total += len(content)
        for child in getattr(item, "children", None) or []:
            walk(child)

    for child in view.children:
        walk(child)
    return total


def test_panel_builds_with_defaults(fake_pool):
    cog = _make_cog(fake_pool)
    view = AutoModPanel(cog, _FakeGuild(), 1, _default_state())
    assert len(view.children) == 1  # a single Container
    assert _text_chars(view) < 4000


def test_panel_builds_with_native_unavailable(fake_pool):
    cog = _make_cog(fake_pool)
    state = _default_state()
    state.update(kw=None, nspam=None, nmention=None)
    view = AutoModPanel(cog, _FakeGuild(), 1, state)
    assert _text_chars(view) < 4000


def test_panel_stays_in_budget_with_maxed_exemptions(fake_pool):
    # 25 roles + 25 channels is the hard cap (each select's max_values=25). The
    # ids drive the rendered mention lists (the only variable text); an empty
    # guild resolves no default_values, which is fine for the budget check.
    cog = _make_cog(fake_pool)
    state = _default_state()
    state["exempt_roles"] = [10**18 + i for i in range(25)]
    state["exempt_channels"] = [2 * 10**18 + i for i in range(25)]
    view = AutoModPanel(cog, _FakeGuild(), 1, state)
    assert _text_chars(view) < 4000


# ---------------------------------------------------------------------------
# Toggle button: custom vs native, and the disabled-when-unavailable state
# ---------------------------------------------------------------------------
def test_filter_toggle_reflects_state(fake_pool):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(), 1, _default_state())
    on = _FilterToggle(panel, "link", "🔗", "Links", native=False)
    assert on.style == discord.ButtonStyle.secondary  # off -> grey
    panel.state["link"] = True
    on2 = _FilterToggle(panel, "link", "🔗", "Links", native=False)
    assert on2.style == discord.ButtonStyle.success  # on -> green

    panel.state["kw"] = None
    na = _FilterToggle(panel, "kw", "🚫", "Keywords", native=True)
    assert na.disabled is True
    assert "N/A" in na.label


async def test_custom_toggle_writes_and_rerenders(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(guild_id=3), 1, _default_state())
    panel.message = types.SimpleNamespace()
    interaction = make_interaction()

    await panel.toggle(interaction, "link", native=False)

    assert panel.state["link"] is True
    assert any("INSERT INTO automod" in c[1] for c in fake_pool.calls)
    assert len(interaction.edits) == 1  # view=-only in-place refresh


async def test_native_toggle_refuses_without_permission(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)

    async def _fake_set_native_rule(guild, key, enabled):
        return False, None

    cog.set_native_rule = _fake_set_native_rule
    panel = AutoModPanel(cog, _FakeGuild(guild_id=3), 1, _default_state())
    panel.message = types.SimpleNamespace()
    interaction = make_interaction()

    await panel.toggle(interaction, "kw", native=True)

    assert interaction.sent, "an ephemeral refusal should be sent"
    assert "Manage Server" in interaction.sent[0][0][0]
    assert interaction.edits == []  # nothing changed -> no re-render


async def test_native_toggle_success_updates_state(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)

    async def _fake_set_native_rule(guild, key, enabled):
        return True, enabled

    cog.set_native_rule = _fake_set_native_rule
    panel = AutoModPanel(cog, _FakeGuild(guild_id=3), 1, _default_state())
    panel.message = types.SimpleNamespace()
    interaction = make_interaction()

    await panel.toggle(interaction, "kw", native=True)

    assert panel.state["kw"] is True
    assert len(interaction.edits) == 1


# ---------------------------------------------------------------------------
# Action select + exemption selects persist through the settings blob
# ---------------------------------------------------------------------------
async def test_set_action_persists_and_rerenders(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(guild_id=8), 1, _default_state())
    panel.message = types.SimpleNamespace()
    interaction = make_interaction()

    await panel.set_action(interaction, "mute")

    assert panel.state["action"] == "mute"
    assert any(
        c[0] == "execute" and "guild_settings" in c[1] for c in fake_pool.calls
    )
    assert len(interaction.edits) == 1


async def test_set_action_coerces_an_unknown_value(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(guild_id=8), 1, _default_state())
    panel.message = types.SimpleNamespace()

    await panel.set_action(make_interaction(), "not-a-real-action")

    assert panel.state["action"] == DEFAULT_ACTION


async def test_set_exempt_roles_persists(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(guild_id=8), 1, _default_state())
    panel.message = types.SimpleNamespace()

    await panel.set_exempt(make_interaction(), "roles", [111, 222])

    assert panel.state["exempt_roles"] == [111, 222]
    assert any("guild_settings" in c[1] for c in fake_pool.calls if c[0] == "execute")


async def test_set_exempt_channels_persists(fake_pool, make_interaction):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(guild_id=8), 1, _default_state())
    panel.message = types.SimpleNamespace()

    await panel.set_exempt(make_interaction(), "channels", [333])

    assert panel.state["exempt_channels"] == [333]


# ---------------------------------------------------------------------------
# Components construct cleanly from the catalog / state
# ---------------------------------------------------------------------------
def test_action_select_preselects_current_action(fake_pool):
    cog = _make_cog(fake_pool)
    state = _default_state()
    state["action"] = "kick"
    panel = AutoModPanel(cog, _FakeGuild(), 1, state)
    select = _ActionSelect(panel)
    defaulted = [o.value for o in select.options if o.default]
    assert defaulted == ["kick"]
    assert {o.value for o in select.options} == VALID_ACTIONS


def test_exempt_selects_construct(fake_pool):
    cog = _make_cog(fake_pool)
    panel = AutoModPanel(cog, _FakeGuild(), 1, _default_state())
    assert _ExemptRoleSelect(panel, defaults=[]).max_values == 25
    assert _ExemptChannelSelect(panel, defaults=[]).max_values == 25
