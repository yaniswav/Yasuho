"""Pure, synchronous leveling maths and per-guild config value objects.

The XP curve, the per-message XP grant, the level-up test and the per-guild
:class:`LevelConfig` all live here as pure functions and immutable value objects -
no discord, no database, no awaits - so the hottest path in the bot (on_message,
one call per message per guild) rests on trivially unit-tested logic. The Leveling
cog and its config toggle wire these into I/O; this module never touches either.

The curve is deliberately UNCHANGED from the original inline formula (the user
decreed no curve change): :func:`level_for_xp` and :func:`xp_for_level` reproduce
``int((xp / 100) ** 0.5)`` and ``level ** 2 * 100`` exactly, and a property test
pins zero drift against those literals.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from datetime import timedelta

# Grant / cooldown defaults, matching the original hard-coded values. These are
# also the level_config table's column defaults (schema.sql), so a freshly
# enabled guild and a guild with no row both behave exactly as leveling always did.
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_XP_MIN = 15
DEFAULT_XP_MAX = 25
DEFAULT_ANNOUNCE_MODE = "channel"

# Voice XP (L7): per-guild opt-in reward for time spent in voice. The rate is XP
# awarded PER ELIGIBLE MINUTE (see is_voice_xp_eligible); the bounds are enforced
# at set time (validate_voice_xp_rate). These are also the level_config column
# defaults (schema.sql), so a freshly enabled guild behaves like the default.
DEFAULT_VOICE_XP_PER_MINUTE = 5
MIN_VOICE_XP_PER_MINUTE = 1
MAX_VOICE_XP_PER_MINUTE = 60

# A voice channel earns its occupants XP only when at least this many non-bot
# humans share it: XP is a reward for HANGING OUT TOGETHER, never for sitting
# alone in a channel to farm it. See is_voice_xp_eligible.
VOICE_MIN_HUMANS = 2

# Where a level-up is announced. Only "channel" is wired into the cog this lot
# (the original behaviour: announce in the channel the message was sent in); the
# rest are reserved for later lots and live here so the value set has one home.
ANNOUNCE_MODES = ("off", "channel", "dm", "fixed")


def level_for_xp(xp):
    """Level reached at a given XP total (the original sqrt curve, verbatim).

    ``int((xp / 100) ** 0.5)`` - kept byte for byte from the cog's former inline
    formula so no member's level shifts. The inverse is :func:`xp_for_level`.
    """
    return int((xp / 100) ** 0.5)


def xp_for_level(level):
    """XP needed to reach ``level`` (its entry threshold), the curve's inverse.

    ``level ** 2 * 100`` - the ``cur_threshold`` / ``next_threshold`` arithmetic the
    rank card has always used, lifted here unchanged.
    """
    return level**2 * 100


def grant_amount(xp_min=DEFAULT_XP_MIN, xp_max=DEFAULT_XP_MAX, *, rng=random):
    """Random XP for one message, inclusive of both bounds.

    ``rng`` is a seam: it defaults to the stdlib ``random`` module, and any object
    exposing ``randint(a, b)`` can be injected (the tests pass a deterministic
    stub). Mirrors the original ``random.randint(15, 25)`` when called with the
    default band.
    """
    return rng.randint(xp_min, xp_max)


def level_up_between(old_xp, new_xp):
    """The new level if going from ``old_xp`` to ``new_xp`` leveled up, else None.

    Reproduces the cog's ``new_level > old_level`` gate: returns the freshly
    reached level (an int) when the grant pushed the member past a threshold, or
    ``None`` when it did not - so a caller reads ``if level is not None: announce``.
    A multi-level jump reports only the final level (the original behaviour).
    """
    old_level = level_for_xp(old_xp)
    new_level = level_for_xp(new_xp)
    return new_level if new_level > old_level else None


@dataclass(frozen=True)
class LevelConfig:
    """Immutable per-guild leveling settings (mirrors one level_config row).

    Only ``enabled``, ``cooldown_seconds`` and the ``xp_min`` / ``xp_max`` band are
    read by the grant path this lot; ``announce_mode`` / ``announce_channel_id`` /
    ``announce_template`` are carried for later lots. Frozen so a cached config is a
    value: a change replaces the map entry rather than mutating a shared object.
    """

    enabled: bool = False
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    xp_min: int = DEFAULT_XP_MIN
    xp_max: int = DEFAULT_XP_MAX
    announce_mode: str = DEFAULT_ANNOUNCE_MODE
    announce_channel_id: int | None = None
    announce_template: str | None = None
    voice_xp_enabled: bool = False
    voice_xp_per_minute: int = DEFAULT_VOICE_XP_PER_MINUTE

    @classmethod
    def from_row(cls, row):
        """Build a config from a level_config DB row (or any mapping).

        Any column that is absent or SQL NULL falls back to the field default, so a
        row written before a later-added column, or a partial mapping in a test,
        still yields a coherent config. The nullable announce columns keep None.
        """

        def _value(key, default):
            got = row.get(key)
            return default if got is None else got

        return cls(
            enabled=bool(_value("enabled", cls.enabled)),
            cooldown_seconds=_value("cooldown_seconds", cls.cooldown_seconds),
            xp_min=_value("xp_min", cls.xp_min),
            xp_max=_value("xp_max", cls.xp_max),
            announce_mode=_value("announce_mode", cls.announce_mode),
            announce_channel_id=row.get("announce_channel_id"),
            announce_template=row.get("announce_template"),
            voice_xp_enabled=bool(_value("voice_xp_enabled", cls.voice_xp_enabled)),
            voice_xp_per_minute=_value(
                "voice_xp_per_minute", cls.voice_xp_per_minute
            ),
        )


# The config for a guild that is ON but has no persisted overrides (the JSONB
# fallback case). A frozen singleton is safe to share.
DEFAULT_ENABLED_CONFIG = LevelConfig(enabled=True)


def resolve_config(row, legacy_enabled):
    """Effective :class:`LevelConfig` for a guild from the two config sources.

    Read-through precedence for the JSONB -> table migration: a level_config row is
    the new source of truth and wins outright when present; ONLY a guild with no row
    falls back to the legacy ``leveling_enabled`` JSONB bool, so a guild that had
    leveling on before the table existed keeps earning XP until its next toggle
    writes a row, while a guild switched OFF via the table is never resurrected by a
    stale JSONB true. Returns a config when leveling is ON for the guild, or ``None``
    when it is OFF (the hot path treats ``None`` as "this guild earns no XP", i.e.
    absence from the enabled-config map).
    """
    if row is not None:
        config = LevelConfig.from_row(row)
        return config if config.enabled else None
    return DEFAULT_ENABLED_CONFIG if legacy_enabled else None


# ============================================================
# No-XP zones (L3): channels/categories and roles that earn no XP.
# ============================================================
#
# The decisions below are pure and allocation-light on purpose: on_message's
# no-xp check runs for every grant-eligible message in every enabled guild, so
# it must never touch the DB and should avoid allocating a new set per message.
# The stateful per-guild cache (a BoundedLRU of NoXpSnapshot) lives on the
# Leveling cog; this module only holds the value object and the pure check.

# Discord caps a single select at 25 options; the admin "remove an entry"
# picker (cogs/community/level_config_ui.py) lists every configured entry in
# one select, mirroring level_rewards.MAX_REWARDS_PER_GUILD. A generous cap
# above that Discord limit still keeps a guild's snapshot tiny.
MAX_NO_XP_PER_GUILD = 50

NO_XP_CHANNEL = "channel"
NO_XP_ROLE = "role"
NO_XP_KINDS = (NO_XP_CHANNEL, NO_XP_ROLE)


def can_add_no_xp_entry(existing_count, cap=MAX_NO_XP_PER_GUILD):
    """Whether one more no-xp entry may be added given the guild's current count."""
    return existing_count < cap


@dataclass(frozen=True)
class NoXpSnapshot:
    """A guild's no-xp zones as two frozensets, ready for O(1) hot-path checks.

    ``channels`` holds BOTH text-channel ids and category ids under the SAME
    ``kind='channel'`` row - a category is itself a GuildChannel on Discord's
    side, so muting a whole category is one row, not one per channel inside it.
    The hot-path check (:func:`is_no_xp_message`) tests a message's channel id
    OR its category id against this single set; see that function's docstring
    for why this is the deliberate design (not a third 'category' kind).
    ``roles`` holds role ids; a message author holding ANY of those roles earns
    no XP, wherever they post.
    """

    channels: frozenset = frozenset()
    roles: frozenset = frozenset()

    @classmethod
    def from_rows(cls, rows):
        """Build from level_no_xp rows (or any mapping with 'kind'/'target_id')."""
        channels = frozenset(
            r["target_id"] for r in rows if r["kind"] == NO_XP_CHANNEL
        )
        roles = frozenset(r["target_id"] for r in rows if r["kind"] == NO_XP_ROLE)
        return cls(channels=channels, roles=roles)


# Shared immutable value for "this guild has no no-xp zones configured" - the
# overwhelming majority of guilds that use any. Safe to share (frozen).
EMPTY_NO_XP_SNAPSHOT = NoXpSnapshot()


def is_no_xp_message(snapshot, channel_id, category_id, role_ids):
    """Whether a message posted under these ids must earn zero XP.

    Pure set membership, allocation-free: the message's channel id OR its
    category id (so a category-level mute covers every channel inside it, see
    :class:`NoXpSnapshot`) hits ``snapshot.channels``, OR any id in
    ``role_ids`` (the author's role ids) is in ``snapshot.roles``. ``role_ids``
    may be any iterable; an empty snapshot short-circuits both checks without
    ever iterating ``role_ids`` at all, so a guild with no rules configured
    (the overwhelming majority) pays only two ``in frozenset()`` tests.
    """
    if channel_id in snapshot.channels:
        return True
    if category_id is not None and category_id in snapshot.channels:
        return True
    if snapshot.roles:
        for role_id in role_ids:
            if role_id in snapshot.roles:
                return True
    return False


# ============================================================
# Announce control (L3): mode routing + custom template validation/render.
# ============================================================

DEFAULT_ANNOUNCE_TEMPLATE = "{user} reached level **{level}**!"

# The only placeholders a custom announce_template may use. Deliberately small
# (no attribute/index access via "{user.x}", no positional "{}" / "{0}") so a
# template can never reach into an object's internals - see
# validate_announce_template, the sole gate that lets a template be SET.
ANNOUNCE_PLACEHOLDERS = frozenset({"user", "level", "guild"})

MAX_ANNOUNCE_TEMPLATE_LEN = 300

# Hard ceiling on a RENDERED announce (Discord's own message limit). A validated
# template can never approach this - its output is bounded by the 300-char
# template plus a mention and a guild name - so this only ever trips on a stale
# or corrupt stored value (e.g. an abusive format spec that predates the
# validation tightening); render then falls back to the default. See
# render_announce_template.
MAX_RENDERED_ANNOUNCE_LEN = 2000

_template_formatter = string.Formatter()


def validate_announce_template(template):
    """Validate a candidate announce_template. Returns ``(ok, reason)``.

    ``reason`` is ``None`` on success, else one of ``"empty"`` / ``"too_long"``
    / ``"malformed"`` / ``"unknown_placeholder"`` - a short code the cog turns
    into a localized message (this module has no i18n dependency, like every
    other tools/*.py pure decision engine). Uses ``string.Formatter.parse``
    rather than a hand-rolled regex so a malformed brace pair (e.g. a lone
    ``"{"``) is caught HERE, at SET time, instead of surfacing as a
    ``ValueError`` out of ``str.format`` on the hot announce path.
    """
    if template is None:
        return False, "empty"
    stripped = template.strip()
    if not stripped:
        return False, "empty"
    if len(stripped) > MAX_ANNOUNCE_TEMPLATE_LEN:
        return False, "too_long"
    try:
        fields = set()
        for _literal, name, spec, conv in _template_formatter.parse(stripped):
            if name is None:
                continue
            # A placeholder must be BARE. parse() reports a format spec and a
            # conversion SEPARATELY from the name, so the name-only allow-list
            # below would otherwise wave through "{level:>9999999}" (name=level,
            # spec=">9999999" - renders to a multi-megabyte string, a memory
            # DoS) or "{user!r}" (name=user, conv="r"). Reject any non-empty
            # spec or any conversion here, at SET time.
            if spec or conv is not None:
                return False, "unknown_placeholder"
            fields.add(name)
    except ValueError:
        return False, "malformed"
    if fields - ANNOUNCE_PLACEHOLDERS:
        return False, "unknown_placeholder"
    return True, None


def render_announce_template(template, *, user_text, level, guild_name):
    """Render a (previously validated) template against the allowed mapping.

    Falls back to :data:`DEFAULT_ANNOUNCE_TEMPLATE` when ``template`` is falsy
    or somehow fails to render (defensive only - :func:`validate_announce_template`
    is the real gate and runs once at SET time; this never re-validates on the
    hot announce path, it only guards against a stored value going stale, e.g.
    a future placeholder-set shrink). It ALSO falls back when the rendered text
    blows past :data:`MAX_RENDERED_ANNOUNCE_LEN`, so a stored template carrying an
    abusive format spec (which format_map honours WITHOUT raising) can never
    emit a multi-megabyte string - belt-and-suspenders behind the validation.
    """
    mapping = {"user": user_text, "level": level, "guild": guild_name}
    source = template or DEFAULT_ANNOUNCE_TEMPLATE
    try:
        rendered = source.format_map(mapping)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_ANNOUNCE_TEMPLATE.format_map(mapping)
    if len(rendered) > MAX_RENDERED_ANNOUNCE_LEN:
        return DEFAULT_ANNOUNCE_TEMPLATE.format_map(mapping)
    return rendered


def resolve_announce_target(mode, source_channel_id, fixed_channel_id):
    """Where a level-up announce should go, given the guild's announce_mode.

    Returns a ``(route, channel_id)`` pair: ``route`` is one of ``"off"`` /
    ``"channel"`` / ``"dm"`` / ``"fixed"``; ``channel_id`` is the channel to
    send to for ``"channel"``/``"fixed"`` (``None`` for ``"off"`` and ``"dm"`` -
    the DM target is the leveled-up member, not a channel). An unrecognised
    mode, and a ``"fixed"`` mode with no configured channel, both fall back to
    ``"channel"`` (the original, always-safe behaviour) - mirroring
    ``tools.level_rewards``'s "unknown mode behaves like the safer default".
    This is the ONLY decision made here: whether the member opted out of
    announces entirely is a separate, outer gate the cog checks first (the
    existing ``levelup_announce`` per-user preference), so this function is
    never even called for an opted-out member.
    """
    if mode == "off":
        return "off", None
    if mode == "dm":
        return "dm", None
    if mode == "fixed":
        if fixed_channel_id is None:
            return "channel", source_channel_id
        return "fixed", fixed_channel_id
    return "channel", source_channel_id


# ============================================================
# Voice XP (L7): rate validation, eligibility predicate, credit maths.
# ============================================================
#
# The cog (cogs/community/voice_xp.py) owns the in-memory sessions, the periodic
# sweep, and the batched DB write; this module holds only the pure decisions the
# sweep leans on - none touch discord, the DB, or the clock, so the eligibility
# truth table and the credit arithmetic are trivially unit-tested.


def validate_voice_xp_rate(rate):
    """Validate a candidate voice XP per-minute rate. Returns ``(ok, reason)``.

    ``reason`` is ``None`` on success or ``"out_of_range"`` (a short code the cog
    turns into a localized message, like validate_announce_template - this module
    carries no i18n dependency). A bool is rejected explicitly: ``True``/``False``
    are ``int`` subclasses in Python and would otherwise slip through the range
    test as 1/0.
    """
    if not isinstance(rate, int) or isinstance(rate, bool):
        return False, "out_of_range"
    if rate < MIN_VOICE_XP_PER_MINUTE or rate > MAX_VOICE_XP_PER_MINUTE:
        return False, "out_of_range"
    return True, None


def is_voice_xp_eligible(
    *,
    enabled,
    in_voice,
    human_count,
    is_afk_channel,
    self_deaf,
    self_mute,
    is_no_xp,
):
    """Whether a member should earn voice XP for the window ending now.

    A single boolean predicate over the state SAMPLED AT CREDIT TIME (the sweep
    reads live voice state, then asks this): voice XP must be ON for the guild
    (``enabled`` folds in "leveling on AND voice_xp on"), the member must still be
    IN a voice channel, NOT alone (at least :data:`VOICE_MIN_HUMANS` non-bot
    humans share it), NOT parked in the guild's AFK channel, NOT self-deafened or
    self-muted (a proxy for "actually present"), and the channel/category/role
    must not be a no-XP zone (the L3 snapshot, reused here). Any one failing means
    the window's minutes are simply not credited - see :func:`voice_credit`.
    """
    return (
        enabled
        and in_voice
        and human_count >= VOICE_MIN_HUMANS
        and not is_afk_channel
        and not self_deaf
        and not self_mute
        and not is_no_xp
    )


def voice_credit(elapsed_seconds, rate, interval_seconds, *, eligible):
    """XP to award and marker advance for one swept voice window.

    Returns ``(xp, consumed_seconds)``. ``elapsed_seconds`` is the wall time since
    this session was last credited; only WHOLE minutes count (partial minutes
    floor and their sub-minute remainder carries to the next sweep by advancing
    the marker only by the whole minutes consumed). Credited minutes are capped at
    ``interval_seconds // 60`` so a returning session (a missed sweep, an outage)
    can never BANK catch-up XP: the excess whole minutes past the cap are still
    CONSUMED (the marker advances past them, up to but never beyond ``now``) but
    are not paid out. An ineligible window credits nothing yet still advances the
    marker by its whole minutes, so ineligible time is never banked either.
    """
    whole_minutes = int(elapsed_seconds // 60)
    if whole_minutes <= 0:
        return 0, 0
    cap_minutes = max(interval_seconds // 60, 0)
    credited_minutes = min(whole_minutes, cap_minutes) if eligible else 0
    consumed_seconds = whole_minutes * 60
    return credited_minutes * rate, consumed_seconds


def apply_multiplier(value, multiplier):
    """Multiply an XP amount by an effective multiplier - one rounding rule
    shared by BOTH hot paths (see cogs/community/leveling.py's on_message and
    cogs/community/voice_xp.py's sweep). Rounds to the nearest whole XP and
    floors at 0: a multiplier of 0.0 (or any factor small enough that
    ``value * multiplier`` rounds down to zero) means the message/window earns
    literally 0 XP, never an artificial floor of 1 - "mute XP via multiplier" is
    an explicitly supported outcome (the Lurkr rule, see xp_multipliers in
    schema.sql). In the voice path this is applied ONCE to the per-minute RATE
    (``config.voice_xp_per_minute * multiplier``), not to the aggregated
    per-tick total - so a session credited for several minutes in one sweep
    tick never compounds rounding drift across those minutes; the effective,
    already-rounded rate is what tools.leveling.voice_credit then multiplies by
    the whole-minute count.
    """
    return max(0, round(value * multiplier))


# ============================================================
# XP multipliers (leveling L4): boost/reduce XP per channel, per role,
# globally, and via a timed double-XP event. The Lurkr stacking rule:
# effective = global_factor * channel_factor * role_factor * event_factor,
# each tier defaulting to 1.0 when unconfigured. See MultiplierSnapshot and
# compute_multiplier below for the full contract.
# ============================================================

MIN_MULTIPLIER_FACTOR = 0.0
MAX_MULTIPLIER_FACTOR = 5.0

# Same 25-cap precedent as level_rewards / level_no_xp's admin pickers
# (Discord's own 25-option select limit); counted across every kind
# (global + channel + role) for a guild, so the "boost remove" picker never
# needs pagination.
MAX_MULTIPLIERS_PER_GUILD = 25

MULTIPLIER_GLOBAL = "global"
MULTIPLIER_CHANNEL = "channel"
MULTIPLIER_ROLE = "role"
MULTIPLIER_KINDS = (MULTIPLIER_GLOBAL, MULTIPLIER_CHANNEL, MULTIPLIER_ROLE)

# xp_multipliers.target_id for the single 'global' row a guild may have (the
# PK (guild_id, kind, target_id) then enforces AT MOST ONE global row per
# guild, the same way level_no_xp's PK enforces one row per (kind, target)).
GLOBAL_MULTIPLIER_TARGET_ID = 0

# Timed double-XP event bounds (level_config.event_factor / event_ends_at).
# The floor keeps an admin from setting a near-instant event that expires
# before anyone notices; the ceiling (14 days) is the locked design bound.
MIN_EVENT_DURATION_SECONDS = 60
MAX_EVENT_DURATION_SECONDS = 14 * 24 * 3600


def validate_multiplier_factor(factor):
    """Validate a candidate boost/event factor. Returns ``(ok, reason)``.

    ``reason`` is ``None`` on success or one of ``"invalid"`` / ``"out_of_range"``
    - short codes the cog turns into localized text (this module carries no
    i18n dependency, like validate_announce_template / validate_voice_xp_rate).
    A bool is rejected explicitly (an ``int`` subclass in Python) so an admin
    can never set a factor to "True"/"False". 0.0 is IN range - muting XP via
    a zero factor is an explicitly supported outcome.
    """
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        return False, "invalid"
    if factor < MIN_MULTIPLIER_FACTOR or factor > MAX_MULTIPLIER_FACTOR:
        return False, "out_of_range"
    return True, None


def validate_event_duration(seconds):
    """Validate a candidate event duration in whole seconds. Returns ``(ok,
    reason)`` with the same short-code contract as validate_multiplier_factor.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return False, "invalid"
    if seconds < MIN_EVENT_DURATION_SECONDS or seconds > MAX_EVENT_DURATION_SECONDS:
        return False, "out_of_range"
    return True, None


def can_add_multiplier(existing_count, cap=MAX_MULTIPLIERS_PER_GUILD):
    """Whether one more multiplier rule may be added given the guild's count."""
    return existing_count < cap


# A tiny fallback duration parser (Nd/Nh/Nm/Ns, any subset, concatenated - e.g.
# "2h", "3d", "1d12h"), used ONLY if tools.time.ShortTime cannot be imported
# (see cogs/community/level_config_ui.py's event command - the house
# ShortTime converter is preferred whenever it is importable). Deliberately
# tiny: no relativedelta, no "weeks"/"months"/"years" units, just the four a
# double-XP event realistically needs.
_DURATION_RE = re.compile(
    r"""
    (?:(?P<days>[0-9]{1,4})d)?
    (?:(?P<hours>[0-9]{1,4})h)?
    (?:(?P<minutes>[0-9]{1,5})m)?
    (?:(?P<seconds>[0-9]{1,5})s)?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_short_duration(text):
    """Parse a fallback ``NdNhNmNs``-shaped duration string into whole seconds.

    Returns ``None`` when nothing at all matched (an empty string, garbage, or
    a string with no recognised unit) - mirroring ShortTime's own "no groups
    matched" rejection, so the caller can turn a ``None`` into the same
    "invalid duration" refusal regardless of which parser ran.
    """
    if not text:
        return None
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None or not match.group(0):
        return None
    parts = match.groupdict()
    days = int(parts["days"] or 0)
    hours = int(parts["hours"] or 0)
    minutes = int(parts["minutes"] or 0)
    seconds = int(parts["seconds"] or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total or None


@dataclass(frozen=True)
class MultiplierSnapshot:
    """A guild's full XP-multiplier configuration, ready for O(1) hot-path use.

    Mirrors NoXpSnapshot's role in the L3 lot: a single cached, immutable
    object per guild (see the Leveling cog's ``self._multipliers`` BoundedLRU)
    that both hot paths (on_message's grant and the voice sweep's per-minute
    credit) read with zero DB and minimal allocation on the common "nothing
    configured" case (see :attr:`is_trivial`).

    ``channels`` holds BOTH text-channel ids and category ids under the SAME
    ``kind='channel'`` row, exactly like NoXpSnapshot.channels - a category is
    itself a GuildChannel on Discord's side, so boosting a whole category is
    one row. ``roles`` holds role-id -> factor. ``global_factor`` is the
    single ``kind='global'`` row's factor, or 1.0 when unconfigured.
    ``event_factor`` / ``event_ends_at`` mirror level_config's own columns;
    ``event_ends_at`` is ``None`` when no event is running OR the stored one
    has already expired (the cog lazily nulls an expired row at refresh time -
    see refresh_multiplier_snapshot - so this snapshot never itself needs to
    re-check expiry against a clock; compute_multiplier's own ``now`` check is
    the belt to that suspenders, for the (short) window between expiry and the
    next refresh).
    """

    global_factor: float = 1.0
    channels: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    event_factor: float | None = None
    event_ends_at: object = None  # datetime.datetime | None

    @property
    def is_trivial(self):
        """True when this guild has NO multiplier configuration at all - the
        overwhelming majority of guilds. Both hot paths check this FIRST (a
        single attribute read) so the common case pays zero further work: no
        role-id generator is built, compute_multiplier is never even called,
        and the grant/rate passes through completely unchanged - mirroring the
        no-xp snapshot's own ``no_xp.channels or no_xp.roles`` short circuit.
        """
        return (
            self.global_factor == 1.0
            and not self.channels
            and not self.roles
            and self.event_factor is None
        )

    @classmethod
    def from_rows(cls, rows, event_factor=None, event_ends_at=None):
        """Build from xp_multipliers rows (kind/target_id/factor) plus the
        guild's level_config event columns (read separately - see the cog)."""
        global_factor = 1.0
        channels: dict = {}
        roles: dict = {}
        for row in rows:
            kind = row["kind"]
            factor = row["factor"]
            if kind == MULTIPLIER_GLOBAL:
                global_factor = factor
            elif kind == MULTIPLIER_CHANNEL:
                channels[row["target_id"]] = factor
            elif kind == MULTIPLIER_ROLE:
                roles[row["target_id"]] = factor
        return cls(
            global_factor=global_factor,
            channels=channels,
            roles=roles,
            event_factor=event_factor,
            event_ends_at=event_ends_at,
        )


# Shared immutable value for "this guild has no multiplier configuration at
# all" - the overwhelming majority. Safe to share (frozen, is_trivial == True).
EMPTY_MULTIPLIER_SNAPSHOT = MultiplierSnapshot()


def compute_multiplier(snapshot, channel_id, category_id, role_ids, now):
    """The effective XP multiplier for a grant, per the Lurkr stacking rule:

        effective = global_factor * channel_factor * role_factor * event_factor

    Each tier defaults to 1.0 when this guild has no rule for it.

    ``channel_factor`` is the entry for ``channel_id`` OR - only when there is
    no channel-specific entry - the entry for ``category_id``: a channel-level
    rule always wins over its category (mirrors NoXpSnapshot's own
    channel-or-category lookup order in is_no_xp_message, so the two L3/L4
    "which wins" rules read identically).

    ``role_factor`` is the HIGHEST factor among every role in ``role_ids``
    that has its own entry - NOT a product across roles: a member holding two
    boosted roles gets the bigger boost, never a stacked/multiplied one. A
    member with no matching role contributes 1.0.

    ``event_factor`` applies only while ``snapshot.event_ends_at`` is set AND
    strictly in the future relative to ``now`` (``now < event_ends_at``); at
    or past the boundary the event is treated as expired and contributes 1.0
    - this function never mutates anything, so an actually-expired event still
    stored in a stale snapshot is simply ignored here, belt-and-suspenders
    behind the cog's own lazy-null refresh.
    """
    channel_factor = snapshot.channels.get(channel_id)
    if channel_factor is None and category_id is not None:
        channel_factor = snapshot.channels.get(category_id)
    if channel_factor is None:
        channel_factor = 1.0

    role_factor = 1.0
    if snapshot.roles:
        matched = [snapshot.roles[rid] for rid in role_ids if rid in snapshot.roles]
        if matched:
            role_factor = max(matched)

    event_factor = 1.0
    if (
        snapshot.event_factor is not None
        and snapshot.event_ends_at is not None
        and now < snapshot.event_ends_at
    ):
        event_factor = snapshot.event_factor

    return snapshot.global_factor * channel_factor * role_factor * event_factor


# ============================================================
# Period leaderboards (leveling L6): weekly/monthly XP rollups that ride the
# SAME grant statements as the lifetime `levels` table (see xp_period in
# schema.sql) - NO destructive resets, a period simply rolls to a new key.
# Pure period-key maths and the lazy-prune decision live here; the writes
# (both hot paths), the reads (/top weekly|monthly) and the per-guild "last
# seen period" marker all live in the cog (cogs/community/leveling.py).
# ============================================================

PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
PERIOD_KINDS = (PERIOD_WEEKLY, PERIOD_MONTHLY)

# How many PRIOR periods (beyond the current one) a guild's xp_period rows
# survive before the lazy prune drops them. Generous enough that a period
# which just rolled over is never pruned out from under an in-flight read,
# small enough that a guild's row count never grows without bound.
PRUNE_PERIODS_BACK = 3


def iso_week_period_key(now):
    """The ISO year-week period key for ``now`` (e.g. ``"W2026-28"``).

    Built from ``now.isocalendar()`` (ISO 8601: weeks run Monday..Sunday, and
    the ISO YEAR a week belongs to can differ from the calendar year right at
    the Dec/Jan boundary - e.g. Dec 29 2025 falls in ISO week 2026-W01), so
    the key always names the week the timestamp actually falls in, never a
    calendar-year mismatch. Zero-padded on both fields so keys for the same
    ISO year sort lexically in chronological order (W2026-02 < W2026-10).
    """
    iso_year, iso_week, _iso_weekday = now.isocalendar()
    return f"W{iso_year:04d}-{iso_week:02d}"


def month_period_key(now):
    """The calendar-month period key for ``now`` (e.g. ``"M2026-07"``).

    Zero-padded like :func:`iso_week_period_key`, for the same lexical-sort
    reason (and so the 'W'/'M' prefixes never collide with each other).
    """
    return f"M{now.year:04d}-{now.month:02d}"


def current_period_keys(now):
    """Both period keys (weekly, monthly) for the current instant ``now``.

    The single place that pairs them, so a grant statement or a read can
    never accidentally compute one key from a different ``now`` than the
    other (a message grant writes BOTH in the same round trip - see the cog).
    """
    return iso_week_period_key(now), month_period_key(now)


def weekly_prune_cutoff_key(now, periods_back=PRUNE_PERIODS_BACK):
    """The oldest WEEKLY period key a prune must still KEEP (rows with a key
    strictly less than this are dropped). Subtracting whole weeks keeps the
    ISO year/week maths exact across a year boundary - unlike subtracting
    calendar months, a week is a fixed 7 days, so plain timedelta arithmetic
    is correct here.
    """
    return iso_week_period_key(now - timedelta(weeks=periods_back))


def monthly_prune_cutoff_key(now, periods_back=PRUNE_PERIODS_BACK):
    """The oldest MONTHLY period key a prune must still KEEP. Calendar months
    are not a fixed number of days, so this walks back whole months by
    integer arithmetic (a zero-based month-of-all-time index, rolling the
    year over every 12) rather than subtracting a timedelta.
    """
    month_index = (now.year * 12 + (now.month - 1)) - periods_back
    year, month0 = divmod(month_index, 12)
    return f"M{year:04d}-{month0 + 1:02d}"


def period_marker_changed(previous, current):
    """Whether a guild's cached "last seen period" marker is stale.

    ``previous`` is whatever the cog's per-guild marker cache holds for a
    guild (``None`` for a guild never marked - cold since restart, or evicted
    under cache pressure); ``current`` is the freshly computed ``(week_key,
    month_key)`` pair. True exactly when the lazy prune should fire: either
    the guild has never been marked, or at least one of the two periods
    rolled over since the marker was last set. A plain identity/tuple
    compare - called on every grant-eligible message and every voice-sweep
    tick that credited a guild, so it must stay allocation-free, and does.
    """
    return previous is None or previous != current


def build_voice_grant_payload(credits):
    """Fold ``(guild_id, user_id, gain)`` triples into three parallel arrays.

    The sweep's single batched upsert feeds these to ``unnest($1, $2, $3)`` so one
    round-trip credits every member who earned XP this tick (see the cog). Kept
    pure so the array-building is unit-tested without a DB. Order is preserved;
    an empty input yields three empty lists (the cog skips the write entirely).
    """
    guild_ids: list[int] = []
    user_ids: list[int] = []
    gains: list[int] = []
    for guild_id, user_id, gain in credits:
        guild_ids.append(guild_id)
        user_ids.append(user_id)
        gains.append(gain)
    return guild_ids, user_ids, gains
