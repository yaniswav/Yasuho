"""Dashboard -> bot action queue: the bot side of an in-process work queue.

The Remix dashboard is a SEPARATE Node process with no Discord gateway
connection, so it cannot itself do things that require the live bot - e.g.
posting the persistent Verify button into a channel. Instead it enqueues the
request as a row in the ``dashboard_actions`` table (written under its
``requireManageGuild`` gate) and fires::

    SELECT pg_notify('yasuho_dashboard_action', '<id>')

on a channel DEDICATED to this queue (distinct from the ``yasuho_dashboard``
cache-invalidation channel that ``cogs/system/dashboard_sync.py`` owns). This
cog LISTENs on that channel over its OWN dedicated asyncpg connection (separate
from both the shared pool and the sync cog's listen connection) and, per
notification, drives the action to completion.

Design (mirrors the house patterns and the security brief):

* CLAIM-then-run, single-flight: ``_claim`` runs
  ``UPDATE dashboard_actions SET status='running' ... WHERE id=$1 AND
  status='pending' RETURNING guild_id, kind, payload``. Because the guard is
  ``status='pending'`` and the UPDATE is atomic, exactly ONE caller can claim a
  row; a duplicate notify (or a notify racing the boot reconciliation) finds no
  ``pending`` row and is a silent no-op. This is the idempotence backstop.
* The claimed ``guild_id`` is AUTHORITATIVE (the dashboard wrote it under its
  manage-guild check); the executor re-validates EVERYTHING else in the payload
  against the live gateway state (guild present, channel present + a text
  channel, bot may send) and NEVER trusts the payload. ``result`` never carries
  a secret or a stack trace - only short machine-readable error codes.
* Boot reconciliation (``reconcile``): a notify emitted while the bot was
  restarting is lost (LISTEN/NOTIFY does not buffer), so once at startup we
  expire actions too old to still be wanted, reset every ``running`` row whose
  claim is older than a short grace window (``_ORPHAN_RESET_SECONDS``) back to
  ``pending``, and re-drive every remaining ``pending`` row through the SAME
  claim path. The age guard matters because the listener is attached BEFORE
  reconcile runs, so a live handler of THIS process may already hold a freshly
  claimed ``running`` row; its ``updated_at`` (stamped by the claim) is inside
  the window, so it is NOT mistaken for an orphan and re-driven. Delivery is
  still at-least-once, but a duplicate is now possible only on a crash AFTER an
  action's side effect but BEFORE its status write (a duplicate Verify button,
  low harm) - the price of never silently dropping one.

Everything is defensive: a malformed payload, a missing guild/channel, a DB
blip or an executor exception is caught, logged without secrets, and recorded as
a ``failed`` result; a single bad action can never take down the listener, and a
dropped listen connection is re-established with backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
import discord
from discord.ext import commands

from tools import i18n, role_menus
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# The Postgres NOTIFY channel the dashboard publishes action ids on. DEDICATED
# to this queue - deliberately NOT 'yasuho_dashboard' (the cache-sync channel).
CHANNEL = "yasuho_dashboard_action"

# Reconnect backoff bounds for the listen connection supervisor (match dashboard_sync).
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
# Active liveness probe cadence: a dropped TCP socket is not always reflected by
# is_closed() until a query runs, so a light SELECT 1 detects a dead conn promptly.
_KEEPALIVE_INTERVAL = 30.0

# A pending/running action older than this at boot is considered stale and is
# marked failed rather than replayed - a request enqueued long before a restart
# is very likely no longer wanted. Generous enough to survive a slow restart.
_STALE_ACTION_MINUTES = 60

# Grace window before boot reconciliation resets a 'running' row back to
# 'pending'. The listener is attached BEFORE reconcile runs (see _supervise), so
# a live handler of THIS process may already hold a freshly claimed 'running'
# row; _claim stamps updated_at = now() on claim, so that row's updated_at is
# inside this window and the age-guarded reset skips it - only rows orphaned by a
# dead previous process (stale updated_at) are reset and re-driven. Comfortably
# exceeds any executor's runtime, so a genuinely in-flight claim is never mistaken
# for an orphan (which would re-run its side effect and double the panel/menu).
_ORPHAN_RESET_SECONDS = 30

# Defensive cap on a custom embed message copied from the payload (Discord's
# embed description limit is 4096; the /verify setup path is bounded like this).
_MAX_MESSAGE_LEN = 2000


# ---------------------------------------------------------------------------
# Defensive payload / id parsing (never raises).
# ---------------------------------------------------------------------------


def _parse_action_id(payload):
    """Parse a NOTIFY payload (a bare decimal action id) into a positive int.

    The dashboard notifies with just ``String(id)``. Anything that is not a
    positive integer string is rejected (the row-level claim then never runs).
    """
    if not isinstance(payload, (str, bytes, bytearray)):
        return None
    try:
        value = int(payload)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _coerce_payload(raw):
    """Coerce a JSONB column value into a dict. Never raises.

    asyncpg returns a JSONB column as a ``str`` unless a codec is registered
    (this bot registers none - see ``tools.settings._load``, which handles both
    shapes), so accept a dict, a JSON string, or fall back to ``{}``.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


# ---------------------------------------------------------------------------
# Executors: kind -> async handler(bot, guild_id, payload) -> result dict.
# Each RE-VALIDATES the payload against live state and returns a JSON-safe dict
# ``{"ok": bool, ...}``. A short ``error`` code on failure - never a secret.
# ---------------------------------------------------------------------------


def _verify_view_cls():
    """Return the persistent ``VerifyView`` class, imported lazily.

    ``cogs.config.verification`` builds ``discord.ui`` classes at import time
    (discord.py 2.x only), so importing it at module load would break this cog's
    import on the 3.7/discord.py-1.5 test box. Deferring the import keeps the
    module importable everywhere; the seam is also the monkeypatch point the
    executor tests use to avoid pulling in ``discord.ui`` at all.
    """
    from cogs.config.verification import VerifyView

    return VerifyView


def _embed_creator():
    """Return the ``tools.embed_creator`` module, imported lazily.

    ``embed_creator`` builds ``discord.ui`` modal classes at import time
    (discord.py 2.x only), so importing it at module load would break this cog's
    import on the 3.7/discord.py-1.5 test box. Deferring keeps the module
    importable everywhere; the seam is also the monkeypatch point the button-panel
    executor tests use to avoid pulling in ``discord.ui`` at all.
    """
    from tools import embed_creator

    return embed_creator


def _button_roles_module():
    """Return the ``cogs.config.buttonroles`` module, imported lazily.

    Same rationale as ``_verify_view_cls`` / ``_embed_creator``: buttonroles
    defines ``discord.ui.Button`` / ``discord.ui.View`` subclasses (``ButtonRoleButton``
    / ``ButtonRoleView``) at import time, so importing it eagerly would break this
    cog on the discord.py-1.5 box. The button-panel post executor REUSES
    ``ButtonRoleView`` (and ``MAX_BUTTONS``) from here, exactly like the cog's own
    ``_do_post`` builds it, so a dashboard-posted panel behaves identically to a
    ``/buttonrole`` one. Tests monkeypatch this seam.
    """
    from cogs.config import buttonroles

    return buttonroles


def _role_menus_module():
    """Return the ``cogs.config.rolemenus`` module, imported lazily.

    Same rationale as ``_button_roles_module``: rolemenus defines
    ``discord.ui.Select`` / ``discord.ui.View`` subclasses (``RoleMenuSelect`` /
    ``RoleMenuView``) at import time, so importing it eagerly would break this cog
    on the discord.py-1.5 box. The role-menu post executor REUSES ``RoleMenuView``
    (and ``MAX_MENUS_PER_GUILD``) from here, exactly like the cog's own
    ``RoleMenuBuilder.post`` builds it, so a dashboard-posted menu behaves
    identically to a ``/rolemenu`` one. Tests monkeypatch this seam.
    """
    from cogs.config import rolemenus

    return rolemenus


async def _exec_verify_button_post(bot, guild_id, payload):
    """Post the persistent Verify button embed into a channel.

    Payload: ``{"channel_id": "<snowflake>", "message"?: "<custom text>"}``.
    ``guild_id`` is authoritative (from the claimed row); EVERYTHING else is
    re-validated here against the live gateway - the payload is never trusted:
    the guild must be present, the channel must exist, be a text channel, and
    the bot must be allowed to send there. The Verify ROLE is intentionally NOT
    required to be configured: the button reads the role at click time and
    reports if it is unset, so posting the button first (then setting the role)
    is a valid order.
    """
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}

    channel = guild.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}
    if not isinstance(channel, discord.TextChannel):
        return {"ok": False, "error": "not_text_channel"}

    me = guild.me
    if me is None:
        return {"ok": False, "error": "guild_unavailable"}
    if not channel.permissions_for(me).send_messages:
        return {"ok": False, "error": "missing_send_permission"}

    # Custom message is optional free text; bound it and never translate it. Only
    # the default copy is localised, to the guild's configured language.
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        message = message[:_MAX_MESSAGE_LEN]
    else:
        message = None

    loc = await i18n.resolve_guild_locale(bot, guild)
    with i18n.locale(loc):
        embed = discord.Embed(
            title=_("Verification"),
            description=(
                message
                or _("Click the button below to verify and unlock the server.")
            ),
            colour=random_colour(),
        )

    sent = await channel.send(embed=embed, view=_verify_view_cls()())
    return {
        "ok": True,
        "channel_id": str(channel.id),
        "message_id": str(getattr(sent, "id", "")),
    }


async def _exec_reaction_role_add(bot, guild_id, payload):
    """Add a reaction-role mapping: react on a live message and store the pair.

    Payload: ``{"channel_id", "message_id", "role_id"}`` (snowflake strings) plus
    ``"emoji"``. ``guild_id`` is authoritative (the claimed row, written under the
    dashboard's manage-guild gate); EVERYTHING else is re-validated here against
    the live gateway and NEVER trusted: the guild must be present, the channel
    must exist in THIS guild, the role must be a real assignable role of it, and
    the emoji must be non-empty. Only then do we fetch the message and add the
    reaction (a failure there -- gone message, missing add-reactions permission,
    a bad emoji -- yields a short code, never a stack).

    On success it upserts ``reaction_roles`` (keyed on (message_id, emoji), so a
    re-add just repoints the role) with the AUTHORITATIVE ``guild_id``, then live-
    patches the ReactionRoles cog's in-memory ``cache`` -- CRUCIAL, because
    ``on_raw_reaction_add`` reads that cache, not the table, on every reaction.
    The emoji is stored WITHOUT U+FE0F to match an incoming reaction payload,
    exactly like the cog's own ``_persist_reaction_role``.
    """
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_not_found"}
    try:
        role_id = int(payload.get("role_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_role"}

    emoji = payload.get("emoji")
    if not isinstance(emoji, str) or not emoji.strip():
        return {"ok": False, "error": "bad_emoji"}
    emoji = emoji.strip()

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}

    channel = guild.get_channel_or_thread(channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}

    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "error": "bad_role"}

    # Fetch first (a missing / inaccessible message is distinct from a reaction
    # that can't be added), then react. Both raise on failure and are mapped to a
    # short code -- the message may be gone, or the bot may lack add-reactions /
    # read-history in a channel that nonetheless "exists".
    try:
        msg = await channel.fetch_message(message_id)
    except Exception:
        return {"ok": False, "error": "message_not_found"}
    try:
        await msg.add_reaction(emoji)
    except Exception:
        return {"ok": False, "error": "cant_add_reaction"}

    stored = emoji.replace("\uFE0F", "")

    query = """
        INSERT INTO reaction_roles
        (message_id, emoji, role_id, guild_id)
        VALUES
        ($1, $2, $3, $4)
        ON CONFLICT (message_id, emoji) DO UPDATE SET role_id = $3;
        """
    await bot.db_pool.execute(query, message_id, stored, role_id, guild_id)

    # Live-patch the cog cache so the very next reaction is honoured without a
    # restart (on_raw_reaction_add reads self.cache). No-op if the cog is absent.
    cog = bot.get_cog("ReactionRoles")
    if cog is not None:
        cog.cache[(message_id, stored)] = role_id

    return {
        "ok": True,
        "message_id": str(message_id),
        "emoji": stored,
        "role_id": str(role_id),
    }


async def _exec_reaction_role_remove(bot, guild_id, payload):
    """Remove a reaction-role mapping: drop the row (guild-scoped) + cache entry.

    Payload: ``{"message_id", "emoji"}``. ``guild_id`` is authoritative (the
    claimed row): the DELETE is scoped to it so a crafted request can never wipe
    another guild's mapping by guessing a message id. The cog cache entry is
    popped so ``on_raw_reaction_add`` stops granting immediately. Best-effort, we
    also try to strip the bot's own reaction from the message IF it is still in
    the gateway message cache (the payload carries no channel id, so we cannot
    fetch it by REST); any failure there is ignored -- a leftover reaction is
    cosmetic, and never affects the ``ok`` result.
    """
    emoji = payload.get("emoji")
    if not isinstance(emoji, str):
        emoji = ""
    stored = emoji.replace("\uFE0F", "")
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_not_found"}

    query = """
        DELETE FROM reaction_roles
        WHERE message_id = $1 AND emoji = $2 AND guild_id = $3;
        """
    await bot.db_pool.execute(query, message_id, stored, guild_id)

    cog = bot.get_cog("ReactionRoles")
    if cog is not None:
        cog.cache.pop((message_id, stored), None)

    # Best-effort: unreact if the message is still cached (no channel id to fetch
    # by). Never let a hiccup here fail the removal.
    try:
        guild = bot.get_guild(guild_id)
        message = discord.utils.get(bot.cached_messages, id=message_id)
        if (
            guild is not None
            and message is not None
            and getattr(message.guild, "id", None) == guild_id
            and guild.me is not None
        ):
            await message.remove_reaction(emoji or stored, guild.me)
    except Exception:
        pass

    return {"ok": True}


# Discord caps a role button's label at 80 chars; bound it exactly like the cog.
_MAX_BUTTON_LABEL = 80


async def _exec_button_panel_post(bot, guild_id, payload):
    """Post an embed + self-assignable role buttons panel into a channel.

    Payload: ``{"channel_id", "embed": {<embed_creator blob>},
    "buttons": [{"role_id", "label"?, "emoji"?, "style"}]}``. ``guild_id`` is
    authoritative (the claimed row, written under the dashboard's manage-guild
    gate); EVERYTHING else is re-validated here against the live gateway and NEVER
    trusted: the guild must be present, the channel must exist in THIS guild, be a
    text channel and be sendable, there must be 1..MAX_BUTTONS buttons, and each
    role must be a real role of the guild. Style is coerced to a callable
    ButtonStyle (1/2/3/4, secondary fallback), the label is bounded to 80 (empty
    -> the role name), the emoji is optional, and role ids are DE-DUPLICATED (one
    button per role, mirroring the ``(message_id, role_id)`` primary key).

    This REPLICATES the cog's ``ButtonRoles._do_post`` / ``_persist``: it renders
    the embed via ``embed_creator.render`` (the same blob shape the dashboard's
    Embed Builder produces), sends it with a ``ButtonRoleView`` REUSED from the
    cog, persists one ``button_roles`` row per button (message-authoritative:
    DELETE the message's rows then re-INSERT), and RE-REGISTERS the persistent
    view via ``bot.add_view`` so the buttons keep working after a restart of THIS
    process (a restart of the bot re-registers them from the table in
    ``ButtonRoles.cog_load``). The rendered embed is NOT stored -- it lives in the
    posted message, so a panel's embed cannot be edited from the dashboard.
    """
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}

    channel = guild.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}
    if not isinstance(channel, discord.TextChannel):
        return {"ok": False, "error": "not_text_channel"}

    me = guild.me
    if me is None:
        return {"ok": False, "error": "guild_unavailable"}
    if not channel.permissions_for(me).send_messages:
        return {"ok": False, "error": "missing_send_permission"}

    br = _button_roles_module()
    max_buttons = getattr(br, "MAX_BUTTONS", 25)

    raw_buttons = payload.get("buttons")
    if not isinstance(raw_buttons, list) or not raw_buttons:
        return {"ok": False, "error": "no_buttons"}
    if len(raw_buttons) > max_buttons:
        return {"ok": False, "error": "too_many_buttons"}

    # Validate + normalise each button; dedup by role (the PK is (message, role)).
    seen_roles = set()
    buttons = []
    for entry in raw_buttons:
        if not isinstance(entry, dict):
            continue
        try:
            role_id = int(entry.get("role_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_role"}
        role = guild.get_role(role_id)
        if role is None:
            return {"ok": False, "error": "bad_role"}
        # Mirror the /buttonrole builder's assignability guard (BuilderView.
        # _can_assign) so a dashboard write can't persist a button for a
        # dead/dangerous role: @everyone, an integration-managed role, or one
        # at/above our own top role.
        if role.is_default() or role.managed or not (role < me.top_role):
            return {"ok": False, "error": "role_not_assignable"}
        if role_id in seen_roles:
            continue  # one button per role, mirroring the primary key
        seen_roles.add(role_id)

        try:
            style = int(entry.get("style"))
        except (TypeError, ValueError):
            style = 2
        if style not in (1, 2, 3, 4):
            style = 2

        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            label = role.name
        label = label[:_MAX_BUTTON_LABEL]

        emoji = entry.get("emoji")
        if not isinstance(emoji, str) or not emoji.strip():
            emoji = None
        else:
            emoji = emoji.strip()

        buttons.append(
            {"role_id": role_id, "label": label, "emoji": emoji, "style": style}
        )

    if not buttons:
        return {"ok": False, "error": "no_buttons"}

    # Render the embed through the SAME path as the cog + the dashboard preview.
    ec = _embed_creator()
    embed_blob = payload.get("embed")
    if not isinstance(embed_blob, dict):
        embed_blob = {}
    embed = ec.render(embed_blob)
    if not ec.embed_has_content(embed):
        return {"ok": False, "error": "empty_embed"}

    # rows shape matches ButtonRoleView.__init__: (role_id, label, emoji, style).
    rows = [(b["role_id"], b["label"], b["emoji"], b["style"]) for b in buttons]
    msg = await channel.send(embed=embed, view=br.ButtonRoleView(rows))

    # Persist message-authoritatively, exactly like BuilderView._persist: replace
    # the message's whole stored set so nothing stale lingers.
    records = [
        (
            msg.id,
            guild_id,
            channel.id,
            b["role_id"],
            b["label"][:_MAX_BUTTON_LABEL],
            b["emoji"],
            int(b["style"]),
        )
        for b in buttons
    ]
    async with bot.db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM button_roles WHERE message_id = $1;", msg.id
            )
            await conn.executemany(
                """
                INSERT INTO button_roles
                (message_id, guild_id, channel_id, role_id, label, emoji, style)
                VALUES ($1, $2, $3, $4, $5, $6, $7);
                """,
                records,
            )

    # Re-register the persistent view so the buttons survive a restart of THIS
    # process (the cog rebuilds it from the table on the bot's next boot).
    try:
        bot.add_view(br.ButtonRoleView(rows), message_id=msg.id)
    except Exception:
        log.exception(
            "dashboard_actions: failed to register button-role view for message %s",
            msg.id,
        )

    return {
        "ok": True,
        "message_id": str(msg.id),
        "channel_id": str(channel.id),
    }


async def _exec_button_panel_delete(bot, guild_id, payload):
    """Delete a button-role panel: drop its rows (guild-scoped) + strip the buttons.

    Payload: ``{"message_id"}``. ``guild_id`` is authoritative (the claimed row):
    the DELETE is scoped to it so a crafted request can never wipe another guild's
    panel by guessing a message id. ``RETURNING channel_id`` lets us best-effort
    fetch the message and ``msg.edit(view=None)`` to strip the live buttons (so an
    attached announcement keeps its content); any failure there is cosmetic and
    never affects the ``ok`` result. Mirrors the cog's ``buttonrole_remove``.
    """
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_not_found"}

    rows = await bot.db_pool.fetch(
        "DELETE FROM button_roles "
        "WHERE message_id = $1 AND guild_id = $2 "
        "RETURNING channel_id;",
        message_id,
        guild_id,
    )

    # Best-effort: strip the buttons off the message. Never let a hiccup here fail
    # the delete (the rows are already gone).
    if rows:
        try:
            guild = bot.get_guild(guild_id)
            channel = (
                guild.get_channel_or_thread(rows[0]["channel_id"])
                if guild is not None
                else None
            )
            if channel is not None:
                msg = await channel.fetch_message(message_id)
                await msg.edit(view=None)
        except Exception:
            pass

    return {"ok": True}


# Role-menu header bounds, mirrored from cogs/config/rolemenus.py's builder modals:
# the embed title caps at 256 and the description at 2000 (Discord's own embed
# limits are higher, but the builder bounds them there). The select placeholder
# caps at 150 (Discord's placeholder limit). The colour is a 24-bit RGB int.
_MAX_MENU_TITLE = 256
_MAX_MENU_DESCRIPTION = 2000
_MAX_MENU_PLACEHOLDER = 150
_MAX_COLOUR = 0xFFFFFF


def _coerce_menu_options(raw):
    """Widen each option's STRING role_id to a Python int, then reuse the helper.

    The dashboard serialises every snowflake as a STRING (never a JS number, to
    dodge 2^53 precision loss), but ``role_menus.normalize_options`` requires an
    ``int`` role_id and drops anything else. So we do the SAME boundary conversion
    the reaction/button executors do (``int(...)`` in Python, which is arbitrary
    precision) on each option's role_id, then hand the list to the shared helper
    for all the real work (drop/dedup/cap-at-25, label/emoji/description/temp).
    A non-string, non-int, or unparseable role_id is left as-is so the helper
    drops it. Never raises.
    """
    if not isinstance(raw, list):
        return role_menus.normalize_options(raw)
    widened = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("role_id")
        if isinstance(rid, str):
            try:
                rid = int(rid)
            except ValueError:
                continue  # not a decimal id: the helper would drop it anyway
        entry = dict(entry)
        entry["role_id"] = rid
        widened.append(entry)
    return role_menus.normalize_options(widened)


async def _exec_role_menu_post(bot, guild_id, payload):
    """Post a self-role select menu into a channel + persist + register its view.

    Payload: ``{"channel_id": "<snowflake>", "config": {<menu config>}}``.
    ``guild_id`` is authoritative (the claimed row, written under the dashboard's
    manage-guild gate); EVERYTHING else is re-validated here against the live
    gateway and NEVER trusted: the guild must be present, the channel must exist in
    THIS guild, be a text channel and be sendable, the guild must be under
    MAX_MENUS_PER_GUILD, the option list (normalised through the SAME
    ``role_menus.normalize_options`` helper the cog uses) must be non-empty AND at
    least one kept option's role must be a real role of this guild (foreign/gone
    roles are filtered out; an all-foreign list is rejected). Title/description are
    bounded, the colour is an optional valid 24-bit int and the placeholder is
    bounded.

    This REPLICATES the cog's ``RoleMenuBuilder.post``: it builds the header embed
    from the (bounded) title/description/colour + a Roles field, POSTS it with NO
    view FIRST to learn the message id, THEN edits the message to attach a
    ``RoleMenuView`` REUSED from the cog whose select custom_id is
    ``rolemenu:<message_id>`` -- message-unique and restart-stable, which is why the
    post-then-edit sequence is needed (the view cannot be built before the id
    exists). It then persists the ``role_menus`` row (config normalised) with the
    AUTHORITATIVE guild_id via the SAME INSERT ... ON CONFLICT the cog's
    ``store_menu`` uses, and RE-REGISTERS the persistent view via ``bot.add_view``
    so the select survives a restart of THIS process (the cog rebuilds it from the
    table on the bot's next boot). If the RoleMenus cog is loaded, the new message
    id is added to its in-memory ``_menu_ids`` set so deleting the message still
    prunes the row (parity with ``store_menu``).
    """
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}

    channel = guild.get_channel(channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}
    if not isinstance(channel, discord.TextChannel):
        return {"ok": False, "error": "not_text_channel"}

    me = guild.me
    if me is None:
        return {"ok": False, "error": "guild_unavailable"}
    if not channel.permissions_for(me).send_messages:
        return {"ok": False, "error": "missing_send_permission"}

    rm = _role_menus_module()
    max_menus = getattr(rm, "MAX_MENUS_PER_GUILD", 25)

    # Enforce the per-guild cap BEFORE posting, counting this guild's live menus
    # (mirrors the cog's _menu_count gate on the /rolemenu builder).
    count = await bot.db_pool.fetchval(
        "SELECT COUNT(*) FROM role_menus WHERE guild_id = $1", guild_id
    )
    if (count or 0) >= max_menus:
        return {"ok": False, "error": "too_many_menus"}

    raw_config = payload.get("config")
    if not isinstance(raw_config, dict):
        raw_config = {}

    # Normalise through the SAME helper the cog uses (drops/dedups/caps at 25).
    options = _coerce_menu_options(raw_config.get("options"))
    if not options:
        return {"ok": False, "error": "no_options"}

    # belongs-to-guild defence: keep only options whose role is a real role of THIS
    # guild. A crafted payload naming only foreign/gone roles is rejected wholesale
    # rather than posting an empty menu.
    options = [o for o in options if guild.get_role(o["role_id"]) is not None]
    if not options:
        return {"ok": False, "error": "bad_role_all"}

    title = raw_config.get("title")
    title = title[:_MAX_MENU_TITLE] if isinstance(title, str) else ""
    description = raw_config.get("description")
    description = (
        description[:_MAX_MENU_DESCRIPTION] if isinstance(description, str) else ""
    )
    colour = raw_config.get("colour")
    if not (
        isinstance(colour, int)
        and not isinstance(colour, bool)
        and 0 <= colour <= _MAX_COLOUR
    ):
        colour = None
    exclusive = bool(raw_config.get("exclusive"))
    placeholder = raw_config.get("placeholder")
    placeholder = (
        placeholder[:_MAX_MENU_PLACEHOLDER]
        if isinstance(placeholder, str) and placeholder.strip()
        else None
    )

    # The persisted + view config, in the SAME shape the cog's post() stores.
    config = {
        "title": title,
        "description": description,
        "colour": colour,
        "exclusive": exclusive,
        "options": options,
    }
    if placeholder:
        config["placeholder"] = placeholder

    # Build the header embed exactly like the cog's header_embed (title/description/
    # colour + a Roles field). Only the fallback copy is localised, to the guild's
    # configured language (the user-supplied title/description are left verbatim).
    loc = await i18n.resolve_guild_locale(bot, guild)
    with i18n.locale(loc):
        embed = discord.Embed(
            title=title or _("Pick your roles"),
            description=description or None,
            colour=colour if isinstance(colour, int) else random_colour(),
        )
        embed.add_field(
            name=_("Roles"),
            value=" ".join(f"<@&{o['role_id']}>" for o in options)[:1024],
            inline=False,
        )

    # Post first (no view) to learn the message id, then attach the view so its
    # select carries a message-unique, restart-stable custom_id -- the cog's trick.
    message = await channel.send(embed=embed)
    view = rm.RoleMenuView(message.id, config)
    try:
        await message.edit(view=view)
    except discord.HTTPException:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return {"ok": False, "error": "post_failed"}

    # Persist the row with the AUTHORITATIVE guild_id, exactly like store_menu.
    await bot.db_pool.execute(
        "INSERT INTO role_menus (message_id, guild_id, channel_id, config) "
        "VALUES ($1, $2, $3, $4::jsonb) "
        "ON CONFLICT (message_id) DO UPDATE SET config = $4::jsonb",
        message.id,
        guild_id,
        channel.id,
        json.dumps(config),
    )

    # Re-register the persistent view so the select survives a restart of THIS
    # process (the cog rebuilds it from the table on the bot's next boot).
    try:
        bot.add_view(view, message_id=message.id)
    except Exception:
        log.exception(
            "dashboard_actions: failed to register role-menu view for message %s",
            message.id,
        )
    # Keep the cog's live id set in sync so deleting the message prunes the row.
    cog = bot.get_cog("RoleMenus")
    if cog is not None and hasattr(cog, "_menu_ids"):
        cog._menu_ids.add(message.id)

    return {"ok": True, "message_id": str(message.id), "menu": True}


async def _exec_role_menu_delete(bot, guild_id, payload):
    """Delete a role menu: drop its row (guild-scoped) + strip the live select.

    Payload: ``{"message_id"}``. ``guild_id`` is authoritative (the claimed row):
    the DELETE is scoped to it so a crafted request can never wipe another guild's
    menu by guessing a message id. ``RETURNING channel_id`` lets us best-effort
    fetch the message and ``msg.edit(view=None)`` to strip the live select; any
    failure there is cosmetic and never affects the ``ok`` result. The message id
    is also dropped from the RoleMenus cog's in-memory ``_menu_ids`` set (parity
    with the cog's own on_raw_message_delete pruning).
    """
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_not_found"}

    rows = await bot.db_pool.fetch(
        "DELETE FROM role_menus "
        "WHERE message_id = $1 AND guild_id = $2 "
        "RETURNING channel_id;",
        message_id,
        guild_id,
    )

    cog = bot.get_cog("RoleMenus")
    if cog is not None and hasattr(cog, "_menu_ids"):
        cog._menu_ids.discard(message_id)

    # Best-effort: strip the select off the message. Never let a hiccup here fail
    # the delete (the row is already gone).
    if rows:
        try:
            guild = bot.get_guild(guild_id)
            channel = (
                guild.get_channel_or_thread(rows[0]["channel_id"])
                if guild is not None
                else None
            )
            if channel is not None:
                msg = await channel.fetch_message(message_id)
                await msg.edit(view=None)
        except Exception:
            pass

    return {"ok": True}


_EXECUTORS = {
    "verify_button_post": _exec_verify_button_post,
    "reaction_role_add": _exec_reaction_role_add,
    "reaction_role_remove": _exec_reaction_role_remove,
    "button_panel_post": _exec_button_panel_post,
    "button_panel_delete": _exec_button_panel_delete,
    "role_menu_post": _exec_role_menu_post,
    "role_menu_delete": _exec_role_menu_delete,
}


# ---------------------------------------------------------------------------
# Claim / finish / dispatch (pure-ish, testable without the listen connection).
# All queries ride the SHARED pool (bot.db_pool); the dedicated connection below
# is ONLY for LISTEN.
# ---------------------------------------------------------------------------


async def _claim(pool, action_id):
    """Atomically claim a pending action. Returns the claimed row or ``None``.

    The ``status='pending'`` guard makes this single-flight: a duplicate notify
    (or a notify racing the boot reconciliation) finds no pending row and gets
    ``None`` back - the idempotence backstop.
    """
    return await pool.fetchrow(
        "UPDATE dashboard_actions "
        "SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'pending' "
        "RETURNING guild_id, kind, payload",
        action_id,
    )


async def _finish(pool, action_id, status, result):
    """Write the terminal ``status`` + ``result`` JSON back for an action."""
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = $1, result = $2::jsonb, updated_at = now() "
        "WHERE id = $3",
        status,
        json.dumps(result),
        action_id,
    )


async def handle_action(bot, action_id):
    """Claim, dispatch and finalise one action. Never raises.

    Returns the terminal status (``'done'`` / ``'failed'``) it wrote, or
    ``None`` when there was nothing to do (already claimed/processed, or the
    claim itself errored). Shared by both the notify path and reconciliation.
    """
    pool = bot.db_pool
    try:
        claimed = await _claim(pool, action_id)
    except Exception:
        # A claim failure (DB blip) must not crash the listener; the boot
        # reconciliation is the backstop that re-drives a still-pending row.
        log.exception("dashboard_actions: claim failed for id=%s", action_id)
        return None
    if claimed is None:
        return None  # already claimed elsewhere / not pending: silent no-op

    guild_id = claimed["guild_id"]
    kind = claimed["kind"]
    payload = _coerce_payload(claimed["payload"])

    executor = _EXECUTORS.get(kind)
    if executor is None:
        await _finalise(pool, action_id, {"ok": False, "error": "unknown_kind"})
        return "failed"

    try:
        result = await executor(bot, guild_id, payload)
    except Exception:
        # Never surface the exception text/stack to the dashboard - only a fixed
        # code. The full traceback is logged server-side.
        log.exception(
            "dashboard_actions: executor %r failed for id=%s", kind, action_id
        )
        await _finalise(pool, action_id, {"ok": False, "error": "internal_error"})
        return "failed"

    if not isinstance(result, dict):
        result = {"ok": False, "error": "internal_error"}
    return await _finalise(pool, action_id, result)


async def _finalise(pool, action_id, result):
    """Persist ``result`` with the derived status; returns that status.

    An ``ok`` result is ``done``; a well-formed failure (validation, unknown
    kind, ...) is ``failed`` so the dashboard can surface ``result.error``. The
    write itself is guarded so a persistence blip cannot crash the loop.
    """
    status = "done" if result.get("ok") else "failed"
    try:
        await _finish(pool, action_id, status, result)
    except Exception:
        log.exception("dashboard_actions: failed to persist result for id=%s", action_id)
    return status


async def reconcile(bot):
    """Boot backstop: recover actions a missed notify would otherwise strand.

    LISTEN/NOTIFY does not buffer, so a notify fired while the bot was down is
    gone. Once at startup we (1) fail actions too old to still be wanted, (2)
    reset a ``running`` row back to ``pending`` ONLY once its claim is older than
    ``_ORPHAN_RESET_SECONDS`` - the listener is attached before this runs, so a
    live handler of THIS process may already hold a ``running`` row whose
    ``updated_at`` (stamped by ``_claim``) is recent; the age guard leaves that
    one alone and resets only rows orphaned by a dead previous process - and (3)
    re-drive every remaining ``pending`` row through the normal atomic claim - so
    a concurrent live notify for the same row still can't double-run it. A
    duplicate is therefore possible only when a crash lands AFTER an executor's
    side effect but BEFORE its status write. Never raises out of a per-row failure.
    """
    pool = bot.db_pool

    # (1) Expire the too-old. Bound age is a fixed constant, not user input,
    # but it's still passed as a bound parameter rather than interpolated.
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'failed', result = $2::jsonb, updated_at = now() "
        "WHERE status IN ('pending', 'running') "
        "AND created_at < now() - $1 * INTERVAL '1 minute'",
        _STALE_ACTION_MINUTES,
        json.dumps({"ok": False, "error": "expired"}),
    )

    # (2) Reset orphaned 'running' rows. Age-guarded: only rows whose claim is
    # older than the grace window are reset. The listener is already attached, so
    # a live handler of THIS process may hold a freshly claimed 'running' row
    # (recent updated_at, stamped by _claim); resetting it here would let step 3
    # re-claim and re-run its executor, doubling the side effect. Bound age is a
    # fixed constant but is still passed as a parameter rather than interpolated.
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'pending', updated_at = now() "
        "WHERE status = 'running' "
        "AND updated_at < now() - $1 * INTERVAL '1 second'",
        _ORPHAN_RESET_SECONDS,
    )

    # (3) Re-drive everything still pending, oldest first, one at a time.
    rows = await pool.fetch(
        "SELECT id FROM dashboard_actions WHERE status = 'pending' ORDER BY id"
    )
    for row in rows:
        try:
            await handle_action(bot, row["id"])
        except Exception:
            # handle_action already swallows its own errors; this is belt-and-
            # suspenders so one bad row never aborts the rest of the sweep.
            log.exception(
                "dashboard_actions: reconcile failed for id=%s", row["id"]
            )


# ---------------------------------------------------------------------------
# Cog: supervised dedicated LISTEN connection (mirrors DashboardSync).
# ---------------------------------------------------------------------------


class DashboardActions(commands.Cog):
    """LISTENs for dashboard action ids and drives each to completion."""

    def __init__(self, bot):
        self.bot = bot
        self._conn = None
        self._closing = False
        self._supervisor = None
        self._reconciled = False
        # Strong refs to per-notification / reconcile tasks so the loop can't GC
        # one mid-run (the dashboard_sync / sponsorblock pattern).
        self._handlers = set()

        self._dsn = config_loader.get("Database", "PostgreSQL", fallback=None)
        if not self._dsn:
            log.info(
                "dashboard_actions: no PostgreSQL DSN configured; queue disabled."
            )
            return

        self._supervisor = self.bot.loop.create_task(self._supervise())

        def _on_supervisor_done(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error(
                    "dashboard_actions: supervisor exited unexpectedly: %s", exc
                )

        self._supervisor.add_done_callback(_on_supervisor_done)

    # -- teardown -------------------------------------------------------
    async def cog_unload(self):
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
        for task in list(self._handlers):
            task.cancel()
        await self._teardown_connection()

    async def _teardown_connection(self):
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            await conn.remove_listener(CHANNEL, self._on_notify)
        except Exception:
            pass
        try:
            await conn.close()
        except Exception:
            pass

    def _track(self, task):
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    # -- listener callback ---------------------------------------------
    def _on_notify(self, connection, pid, channel, payload):
        """asyncpg listener callback: runs in the loop, so it must NOT await.

        Hands the (awaiting) work off to a tracked task. Never raises: a failure
        here would otherwise surface inside asyncpg's dispatch.
        """
        try:
            task = self.bot.loop.create_task(self._handle(payload))
        except Exception:
            log.exception("dashboard_actions: failed to schedule handler")
            return
        self._track(task)

    async def _handle(self, payload):
        action_id = _parse_action_id(payload)
        if action_id is None:
            return
        try:
            await handle_action(self.bot, action_id)
        except Exception:
            log.exception("dashboard_actions: handler crashed")

    # -- supervised listen connection ----------------------------------
    async def _supervise(self):
        """Keep the dedicated listen connection alive, reconnecting with backoff.

        Gated on ``wait_until_ready`` so the pool and the guilds exist before we
        react. Every failure path is caught; logs never carry the DSN.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            pass

        backoff = _BACKOFF_START
        while not self._closing:
            try:
                await self._connect_and_listen()
                backoff = _BACKOFF_START  # healthy connect resets the backoff
                self._maybe_reconcile()
                await self._watch_connection()
            except asyncio.CancelledError:
                break
            except Exception:
                log.warning(
                    "dashboard_actions: listen connection error; reconnecting in %.0fs",
                    backoff,
                )
            finally:
                await self._teardown_connection()

            if self._closing:
                break
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, _BACKOFF_MAX)

        log.info("dashboard_actions: listener supervisor stopped.")

    def _maybe_reconcile(self):
        """Schedule the one-shot boot reconciliation as a tracked task.

        Runs AFTER the listener is attached (so no live notify is lost while it
        works) and only once per process. Decoupled from the watch loop so a
        large backlog can't delay keepalive.
        """
        if self._reconciled:
            return
        self._reconciled = True

        async def _run():
            try:
                await reconcile(self.bot)
            except Exception:
                log.exception("dashboard_actions: boot reconciliation failed")

        self._track(self.bot.loop.create_task(_run()))

    async def _connect_and_listen(self):
        conn = await asyncpg.connect(self._dsn)
        self._conn = conn
        await conn.add_listener(CHANNEL, self._on_notify)
        log.info("dashboard_actions: listening on Postgres channel '%s'.", CHANNEL)

    async def _watch_connection(self):
        """Block while the connection is healthy; return to trigger a reconnect."""
        while not self._closing:
            conn = self._conn
            if conn is None or conn.is_closed():
                return
            try:
                await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("dashboard_actions: keepalive failed; reconnecting.")
                return
            await asyncio.sleep(_KEEPALIVE_INTERVAL)


async def setup(bot):
    await bot.add_cog(DashboardActions(bot))
