"""Pure, testable core for the autoroom (join-to-create voice) redesign.

A guild can run several "join-to-create" voice hubs, one per game mode
(Ranked, Quickplay, Arcade, ...). Each hub owns a category, a trigger voice
channel and a room-name template. The live configuration is stored in the
guild_settings JSONB (see tools/settings.py) under the "autorooms" key as a
list of hub dicts.

This module is deliberately free of any discord.py, database or network use:
it only knows how to shape, validate and describe hub configuration so the
cog can lean on well-tested logic and the tests can run without a bot. All
Discord/DB side effects live in the cog.

Hub dict shape::

    {
        "id":              str,   # stable identifier for the hub
        "label":           str,   # human name shown in the panel
        "category_id":     int | None,   # category the rooms are created in
        "hub_channel_id":  int | None,   # the join-to-create trigger channel
        "template":        str,   # room-name pattern, {user}/{count}/{n}
        "user_limit":      int,   # 0 = unlimited, else 1..99
        "max_rooms":       int,   # concurrent temp rooms, 1..50
        "private":         bool,  # grant the creator manage perms on the room
    }
"""

from __future__ import annotations

import uuid

# --- Discord platform limits (fixed, documented for the reader) -------------
CHANNEL_NAME_LIMIT = 100  # a channel name may be at most 100 characters
GUILD_CHANNEL_BUDGET = 500  # a guild may hold at most 500 channels total
MAX_CATEGORIES = 50  # a guild may hold at most 50 categories
CATEGORY_CHILD_LIMIT = 50  # a category may hold at most 50 channels

# --- Autoroom policy --------------------------------------------------------
MAX_HUBS = 5  # at most 5 join-to-create hubs per guild
MAX_ROOMS = 50  # hard ceiling on concurrent temp rooms per hub
DEFAULT_MAX_ROOMS = 20  # sensible default for a new hub
DEFAULT_USER_LIMIT = 0  # 0 means "unlimited"
DEFAULT_TEMPLATE = "{user}'s room"
DEFAULT_LABEL = "Autoroom"
FALLBACK_ROOM_NAME = "voice-room"  # last resort when nothing else is usable
CREATE_COOLDOWN_SECONDS = 5  # per-user anti-spam window (used by the cog)

# Each hub permanently occupies its category plus its trigger channel; the rest
# of its footprint is the temp rooms it may spin up (max_rooms).
HUB_OVERHEAD_CHANNELS = 2


def _coerce_int(value):
    """Return ``value`` as an int, or ``None`` if it cannot be one.

    Accepts ints and clean numeric strings; rejects bools (a stray ``True``
    should never masquerade as a channel id), floats-as-strings and junk.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _clamp(value, low, high):
    return max(low, min(high, value))


def render_room_name(template, name, index=None):
    """Render a hub's room-name ``template`` for ``name``.

    Substitutes ``{user}`` (the member's display name) and ``{count}``/``{n}``
    (the room's ordinal, when ``index`` is given). The result is stripped and
    capped to Discord's 100-character channel-name limit. If the template is
    missing, not a string, or renders to an empty string, we fall back to the
    member name (and, failing even that, to a generic default) so channel
    creation never fails on a bad template.
    """
    fallback = (str(name).strip() if name is not None else "") or FALLBACK_ROOM_NAME

    if not isinstance(template, str):
        return fallback[:CHANNEL_NAME_LIMIT]

    count = "" if index is None else str(index)
    user = str(name) if name is not None else ""
    rendered = (
        template.replace("{user}", user)
        .replace("{count}", count)
        .replace("{n}", count)
        .strip()
    )
    if not rendered:
        rendered = fallback
    return rendered[:CHANNEL_NAME_LIMIT]


def default_hub(
    *,
    id=None,
    label=DEFAULT_LABEL,
    category_id=None,
    hub_channel_id=None,
    template=DEFAULT_TEMPLATE,
    user_limit=DEFAULT_USER_LIMIT,
    max_rooms=DEFAULT_MAX_ROOMS,
    private=False,
):
    """Build a fully-formed hub dict, filling any unset field with its default.

    A fresh random ``id`` is generated when one is not supplied so every hub is
    addressable in the panel. The returned dict is normalised (limits clamped,
    types coerced) so callers can persist it directly.
    """
    hub = {
        "id": str(id) if id is not None else uuid.uuid4().hex[:8],
        "label": label,
        "category_id": category_id,
        "hub_channel_id": hub_channel_id,
        "template": template,
        "user_limit": user_limit,
        "max_rooms": max_rooms,
        "private": private,
    }
    return _normalize_one(hub)


def _normalize_one(entry):
    """Coerce a single raw hub dict into a clean hub, or ``None`` if unusable.

    An entry is unusable (dropped) when it is not a mapping or carries no
    valid join-to-create ``hub_channel_id`` - without that trigger channel the
    hub can never fire.
    """
    if not isinstance(entry, dict):
        return None

    hub_channel_id = _coerce_int(entry.get("hub_channel_id"))
    if hub_channel_id is None:
        return None

    label = entry.get("label")
    label = str(label).strip() if label not in (None, "") else DEFAULT_LABEL
    label = label[:CHANNEL_NAME_LIMIT] or DEFAULT_LABEL

    template = entry.get("template")
    template = template if isinstance(template, str) and template.strip() else DEFAULT_TEMPLATE

    user_limit = _coerce_int(entry.get("user_limit"))
    user_limit = DEFAULT_USER_LIMIT if user_limit is None else _clamp(user_limit, 0, 99)

    max_rooms = _coerce_int(entry.get("max_rooms"))
    max_rooms = DEFAULT_MAX_ROOMS if max_rooms is None else _clamp(max_rooms, 1, MAX_ROOMS)

    hub_id = entry.get("id")
    hub_id = str(hub_id) if hub_id not in (None, "") else uuid.uuid4().hex[:8]

    return {
        "id": hub_id,
        "label": label,
        "category_id": _coerce_int(entry.get("category_id")),
        "hub_channel_id": hub_channel_id,
        "template": template,
        "user_limit": user_limit,
        "max_rooms": max_rooms,
        "private": bool(entry.get("private", False)),
    }


def normalize_hubs(blob):
    """Return a clean list of at most ``MAX_HUBS`` valid hub dicts from ``blob``.

    ``blob`` is whatever came back from settings (ideally a list). Non-list
    input yields an empty list; malformed entries are dropped; limits are
    clamped. Only the first ``MAX_HUBS`` survivors are kept so a corrupted
    store can never balloon past the policy cap.
    """
    if not isinstance(blob, list):
        return []
    hubs = []
    for entry in blob:
        hub = _normalize_one(entry)
        if hub is not None:
            hubs.append(hub)
        if len(hubs) >= MAX_HUBS:
            break
    return hubs


def can_add_hub(hubs):
    """True while the guild is below the ``MAX_HUBS`` cap."""
    return len(hubs) < MAX_HUBS


def channels_needed(hubs):
    """Worst-case channel footprint of ``hubs`` against the 500 guild budget.

    Each hub permanently costs its category and trigger channel, plus up to
    ``max_rooms`` temp rooms at peak. The cog compares this against the guild's
    live channel count to warn before adding a hub would blow the budget.
    """
    total = 0
    for hub in hubs:
        max_rooms = _coerce_int(hub.get("max_rooms"))
        max_rooms = DEFAULT_MAX_ROOMS if max_rooms is None else _clamp(max_rooms, 1, MAX_ROOMS)
        total += HUB_OVERHEAD_CHANNELS + max_rooms
    return total


def summarise_hub(hub):
    """Return a one-line English summary of ``hub`` for the setup panel.

    Kept translation-free on purpose: this is a pure formatting helper the cog
    can wrap or localise. Fields are read defensively so it never raises on a
    partially-formed hub.
    """
    limit = _coerce_int(hub.get("user_limit")) or 0
    limit_text = "unlimited" if limit <= 0 else str(limit)
    max_rooms = _coerce_int(hub.get("max_rooms"))
    max_rooms = DEFAULT_MAX_ROOMS if max_rooms is None else max_rooms
    lock = "private" if hub.get("private") else "open"
    template = hub.get("template") if isinstance(hub.get("template"), str) else DEFAULT_TEMPLATE
    template = template or DEFAULT_TEMPLATE
    label = hub.get("label") or DEFAULT_LABEL
    return "{label} - limit {limit}, up to {rooms} rooms ({lock}) - template: {template}".format(
        label=label,
        limit=limit_text,
        rooms=max_rooms,
        lock=lock,
        template=template,
    )


# --- Per-room control panel (voicemaster) -----------------------------------
# The live per-room state (limit, name, lock, hide, blacklist) is pushed onto
# the Discord channel itself and only the owner is held in memory. The helpers
# below are the pure, deterministic pieces the control view leans on; every
# Discord/DB side effect stays in the cog.

# Sensible user-limit choices offered by the room control panel's slot picker.
# 0 means "unlimited"; Discord caps a voice channel at 99 members.
SLOT_VALUES = (0, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 25, 50, 99)


def slot_value_label(value):
    """Human label for a slot value: 0 (or less) reads 'Unlimited', else the count.

    Kept translation-free on purpose (the cog wraps it): garbage or negative
    input collapses to the unlimited case so the picker never renders a broken
    option.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return "Unlimited" if value <= 0 else str(value)


def blacklisted_targets(pairs):
    """Filter ``(target, connect)`` pairs to targets explicitly denied Connect.

    ``pairs`` is any iterable of ``(target, connect_value)`` where
    ``connect_value`` is the tri-state Connect permission (``True``/``False``/
    ``None``) read from a channel overwrite. A target is blacklisted only when
    its Connect is *explicitly* ``False``; ``None`` (unset) and ``True`` are not
    blacklists. Targets are returned in first-seen order, de-duplicated by
    identity so a target never appears twice.
    """
    out = []
    seen = set()
    for target, connect in pairs:
        if connect is False and id(target) not in seen:
            out.append(target)
            seen.add(id(target))
    return out


def claimable(owner_id, member_ids):
    """True when a room may be claimed: no owner, or the owner has left.

    ``member_ids`` is the collection of user ids currently in the voice channel.
    A room is claimable when it has no recorded owner (``owner_id`` is ``None``)
    or when the recorded owner is no longer present in the channel.
    """
    if owner_id is None:
        return True
    return owner_id not in set(member_ids)


def owner_from_overwrites(pairs):
    """Return the first target explicitly granted ``manage_channels``, else None.

    ``pairs`` is any iterable of ``(target, manage_channels_value)`` where the
    manage value is the tri-state permission overwrite (``True``/``False``/
    ``None``). Room ownership is marked on the Discord channel by an explicit
    ``manage_channels`` grant, so the owner is the first target whose value is
    exactly ``True``; ``None`` (unset) and ``False`` do not own. Targets are
    considered in iteration order and the first owner found is returned, or
    ``None`` when no target is granted the permission.

    This is the channel-backed source of truth for ownership that lets a control
    panel recover its owner after a bot restart wipes the in-memory cache.
    """
    for target, manage in pairs:
        if manage is True:
            return target
    return None
