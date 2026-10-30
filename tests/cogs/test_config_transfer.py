"""V5d: server configuration export / import / templates.

No database, no Discord: guild_settings is held in memory by a pool that answers
the three statements tools.settings and the import write path issue, so the REAL
tools.settings read/write seam (and its LRU) runs underneath.

Typography rule: ASCII '-' and '...' only.
"""

import copy
import datetime
import json
import types

import discord
import pytest

from cogs.config import config_transfer as ct
from cogs.config.tickets import guild_config as tickets_config
from cogs.music import guild_config as music_config
from tools import premium, premium_upsell, role_audit, settings

TEXT = discord.ChannelType.text
VOICE = discord.ChannelType.voice
ADMIN = 0x8


# -- fakes ---------------------------------------------------------------------
class SettingsPool:
    """guild_settings in memory, speaking the SQL tools.settings issues."""

    def __init__(self):
        self.rows = {}
        self.writes = []

    async def fetchval(self, sql, guild_id, *args):
        assert sql.startswith("SELECT settings FROM guild_settings"), sql
        row = self.rows.get(guild_id)
        return None if row is None else json.dumps(row)

    async def execute(self, sql, guild_id, *args):
        if sql == ct._WRITE_KEYS_SQL:  # one statement: merge the set keys, drop the rest
            present, absent = json.loads(args[0]), args[1]
            self.writes.append((guild_id, tuple(sorted(present)) + tuple(absent)))
            row = self.rows.setdefault(guild_id, {})
            for key in absent:
                row.pop(key, None)
            row.update(present)
            return
        assert "jsonb_set" in sql, sql
        key, value = args
        self.writes.append((guild_id, key))
        self.rows.setdefault(guild_id, {})[key] = json.loads(value)


def perms(**overrides):
    base = dict(view_channel=True, send_messages=True, embed_links=True)
    base.update(overrides)
    return types.SimpleNamespace(**base)


class Channel:
    def __init__(self, cid, name, ctype=TEXT, *, bot=None, member=None):
        self.id, self.name, self.type = cid, name, ctype
        self.bot_perms = bot or perms()
        self.member_perms = member or perms()

    def permissions_for(self, who):
        return self.bot_perms if getattr(who, "is_me", False) else self.member_perms


class Role:
    def __init__(self, rid, name, *, value=0, position=1, managed=False, mentionable=True, default=False):
        self.id, self.name = rid, name
        self.permissions = types.SimpleNamespace(value=value)
        self.position, self.managed, self.mentionable = position, managed, mentionable
        self._default = default

    def is_default(self):
        return self._default


def member(uid=7, *, manage_guild=True, mention_everyone=False, manage_roles=True, top=50, me=False):
    return types.SimpleNamespace(
        id=uid,
        is_me=me,
        guild_permissions=types.SimpleNamespace(
            manage_guild=manage_guild, mention_everyone=mention_everyone, manage_roles=manage_roles
        ),
        top_role=types.SimpleNamespace(position=top),
    )


class Guild:
    def __init__(self, gid, channels=(), roles=()):
        self.id = gid
        self.channels = list(channels)
        self.roles = [Role(gid, "@everyone", default=True), *roles]
        self.me = member(999, me=True)
        self.member_count = 10

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class Premium:
    def __init__(self, limits=premium.GUILD_PREMIUM):
        self.limits = limits

    def for_guild(self, gid):
        return self.limits

    def is_guild_premium(self, gid):
        return self.limits is premium.GUILD_PREMIUM


class Engine:
    """Records the follow-up hooks a section write triggers."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def hook(*args, **kwargs):
            self.calls.append((name, args))

        return hook


class BotCacheTripwire:
    """A bot whose global caches must never be consulted by the import."""

    def get_channel(self, cid):  # pragma: no cover - reaching it is the failure
        raise AssertionError("the bot-wide channel cache was consulted")

    get_role = get_guild = get_channel


def make_bot(pool, limits=premium.GUILD_PREMIUM, cogs=None):
    bot = types.SimpleNamespace(db_pool=pool, premium=Premium(limits))
    bot.get_cog = lambda name: (cogs or {}).get(name)
    bot.get_channel = BotCacheTripwire().get_channel
    return bot


@pytest.fixture(autouse=True)
def _fresh_settings_cache():
    settings.invalidate_all()
    ct._import_cooldown._seen.clear()
    yield
    settings.invalidate_all()


def source_guild():
    return Guild(
        1,
        channels=[Channel(11, "welcome"), Channel(12, "ticket-logs"), Channel(13, "events"), Channel(14, "stats")],
        roles=[Role(21, "Support"), Role(22, "DJ"), Role(23, "Birthday", position=5), Role(24, "Events")],
    )


def target_guild(**channel_kwargs):
    return Guild(
        2,
        channels=[
            Channel(111, "welcome", **channel_kwargs),
            Channel(112, "ticket-logs"),
            Channel(113, "events"),
            Channel(114, "stats"),
        ],
        roles=[Role(121, "Support"), Role(122, "DJ"), Role(123, "Birthday", position=5), Role(124, "Events")],
    )


def configure(pool, gid, ids):
    """Store a realistic configuration for guild ``gid`` (ids: name -> id)."""
    pool.rows[gid] = {
        "locale": "fr",
        "welcome": {"enabled": True, "channel_id": ids["welcome"], "embed": {"title": "Hi {user}"}},
        tickets_config.KEY_SUPPORT_ROLE: ids["Support"],
        tickets_config.KEY_LOG_CHANNEL: str(ids["ticket-logs"]),  # the dashboard writes strings
        tickets_config.KEY_MAX_OPEN_PER_USER: 3,
        music_config.KEY_DJ_ROLE: ids["DJ"],
        music_config.KEY_DEFAULT_VOLUME: 80,
        "birthdays": {"enabled": True, "channel_id": ids["events"], "role_id": ids["Birthday"], "hour": 8},
        "event_reminders": {"enabled": True, "channel_id": ids["events"], "role_id": ids["Events"]},
        "serverstats_digest_channel": ids["stats"],
    }


SOURCE_IDS = {"welcome": 11, "ticket-logs": 12, "events": 13, "stats": 14, "Support": 21, "DJ": 22, "Birthday": 23, "Events": 24}
TARGET_IDS = {"welcome": 111, "ticket-logs": 112, "events": 113, "stats": 114, "Support": 121, "DJ": 122, "Birthday": 123, "Events": 124}


async def exported(pool, guild, who=None):
    return ct.dump(await ct.build_export(pool, guild, who or member()))


def by_name(plans):
    return {p.name: p for p in plans}


# -- export / round trip --------------------------------------------------------
async def test_export_carries_names_and_string_ids_and_only_configured_sections():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    document = json.loads(await exported(pool, source_guild()))
    assert document["format"] == "yasuho-config" and document["version"] == 1
    assert document["source_guild_id"] == "1"
    sections = document["sections"]
    assert set(sections) == {"language", "welcome", "tickets", "music", "birthdays", "event_reminders", "serverstats"}
    assert sections["welcome"]["channel_id"] == {"id": "11", "name": "welcome"}
    assert sections["tickets"][tickets_config.KEY_LOG_CHANNEL] == {"id": "12", "name": "ticket-logs"}
    assert sections["music"][music_config.KEY_DJ_ROLE] == {"id": "22", "name": "DJ"}
    # The panel channel is never exported (it is a posted panel's on-switch).
    assert tickets_config.KEY_PANEL_CHANNEL not in sections["tickets"]


async def test_round_trip_into_the_same_guild_is_a_no_op():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    guild = source_guild()
    before = copy.deepcopy(pool.rows)
    parsed = ct.parse_document(await exported(pool, guild))
    plans = await ct.build_plan(pool, guild, member(), parsed)
    assert plans and all(p.status == ct.UNCHANGED for p in plans)
    assert all(not p.notes for p in plans)
    pool.writes.clear()
    await ct.apply_plan(make_bot(pool), guild, plans)
    assert pool.writes == [] and pool.rows == before


# -- cross-guild remap ----------------------------------------------------------
async def test_cross_guild_import_remaps_every_reference_by_name():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    data = await exported(pool, source_guild())
    target = target_guild()
    engine = Engine()
    bot = make_bot(pool, cogs={"Birthdays": engine, "EventReminders": engine})
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(data))
    assert {p.name: p.status for p in plans} == {
        "language": ct.CHANGE, "welcome": ct.CHANGE, "tickets": ct.CHANGE, "music": ct.CHANGE,
        "birthdays": ct.CHANGE, "event_reminders": ct.CHANGE, "serverstats": ct.CHANGE,
    }
    assert {n.outcome for p in plans for n in p.notes} == {"remapped"}
    results = await ct.apply_plan(bot, target, plans)
    assert {outcome for _plan, outcome in results} == {"applied"}
    stored = pool.rows[2]
    assert stored["welcome"]["channel_id"] == 111
    assert stored[tickets_config.KEY_SUPPORT_ROLE] == 121
    assert stored[tickets_config.KEY_LOG_CHANNEL] == 112
    assert stored[music_config.KEY_DJ_ROLE] == 122
    assert stored["birthdays"]["role_id"] == 123 and stored["event_reminders"]["channel_id"] == 113
    assert stored["serverstats_digest_channel"] == 114
    # The source guild was never written.
    assert all(gid == 2 for gid, _key in pool.writes)
    # Each feature's own follow-up ran.
    assert ("reconcile", (2,)) in engine.calls
    assert any(name == "reconcile_guild" for name, _args in engine.calls)
    # And importing the same file again is now a no-op.
    again = await ct.build_plan(pool, target, member(), ct.parse_document(data))
    assert all(p.status == ct.UNCHANGED for p in again)


async def test_unmapped_and_wrong_type_references_are_dropped_never_kept():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    data = await exported(pool, source_guild())
    target = Guild(
        3,
        channels=[
            Channel(111, "welcome", VOICE),  # same name, wrong type: no match
            Channel(14, "elsewhere", VOICE),  # the SOURCE's stats id, but a voice channel here
        ],
        roles=[Role(122, "DJ")],
    )
    plans = by_name(await ct.build_plan(pool, target, member(), ct.parse_document(data)))
    assert plans["welcome"].new["channel_id"] is None
    assert [n.outcome for n in plans["welcome"].notes] == ["unmapped"]
    assert plans["serverstats"].new["serverstats_digest_channel"] is None
    assert [n.outcome for n in plans["serverstats"].notes] == ["wrong_type"]
    assert plans["tickets"].new[tickets_config.KEY_SUPPORT_ROLE] is None
    assert plans["music"].new[music_config.KEY_DJ_ROLE] == 122
    await ct.apply_plan(make_bot(pool), target, list(plans.values()))
    stored = pool.rows[3]
    assert stored["welcome"]["channel_id"] is None
    assert "serverstats_digest_channel" not in stored  # dropped = key deleted, never kept
    assert tickets_config.KEY_SUPPORT_ROLE not in stored


async def test_an_id_of_another_guild_is_rejected_even_when_the_bot_can_see_it():
    pool = SettingsPool()
    other = Channel(900, "secret")  # lives in ANOTHER guild the bot is in
    payload = {
        "format": "yasuho-config", "version": 1, "source_guild_id": "2",
        "sections": {"welcome": {"enabled": True, "channel_id": {"id": "900", "name": "no-such-name"}}},
    }
    target = target_guild()
    bot = make_bot(pool)
    bot.get_channel = lambda cid: other  # a bot-wide lookup WOULD find it
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(json.dumps(payload).encode()))
    assert plans[0].new["channel_id"] is None
    assert [n.outcome for n in plans[0].notes] == ["unmapped"]
    # Negative control: the same id in the target's own cache IS kept, so the
    # rejection above is the guild scope, not a broken lookup.
    target.channels.append(Channel(900, "secret"))
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(json.dumps(payload).encode()))
    assert plans[0].new["channel_id"] == 900


async def test_a_dangerous_role_is_never_applied(monkeypatch):
    pool = SettingsPool()
    payload = {
        "format": "yasuho-config", "version": 1, "sections": {
            "birthdays": {"enabled": True, "role_id": {"id": "5", "name": "Mods"}},
        },
    }
    target = Guild(4, roles=[Role(55, "Mods", value=ADMIN)])  # below Yasuho: grantable
    data = json.dumps(payload).encode()
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(data))
    assert plans[0].new["role_id"] is None
    assert [n.outcome for n in plans[0].notes] == ["dangerous"]
    # Negative control: with the guard neutralised the same role WOULD be kept.
    monkeypatch.setattr(role_audit, "refuse_dangerous_role", lambda *a, **k: False)
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(data))
    assert plans[0].new["role_id"] == 55


async def test_feature_role_rules_are_reapplied():
    pool = SettingsPool()
    payload = {
        "format": "yasuho-config", "version": 1, "sections": {
            "birthdays": {"enabled": True, "role_id": {"id": "1", "name": "Above"}},
            "event_reminders": {"enabled": True, "role_id": {"id": "2", "name": "Quiet"}},
            "suggestions": {"staff_role_id": {"id": "3", "name": "@everyone"}},
        },
    }
    target = Guild(5, roles=[Role(51, "Above", position=60), Role(52, "Quiet", mentionable=False)])
    plans = by_name(await ct.build_plan(pool, target, member(), ct.parse_document(json.dumps(payload).encode())))
    assert plans["birthdays"].new["role_id"] is None  # above Yasuho: she cannot hand it out
    assert plans["event_reminders"].new["role_id"] is None  # nobody can ping it
    assert plans["suggestions"].new["staff_role_id"] is None  # @everyone
    assert {n.outcome for p in plans.values() for n in p.notes} == {"unusable"}


# -- strict parsing ---------------------------------------------------------------
@pytest.mark.parametrize(
    "data,reason",
    [
        (b"x" * (ct.MAX_BYTES + 1), "too_large"),
        (b"not json", "malformed"),
        (b"\xff\xfe", "malformed"),
        (b"[" * 60000, "malformed"),  # recursion bomb
        (b"[]", "malformed"),
        (json.dumps({"format": "other", "version": 1, "sections": {}}).encode(), "malformed"),
        (json.dumps({"format": "yasuho-config", "version": 2, "sections": {}}).encode(), "version"),
        (json.dumps({"format": "yasuho-config", "version": True, "sections": {}}).encode(), "version"),
        (json.dumps({"format": "yasuho-config", "version": 1, "sections": {"secrets": {}}}).encode(), "malformed"),
        (json.dumps({"format": "yasuho-config", "version": 1, "sections": {"welcome": []}}).encode(), "malformed"),
        (
            json.dumps({"format": "yasuho-config", "version": 1, "sections": {"welcome": {"channel_id": 11}}}).encode(),
            "malformed",
        ),
        (
            json.dumps(
                {"format": "yasuho-config", "version": 1, "sections": {"welcome": {"channel_id": {"id": "abc"}}}}
            ).encode(),
            "malformed",
        ),
        (
            json.dumps(
                {"format": "yasuho-config", "version": 1,
                 "sections": {"welcome": {"channel_id": {"id": "1", "name": "n" * 101}}}}
            ).encode(),
            "malformed",
        ),
    ],
)
def test_bad_files_are_refused(data, reason):
    with pytest.raises(ct.DocumentError) as caught:
        ct.parse_document(data)
    assert caught.value.reason == reason


@pytest.mark.parametrize("junk", [None, 1, "x", [], [1, {}], {"a": {"b": []}}, {"embed": [1], "gifs": "x"}])
def test_every_section_cleaner_survives_junk(junk):
    for spec in ct.SECTIONS.values():
        values = {key: junk for key in (*spec.keys, "enabled", "message", "embed", "gifs", "card_color", "hour")}
        cleaned = spec.clean(values)
        assert isinstance(cleaned, dict)
        json.dumps(cleaned)  # always storable


def test_welcome_cleaner_types_the_embed_and_caps_the_gifs():
    cleaned = ct._clean_welcome(
        {
            "enabled": 1,
            "gifs": ["https://a/b.gif", "javascript:x", 5] + ["https://c/d.gif"] * 100,
            "embed": {"title": {"x": 1}, "color": 2**40, "fields": [{"name": 3, "value": "v"}] * 40},
        }
    )
    assert cleaned["enabled"] is True
    # The welcome UI's rule: http(s) and <= 1024 characters, no count cap.
    assert cleaned["gifs"][0] == "https://a/b.gif" and len(cleaned["gifs"]) == 101
    assert cleaned["embed"]["title"] == "" and cleaned["embed"]["color"] is None
    assert len(cleaned["embed"]["fields"]) == 25 and cleaned["embed"]["fields"][0]["name"] == ""


# -- permissions at the target --------------------------------------------------
async def test_a_channel_without_my_permissions_skips_its_section_with_a_reason():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    data = await exported(pool, source_guild())
    target = target_guild(bot=perms(embed_links=False))
    plans = by_name(await ct.build_plan(pool, target, member(), ct.parse_document(data)))
    welcome = plans["welcome"]
    assert welcome.status == ct.SKIPPED and welcome.problem[0] == "bot" and welcome.problem[2] == ["embed_links"]
    assert "Embed Links" in ct.render_preview([welcome])
    results = dict((p.name, outcome) for p, outcome in await ct.apply_plan(make_bot(pool), target, list(plans.values())))
    assert results["welcome"] == ct.SKIPPED and results["tickets"] == "applied"
    assert "welcome" not in pool.rows[2]


async def test_a_channel_the_manager_cannot_see_is_never_matched_nor_named():
    """Review P3: neither the id nor a name match may reveal a hidden channel."""
    pool = SettingsPool()
    hidden = Channel(50, "secret-staff", member=perms(view_channel=False))
    guild = Guild(1, channels=[hidden])
    for ref in ({"id": "1", "name": "secret-staff"}, {"id": "50", "name": None}):
        raw = json.dumps({"format": "yasuho-config", "version": 1, "sections": {
            "milestones": {"enabled": True, "channel_id": ref}}}).encode()
        plans = await ct.build_plan(pool, guild, member(), ct.parse_document(raw))
        assert plans[0].new["channel_id"] is None
        assert [n.outcome for n in plans[0].notes] == ["unmapped"]
        assert "<#50>" not in ct.render_preview(plans)


async def test_export_omits_the_names_of_channels_the_exporter_cannot_see():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    guild = source_guild()
    guild.channels[0].member_perms = perms(view_channel=False)  # #welcome
    document = json.loads(await exported(pool, guild))
    assert document["sections"]["welcome"]["channel_id"] == {"id": "11", "name": None}
    assert document["sections"]["tickets"][tickets_config.KEY_LOG_CHANNEL]["name"] == "ticket-logs"


async def test_serverstats_holds_the_manager_to_its_configurer_rule():
    """Review P2: /serverstats digest set needs Send Messages from the MANAGER too."""
    pool = SettingsPool()
    announcements = Channel(60, "announcements", member=perms(send_messages=False))
    guild = Guild(1, channels=[announcements])
    raw = json.dumps({"format": "yasuho-config", "version": 1, "sections": {
        "serverstats": {"serverstats_digest_channel": {"id": "60", "name": "announcements"}}}}).encode()
    plans = await ct.build_plan(pool, guild, member(), ct.parse_document(raw))
    assert plans[0].status == ct.SKIPPED and plans[0].problem == ("member", announcements, ["send_messages"])
    assert "you need Send Messages in <#60>" in ct.render_preview(plans)
    # The other channel refs keep the plain "can see it" rule of their own setters.
    assert {ref.member_perms for spec in ct.SECTIONS.values() for ref in spec.refs
            if ref.kind == "channel" and spec.name != "serverstats"} == {("view_channel",)}


async def test_a_failing_section_does_not_stop_the_others(monkeypatch):
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    data = await exported(pool, source_guild())
    target = target_guild()
    plans = await ct.build_plan(pool, target, member(), ct.parse_document(data))
    real = ct.write_section

    async def flaky(pool_, gid, spec, values):
        if spec.name == "music":
            raise RuntimeError("boom")
        await real(pool_, gid, spec, values)

    monkeypatch.setattr(ct, "write_section", flaky)
    results = {p.name: outcome for p, outcome in await ct.apply_plan(make_bot(pool), target, plans)}
    assert results["music"] == "failed" and results["welcome"] == "applied"
    assert "**Music**: failed" in ct.render_results([(p, results[p.name]) for p in plans])


# -- the confirm view -------------------------------------------------------------
class Response:
    def __init__(self):
        self.sent, self.deferred, self.edited = [], False, []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


def interaction(guild, user):
    inter = types.SimpleNamespace(
        guild=guild, guild_id=guild.id, user=user, client=None, response=Response(), edits=[]
    )

    async def edit_original_response(**kwargs):
        inter.edits.append(kwargs)

    inter.edit_original_response = edit_original_response
    return inter


async def _view_for_cross_import(limits=premium.GUILD_PREMIUM):
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    parsed = ct.parse_document(await exported(pool, source_guild()))
    target = target_guild()
    bot = make_bot(pool, limits)
    pool.writes.clear()
    return pool, bot, target, ct.ImportConfirmView(bot, target, 7, parsed)


async def test_only_the_author_may_confirm():
    pool, bot, target, view = await _view_for_cross_import()
    stranger = interaction(target, member(8))
    assert await view.interaction_check(stranger) is False
    assert stranger.response.sent  # told, ephemerally
    assert pool.writes == []


async def test_confirm_applies_after_rechecking_and_only_once():
    pool, bot, target, view = await _view_for_cross_import()
    click = interaction(target, member(7))
    assert await view.interaction_check(click) is True
    await view.apply_button.callback(click)
    assert pool.rows[2]["welcome"]["channel_id"] == 111
    assert click.edits and all(child.disabled for child in view.children)
    writes = list(pool.writes)
    await view.apply_button.callback(interaction(target, member(7)))  # a double click
    assert pool.writes == writes


async def test_confirm_refused_when_the_manager_lost_manage_server():
    pool, bot, target, view = await _view_for_cross_import()
    click = interaction(target, member(7, manage_guild=False))
    await view.apply_button.callback(click)
    assert pool.writes == [] and click.response.sent


async def test_confirm_refused_with_the_upsell_when_premium_lapsed(monkeypatch):
    pool, bot, target, view = await _view_for_cross_import()
    bot.premium = Premium(premium.GUILD_FREE)  # lapsed between preview and click
    seen = []

    async def refusal(bot_, **kwargs):
        seen.append(kwargs["limit_key"])

    monkeypatch.setattr(premium_upsell, "for_guild_refusal", refusal)
    click = interaction(target, member(7))
    await view.apply_button.callback(click)
    assert seen == ["config_import"] and pool.writes == [] and click.response.sent


async def test_the_click_revalidates_against_the_guild_as_it_is_then():
    pool, bot, target, view = await _view_for_cross_import()
    target.channels[0].bot_perms = perms(send_messages=False)  # changed after the preview
    await view.apply_button.callback(interaction(target, member(7)))
    assert "welcome" not in pool.rows.get(2, {})
    assert pool.rows[2][music_config.KEY_DJ_ROLE] == 122


async def test_cancel_writes_nothing():
    pool, bot, target, view = await _view_for_cross_import()
    click = interaction(target, member(7))
    await view.cancel_button.callback(click)
    assert pool.writes == [] and click.response.edited


# -- commands: premium, cooldown, deadline ----------------------------------------
class Ctx:
    def __init__(self, guild, author, bot):
        self.guild, self.author, self.bot = guild, author, bot
        self.sent, self.deferred = [], False
        self.interaction = types.SimpleNamespace()  # a slash invocation

    async def defer(self, **kwargs):
        self.deferred = True

    async def send(self, content=None, **kwargs):
        assert self.deferred or not kwargs.get("file"), "work before the defer"
        self.sent.append((content, kwargs))
        return types.SimpleNamespace()


class Attachment:
    def __init__(self, data):
        self.data, self.size, self.reads = data, len(data), 0

    async def read(self):
        self.reads += 1
        return self.data


@pytest.fixture
def refusals(monkeypatch):
    seen = []

    async def refuse(bot, ctx, text, *, limit_key, kind="raise", benefit=None):
        seen.append(limit_key)

    monkeypatch.setattr(premium_upsell, "refuse_in_context", refuse)
    return seen


async def test_import_is_yasuho_plus_and_refused_before_reading_the_file(refusals):
    pool = SettingsPool()
    bot = make_bot(pool, premium.GUILD_FREE)
    attachment = Attachment(b"{}")
    await ct.import_command(bot, Ctx(target_guild(), member(), bot), attachment)
    assert refusals == ["config_import"] and attachment.reads == 0


async def test_export_is_free():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    bot = make_bot(pool, premium.GUILD_FREE)
    ctx = Ctx(source_guild(), member(), bot)
    await ct.export_command(bot, ctx)
    assert ctx.deferred and ctx.sent[0][1]["file"].filename == "yasuho-config.json"


async def test_import_defers_then_previews_and_is_rate_limited_per_guild():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    data = await exported(pool, source_guild())
    bot = make_bot(pool)
    ctx = Ctx(target_guild(), member(), bot)
    await ct.import_command(bot, ctx, Attachment(data))
    assert ctx.deferred and isinstance(ctx.sent[0][1]["view"], ct.ImportConfirmView)
    again = Ctx(target_guild(), member(), bot)
    attachment = Attachment(data)
    await ct.import_command(bot, again, attachment)
    assert attachment.reads == 0 and "wait" in again.sent[0][0]


async def test_an_oversize_attachment_is_refused_without_downloading_it():
    bot = make_bot(SettingsPool())
    ctx = Ctx(target_guild(), member(), bot)
    attachment = Attachment(b"x")
    attachment.size = ct.MAX_BYTES + 1
    await ct.import_command(bot, ctx, attachment)
    assert attachment.reads == 0 and "too large" in ctx.sent[0][0]


async def test_template_save_needs_yasuho_plus(refusals):
    bot = make_bot(SettingsPool(), premium.GUILD_FREE)
    await ct.template_save_command(bot, Ctx(target_guild(), member(), bot), "base")
    assert refusals == ["max_config_templates"]


async def test_after_a_lapse_templates_are_listed_and_deletable_but_not_applied(refusals, monkeypatch):
    store = {(2, "base"): "{}"}

    async def fake_list(pool, gid):
        return [{"name": n, "created_at": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}
                for (g, n) in store if g == gid]

    async def fake_delete(pool, gid, name):
        return store.pop((gid, name), None) is not None

    async def fake_load(pool, gid, name):  # pragma: no cover - must not be reached
        raise AssertionError("a lapsed server loaded a template")

    monkeypatch.setattr(ct, "list_templates", fake_list)
    monkeypatch.setattr(ct, "delete_template", fake_delete)
    monkeypatch.setattr(ct, "load_template", fake_load)
    bot = make_bot(SettingsPool(), premium.GUILD_FREE)
    guild = target_guild()
    await ct.template_apply_command(bot, Ctx(guild, member(), bot), "base")
    assert refusals == ["config_import"] and (2, "base") in store
    listed = Ctx(guild, member(), bot)
    await ct.template_list_command(bot, listed)
    assert "base" in listed.sent[0][1]["embed"].description
    await ct.template_delete_command(bot, Ctx(guild, member(), bot), "base")
    assert store == {}


async def test_template_apply_previews_through_the_same_flow(monkeypatch):
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    payload = (await exported(pool, source_guild())).decode()

    async def fake_load(pool_, gid, name):
        assert (gid, name) == (2, "base")
        return payload

    monkeypatch.setattr(ct, "load_template", fake_load)
    bot = make_bot(pool)
    ctx = Ctx(target_guild(), member(), bot)
    await ct.template_apply_command(bot, ctx, "  base ")
    assert isinstance(ctx.sent[0][1]["view"], ct.ImportConfirmView)


async def test_a_corrupt_stored_template_is_refused(monkeypatch):
    async def fake_load(pool_, gid, name):
        return {"format": "yasuho-config", "version": 99}

    monkeypatch.setattr(ct, "load_template", fake_load)
    bot = make_bot(SettingsPool())
    ctx = Ctx(target_guild(), member(), bot)
    await ct.template_apply_command(bot, ctx, "base")
    assert "can no longer be read" in ctx.sent[0][0]


async def test_template_save_reports_a_full_server(monkeypatch):
    async def full(pool, gid, name, payload, cap):
        assert cap == premium.GUILD_PREMIUM.max_config_templates
        return "full"

    monkeypatch.setattr(ct, "save_template", full)
    bot = make_bot(SettingsPool())
    ctx = Ctx(target_guild(), member(), bot)
    await ct.template_save_command(bot, ctx, "base")
    assert "10 templates" in ctx.sent[0][0]


@pytest.mark.parametrize("name,expected", [("  a  b ", "a b"), ("", None), ("x" * 33, None), ("x" * 32, "x" * 32)])
def test_template_names(name, expected):
    assert ct.clean_template_name(name) == expected


# -- review fixes (V5d security review) --------------------------------------------
MANAGE_MESSAGES = 1 << 13
KICK_MEMBERS = 1 << 1


async def test_a_moderator_staff_role_survives_a_same_server_round_trip():
    """Review P2: only a role Yasuho HANDS OUT goes through refuse_dangerous_role."""
    pool = SettingsPool()
    mod = MANAGE_MESSAGES | KICK_MEMBERS
    guild = Guild(1, channels=[Channel(10, "logs"), Channel(11, "suggest")],
                  roles=[Role(20, "Support", value=mod), Role(21, "DJ", value=mod)])
    pool.rows[1] = {
        tickets_config.KEY_SUPPORT_ROLE: 20, tickets_config.KEY_LOG_CHANNEL: 10,
        music_config.KEY_DJ_ROLE: 21,
        "suggestions": {"enabled": True, "channel_id": 11, "staff_role_id": 20},
    }
    plans = await ct.build_plan(pool, guild, member(), ct.parse_document(await exported(pool, guild)))
    assert {p.name: p.status for p in plans} == {
        "tickets": ct.UNCHANGED, "music": ct.UNCHANGED, "suggestions": ct.UNCHANGED}
    assert [ref.field for spec in ct.SECTIONS.values() for ref in spec.refs if ref.grant] == ["role_id"]
    assert ct.SECTIONS["birthdays"].refs[1].grant


async def test_the_prefix_form_is_refused_because_it_cannot_be_private():
    pool = SettingsPool()
    configure(pool, 1, SOURCE_IDS)
    bot = make_bot(pool)
    for run in (
        lambda ctx: ct.export_command(bot, ctx),
        lambda ctx: ct.import_command(bot, ctx, Attachment(b"{}")),
        lambda ctx: ct.template_apply_command(bot, ctx, "base"),
    ):
        ctx = Ctx(source_guild(), member(), bot)
        ctx.interaction = None
        await run(ctx)
        assert ctx.sent == [("Please use the slash command for this, so the answer stays private.", {})]


@pytest.mark.parametrize("bad", ['"a\\u0000b"', '"\\ud800"', '"x\\udfffy"'])
def test_nul_and_lone_surrogates_are_refused_anywhere(bad):
    for template in (
        '{"format":"yasuho-config","version":1,"sections":{"youtube":{"message":%s}}}',
        '{"format":"yasuho-config","version":1,"sections":{"welcome":{"embed":{"fields":[{"name":%s}]}}}}',
        '{"format":"yasuho-config","version":1,"sections":{"youtube":{%s:1}}}',
    ):
        with pytest.raises(ct.DocumentError):
            ct.parse_document((template % bad).encode())


def test_a_reference_name_with_a_line_break_is_refused():
    for name in ("a\nb", "a\rb"):
        raw = json.dumps({"format": "yasuho-config", "version": 1, "sections": {
            "welcome": {"channel_id": {"id": "1", "name": name}}}}).encode()
        with pytest.raises(ct.DocumentError):
            ct.parse_document(raw)


def test_a_byte_order_mark_is_accepted():
    raw = b"\xef\xbb\xbf" + json.dumps({"format": "yasuho-config", "version": 1, "sections": {}}).encode()
    assert ct.parse_document(raw) == {}


async def test_a_flat_section_is_written_in_one_statement():
    pool = SettingsPool()
    pool.rows[2] = {tickets_config.KEY_SUPPORT_ROLE: 121, "other": 1}
    spec = ct.SECTIONS["tickets"]
    await ct.write_section(pool, 2, spec, spec.clean({tickets_config.KEY_MAX_OPEN_PER_USER: 3}))
    assert len(pool.writes) == 1
    assert pool.rows[2] == {tickets_config.KEY_MAX_OPEN_PER_USER: 3, "other": 1}


async def test_the_preview_shows_changed_text_excerpted_and_defused():
    pool = SettingsPool()
    text = "@everyone **free** <@&123456789012345678> " + "x" * 200
    raw = json.dumps({"format": "yasuho-config", "version": 1, "sections": {
        "youtube": {"message": text},
        "welcome": {"embed": {"description": "Hello\nthere", "fields": [{"name": "Rules", "value": "Be nice"}]}},
    }}).encode()
    plans = await ct.build_plan(pool, target_guild(), member(), ct.parse_document(raw))
    preview = ct.render_preview(plans)
    youtube_line = next(line for line in preview.splitlines() if line.startswith("- `message`"))
    assert "@everyone" not in youtube_line and "<@&123456789012345678>" not in youtube_line
    assert "\\*\\*free" in youtube_line and youtube_line.endswith('..."')
    assert len(youtube_line) < 160
    assert '- `embed.description`: "Hello there"' in preview
    assert '- `embed.fields`: "Rules Be nice"' in preview
    # An unchanged text is not repeated.
    pool.rows[2] = {"youtube": {"message": text}}
    settings.invalidate_all()
    plans = await ct.build_plan(pool, target_guild(), member(), ct.parse_document(raw))
    assert "`message`" not in ct.render_preview([p for p in plans if p.name == "youtube"])


async def test_the_result_lists_the_remaps_as_rebuilt_at_the_click():
    pool, bot, target, view = await _view_for_cross_import()
    target.channels[0].name = "renamed"  # #welcome renamed between preview and click
    click = interaction(target, member(7))
    await view.apply_button.callback(click)
    description = click.edits[0]["embed"].description
    assert "`channel_id`: welcome dropped, nothing with that name here" in description
    assert "`tickets_log_channel`: ticket-logs -> <#112> (matched by name)" in description


def _maximal_export():
    """Every exported text at its limit, as 6-byte JSON escapes (the worst case),
    plus 50 max-length gifs - the bound MAX_BYTES is sized from."""
    worst = "\x01"
    limits = ct.embed_creator
    embed = {
        "title": worst * limits.LIMIT_TITLE,
        "description": worst * limits.LIMIT_DESC,
        "color": 0xFFFFFF,
        "author": {"name": worst * limits.LIMIT_AUTHOR, "icon": "h" * 1024},
        "footer": {"text": worst * limits.LIMIT_FOOTER, "icon": "h" * 1024},
        "thumbnail": "h" * 1024,
        "image": "h" * 1024,
        "fields": [
            {"name": worst * limits.LIMIT_FIELD_NAME, "value": worst * limits.LIMIT_FIELD_VALUE, "inline": True}
        ] * limits.LIMIT_FIELDS,
    }
    ref = {"id": "1" * 20, "name": worst * 100}
    return {
        "format": "yasuho-config", "version": 1, "exported_at": "2026-10-30T00:00:00+00:00",
        "source_guild_id": "1" * 20,
        "sections": {
            "language": {"locale": "fr"},
            "welcome": {
                "channel_id": ref, "enabled": True, "ping": True, "card": True, "random_gif": True,
                "gifs": ["https://" + "g" * 1016] * 50,
                "card_greeting": worst * 200, "card_subtitle": worst * 200, "card_color": "#FFFFFF",
                "embed": embed,
            },
            "tickets": {tickets_config.KEY_PANEL_MESSAGE: worst * tickets_config.MAX_PANEL_MESSAGE_LENGTH,
                        tickets_config.KEY_SUPPORT_ROLE: ref, tickets_config.KEY_LOG_CHANNEL: ref},
            "milestones": {"message": worst * 500, "channel_id": ref, "milestones": [10_000_000] * 10},
            "birthdays": {"message": worst * 500, "channel_id": ref, "role_id": ref, "timezone": "x" * 64},
            "suggestions": {"channel_id": ref, "staff_role_id": ref,
                            "custom_statuses": [{"label": worst * 30, "colour": 0xFFFFFF}] * 5},
            "event_reminders": {"channel_id": ref, "role_id": ref, "offsets": [1440, 60, 15]},
            "youtube": {"role_id": ref, "message": worst * 500, "shorts": True},
            "music": {music_config.KEY_DJ_ROLE: ref},
            "serverstats": {key: ref for key in ct._STATS_KEYS},
        },
    }


def test_a_fully_loaded_export_fits_the_size_cap():
    from cogs.config import welcome_card

    assert welcome_card.MAX_LINE_LENGTH <= 200  # the card lines above are at least full
    data = ct.dump(_maximal_export())
    assert len(data) <= ct.MAX_BYTES
    ct.parse_document(data)  # and it parses


async def test_gifs_survive_a_welcome_change_elsewhere():
    pool = SettingsPool()
    gifs = ["https://g/%d.gif" % i for i in range(120)]
    pool.rows[2] = {"welcome": {"enabled": True, "channel_id": 111, "gifs": gifs}}
    raw = json.dumps({"format": "yasuho-config", "version": 1, "sections": {
        "welcome": {"enabled": False, "channel_id": {"id": "111", "name": "welcome"}, "gifs": gifs}}}).encode()
    plans = await ct.build_plan(pool, target_guild(), member(), ct.parse_document(raw))
    assert plans[0].changed == ("enabled",)
    await ct.apply_plan(make_bot(pool), target_guild(), plans)
    assert pool.rows[2]["welcome"]["gifs"] == gifs


def test_onboarding_points_at_the_panel_subcommand():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "cogs" / "system" / "onboarding.py").read_text()
    assert "Use `/config panel` to set up" in source and "Use `/config` to set up" not in source
