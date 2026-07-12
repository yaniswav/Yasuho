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
from dataclasses import dataclass

# Grant / cooldown defaults, matching the original hard-coded values. These are
# also the level_config table's column defaults (schema.sql), so a freshly
# enabled guild and a guild with no row both behave exactly as leveling always did.
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_XP_MIN = 15
DEFAULT_XP_MAX = 25
DEFAULT_ANNOUNCE_MODE = "channel"

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
