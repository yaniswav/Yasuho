"""Owner audit: the roles Yasuho grants HERSELF, judged against what they carry.

Why this exists
---------------
Six stored settings name a role Yasuho hands out on her own initiative, with no
human in the loop at grant time:

* ``muterole.role_id``        - applied by ``?mute`` and re-applied on rejoin;
* ``autorole.role_id``        - applied to every member who joins;
* ``guild_settings.verify_role`` - applied on one click of a public button;
* ``level_rewards.role_id``   - applied on reaching a level;
* ``level_config.season_champion_role_id`` - applied at every season rollover;
* ``guild_settings.twitch.role_id`` - applied when a watched member goes live.

Until this week nothing checked that the person CONFIGURING one of them outranked
the role, and five of the six are written straight into Postgres by the web
dashboard, which the bot never observes (see the module docstring of
``cogs/system/dashboard_actions.py`` for the full account). Both write paths are
gated now - but a gate only judges a WRITE. A guild configured before the gate
existed keeps exactly the configuration it had, and no write path can ever find
it. Reading the settings back and judging what they point AT is the only way to
see those guilds, and it needs the gateway: role permissions, role positions and
Yasuho's own top role live in the member/role cache, not in the database.

The failure shape this module is built against
----------------------------------------------
Its success condition is an EMPTY report. That is the shape that stays green
forever once the detector quietly stops detecting - a broken sweep and a clean
estate produce the same silence. Three things are done about that:

1. The judgement is a PURE function of (role permissions, role position,
   managed/default flags, Yasuho's top role) - :func:`audit_setting`. Nothing in
   it touches the database, Discord or the clock, so tests aim fixtures straight
   at it: a case it must flag, a case it must clear, and an exact count.
2. Silence is never printed on its own. Every report states how many guilds were
   swept and how many settings were examined, so an empty finding list is
   evidence the sweep RAN.
3. Not-knowing is never rendered as safety. A guild whose role cache is
   unavailable is reported UNKNOWN with its settings counted as UNEXAMINED; a
   surface whose query failed marks the whole sweep INCOMPLETE; and so does a
   single orphan row, because a setting for a guild that is not in the cache yet
   is a setting nobody looked at.

The scope is those six settings and nothing else. Reaction roles, button roles
and role menus also hand out roles, but a MEMBER takes those by clicking - they
are self-service, not something Yasuho does on her own initiative. That is why
the clean verdict says "every audited setting", never "every role".

Cost
----
Five queries total, whatever the guild count (:func:`load_settings`) - never one
per guild. Role resolution is CACHE ONLY (``guild.get_role``): no REST call, no
``fetch_member``, no member chunking.

Privacy
-------
The report names guilds and roles, so it is never logged and never persisted -
it exists only in the reply the owner receives. The one log line this module can
emit names a SURFACE and an exception, never a guild, a role or an id.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import discord

from tools.formats import one_line
from tools.i18n import _
from tools.snowflake import coerce_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------
SURFACE_MUTEROLE = "muterole"
SURFACE_AUTOROLE = "autorole"
SURFACE_VERIFY = "verify_role"
SURFACE_LEVEL_REWARD = "level_reward"
SURFACE_SEASON_CHAMPION = "season_champion"
SURFACE_TWITCH_LIVE = "twitch_live"

# Rendered as-is (never translated): each is the exact DB location of the
# setting, so a flagged line doubles as the grep key for going and fixing it.
SURFACE_LOCATIONS = {
    SURFACE_MUTEROLE: "muterole.role_id",
    SURFACE_AUTOROLE: "autorole.role_id",
    SURFACE_VERIFY: "guild_settings.verify_role",
    SURFACE_LEVEL_REWARD: "level_rewards.role_id",
    SURFACE_SEASON_CHAMPION: "level_config.season_champion_role_id",
    SURFACE_TWITCH_LIVE: "guild_settings.twitch.role_id",
}

# Stable ordering for the report (worst-to-explain first within a tier).
SURFACE_ORDER = (
    SURFACE_MUTEROLE,
    SURFACE_AUTOROLE,
    SURFACE_VERIFY,
    SURFACE_LEVEL_REWARD,
    SURFACE_SEASON_CHAMPION,
    SURFACE_TWITCH_LIVE,
)


# ---------------------------------------------------------------------------
# Tiers and reasons
# ---------------------------------------------------------------------------
TIER_CRITICAL = "CRITICAL"
TIER_WARNING = "WARNING"
TIER_NOTICE = "NOTICE"

TIER_ORDER = {TIER_CRITICAL: 0, TIER_WARNING: 1, TIER_NOTICE: 2}

REASON_DANGEROUS = "dangerous_permissions"
REASON_MUTE_NOT_SILENT = "mute_role_not_silent"
REASON_ROLE_MISSING = "role_missing"
REASON_UNGRANTABLE = "ungrantable"
REASON_GRANT_UNKNOWN = "grantability_unknown"

# A role Yasuho hands out automatically must not carry power. Holding ANY of
# these turns an autorole, a verify click or a mute into a privilege escalation:
# the configurer picks the role, Yasuho puts it on somebody, and the permission
# rides along. ``administrator`` is listed like the rest even though Discord
# treats it as implying every other permission - discord.py's flag accessors do
# NOT imply, so it has to be named to be seen.
DANGEROUS_PERMISSIONS = (
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
)

# Yasuho creates her own mute role with a permission value of EXACTLY 0
# (cogs/moderation/mute_perms.role_permissions - every flag False). A mute role
# exists to remove speech via channel overwrites, never to add anything, so a
# non-empty mask means the row drifted or was pointed at some other role.
MUTE_ROLE_EXPECTED_PERMISSIONS = 0

# Answers to "could Yasuho actually put this role on somebody right now".
GRANT_OK = "grantable"
GRANT_NO = "ungrantable"
GRANT_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoleFacts:
    """Everything the predicate is allowed to know about one role.

    Deliberately plain data (ints, strs, bools) rather than a
    :class:`discord.Role`: it is what makes the judgement testable against
    fixtures with no Discord object anywhere in sight.
    """

    role_id: int
    name: str = ""
    permissions: int = 0
    position: int = 0
    managed: bool = False
    is_default: bool = False

    @classmethod
    def from_role(cls, role):
        """Snapshot a cached :class:`discord.Role`. No network, no I/O."""
        return cls(
            role_id=role.id,
            name=getattr(role, "name", "") or "",
            permissions=int(getattr(role.permissions, "value", 0)),
            position=int(getattr(role, "position", 0)),
            managed=bool(getattr(role, "managed", False)),
            is_default=bool(role.is_default()),
        )


@dataclass(frozen=True)
class BotTop:
    """Yasuho's highest role, reduced to what ordering needs."""

    position: int
    role_id: int

    @classmethod
    def from_member(cls, me):
        """``BotTop`` for ``guild.me``, or None when she is not in the cache."""
        top = getattr(me, "top_role", None) if me is not None else None
        if top is None:
            return None
        return cls(position=int(top.position), role_id=int(top.id))


@dataclass(frozen=True)
class Setting:
    """One configured (surface, role id) pair, with an optional detail label."""

    surface: str
    role_id: int
    label: str = ""


@dataclass(frozen=True)
class Finding:
    """One thing worth telling the owner about one configured setting.

    One finding per REASON, not per role: a mute role carrying
    ``manage_messages`` is both a CRITICAL escalation and a broken mute-role
    invariant, and collapsing the two would hide half of what is wrong.
    """

    surface: str
    role_id: int
    tier: str
    reason: str
    role_name: str = ""
    label: str = ""
    permissions: tuple = ()
    permission_value: int = 0

    @property
    def sort_key(self):
        return (
            TIER_ORDER.get(self.tier, 99),
            SURFACE_ORDER.index(self.surface)
            if self.surface in SURFACE_ORDER
            else 99,
            self.label,
            self.role_id,
            self.reason,
        )


@dataclass
class GuildReport:
    """A single guild's outcome: findings, or the reason there are none."""

    guild_id: int
    guild_name: str
    findings: list = field(default_factory=list)
    examined: int = 0
    # Set when the role cache could not answer at all: the settings below were
    # NOT judged. Never conflate with "clean".
    unknown: bool = False
    unexamined: int = 0

    @property
    def worst_tier(self):
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: f.sort_key).tier


@dataclass
class AuditResult:
    """The whole sweep, ready to render."""

    guilds_scanned: int = 0
    # Every setting the five queries yielded, before any judging. The books
    # close: settings_found == examined + unexamined + orphan_rows, asserted in
    # the tests. It is what turns "examined 0" from ambiguous into a fact - a
    # broken query reads 0 found, a genuinely unconfigured estate reads 0 found
    # too, but a sweep that FOUND 400 and examined 0 is visibly wrong.
    settings_found: int = 0
    settings_examined: int = 0
    settings_unexamined: int = 0
    orphan_rows: int = 0
    malformed_ids: int = 0
    failed_surfaces: tuple = ()
    reports: list = field(default_factory=list)

    @property
    def unknown_reports(self):
        return [r for r in self.reports if r.unknown]

    @property
    def flagged_reports(self):
        return [r for r in self.reports if r.findings]

    def count(self, tier):
        return sum(
            1 for r in self.reports for f in r.findings if f.tier == tier
        )

    @property
    def complete(self):
        """True only when every surface was read AND every row was judged.

        Orphans count against completeness for the same reason unknowns do.
        ``?roleaudit`` is dispatched through ``on_message`` and nothing gates
        command processing on ready, so it is reachable while GUILD_CREATEs are
        still streaming - and every setting for a guild not yet in ``bot.guilds``
        is counted as an orphan. A sweep that found 400 settings, examined 0 and
        called itself complete would print "safe to hand out" over an estate it
        never looked at. An orphan role may well be dangerous; we simply could
        not look, and not-knowing is never rendered as safety.

        ``malformed_ids`` is deliberately NOT in here: an id ``guild.get_role``
        would refuse is inert for the bot too, so it cannot be an escalation.
        """
        return (
            not self.failed_surfaces
            and not self.unknown_reports
            and not self.orphan_rows
        )


# ---------------------------------------------------------------------------
# THE PREDICATE - pure. No DB, no Discord objects, no clock.
# ---------------------------------------------------------------------------
def dangerous_permissions(permission_value):
    """The dangerous permissions present in ``permission_value``, in order.

    Pure bitfield decoding through :class:`discord.Permissions`, so the bit
    numbers can never drift from Discord's. Returns a tuple of flag names,
    empty when the mask carries none of them.
    """
    perms = discord.Permissions(int(permission_value or 0))
    return tuple(name for name in DANGEROUS_PERMISSIONS if getattr(perms, name))


def role_is_below(facts, bot_top):
    """Whether ``facts`` sits strictly below ``bot_top`` in the hierarchy.

    Mirrors :meth:`discord.Role.__lt__` exactly, tie-break included: two roles
    on the same position are ordered by id, and the LARGER id is the LOWER role
    (Discord sorts the later-created one underneath). Getting that backwards
    would silently mis-answer every guild whose roles share a position, which is
    common - Discord does not renumber positions when roles are reordered.
    """
    if facts.position < bot_top.position:
        return True
    if facts.position == bot_top.position:
        return facts.role_id > bot_top.role_id
    return False


def grant_status(facts, bot_top):
    """Could Yasuho put this role on somebody: GRANT_OK / GRANT_NO / GRANT_UNKNOWN.

    Same shape as :func:`tools.modchecks.bot_can_assign_role` (not @everyone,
    not integration-managed, strictly below her top role), with one difference
    that matters here: a missing ``guild.me`` yields GRANT_UNKNOWN rather than
    False, because "I could not check" must never be rendered as an answer.
    """
    if facts.is_default or facts.managed:
        return GRANT_NO
    if bot_top is None:
        return GRANT_UNKNOWN
    return GRANT_OK if role_is_below(facts, bot_top) else GRANT_NO


def audit_setting(setting, facts, bot_top):
    """Judge ONE configured setting. Returns a tuple of :class:`Finding`.

    ``facts`` is the :class:`RoleFacts` for the configured role, or None when
    the id resolves to nothing in a role cache that IS available (a deleted
    role). A guild whose cache is unavailable must never reach this function -
    every id would look deleted, which is a lie. :func:`run_audit` routes those
    guilds to the UNKNOWN section instead.

    The tiers:

    * CRITICAL - the role carries a permission from
      :data:`DANGEROUS_PERMISSIONS`. Yasuho hands this role out on her own, so
      whoever configured it granted themselves (or anyone) that power.
    * WARNING  - ``muterole`` specifically with a non-empty permission mask.
      Yasuho creates hers with value 0; anything else drifted or was pointed
      somewhere it should not be.
    * NOTICE   - a dead setting: the role no longer exists, or Yasuho can no
      longer grant it (above her top role, integration-managed, @everyone). Not
      an escalation, but a setting the owner believes is working and is not.
    """
    surface, role_id, label = setting.surface, setting.role_id, setting.label

    if facts is None:
        return (
            Finding(
                surface=surface,
                role_id=role_id,
                tier=TIER_NOTICE,
                reason=REASON_ROLE_MISSING,
                label=label,
            ),
        )

    findings = []

    carried = dangerous_permissions(facts.permissions)
    if carried:
        findings.append(
            Finding(
                surface=surface,
                role_id=role_id,
                tier=TIER_CRITICAL,
                reason=REASON_DANGEROUS,
                role_name=facts.name,
                label=label,
                permissions=carried,
                permission_value=facts.permissions,
            )
        )

    if surface == SURFACE_MUTEROLE and facts.permissions != MUTE_ROLE_EXPECTED_PERMISSIONS:
        findings.append(
            Finding(
                surface=surface,
                role_id=role_id,
                tier=TIER_WARNING,
                reason=REASON_MUTE_NOT_SILENT,
                role_name=facts.name,
                label=label,
                permissions=carried,
                permission_value=facts.permissions,
            )
        )

    status = grant_status(facts, bot_top)
    if status != GRANT_OK:
        findings.append(
            Finding(
                surface=surface,
                role_id=role_id,
                tier=TIER_NOTICE,
                reason=(
                    REASON_UNGRANTABLE
                    if status == GRANT_NO
                    else REASON_GRANT_UNKNOWN
                ),
                role_name=facts.name,
                label=label,
                permission_value=facts.permissions,
            )
        )

    return tuple(findings)


def audit_guild(settings, facts_by_role_id, bot_top):
    """Judge every setting of one guild. Pure; returns findings worst tier first.

    ``facts_by_role_id`` maps role id -> :class:`RoleFacts` for the ids that
    resolved; an id absent from it is a deleted role.
    """
    findings = []
    for setting in settings:
        findings.extend(
            audit_setting(setting, facts_by_role_id.get(setting.role_id), bot_top)
        )
    findings.sort(key=lambda f: f.sort_key)
    return tuple(findings)


# ---------------------------------------------------------------------------
# Bulk reads - five queries for the whole estate, never one per guild
# ---------------------------------------------------------------------------
_Q_MUTEROLE = "SELECT guild_id, role_id FROM muterole;"
_Q_AUTOROLE = "SELECT guild_id, role_id FROM autorole;"
# verify_role and the Twitch Live role both live in guild_settings, so ONE scan
# of that table serves both surfaces. ``->>`` yields SQL NULL for a missing key
# AND for a JSON null, which is exactly how both are switched off (the /verify
# remove path writes null), so the WHERE keeps only rows that really configure
# something. Projecting the two keys instead of the blob also means a guild's
# unrelated settings never leave the database.
_Q_GUILD_SETTINGS = (
    "SELECT guild_id, "
    "settings->>'verify_role' AS verify_role, "
    "settings->'twitch'->>'role_id' AS twitch_role "
    "FROM guild_settings "
    "WHERE settings->>'verify_role' IS NOT NULL "
    "OR settings->'twitch'->>'role_id' IS NOT NULL;"
)
_Q_LEVEL_REWARDS = "SELECT guild_id, level, role_id FROM level_rewards;"
_Q_SEASON_CHAMPION = (
    "SELECT guild_id, season_champion_role_id FROM level_config "
    "WHERE season_champion_role_id IS NOT NULL;"
)


class _Collector:
    """Accumulates (guild -> settings) across the five reads, counting junk."""

    def __init__(self):
        self.by_guild = defaultdict(list)
        self.failed = []
        self.malformed = 0
        self.found = 0

    def add(self, guild_id, surface, raw_role_id, label=""):
        """Record one configured role, or count it as unreadable.

        The dashboard is a Node process and JavaScript cannot hold a snowflake
        in a Number, so ids come back as STRINGS from the JSONB surfaces;
        :func:`tools.snowflake.coerce_id` accepts both spellings. Anything it
        refuses is inert for the bot too (``guild.get_role`` would return None),
        so it cannot be an escalation - but it is counted and reported rather
        than dropped, because a surface quietly discarding rows is how a sweep
        starts under-reporting.
        """
        gid = coerce_id(guild_id)
        rid = coerce_id(raw_role_id)
        if gid is None or rid is None:
            self.malformed += 1
            return
        self.by_guild[gid].append(Setting(surface, rid, label))
        self.found += 1


async def load_settings(pool):
    """Read all six surfaces for the WHOLE estate. Five queries, flat.

    Cost is constant in guild count: five round trips. Row volume at 1000
    guilds, worst case, is bounded by the shape of the tables themselves -
    ``muterole``/``autorole``/``level_config`` are one row per guild at most
    (guild_id is the primary key), ``guild_settings`` likewise and filtered to
    the rows that configure one of the two keys, and ``level_rewards`` is capped
    at 25 rules per guild in code. So ~29k small rows at the absolute ceiling,
    and a few thousand in practice.

    A query that FAILS records its surfaces in ``failed`` instead of raising:
    the sweep still reports everything it could read, and the report is stamped
    INCOMPLETE so the gap is never mistaken for a clean estate.
    """
    c = _Collector()

    async def read(surfaces, query, handle):
        try:
            rows = await pool.fetch(query)
        except Exception as exc:
            # Surface names only - no guild, no role, no id reaches the log, and
            # no traceback either: a row-decode failure can carry the very value
            # this module exists to keep out of the log file, so only the
            # exception's CLASS is named.
            log.warning(
                "role audit: could not read %s (%s)",
                ", ".join(surfaces),
                type(exc).__name__,
            )
            c.failed.extend(surfaces)
            return
        for row in rows:
            handle(row)

    await read(
        (SURFACE_MUTEROLE,),
        _Q_MUTEROLE,
        lambda r: c.add(r["guild_id"], SURFACE_MUTEROLE, r["role_id"]),
    )
    await read(
        (SURFACE_AUTOROLE,),
        _Q_AUTOROLE,
        lambda r: c.add(r["guild_id"], SURFACE_AUTOROLE, r["role_id"]),
    )

    def _guild_settings_row(row):
        if row["verify_role"] is not None:
            c.add(row["guild_id"], SURFACE_VERIFY, row["verify_role"])
        if row["twitch_role"] is not None:
            c.add(row["guild_id"], SURFACE_TWITCH_LIVE, row["twitch_role"])

    await read(
        (SURFACE_VERIFY, SURFACE_TWITCH_LIVE), _Q_GUILD_SETTINGS, _guild_settings_row
    )
    await read(
        (SURFACE_LEVEL_REWARD,),
        _Q_LEVEL_REWARDS,
        lambda r: c.add(
            r["guild_id"],
            SURFACE_LEVEL_REWARD,
            r["role_id"],
            label="level {0}".format(r["level"]),
        ),
    )
    await read(
        (SURFACE_SEASON_CHAMPION,),
        _Q_SEASON_CHAMPION,
        lambda r: c.add(
            r["guild_id"], SURFACE_SEASON_CHAMPION, r["season_champion_role_id"]
        ),
    )

    return c


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
def role_cache_available(guild):
    """Whether this guild's role cache can be trusted to answer at all.

    An unavailable guild (an outage GUILD_DELETE with ``unavailable``) or one
    with an empty role list cannot resolve anything - and "resolves to nothing"
    would otherwise render as "every setting points at a deleted role", which
    reads as five harmless NOTICEs instead of "I do not know". Kept as its own
    named predicate so the UNKNOWN path is one testable condition.
    """
    if getattr(guild, "unavailable", False):
        return False
    return bool(getattr(guild, "roles", ()))


async def run_audit(bot):
    """Sweep every guild the bot is in and return an :class:`AuditResult`.

    Reads the database in bulk ONCE (:func:`load_settings`), then resolves each
    configured id against the in-memory role cache only - ``guild.get_role``, no
    REST, no member chunking. Guilds Yasuho is in but cannot see the roles of
    are reported UNKNOWN with their settings counted as unexamined; settings
    rows for guilds she is NOT in are counted as orphans rather than judged
    (their roles are unreachable from here), and orphans stamp the sweep
    incomplete - see :attr:`AuditResult.complete`.
    """
    collected = await load_settings(bot.db_pool)

    result = AuditResult(
        settings_found=collected.found,
        malformed_ids=collected.malformed,
        failed_surfaces=tuple(collected.failed),
    )

    seen_guild_ids = set()
    for guild in bot.guilds:
        result.guilds_scanned += 1
        seen_guild_ids.add(guild.id)
        settings = collected.by_guild.get(guild.id) or []
        if not settings:
            continue

        report = GuildReport(
            guild_id=guild.id, guild_name=getattr(guild, "name", "") or ""
        )

        if not role_cache_available(guild):
            report.unknown = True
            report.unexamined = len(settings)
            result.settings_unexamined += len(settings)
            result.reports.append(report)
            continue

        bot_top = BotTop.from_member(getattr(guild, "me", None))
        facts = {}
        for setting in settings:
            if setting.role_id in facts:
                continue
            role = guild.get_role(setting.role_id)
            if role is not None:
                facts[setting.role_id] = RoleFacts.from_role(role)

        report.examined = len(settings)
        result.settings_examined += len(settings)
        report.findings = list(audit_guild(settings, facts, bot_top))
        if report.findings:
            result.reports.append(report)

    result.orphan_rows = sum(
        len(v) for gid, v in collected.by_guild.items() if gid not in seen_guild_ids
    )
    result.reports.sort(
        key=lambda r: (
            0 if r.findings else 1,
            TIER_ORDER.get(r.worst_tier, 99),
            r.guild_name.lower(),
            r.guild_id,
        )
    )
    return result


# ---------------------------------------------------------------------------
# Rendering - plain ASCII text, readable long and readable empty
# ---------------------------------------------------------------------------
NAME_LIMIT = 60


def plain(text, limit=NAME_LIMIT):
    """A guild/role name made inert for a code block: flat, backtick-free, clipped."""
    flat = one_line(text).replace("`", "'")
    if len(flat) > limit:
        flat = flat[: max(1, limit - 3)] + "..."
    return flat or "(unnamed)"


def _role_ref(finding):
    if finding.role_name:
        return "{0} ({1})".format(plain(finding.role_name), finding.role_id)
    return str(finding.role_id)


def _where(finding):
    location = SURFACE_LOCATIONS.get(finding.surface, finding.surface)
    if finding.label:
        return "{0} [{1}]".format(location, finding.label)
    return location


def _finding_line(finding):
    where = _where(finding)
    role = _role_ref(finding)

    if finding.reason == REASON_DANGEROUS:
        body = _("{role} carries {permissions}").format(
            role=role, permissions=", ".join(finding.permissions)
        )
    elif finding.reason == REASON_MUTE_NOT_SILENT:
        body = _(
            "{role} is a mute role with a non-empty permission mask (value {value})"
        ).format(role=role, value=finding.permission_value)
    elif finding.reason == REASON_ROLE_MISSING:
        body = _("role {role} no longer exists").format(role=role)
    elif finding.reason == REASON_UNGRANTABLE:
        body = _(
            "{role} cannot be granted by me (above my top role, "
            "integration-managed, or @everyone)"
        ).format(role=role)
    else:
        body = _(
            "{role} cannot be judged: I am not in this guild's member cache"
        ).format(role=role)

    return "  {tier:<8} {where} -> {body}".format(
        tier=finding.tier, where=where, body=body
    )


def render_report(result):
    """The whole audit as one plain-text block. Never empty, never silent.

    The summary comes FIRST and always: guilds swept, settings examined, tier
    counts. That is what makes an empty finding list evidence rather than
    ambiguity - a report saying "swept 183 guilds, examined 412 settings,
    nothing flagged" proves the sweep ran, where a blank reply proves nothing.
    """
    lines = [_("Role grant audit - the roles Yasuho hands out on her own.")]

    if result.failed_surfaces:
        lines.append(
            _("INCOMPLETE: could not read {surfaces}. Treat this report as partial.")
            .format(surfaces=", ".join(sorted(set(result.failed_surfaces))))
        )

    lines.append(
        _(
            "Swept {guilds} guild(s); found {found} configured role setting(s), "
            "examined {examined}."
        ).format(
            guilds=result.guilds_scanned,
            found=result.settings_found,
            examined=result.settings_examined,
        )
    )
    lines.append(
        _("CRITICAL {critical} | WARNING {warning} | NOTICE {notice}").format(
            critical=result.count(TIER_CRITICAL),
            warning=result.count(TIER_WARNING),
            notice=result.count(TIER_NOTICE),
        )
    )

    if result.unknown_reports:
        lines.append(
            _(
                "UNKNOWN: {guilds} guild(s) could not be judged, {settings} "
                "setting(s) NOT examined."
            ).format(
                guilds=len(result.unknown_reports),
                settings=result.settings_unexamined,
            )
        )
    if result.orphan_rows or result.malformed_ids:
        lines.append(
            _(
                "Skipped {orphans} row(s) for guilds I am not in; "
                "{malformed} unreadable role id(s)."
            ).format(orphans=result.orphan_rows, malformed=result.malformed_ids)
        )

    lines.append("")

    flagged = result.flagged_reports
    if not flagged:
        if result.complete:
            lines.append(
                # "audited setting", not "configured role": the sweep covers the
                # six settings Yasuho grants on her OWN initiative. Roles a
                # member takes for themselves through reaction roles, button
                # roles or a role menu are self-service and out of scope, so the
                # verdict must not sound like it cleared them too.
                _("Nothing flagged: every audited setting is safe to hand out.")
            )
        else:
            lines.append(
                _("Nothing flagged in what could be read - see the gaps above.")
            )

    for report in flagged:
        lines.append(
            "[{tier}] {name} ({gid}) - {count} setting(s) examined".format(
                tier=report.worst_tier,
                name=plain(report.guild_name),
                gid=report.guild_id,
                count=report.examined,
            )
        )
        for finding in report.findings:
            lines.append(_finding_line(finding))

    for report in result.unknown_reports:
        lines.append(
            "[UNKNOWN] {name} ({gid}) - {body}".format(
                name=plain(report.guild_name),
                gid=report.guild_id,
                body=_(
                    "role cache unavailable, {count} setting(s) not examined"
                ).format(count=report.unexamined),
            )
        )

    return "\n".join(lines)
