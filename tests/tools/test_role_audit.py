"""The role-grant audit: what it MUST flag, what it MUST clear, and how many.

``?roleaudit`` is a guard whose success condition is a SILENCE - an empty
report. That is the failure shape that stays green forever once the detector
quietly stops detecting: a broken sweep and a clean estate print the same
nothing. So the judgement was written as a PURE function of (role permissions,
role position, managed/default flags, Yasuho's top role) and this module aims
fixtures straight at it.

Three properties are pinned, in this order:

1. THE CASES IT MUST FLAG. Every one of the ten dangerous permissions, alone, on
   every one of the six surfaces. A mute role whose mask is merely non-empty. A
   configured role that no longer exists. A role Yasuho can no longer grant.
2. THE CASES IT MUST CLEAR. A benign mask, a silent mute role, a grantable
   position, an @everyone-relative tie-break - each with an exact finding count
   of zero, so a predicate that started flagging everything fails here too.
3. THE SWEEP ITSELF. Five queries whatever the guild count; an unavailable role
   cache reported UNKNOWN and never as clean; a failed surface stamping the
   report INCOMPLETE; and a report that states its own coverage even when it
   found nothing.
4. THE REPORT THE OWNER READS. The tier printed on every line, the tier counted
   in the header, the tier heading each guild block and the order the blocks
   come in - a renderer that prints an escalation as a NOTICE fails at the one
   job the tool has, and no assertion on the result OBJECT can see that.
5. THE SQL. The pool fake dispatches on "FROM <table>", so the rest of the query
   text is free: the columns are checked against schema.sql and the two JSONB
   paths against the cogs that write those blobs.

No Discord, no database, no network: :class:`RoleFacts` is plain data and the
pool is a dispatching fake.
"""

import ast
import os
import re

import discord
import pytest

from tools import role_audit as ra

# ---------------------------------------------------------------------------
# Fixtures - plain data, no Discord objects
# ---------------------------------------------------------------------------
BOT_TOP = ra.BotTop(position=10, role_id=5000)


def facts(**over):
    """A harmless role sitting safely below Yasuho: permissions 0, position 3."""
    base = dict(role_id=777, name="Members", permissions=0, position=3)
    base.update(over)
    return ra.RoleFacts(**base)


def perms(**flags):
    """The raw bitfield value for a set of named permission flags."""
    return discord.Permissions(**flags).value


def setting(surface=ra.SURFACE_AUTOROLE, role_id=777, label=""):
    return ra.Setting(surface, role_id, label)


def tiers(findings):
    return [f.tier for f in findings]


def reasons(findings):
    return [f.reason for f in findings]


# ---------------------------------------------------------------------------
# 1. dangerous_permissions - the bitfield half of the predicate
# ---------------------------------------------------------------------------
def test_the_dangerous_list_is_the_ten_the_audit_was_specified_with():
    """Pinned by name: dropping one is how a whole class of escalation goes dark."""
    assert set(ra.DANGEROUS_PERMISSIONS) == {
        "administrator",
        "manage_guild",
        "manage_roles",
        "manage_channels",
        "manage_webhooks",
        "ban_members",
        "kick_members",
        "moderate_members",
        "manage_messages",
        "mention_everyone",
    }
    assert len(ra.DANGEROUS_PERMISSIONS) == 10


@pytest.mark.parametrize("name", ra.DANGEROUS_PERMISSIONS)
def test_every_dangerous_permission_is_detected_on_its_own(name):
    """One flag, nothing else set: it must still come back named."""
    assert ra.dangerous_permissions(perms(**{name: True})) == (name,)


def test_a_harmless_mask_yields_nothing():
    """The negative control for the bitfield half - an exact empty tuple."""
    benign = perms(
        view_channel=True,
        send_messages=True,
        add_reactions=True,
        read_message_history=True,
        connect=True,
        speak=True,
        change_nickname=True,
    )
    assert benign != 0  # the mask really is populated
    assert ra.dangerous_permissions(benign) == ()


def test_an_empty_mask_yields_nothing():
    assert ra.dangerous_permissions(0) == ()


def test_several_dangerous_flags_come_back_in_declaration_order():
    got = ra.dangerous_permissions(
        perms(ban_members=True, manage_guild=True, send_messages=True)
    )
    assert got == ("manage_guild", "ban_members")


# ---------------------------------------------------------------------------
# 2. role_is_below / grant_status - the hierarchy half
# ---------------------------------------------------------------------------
def test_a_lower_position_is_below():
    assert ra.role_is_below(facts(position=3), BOT_TOP) is True


def test_a_higher_position_is_not_below():
    assert ra.role_is_below(facts(position=11), BOT_TOP) is False


def test_equal_positions_break_the_tie_on_id_the_way_discord_does():
    """discord.Role.__lt__: same position, the LARGER id is the LOWER role.

    Getting this backwards mis-answers every guild whose roles share a position,
    which is common - Discord does not renumber positions on a reorder.
    """
    later = facts(position=BOT_TOP.position, role_id=BOT_TOP.role_id + 1)
    earlier = facts(position=BOT_TOP.position, role_id=BOT_TOP.role_id - 1)
    assert ra.role_is_below(later, BOT_TOP) is True
    assert ra.role_is_below(earlier, BOT_TOP) is False


def test_grant_status_answers_the_three_cases():
    assert ra.grant_status(facts(position=3), BOT_TOP) == ra.GRANT_OK
    assert ra.grant_status(facts(position=99), BOT_TOP) == ra.GRANT_NO
    assert ra.grant_status(facts(managed=True), BOT_TOP) == ra.GRANT_NO
    assert ra.grant_status(facts(is_default=True), BOT_TOP) == ra.GRANT_NO


def test_a_missing_bot_member_is_unknown_not_false():
    """"I could not check" must never be rendered as an answer either way."""
    assert ra.grant_status(facts(position=3), None) == ra.GRANT_UNKNOWN


# ---------------------------------------------------------------------------
# 3. audit_setting - the cases it MUST flag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("surface", ra.SURFACE_ORDER)
def test_a_dangerous_permission_is_critical_on_every_surface(surface):
    found = ra.audit_setting(
        setting(surface=surface), facts(permissions=perms(manage_roles=True)), BOT_TOP
    )
    critical = [f for f in found if f.tier == ra.TIER_CRITICAL]
    assert len(critical) == 1
    assert critical[0].reason == ra.REASON_DANGEROUS
    assert critical[0].permissions == ("manage_roles",)
    assert critical[0].surface == surface


def test_administrator_alone_is_critical():
    """Discord implies every permission from it; discord.py's accessors do not."""
    found = ra.audit_setting(
        setting(), facts(permissions=perms(administrator=True)), BOT_TOP
    )
    assert tiers(found) == [ra.TIER_CRITICAL]
    assert found[0].permissions == ("administrator",)


def test_a_mute_role_with_a_benign_but_non_empty_mask_is_a_warning():
    """A mute role exists to REMOVE speech. Yasuho creates hers with value 0."""
    mask = perms(add_reactions=True)
    found = ra.audit_setting(
        setting(surface=ra.SURFACE_MUTEROLE), facts(permissions=mask), BOT_TOP
    )
    assert tiers(found) == [ra.TIER_WARNING]
    assert found[0].reason == ra.REASON_MUTE_NOT_SILENT
    assert found[0].permission_value == mask


def test_a_dangerous_mute_role_reports_both_facts():
    """Both lines: it is an escalation AND the mute-role invariant is broken."""
    found = ra.audit_setting(
        setting(surface=ra.SURFACE_MUTEROLE),
        facts(permissions=perms(manage_messages=True)),
        BOT_TOP,
    )
    assert tiers(found) == [ra.TIER_CRITICAL, ra.TIER_WARNING]
    assert reasons(found) == [ra.REASON_DANGEROUS, ra.REASON_MUTE_NOT_SILENT]


def test_a_deleted_role_is_a_notice():
    found = ra.audit_setting(setting(), None, BOT_TOP)
    assert tiers(found) == [ra.TIER_NOTICE]
    assert found[0].reason == ra.REASON_ROLE_MISSING
    assert found[0].role_id == 777


def test_a_role_above_yasuho_is_a_notice():
    found = ra.audit_setting(setting(), facts(position=99), BOT_TOP)
    assert tiers(found) == [ra.TIER_NOTICE]
    assert found[0].reason == ra.REASON_UNGRANTABLE


def test_an_integration_managed_role_is_a_notice():
    found = ra.audit_setting(setting(), facts(managed=True), BOT_TOP)
    assert reasons(found) == [ra.REASON_UNGRANTABLE]


def test_an_unknown_bot_top_role_is_reported_not_swallowed():
    found = ra.audit_setting(setting(), facts(), None)
    assert reasons(found) == [ra.REASON_GRANT_UNKNOWN]
    assert found[0].tier == ra.TIER_NOTICE


# ---------------------------------------------------------------------------
# 4. audit_setting - the cases it MUST clear (the negative controls)
# ---------------------------------------------------------------------------
def test_a_clean_grantable_role_produces_exactly_nothing():
    assert ra.audit_setting(setting(), facts(), BOT_TOP) == ()


def test_a_silent_mute_role_produces_exactly_nothing():
    """Value 0 is what cogs/moderation/mute_perms.role_permissions() creates."""
    assert ra.audit_setting(setting(surface=ra.SURFACE_MUTEROLE), facts(), BOT_TOP) == ()


def test_the_mute_role_yasuho_actually_creates_has_permission_value_zero():
    """Anchor the WARNING threshold to the real creation path, not to a guess."""
    from cogs.moderation import mute_perms

    assert mute_perms.role_permissions().value == ra.MUTE_ROLE_EXPECTED_PERMISSIONS
    assert ra.MUTE_ROLE_EXPECTED_PERMISSIONS == 0


def test_a_benign_mask_on_a_non_mute_surface_produces_nothing():
    """Only muterole is held to the empty-mask rule; an autorole may speak."""
    for surface in ra.SURFACE_ORDER:
        if surface == ra.SURFACE_MUTEROLE:
            continue
        found = ra.audit_setting(
            setting(surface=surface),
            facts(permissions=perms(send_messages=True, add_reactions=True)),
            BOT_TOP,
        )
        assert found == (), surface


def test_a_role_tied_on_position_but_below_by_id_is_clean():
    below = facts(position=BOT_TOP.position, role_id=BOT_TOP.role_id + 1)
    assert ra.audit_setting(setting(), below, BOT_TOP) == ()


# ---------------------------------------------------------------------------
# 5. audit_guild - the count
# ---------------------------------------------------------------------------
def _mixed_guild():
    """Six settings: two escalations, one drifted mute, one dead, two clean."""
    settings = [
        setting(ra.SURFACE_MUTEROLE, 1),          # WARNING (non-empty mask)
        setting(ra.SURFACE_AUTOROLE, 2),          # CRITICAL (manage_guild)
        setting(ra.SURFACE_VERIFY, 3),            # CRITICAL (administrator)
        setting(ra.SURFACE_LEVEL_REWARD, 4, "level 10"),  # NOTICE (deleted)
        setting(ra.SURFACE_SEASON_CHAMPION, 5),   # clean
        setting(ra.SURFACE_TWITCH_LIVE, 6),       # clean
    ]
    by_id = {
        1: facts(role_id=1, permissions=perms(add_reactions=True)),
        2: facts(role_id=2, permissions=perms(manage_guild=True)),
        3: facts(role_id=3, permissions=perms(administrator=True)),
        # 4 deliberately absent: the role was deleted.
        5: facts(role_id=5),
        6: facts(role_id=6),
    }
    return settings, by_id


def test_a_mixed_guild_produces_exactly_the_expected_findings():
    settings, by_id = _mixed_guild()
    found = ra.audit_guild(settings, by_id, BOT_TOP)

    assert len(found) == 4
    assert tiers(found).count(ra.TIER_CRITICAL) == 2
    assert tiers(found).count(ra.TIER_WARNING) == 1
    assert tiers(found).count(ra.TIER_NOTICE) == 1
    # Worst tier first, so the owner reads the escalations before the debris.
    assert tiers(found) == [
        ra.TIER_CRITICAL,
        ra.TIER_CRITICAL,
        ra.TIER_WARNING,
        ra.TIER_NOTICE,
    ]
    assert {f.surface for f in found if f.tier == ra.TIER_CRITICAL} == {
        ra.SURFACE_AUTOROLE,
        ra.SURFACE_VERIFY,
    }


def test_a_fully_clean_guild_produces_zero_findings():
    """The negative control at guild scale: six settings in, nothing out."""
    settings = [setting(s, 10 + i) for i, s in enumerate(ra.SURFACE_ORDER)]
    by_id = {s.role_id: facts(role_id=s.role_id) for s in settings}
    assert ra.audit_guild(settings, by_id, BOT_TOP) == ()


# ---------------------------------------------------------------------------
# 6. The sweep - bulk reads, unknowns, failures
# ---------------------------------------------------------------------------
class FakeRole:
    def __init__(self, role_id, name="Role", permissions=0, position=3,
                 managed=False, default=False):
        self.id = role_id
        self.name = name
        self.permissions = discord.Permissions(permissions)
        self.position = position
        self.managed = managed
        self._default = default

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, guild_id, name="Guild", roles=(), bot_top_position=10,
                 bot_top_id=5000, unavailable=False, has_me=True):
        self.id = guild_id
        self.name = name
        self.unavailable = unavailable
        top = FakeRole(bot_top_id, "Yasuho", position=bot_top_position)
        self._roles = {r.id: r for r in roles}
        self.roles = list(roles) + [top]
        if unavailable:
            self.roles = []
        self.me = type("Me", (), {"top_role": top})() if has_me else None

    def get_role(self, role_id):
        return self._roles.get(role_id)


class DispatchPool:
    """A pool that answers each of the five audit queries from its own table.

    Records every query so the test can assert the round-trip COUNT - the
    property that stops the sweep degenerating into one query per guild.
    """

    def __init__(self, muterole=(), autorole=(), guild_settings=(),
                 level_rewards=(), level_config=(), fail=()):
        self.tables = {
            "FROM muterole": list(muterole),
            "FROM autorole": list(autorole),
            "FROM guild_settings": list(guild_settings),
            "FROM level_rewards": list(level_rewards),
            "FROM level_config": list(level_config),
        }
        self.fail = set(fail)
        self.queries = []

    async def fetch(self, query):
        self.queries.append(query)
        for marker, rows in self.tables.items():
            if marker in query:
                if marker in self.fail:
                    raise RuntimeError("boom")
                return rows
        raise AssertionError("unexpected query: " + query)


class FakeBot:
    def __init__(self, pool, guilds):
        self.db_pool = pool
        self.guilds = list(guilds)


async def test_the_sweep_costs_five_queries_whatever_the_guild_count():
    """Bulk or nothing: 3 guilds and 300 guilds must cost the same round trips."""
    for count in (3, 300):
        pool = DispatchPool(
            muterole=[{"guild_id": i, "role_id": 1} for i in range(count)]
        )
        guilds = [
            FakeGuild(i, roles=[FakeRole(1, "Muted")]) for i in range(count)
        ]
        await ra.run_audit(FakeBot(pool, guilds))
        assert len(pool.queries) == 5, pool.queries


async def test_a_clean_estate_still_proves_the_sweep_ran():
    """The whole point: an empty report must carry its own coverage numbers."""
    pool = DispatchPool(
        muterole=[{"guild_id": 1, "role_id": 10}],
        autorole=[{"guild_id": 1, "role_id": 11}],
    )
    guild = FakeGuild(1, "Nice Place", roles=[FakeRole(10, "Muted"), FakeRole(11, "Member")])
    result = await ra.run_audit(FakeBot(pool, [guild]))

    assert result.flagged_reports == []
    assert result.guilds_scanned == 1
    assert result.settings_examined == 2
    assert result.complete is True

    text = ra.render_report(result)
    assert "Swept 1 guild(s); found 2 configured role setting(s), examined 2." in text
    # "audited setting", not "configured role": reaction roles, button roles and
    # role menus also hand out roles and are NOT in this sweep, so the clean
    # verdict must not sound like it cleared them.
    assert "Nothing flagged: every audited setting is safe to hand out." in text


async def test_an_exploited_guild_is_named_with_its_role_and_permission():
    pool = DispatchPool(
        autorole=[{"guild_id": 7, "role_id": 42}],
        guild_settings=[
            {"guild_id": 7, "verify_role": "43", "twitch_role": None}
        ],
        level_rewards=[{"guild_id": 7, "level": 10, "role_id": 44}],
        level_config=[{"guild_id": 7, "season_champion_role_id": 45}],
    )
    guild = FakeGuild(
        7,
        "Pwned",
        roles=[
            FakeRole(42, "Newcomer", permissions=perms(manage_guild=True)),
            FakeRole(43, "Verified", permissions=perms(ban_members=True)),
            FakeRole(44, "Regular"),
            FakeRole(45, "Champion"),
        ],
    )
    result = await ra.run_audit(FakeBot(pool, [guild]))

    assert result.settings_examined == 4
    assert result.count(ra.TIER_CRITICAL) == 2
    assert result.count(ra.TIER_WARNING) == 0
    assert len(result.flagged_reports) == 1

    text = ra.render_report(result)
    # The TIER the owner actually reads, not just the tier on the object. The
    # engine is defended above; these three assertions defend the artifact - a
    # renderer that printed every line as NOTICE, or a header that counted
    # CRITICAL 0 while listing two, would look reassuring and be wrong.
    assert "[CRITICAL] Pwned (7) - 4 setting(s) examined" in text
    assert "CRITICAL 2 | WARNING 0 | NOTICE 0" in text
    assert "CRITICAL autorole.role_id -> Newcomer (42) carries manage_guild" in text
    assert (
        "CRITICAL guild_settings.verify_role -> Verified (43) carries ban_members"
        in text
    )
    # The clean level reward and champion role are examined but not named.
    assert "Regular" not in text
    assert "Champion" not in text


async def test_the_report_prints_each_tier_where_that_tier_belongs():
    """One guild carrying all three tiers at once, read as the owner reads it.

    Asserting tiers on the RESULT OBJECT cannot catch a broken renderer, and a
    tool whose whole job is to make an escalation visible fails completely if it
    prints CRITICAL findings as NOTICEs or summarises them as zero. So this goes
    through render_report and checks the rendered tier of every line plus the
    header counts.
    """
    pool = DispatchPool(
        muterole=[{"guild_id": 7, "role_id": 41}],
        autorole=[{"guild_id": 7, "role_id": 42}],
        level_rewards=[{"guild_id": 7, "level": 10, "role_id": 44}],
    )
    mask = perms(add_reactions=True)
    guild = FakeGuild(
        7,
        "Pwned",
        roles=[
            FakeRole(41, "Muted", permissions=mask),
            FakeRole(42, "Newcomer", permissions=perms(administrator=True)),
            # 44 deliberately absent: the reward points at a deleted role.
        ],
    )
    result = await ra.run_audit(FakeBot(pool, [guild]))
    text = ra.render_report(result)

    assert "CRITICAL 1 | WARNING 1 | NOTICE 1" in text
    assert "CRITICAL autorole.role_id -> Newcomer (42) carries administrator" in text
    assert (
        "WARNING  muterole.role_id -> Muted (41) is a mute role with a "
        "non-empty permission mask (value {0})".format(mask) in text
    )
    assert "NOTICE   level_rewards.role_id [level 10] -> role 44 no longer exists" in text


async def test_a_guild_block_is_headed_by_its_WORST_tier_and_sorts_on_it():
    """worst_tier is a min over sort_key, never a max.

    A guild holding an administrator autorole AND one dead reward must head its
    block with CRITICAL and sort above a guild carrying only debris. Taking the
    mildest tier instead would file the worst guild in the estate underneath the
    quiet ones, where the owner stops reading.
    """
    pool = DispatchPool(
        autorole=[{"guild_id": 7, "role_id": 42}, {"guild_id": 8, "role_id": 52}],
        level_rewards=[{"guild_id": 7, "level": 10, "role_id": 44}],
    )
    bad = FakeGuild(
        7,
        "Zzz Pwned",
        roles=[FakeRole(42, "Newcomer", permissions=perms(administrator=True))],
    )
    # Named to sort FIRST on every other key (name, then guild id), so nothing
    # but the tier can put the exploited guild above it.
    dull = FakeGuild(8, "Aaa Dusty", roles=[FakeRole(52, "Gone", position=99)])
    result = await ra.run_audit(FakeBot(pool, [bad, dull]))

    assert result.reports[0].worst_tier == ra.TIER_CRITICAL
    assert result.reports[1].worst_tier == ra.TIER_NOTICE

    text = ra.render_report(result)
    assert "[CRITICAL] Zzz Pwned (7)" in text
    assert "[NOTICE] Aaa Dusty (8)" in text
    assert text.index("[CRITICAL] Zzz Pwned") < text.index("[NOTICE] Aaa Dusty")


async def test_the_dashboards_string_snowflakes_resolve_like_ints():
    """JavaScript cannot hold a snowflake in a Number, so the blob holds "42"."""
    pool = DispatchPool(
        guild_settings=[{"guild_id": "7", "verify_role": "42", "twitch_role": "42"}]
    )
    guild = FakeGuild(7, roles=[FakeRole(42, "Verified", permissions=perms(kick_members=True))])
    result = await ra.run_audit(FakeBot(pool, [guild]))
    assert result.settings_examined == 2
    assert result.count(ra.TIER_CRITICAL) == 2
    assert result.malformed_ids == 0


async def test_an_unavailable_role_cache_is_unknown_never_clean():
    """Silence must not read as safety: the settings are counted as UNEXAMINED."""
    pool = DispatchPool(
        muterole=[{"guild_id": 1, "role_id": 10}],
        autorole=[{"guild_id": 1, "role_id": 11}],
    )
    guild = FakeGuild(1, "Dark", unavailable=True)
    result = await ra.run_audit(FakeBot(pool, [guild]))

    assert result.settings_examined == 0
    assert result.settings_unexamined == 2
    assert len(result.unknown_reports) == 1
    assert result.flagged_reports == []
    assert result.complete is False

    text = ra.render_report(result)
    assert "[UNKNOWN] Dark (1)" in text
    assert "2 setting(s) not examined" in text
    assert "Nothing flagged: every audited setting is safe" not in text


async def test_a_guild_with_no_cached_bot_member_is_judged_but_flagged_unknown():
    """Permissions are still knowable; only grantability is not - say so."""
    pool = DispatchPool(autorole=[{"guild_id": 1, "role_id": 10}])
    guild = FakeGuild(1, roles=[FakeRole(10, "Member")], has_me=False)
    result = await ra.run_audit(FakeBot(pool, [guild]))
    assert result.settings_examined == 1
    assert result.count(ra.TIER_NOTICE) == 1
    assert reasons(result.flagged_reports[0].findings) == [ra.REASON_GRANT_UNKNOWN]


async def test_a_failed_surface_logs_its_name_and_nothing_else(caplog):
    """The one log line this module emits: a surface and an exception CLASS.

    No traceback either - ``exc_info`` would put an asyncpg row-decode failure's
    payload, i.e. the very ids and names this report is never allowed to persist,
    straight into the log file.
    """
    pool = DispatchPool(
        muterole=[{"guild_id": 1, "role_id": 10}], fail={"FROM guild_settings"}
    )
    guild = FakeGuild(1, "Secret Server", roles=[FakeRole(10, "Secret Role")])
    with caplog.at_level(0):
        await ra.run_audit(FakeBot(pool, [guild]))

    records = [r for r in caplog.records if "role audit" in r.getMessage()]
    assert len(records) == 1
    assert ra.SURFACE_VERIFY in records[0].getMessage()
    assert records[0].exc_info is None
    assert "Secret Server" not in caplog.text
    assert "Secret Role" not in caplog.text
    assert "Traceback" not in caplog.text


async def test_a_failed_surface_stamps_the_report_incomplete():
    pool = DispatchPool(
        muterole=[{"guild_id": 1, "role_id": 10}], fail={"FROM level_rewards"}
    )
    guild = FakeGuild(1, roles=[FakeRole(10, "Muted")])
    result = await ra.run_audit(FakeBot(pool, [guild]))

    assert result.failed_surfaces == (ra.SURFACE_LEVEL_REWARD,)
    assert result.complete is False
    text = ra.render_report(result)
    assert "INCOMPLETE" in text
    assert "Nothing flagged: every audited setting is safe" not in text


async def test_the_books_close_every_found_setting_is_accounted_for():
    """found == examined + unexamined + orphan. A setting cannot go missing.

    This is the accounting that makes an empty finding list mean something: a
    sweep that silently dropped rows would leave the sum short, and the numbers
    it prints would stop adding up in the owner's own report.
    """
    pool = DispatchPool(
        muterole=[
            {"guild_id": 1, "role_id": 10},   # examined
            {"guild_id": 2, "role_id": 20},   # unexamined (dark guild)
            {"guild_id": 99, "role_id": 30},  # orphan (not in this bot's guilds)
        ],
        autorole=[{"guild_id": 1, "role_id": 11}],  # examined
        level_rewards=[{"guild_id": 2, "level": 3, "role_id": 21}],  # unexamined
    )
    guilds = [
        FakeGuild(1, "Lit", roles=[FakeRole(10, "Muted"), FakeRole(11, "Member")]),
        FakeGuild(2, "Dark", unavailable=True),
    ]
    result = await ra.run_audit(FakeBot(pool, guilds))

    assert result.settings_found == 5
    assert result.settings_examined == 2
    assert result.settings_unexamined == 2
    assert result.orphan_rows == 1
    assert (
        result.settings_found
        == result.settings_examined + result.settings_unexamined + result.orphan_rows
    )


async def test_rows_for_guilds_the_bot_left_are_counted_not_judged():
    pool = DispatchPool(
        autorole=[{"guild_id": 1, "role_id": 10}, {"guild_id": 999, "role_id": 10}]
    )
    guild = FakeGuild(1, roles=[FakeRole(10, "Member")])
    result = await ra.run_audit(FakeBot(pool, [guild]))
    assert result.orphan_rows == 1
    assert result.settings_examined == 1


async def test_a_junk_role_id_is_counted_not_silently_dropped():
    pool = DispatchPool(
        guild_settings=[{"guild_id": 1, "verify_role": "not-an-id", "twitch_role": None}]
    )
    guild = FakeGuild(1, roles=[])
    result = await ra.run_audit(FakeBot(pool, [guild]))
    assert result.malformed_ids == 1
    assert result.settings_examined == 0
    assert "unreadable role id(s)" in ra.render_report(result)


# ---------------------------------------------------------------------------
# 7. Rendering hygiene
# ---------------------------------------------------------------------------
def test_a_backtick_in_a_guild_name_cannot_break_out_of_the_code_block():
    assert "`" not in ra.plain("Evil```py\nimport os")
    assert "\n" not in ra.plain("Evil```py\nimport os")


def test_every_surface_has_a_location_label():
    """A flagged line doubles as the grep key for going and fixing the row."""
    for surface in ra.SURFACE_ORDER:
        assert ra.SURFACE_LOCATIONS[surface]
    assert len(ra.SURFACE_LOCATIONS) == len(ra.SURFACE_ORDER) == 6


# ---------------------------------------------------------------------------
# 8. The SQL itself - grounded, never asserted against the fake
# ---------------------------------------------------------------------------
# DispatchPool above matches on the substring "FROM <table>", which makes
# EVERYTHING ELSE in the query text free: a renamed column, an alias the row
# handler does not index, a dropped WHERE or a JSON path pointed at a key nobody
# writes all keep the suite green while that surface returns zero rows forever.
# A surface silently not scanned is the precise bug this whole tool exists to
# avoid, so the queries are checked against the three things that can drift
# underneath them: the DDL in schema.sql, the keys the row handlers index, and
# the blob keys the cogs that WRITE these settings actually use.
SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "schema.sql",
)

AUDIT_QUERIES = {
    "muterole": ra._Q_MUTEROLE,
    "autorole": ra._Q_AUTOROLE,
    "guild_settings": ra._Q_GUILD_SETTINGS,
    "level_rewards": ra._Q_LEVEL_REWARDS,
    "level_config": ra._Q_SEASON_CHAMPION,
}

# The row keys load_settings' handlers index, per table. A projection that stops
# returning one of these raises KeyError against a real asyncpg Record while the
# dict-based fake never notices.
HANDLER_KEYS = {
    "muterole": {"guild_id", "role_id"},
    "autorole": {"guild_id", "role_id"},
    "guild_settings": {"guild_id", "verify_role", "twitch_role"},
    "level_rewards": {"guild_id", "level", "role_id"},
    "level_config": {"guild_id", "season_champion_role_id"},
}


def _ddl_columns(table):
    """The column names schema.sql declares for one table."""
    source = open(SCHEMA_PATH, encoding="utf-8").read()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS " + table + r"\s*\((.*?)\n\);",
        source,
        re.DOTALL,
    )
    assert match, "no CREATE TABLE for " + table + " in schema.sql"
    columns = set()
    for line in match.group(1).splitlines():
        line = line.split("--")[0].strip()
        head = line.split()[0] if line else ""
        if head and head.upper() not in (
            "PRIMARY",
            "UNIQUE",
            "FOREIGN",
            "CHECK",
            "CONSTRAINT",
        ):
            columns.add(head.strip(","))
    return columns


def _projection(query):
    """{returned column name: the expression producing it} for one SELECT."""
    select = query.split("SELECT", 1)[1].split("FROM", 1)[0]
    out = {}
    for item in select.split(","):
        item = " ".join(item.split())
        expr, _sep, name = item.partition(" AS ")
        out[(name or expr).strip()] = expr.strip()
    return out


def _settings_blob_keys(module):
    """The literal blob keys a cog passes to settings.get_guild / set_guild.

    Read off the cog's own AST rather than restated here, so renaming the key in
    the cog that owns the blob breaks this test instead of blinding the audit.
    """
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("get_guild", "set_guild"):
            continue
        if len(node.args) < 3 or not isinstance(node.args[2], ast.Constant):
            continue
        if isinstance(node.args[2].value, str):
            keys.add(node.args[2].value)
    return keys


@pytest.mark.parametrize("table", sorted(AUDIT_QUERIES))
def test_each_query_returns_exactly_the_names_its_row_handler_indexes(table):
    """An alias the handler does not index is a KeyError against a real Record."""
    query = AUDIT_QUERIES[table]
    assert "FROM " + table in query
    assert set(_projection(query)) == HANDLER_KEYS[table]


@pytest.mark.parametrize("table", sorted(AUDIT_QUERIES))
def test_every_column_the_audit_reads_exists_in_the_schema(table):
    """A column renamed in schema.sql would blind its surface, silently."""
    declared = _ddl_columns(table)
    for name, expr in _projection(AUDIT_QUERIES[table]).items():
        # "settings->'twitch'->>'role_id'" reads the settings COLUMN.
        root = expr.split("->")[0].strip()
        assert root in declared, (table, name, root, sorted(declared))


def test_the_guild_settings_query_reads_the_keys_the_cogs_actually_write():
    """The two JSONB surfaces, tied to the cogs that own those blobs.

    ``verify_role`` and the Twitch Live role live INSIDE a JSONB blob rather
    than in a column, so nothing in the DDL constrains them. The only thing
    making ``settings->'twitch'->>'role_id'`` correct is that
    cogs/config/twitch.py stores its blob under the key ``twitch`` with a
    ``role_id`` in it; rename either end and the sweep reports a clean estate
    forever, which is exactly the silence this tool must never produce.
    """
    from cogs.config import twitch, verification

    assert _settings_blob_keys(verification) == {"verify_role"}
    assert _settings_blob_keys(twitch) == {"twitch"}
    # role_id is a key of the blob twitch.py itself defines.
    assert "role_id" in twitch._default_config()

    projection = _projection(ra._Q_GUILD_SETTINGS)
    assert projection["verify_role"] == "settings->>'verify_role'"
    assert projection["twitch_role"] == "settings->'twitch'->>'role_id'"


def test_the_guild_settings_query_keeps_only_rows_that_configure_something():
    """Both keys in the WHERE, so a guild with neither never leaves the database.

    ``->>`` yields SQL NULL for a missing key AND for a JSON null, which is
    exactly how both surfaces are switched off - dropping either arm would
    either leak unrelated rows or blind one surface.
    """
    where = ra._Q_GUILD_SETTINGS.split("WHERE", 1)[1]
    assert "settings->>'verify_role' IS NOT NULL" in where
    assert "settings->'twitch'->>'role_id' IS NOT NULL" in where
    assert " OR " in where


def test_the_season_champion_query_skips_the_guilds_that_never_set_one():
    """level_config holds a row per levelling guild; almost none set the role."""
    where = ra._Q_SEASON_CHAMPION.split("WHERE", 1)[1]
    assert "season_champion_role_id IS NOT NULL" in where


def test_the_unfiltered_surfaces_are_read_WHOLE():
    """muterole/autorole/level_rewards: every row IS a configured setting.

    A WHERE or a LIMIT creeping into one of these is the silent under-report the
    coverage numbers exist to expose - and it would leave this suite green,
    because the fake pool answers with its own rows whatever the query says.
    """
    for query in (ra._Q_MUTEROLE, ra._Q_AUTOROLE, ra._Q_LEVEL_REWARDS):
        assert "WHERE" not in query.upper()
    for query in AUDIT_QUERIES.values():
        assert "LIMIT" not in query.upper()
        assert query.endswith(";")


# ---------------------------------------------------------------------------
# 9. Orphans are not-knowing, not safety
# ---------------------------------------------------------------------------
async def test_rows_for_guilds_not_in_the_cache_stamp_the_sweep_incomplete():
    """?roleaudit is reachable before the guild cache has filled.

    Nothing gates command processing on ready, so a run during startup sees
    every setting as an "orphan" - a row for a guild that is not in
    ``bot.guilds`` YET. Those settings were never looked at, and an unlooked-at
    role may be exactly the dangerous one, so the verdict must not read as a
    clean bill of health.
    """
    pool = DispatchPool(
        autorole=[{"guild_id": 1, "role_id": 10}, {"guild_id": 999, "role_id": 11}]
    )
    guild = FakeGuild(1, "Lit", roles=[FakeRole(10, "Member")])
    result = await ra.run_audit(FakeBot(pool, [guild]))

    assert result.orphan_rows == 1
    assert result.flagged_reports == []
    assert result.complete is False

    text = ra.render_report(result)
    assert "Nothing flagged in what could be read" in text
    assert "Nothing flagged: every audited setting is safe" not in text
