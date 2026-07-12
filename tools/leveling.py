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
import string
from dataclasses import dataclass

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
