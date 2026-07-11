"""Audio effect presets and their application (the Rythm-premium feature, free).

This module owns one concern: the catalog of named audio-effect presets and the
seam that applies a chosen preset to a live player. Exactly ONE preset is active
per player at a time - applying a new one REPLACES the previous, and ``Off``
clears every filter.

Layering, mirroring ``sponsorblock.py``:

* The catalog (:data:`PRESET_CATALOG`) and every ``build()`` are PURE - they
  return a plain spec ``dict`` and touch no sonolink type, so they import and
  unit-test identically under the stubbed sonolink used on the dev box.
* Only :func:`_filters_from_spec` and :func:`apply_preset` touch sonolink, and
  they import its filter models LAZILY inside the function, so importing this
  module never needs the real package.

The application seam is sonolink's public ``player.set_filters(Filters, ...)``,
which PATCHes ``/v4/sessions/{session}/players/{guild}`` with the full filter
object. Lavalink v4 OVERRIDES all previously applied filters on that PATCH, so
sending a freshly-built ``Filters`` (or an empty one for ``Off``) is exactly the
"one preset replaces the last" semantics we want - no merge bookkeeping needed.

Plugin filters (echo / high-pass / low-pass / normalization) ride the SAME seam:
sonolink's ``Filters`` carries a ``plugin_filters`` dict that serialises to
Lavalink's ``filters.pluginFilters``, so the LavaDSPX presets need no separate
REST route. The field names below are verified against the LavaDSPX 0.0.5 jar
(``echoLength``/``decay``, ``cutoffFrequency``/``boostFactor``,
``maxAmplitude``/``adaptive``).

The process-wide ``filtered_players`` ceiling (see :mod:`tools.quotas`) bounds how
many players may be filtered at once; :func:`apply_preset` acquires a slot for a
non-off preset and releases it on ``Off`` (the cog's ``_clear`` releases it on
disconnect). Nothing here logs a refusal or raises into a caller: it returns a
result code and the cog formats (and translates) the user-facing message.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable

from tools.i18n import N_

log = logging.getLogger(__name__)


# apply_preset result codes. The cog maps each to a translated message; keeping
# them as bare strings keeps this module i18n-free and trivially testable.
RESULT_OK = "ok"
RESULT_CLEARED = "cleared"
RESULT_CEILING_FULL = "ceiling_full"
RESULT_ERROR = "error"
RESULT_UNKNOWN = "unknown"

# The reset preset's key. Its build() is the empty spec: an empty Filters object
# clears the whole chain on Lavalink.
OFF_KEY = "off"


@dataclasses.dataclass(frozen=True)
class Preset:
    """One selectable effect: identity, presentation and a pure payload builder.

    ``label`` is a proper name (Nightcore, Vaporwave, ...) shown as-is and never
    translated. ``description`` is an ``N_``-marked literal - collected for
    translation now, resolved with ``_()`` at the in-task use site (the select
    option / command help). ``builder`` returns a FRESH spec dict each call, so a
    caller can never mutate the shared catalog.
    """

    key: str
    emoji: str
    label: str
    description: str
    builder: Callable[[], dict[str, Any]]

    def build(self) -> dict[str, Any]:
        """Return this preset's filter spec (a fresh, pure dict)."""
        return self.builder()


# ---------------------------------------------------------------------------
# Pure payload builders. Each returns a fresh spec dict describing the filters
# for one preset. A spec maps a sonolink Filters kwarg to its arguments:
#   "timescale"/"rotation"/"karaoke"/"tremolo"/"vibrato"/"low_pass" -> dict kwargs
#   "equalizer" -> list of (band, gain) pairs
#   "plugin_filters" -> the raw Lavalink pluginFilters dict (LavaDSPX)
# An empty spec ({}) builds an empty Filters, which clears everything.
# ---------------------------------------------------------------------------


def _build_off() -> dict[str, Any]:
    return {}


def _build_nightcore() -> dict[str, Any]:
    # Faster and higher: the classic nightcore lift.
    return {"timescale": {"speed": 1.15, "pitch": 1.15}}


def _build_slowed() -> dict[str, Any]:
    # Ease the tempo back for a slowed-down feel.
    return {"timescale": {"speed": 0.85}}


def _build_bass_boost() -> dict[str, Any]:
    # Lift the low bands (25-160 Hz) without clipping. Gains sit well under the
    # +1.0 ceiling and taper up the spectrum so only the low end swells.
    return {
        "equalizer": [
            (0, 0.20),
            (1, 0.18),
            (2, 0.15),
            (3, 0.10),
            (4, 0.05),
        ]
    }


def _build_eight_d() -> dict[str, Any]:
    # Slow stereo rotation: the audio circles the listener.
    return {"rotation": {"rotation_hz": 0.2}}


def _build_karaoke() -> dict[str, Any]:
    # Lavalink's reference vocal-cut band: duck the lead vocal region.
    return {
        "karaoke": {
            "level": 1.0,
            "mono_level": 1.0,
            "filter_band": 220.0,
            "filter_width": 100.0,
        }
    }


def _build_vaporwave() -> dict[str, Any]:
    # Slowed + pitched down + a slow tremolo wash for the dreamy haze.
    return {
        "timescale": {"speed": 0.82, "pitch": 0.92},
        "tremolo": {"frequency": 2.0, "depth": 0.3},
    }


def _build_echo() -> dict[str, Any]:
    # LavaDSPX echo: a trailing repeat. echoLength (seconds) and decay (0..1)
    # must both be > 0 for the plugin to enable it.
    return {"plugin_filters": {"echo": {"echoLength": 0.4, "decay": 0.5}}}


def _build_soft() -> dict[str, Any]:
    # LavaDSPX high-pass (roll off rumble below 300 Hz) + normalization (even out
    # the peaks) for gentle, level listening. cutoffFrequency is an int Hz value;
    # maxAmplitude in 0..1 with adaptive smoothing.
    return {
        "plugin_filters": {
            "high-pass": {"cutoffFrequency": 300, "boostFactor": 1.0},
            "normalization": {"maxAmplitude": 0.65, "adaptive": True},
        }
    }


# The ordered catalog. Order is the display order in the select and the slash
# choices; Off leads so "clear it" is always the first, obvious option.
PRESET_CATALOG: tuple[Preset, ...] = (
    Preset(
        OFF_KEY,
        "\N{HEAVY LARGE CIRCLE}",
        "Off",
        N_("Clear every effect and hear the track as it is."),
        _build_off,
    ),
    Preset(
        "nightcore",
        "\N{HIGH VOLTAGE SIGN}",
        "Nightcore",
        N_("Speed it up and pitch it higher for that nightcore rush."),
        _build_nightcore,
    ),
    Preset(
        "slowed",
        "\N{TURTLE}",
        "Slowed",
        N_("Ease off the tempo for a slowed-down feel."),
        _build_slowed,
    ),
    Preset(
        "bassboost",
        "\N{DRUM WITH DRUMSTICKS}",
        "Bass Boost",
        N_("Push the low end for a heavier, punchier sound."),
        _build_bass_boost,
    ),
    Preset(
        "8d",
        "\N{CYCLONE}",
        "8D",
        N_("Spin the audio around you for a surround effect."),
        _build_eight_d,
    ),
    Preset(
        "karaoke",
        "\N{MICROPHONE}",
        "Karaoke",
        N_("Duck the lead vocals so you can sing over the track."),
        _build_karaoke,
    ),
    Preset(
        "vaporwave",
        "\N{VIDEOCASSETTE}",
        "Vaporwave",
        N_("Slow it down and wash it out for a dreamy vaporwave haze."),
        _build_vaporwave,
    ),
    Preset(
        "echo",
        "\N{PUBLIC ADDRESS LOUDSPEAKER}",
        "Echo",
        N_("Add a trailing echo to every sound."),
        _build_echo,
    ),
    Preset(
        "soft",
        "\N{LEAF FLUTTERING IN WIND}",
        "Soft",
        N_("Smooth the lows and even the volume for gentle listening."),
        _build_soft,
    ),
)

# O(1) lookup by key, built once from the ordered catalog.
PRESETS_BY_KEY: dict[str, Preset] = {preset.key: preset for preset in PRESET_CATALOG}


def resolve_preset(key: str | None) -> Preset | None:
    """Return the preset for ``key`` (matched by key or label), or None.

    Matching is case-insensitive on both the stable key and the proper-name
    label, so the slash choice value (``bassboost``) and a prefix user's
    ``Nightcore`` both resolve, while an unknown / stale key drops to None (used
    by the cog and the restore path to discard retired preset keys).
    """
    if not key:
        return None
    needle = key.strip().lower()
    for preset in PRESET_CATALOG:
        if preset.key == needle or preset.label.lower() == needle:
            return preset
    return None


def is_effect_exempt(
    dj_id: int | None, actor_id: int, has_manage_guild: bool
) -> bool:
    """True when ``actor`` may change effects without spending the guild quota.

    Pure decision helper: the session DJ and anyone with Manage Server are
    exempt (they are trusted to drive the room), so the 6-per-10-minutes guild
    quota only ever bites ordinary listeners. A session with no DJ (``dj_id`` is
    None) leaves only the Manage-Server exemption.
    """
    if has_manage_guild:
        return True
    return dj_id is not None and actor_id == dj_id


# ---------------------------------------------------------------------------
# Application seam (the only sonolink-touching code).
# ---------------------------------------------------------------------------


def _guild_id_of(player: Any) -> int | None:
    """Return the player's guild id, or None if it cannot be resolved.

    ``player.guild`` can raise on an unbound player, so this normalises any
    failure to None for the ceiling bookkeeping (a None id simply skips the
    ceiling - the filter still applies).
    """
    try:
        guild = getattr(player, "guild", None)
    except Exception:
        return None
    return getattr(guild, "id", None)


def _filters_from_spec(spec: dict[str, Any]) -> Any:
    """Build a sonolink ``Filters`` from a pure preset spec (lazy sonolink import).

    An empty ``spec`` yields an empty ``Filters`` whose payload carries no active
    sub-filter, which is exactly what clears the chain on Lavalink. sonolink is
    imported here, not at module load, so the pure catalog above stays importable
    under the stubbed sonolink on the dev box.
    """
    from sonolink.models import (
        Equalizer,
        Filters,
        Karaoke,
        LowPass,
        Rotation,
        Timescale,
        Tremolo,
        Vibrato,
    )

    kwargs: dict[str, Any] = {}
    timescale = spec.get("timescale")
    if timescale:
        kwargs["timescale"] = Timescale(**timescale)
    equalizer = spec.get("equalizer")
    if equalizer:
        kwargs["equalizer"] = [Equalizer(band=band, gain=gain) for band, gain in equalizer]
    rotation = spec.get("rotation")
    if rotation:
        kwargs["rotation"] = Rotation(**rotation)
    karaoke = spec.get("karaoke")
    if karaoke:
        kwargs["karaoke"] = Karaoke(**karaoke)
    tremolo = spec.get("tremolo")
    if tremolo:
        kwargs["tremolo"] = Tremolo(**tremolo)
    vibrato = spec.get("vibrato")
    if vibrato:
        kwargs["vibrato"] = Vibrato(**vibrato)
    low_pass = spec.get("low_pass")
    if low_pass:
        kwargs["low_pass"] = LowPass(**low_pass)
    plugin_filters = spec.get("plugin_filters")
    if plugin_filters:
        # Copy each plugin payload so the live Filters can never alias the catalog.
        kwargs["plugin_filters"] = {
            name: dict(payload) for name, payload in plugin_filters.items()
        }
    return Filters(**kwargs)


async def apply_preset(player: Any, key: str, *, quotas: Any) -> str:
    """Apply preset ``key`` to ``player``; return a result code, never raise.

    ``Off`` clears every filter and RELEASES the player's ``filtered_players``
    ceiling slot. Any other preset first tries to ACQUIRE a slot (idempotent, so
    switching presets on an already-filtered player takes no second slot); a full
    ceiling refuses with :data:`RESULT_CEILING_FULL` and leaves the current sound
    untouched. On success the player's ``effect_preset`` attribute is set to the
    key (or None for Off) so the controller and the snapshot can read it back.

    Best-effort throughout: an unknown key returns :data:`RESULT_UNKNOWN` and a
    failed ``set_filters`` returns :data:`RESULT_ERROR` (releasing a slot only if
    this call is the one that took it), so a node hiccup never propagates.
    """
    preset = PRESETS_BY_KEY.get(key)
    if preset is None:
        return RESULT_UNKNOWN

    guild_id = _guild_id_of(player)
    ceiling = quotas.filtered_players

    if preset.key == OFF_KEY:
        try:
            await player.set_filters(_filters_from_spec(preset.build()))
        except Exception:
            log.exception("Failed to clear effects for guild %s", guild_id)
        # Clear our tracking regardless: the intent is "no effect", and releasing
        # the slot keeps the ceiling honest even if the node rejected the reset.
        player.effect_preset = None
        if guild_id is not None:
            ceiling.release(guild_id)
        return RESULT_CLEARED

    was_holding = guild_id is not None and guild_id in ceiling
    if guild_id is not None and not ceiling.acquire(guild_id):
        return RESULT_CEILING_FULL
    try:
        await player.set_filters(_filters_from_spec(preset.build()))
    except Exception:
        # Only release if THIS call took the slot; a preset switch on an already
        # filtered player must keep the slot its previous effect still holds.
        if guild_id is not None and not was_holding:
            ceiling.release(guild_id)
        log.exception(
            "Failed to apply effect '%s' for guild %s", preset.key, guild_id
        )
        return RESULT_ERROR
    player.effect_preset = preset.key
    return RESULT_OK


# ---------------------------------------------------------------------------
# Quota-heartbeat helpers. The music cog owns the shared QuotaRegistry and logs
# a folded snapshot from its idle loop; these keep that cheap and testable.
# ---------------------------------------------------------------------------


def stats_are_nonzero(stats: dict[str, dict[str, int]]) -> bool:
    """True when any counter in a QuotaRegistry.stats() snapshot is nonzero.

    The heartbeat guards on this so an idle process logs nothing at all.
    """
    return any(value for member in stats.values() for value in member.values())


def format_quota_stats(stats: dict[str, dict[str, int]]) -> str:
    """Fold a QuotaRegistry.stats() snapshot into one compact log line.

    Renders ``name(k=v k=v)`` per member in a stable order, e.g.
    ``effects_guild(hits=3 rejections=1 tracked_keys=2)``.
    """
    parts = []
    for name, member in stats.items():
        inner = " ".join(f"{k}={v}" for k, v in member.items())
        parts.append(f"{name}({inner})")
    return " ".join(parts)
