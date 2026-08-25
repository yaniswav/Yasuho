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
  status='pending' RETURNING guild_id, user_id, kind, payload``. Because the
  guard is ``status='pending'`` and the UPDATE is atomic, exactly ONE caller can
  claim a row; a duplicate notify (or a notify racing the boot reconciliation)
  finds no ``pending`` row and is a silent no-op. This is the idempotence
  backstop.
* The claimed SCOPE id is AUTHORITATIVE (the dashboard wrote it under its
  manage-guild check, or under "this is your own session" for a user row); the
  executor re-validates EVERYTHING else in the payload against the live gateway
  state (guild present, channel present + a text channel, bot may send) and
  NEVER trusts the payload. ``result`` never carries a secret or a stack trace -
  only short machine-readable error codes.
* TWO SCOPES, chosen by the KIND. A row names either a guild or a user (the DB
  CHECK ``dashboard_actions_scope_valid`` makes "exactly one" a schema fact), and
  :data:`_USER_KINDS` - the mirror of ``dashboard_sync.USER_KINDS`` - says which
  column a kind reads. Deriving the scope from the KIND rather than from
  whichever column happens to be populated is what stops a user-scoped kind
  written onto a guild row (or the reverse) from acting on the wrong thing: the
  mismatch yields a NULL id and the action is refused as ``bad_scope``. Every
  executor still receives that id as its second argument, so a guild executor is
  untouched by any of this.
* Reconciliation (``reconcile``): a notify emitted while the bot was
  restarting is lost (LISTEN/NOTIFY does not buffer), so at startup - and again
  after every RECONNECT of this listen connection, which leaves the very same
  hole in delivery, only without a restart to end it - we
  expire actions too old to still be wanted, reset every ``running`` row whose
  claim is older than a short grace window (``_ORPHAN_RESET_SECONDS``) and is not
  one this process is currently handling (``_INFLIGHT_ACTIONS``) back to
  ``pending``, and re-drive every remaining ``pending`` row through the SAME
  claim path. Both guards matter because the listener is attached BEFORE
  reconcile runs, so a live handler of THIS process may already hold a
  ``running`` row: freshly claimed (recent ``updated_at``, caught by the age
  guard) or long-running - an executor may outlast the window, which is what the
  in-flight mark covers. Delivery is
  still at-least-once, but a duplicate is now possible only on a crash AFTER an
  action's side effect but BEFORE its status write (a duplicate Verify button,
  low harm) - the price of never silently dropping one. The harm is NOT always
  cosmetic though: replaying ``autoroom_hub_create`` creates a SECOND real
  category + voice trigger pair on Discord, so the duplicate is bounded only by
  the ``MAX_HUBS`` gate the executor re-reads on every run (a replay that would
  push the guild over the cap is refused before anything is created).

Everything is defensive: a malformed payload, a missing guild/channel, a DB
blip or an executor exception is caught, logged without secrets, and recorded as
a ``failed`` result; a single bad action can never take down the listener, and a
dropped listen connection is re-established with backoff.

CONFIGURER RANK ON THE ROLE-PUBLISHING KINDS (and the surfaces still not gatable)
--------------------------------------------------------------------------------
FIVE kinds publish a role a member can then obtain by clicking:
``verify_button_post``, ``reaction_role_add``, ``button_panel_post``,
``button_panel_edit`` and ``role_menu_post``. Each of them now asks BOTH halves
of the self-role question, the same pair ``/verify setup``, ``/reactionrole``,
``/buttonrole`` and ``/rolemenu`` ask in Discord (both live in
:func:`_role_gate_failure`, which every one of the five calls):

* can Yasuho hand this role out at all (``modchecks.bot_can_assign_role``) -
  kept as its own call so its failure keeps its own code, ``role_not_assignable``;
* does the person who asked for this from the web app OUTRANK the role they are
  publishing (``modchecks.self_assignable_role_error``, which composes the
  hierarchy half with the bot half). Refused as ``role_above_actor``.

The ACTOR is the row's ``requested_by``: the dashboard writes the AUTHENTICATED
SESSION USER there on every ``enqueueBotAction`` call. :func:`_claim` returns the
column, :data:`_ACTOR_KINDS` says which kinds need it, and :func:`_handle_action`
resolves it to a :class:`discord.Member` of the row's guild ONCE, before the
executor is entered - the same discipline :func:`_scope_id` has: an executor of
one of these kinds can never run without a resolved actor, so it has nothing to
defend itself against.

FAIL CLOSED, always. ``requested_by`` is nullable in the schema (it predates this
gate as an audit column) and ``guild.get_member`` is a SPARSE cache here
(``chunk_guilds_at_startup=False`` in core.py), so "I could not check" must never
read as "go ahead". Three distinct refusal codes, so the dashboard can say
something useful:

* ``actor_missing`` - the row carries no usable ``requested_by``. NEVER falls back
  to the bot-half-only check.
* ``actor_left_guild`` - PROVEN absent: ``fetch_member`` returned 404.
* ``actor_unverified`` - membership could not be established (a cache miss whose
  one ``fetch_member`` hit a 403/5xx/timeout). Transient: the dashboard should
  offer a retry.

Cost: cache hit = free; cache miss = at most ONE
``GET /guilds/{id}/members/{user}`` per action. These are operator-driven
dashboard actions, not a hot path.

THE GATE RUNS AT PUBLISH TIME BECAUSE PUBLISH TIME IS THE ONLY GATE THERE IS.
All five kinds name their roles in the payload (or, for ``verify_button_post``,
in a setting read in that same instant), so check and publication are one moment
- and that moment is the last one at which anybody asks the question.
``ButtonRoleButton.callback`` (``cogs/config/buttonroles.py``) grants straight
off its own ``br:<role_id>`` custom_id with NO rank check, and nothing in this
bot re-examines a published role when that role later changes (there is no
``on_guild_role_update`` listener anywhere here - grep it). So the pair below is
load-bearing rather than belt-and-braces: skip it on ONE of the five and the
click path grants whatever was published, forever.

``button_panel_edit`` is held to exactly the same rule as ``button_panel_post``,
against the state of NOW, on EVERY button of the payload - never "these roles
were checked when the panel was first posted". A published panel outlives the
check that let it be posted: a role that was harmless then can since have gained
permissions, moved above the actor, or become integration-managed, and an edit
re-publishes every button it renders, not only the ones that changed. Gating the
post and trusting the edit would leave the front door guarded and the window
open.

NAMING THE OFFENDING ROLES (the ``failures`` list)
--------------------------------------------------
A refusal about roles carries EVERY role it refuses over, so a 25-button panel or
a 25-option menu does not leave the operator guessing which one::

    {"ok": false, "error": "role_above_actor",
     "failures": [{"role_id": "...", "reason": "role_above_actor"},
                  {"role_id": "...", "reason": "role_not_assignable"}]}

* ``error`` stays the DOMINANT single code, for any consumer reading one code.
* ``failures`` is ALWAYS present and ALWAYS a list on a role refusal, even with
  one element (``verify_button_post`` and ``reaction_role_add`` publish exactly
  one role and still answer with a list) - ONE shape for the consumer.
* each id carries ITS OWN ``reason``, never the group's: two roles of one panel
  can fail for two different causes, and a shared code would make the dashboard
  print a sentence that is false for one of them.
* PRECEDENCE, deterministic and documented (:data:`_ROLE_FAILURE_PRECEDENCE`):
  ``role_above_actor`` beats ``role_not_assignable``. It is a fact about the
  ACTOR - their own rank, the person on the screen - where the other is a limit
  of Yasuho's position, fixable by moving her role up. The header must not depend
  on button order.
* role ids are STRINGS, like every snowflake this module returns; the dashboard
  resolves the names itself.

The list only means something because the executors COLLECT: they check every
role and refuse afterwards, instead of returning at the first bad one. A list
with first-failure semantics could never hold more than one element - a shape
that looks like it answers and does not. What is NOT collected is a structurally
impossible role (``bad_role``: an unparsable id, one that is not a role of this
guild at all, or - on the edit kind - a button entry that is not even an object)
- there is nothing to gate, so it refuses on the spot, keeping its own code; the
edit kind adds the offending id as ``role_id`` whenever it had one to name (an id
it could parse but not resolve), and omits the field when the payload carried no
id at all - it echoes nothing back unparsed.
``role_menu_post`` can only ever report ``role_above_actor``: its bot half DROPS
an ungrantable option rather than refusing (documented at the call site), so
``role_not_assignable`` is not one of its outcomes.

ORDER MATTERS FOR THE CODES THE WEB APP RENDERS. On an actor kind the gate runs
BEFORE the executor, so it also runs before the payload is validated: a
malformed row now answers ``guild_unavailable`` / ``actor_*`` where it used to
answer ``bad_channel_id`` / ``bad_role``, and it pays that one member fetch on a
cache miss even though its payload was never going to be usable. Deliberate -
the actor must be established before anything else looks at attacker-supplied
fields - and bounded by the dashboard's own enqueue rate.

``requested_by`` IS NOW SECURITY-BEARING - it was audit-only. It MUST keep coming
from the authenticated session on the dashboard side and must NEVER be taken from
a form field, a URL/query param or any other client-supplied value: whoever
controls it controls which rank the publication is checked against. An "act on
behalf of" feature would need its OWN column (an explicit ``on_behalf_of``),
leaving ``requested_by`` the person who actually clicked.

STILL NOT GATABLE BOT-SIDE - THE DASHBOARD IS THE ONLY DEFENCE. SIX role-bearing
settings are written STRAIGHT INTO THE DATABASE by the Node process and never
pass through this queue. They are every role Yasuho grants ON HER OWN from a
stored setting, and the list is meant to be exhaustive - a new such setting
belongs in it:

* ``autorole`` - Discord guard ``/autorole set`` (``cogs/config/settings.py``);
* ``verify_role`` - Discord guard ``/verify setup`` (``cogs/config/verification.py``);
* the leveling role rewards - Discord guard ``/levelconfig rewards add`` (body in
  ``cogs/community/leveling/level_rewards.py``, NOT a ``/levelrole`` command);
* ``level_config.season_champion_role_id``, granted at every rollover by
  ``Seasons._apply_champion_role`` - Discord guard on the ``/levelconfig``
  seasons panel's champion-role select (``cogs/community/leveling/seasons_views.py``);
* the twitch live role - Discord guard on the ``/twitch`` panel's Live-role select
  (``cogs/config/twitch.py``);
* the ``muterole`` row - NO Discord guard, because there is no Discord surface
  for it AT ALL. See below.

``dashboard_sync`` only invalidates a cache when any of them changes, so the bot
never sees the write, has no actor and no before/after, and cannot refuse
anything: for the first five, the ``modchecks.self_assignable_role_error`` guard
the Discord surface carries is simply bypassed by writing the row directly.

``muterole`` is WORSE than bypassable. There is no ``/muterole`` command: the row
is only ever written by ``Moderation._ensure_mute_role``
(``cogs/moderation/moderation.py``), which persists the id of a role Yasuho JUST
CREATED herself, so no Discord surface has ever had to ask the self-grant
question and there is no guard there to bypass. A dashboard write pointing that
row at an arbitrary EXISTING role has no Discord counterpart at all - and the
mute paths then apply that role to members.

Do not "fix" any of this here: it has to be a rank check in the web app itself
(or those writes have to be moved onto this queue as new kinds).

``verify_button_post`` is gated on the verify role CONFIGURED AT POST TIME, which
is the best this side can do - but read it as DEFENCE IN DEPTH, not as a closed
path: whoever can enqueue the post can also move ``verify_role`` through the
ungated DB-direct path above. Leave it unset (or point it low), post the button
past the gate, then raise it: the button re-reads the setting at click time
(``cogs/config/verification.py``) and hands out whatever it says. The post-time
read is served from the ``tools.settings`` bounded LRU, so a write that is merely
CONCURRENT can be missed too, with no ordering trick - the gate only sees it once
the ``dashboard_sync`` notify has landed and invalidated the guild's blob.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict

import asyncpg
import discord
from discord.ext import commands

from cogs.system.dashboard_music_actions import EXECUTORS as _MUSIC_EXECUTORS
from cogs.system.dashboard_user_actions import EXECUTORS as _USER_EXECUTORS
from tools import autoroom, i18n, modchecks, role_menus, settings
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _
from tools.snowflake import coerce_id

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
# dead previous process (stale updated_at) are reset and re-driven.
#
# The window is NOT a bound on how long an executor may run: mydata_export packs
# an archive and uploads several megabytes to Discord, which can take longer than
# this. What makes a live claim safe is _INFLIGHT_ACTIONS below - reconcile knows
# exactly which ids THIS process is handling and never resets one, whatever its
# age. The window is only what tells a claim left by a DEAD process from one made
# moments ago by a process that has since died too; keeping it short is what
# stops such an orphan from waiting for the NEXT restart to be re-driven.
_ORPHAN_RESET_SECONDS = 30

# The action ids this process is handling right now, as a refcount (a notify and
# the boot sweep can enter for the same id; only one wins the claim, but both
# must be accounted for so the loser's exit does not clear the winner's mark).
# Read by reconcile: an id in here is being worked on by a coroutine of THIS
# process, so resetting its row to 'pending' would let the sweep re-claim and
# re-run it - the exact double side effect the age guard exists to prevent, which
# the age guard alone cannot prevent for an executor that runs longer than the
# window. Process-local by design: it describes THIS process's coroutines and
# nothing else, which is precisely the set the age guard cannot identify.
_INFLIGHT_ACTIONS = {}

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
# Executors: kind -> async handler(bot, scope_id, payload) -> result dict.
# ``scope_id`` is the guild id for every kind except those in _USER_KINDS below,
# which get the user id instead. The kinds listed in _ACTOR_KINDS take ONE more
# argument, ``actor``: the resolved Member who asked for the action from the web
# app, handed over already resolved (or the action is refused before the executor
# is entered - see _handle_action). Each RE-VALIDATES the payload against live state
# and returns a JSON-safe dict ``{"ok": bool, ...}``. A short ``error`` code on
# failure - never a secret.
#
# The five live-player kinds follow the same contract but live in the sibling
# module ``dashboard_music_actions`` (they drive the music package's seams), and
# the user-scoped ones in ``dashboard_user_actions``; both are merged into the
# ONE registry below. They report a failure under ``reason`` rather than
# ``error`` - the shape the dashboard contracted for those kinds.
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


# ---------------------------------------------------------------------------
# The role gate every publishing kind applies, and the ``failures`` list it
# reports. See the module docstring ("NAMING THE OFFENDING ROLES") for the
# contract these three helpers implement.
# ---------------------------------------------------------------------------

# The dominant ``error`` codes a role refusal can carry, HIGHEST PRECEDENCE
# FIRST. One panel can fail for BOTH reasons at once (a role above the actor
# next to an integration-managed booster role), so which one becomes the single
# ``error`` header must be a documented fact rather than a function of button
# order: ``role_above_actor`` wins because it is a fact about the ACTOR - their
# own rank, the person reading the dashboard - whereas ``role_not_assignable``
# is a limit of Yasuho's own position, fixable by moving her role up.
_ROLE_FAILURE_PRECEDENCE = ("role_above_actor", "role_not_assignable")


def _role_gate_failure(actor, guild, role):
    """The reason ``role`` may not be published, or ``None`` when it may.

    The pair the self-role surfaces ask, in the order they all ask it: the BOT
    half first (``bot_can_assign_role`` -> ``role_not_assignable``), then the
    CONFIGURER half (``self_assignable_role_error`` -> ``role_above_actor``).
    The second call composes both halves, so it would cover the first on its
    own; keeping the bot half as its own FIRST call is what gives the two
    failures two distinct codes - and what makes ``role_above_actor`` mean a
    RANK problem and nothing else.
    """
    if not modchecks.bot_can_assign_role(role, guild):
        return "role_not_assignable"
    if modchecks.self_assignable_role_error(actor, guild, role):
        # The helper's reason is a TRANSLATED human string for a Discord reply;
        # the queue speaks codes only and the dashboard renders its own copy.
        return "role_above_actor"
    return None


def _role_failure(role_id, reason):
    """One ``failures`` entry: the role id as a STRING, carrying ITS OWN reason.

    Never the group's reason: two roles of the same panel can fail for two
    different causes, and a shared code would make the dashboard print a
    sentence that is false for one of them. Ids are strings like every other
    snowflake this module returns (JSON-safe); the dashboard resolves names.
    """
    return {"role_id": str(role_id), "reason": reason}


def _role_refusal(failures):
    """Build the refusal from EVERY collected failure: dominant code + the list.

    ``{"ok": False, "error": <dominant>, "failures": [...]}``. ``error`` stays
    the single machine-readable code for anything reading one code, chosen by
    :data:`_ROLE_FAILURE_PRECEDENCE`; ``failures`` is always a list, even with a
    single element, so the dashboard has ONE shape to handle.
    """
    reasons = [f["reason"] for f in failures]
    for code in _ROLE_FAILURE_PRECEDENCE:
        if code in reasons:
            dominant = code
            break
    else:  # pragma: no cover - defensive: every caller passes a known reason
        dominant = reasons[0] if reasons else _ROLE_FAILURE_PRECEDENCE[-1]
    return {"ok": False, "error": dominant, "failures": list(failures)}


async def _exec_verify_button_post(bot, guild_id, payload, actor):
    """Post the persistent Verify button embed into a channel.

    Payload: ``{"channel_id": "<snowflake>", "message"?: "<custom text>"}``.
    ``guild_id`` is authoritative (from the claimed row); EVERYTHING else is
    re-validated here against the live gateway - the payload is never trusted:
    the guild must be present, the channel must exist, be a text channel, and
    the bot must be allowed to send there. The Verify ROLE is intentionally NOT
    required to be configured: the button reads the role at click time and
    reports if it is unset, so posting the button first (then setting the role)
    is a valid order.

    ``actor`` is the resolved Member who asked for this from the dashboard. The
    button is a ONE-CLICK SELF-GRANT of whatever ``verify_role`` says, so when a
    role IS configured it is checked exactly as ``/verify setup`` checks the role
    it is given: Yasuho must be able to hand it out (``role_not_assignable``) and
    the actor must outrank it (``role_above_actor``). When none is configured
    there is nothing to publish yet, so the post stands.

    DEFENCE IN DEPTH, NOT A CLOSED PATH. ``verify_role`` itself is written
    DB-direct by the dashboard and never reaches this queue, so the same person
    can post past this gate and raise the setting afterwards; the button re-reads
    it at click time and grants it. The read here is also served from the
    ``tools.settings`` LRU, so a concurrent write can be invisible until the
    ``dashboard_sync`` notify lands. Module docstring, "still not gatable".
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

    # The configurer gate, on the role this button will hand out. coerce_id
    # because the dashboard writes snowflakes as STRINGS (the VerifyView button
    # reads the same setting through the same coercion).
    role_id = coerce_id(
        await settings.get_guild(bot.db_pool, guild_id, "verify_role", None)
    )
    role = guild.get_role(role_id) if role_id else None
    if role is not None:
        reason = _role_gate_failure(actor, guild, role)
        if reason is not None:
            # ONE role can fail here, but it is reported in the same
            # ``failures`` list shape as a 25-button panel: one shape for the
            # dashboard to handle, never a special case for the single-role
            # kinds (module docstring).
            return _role_refusal([_role_failure(role.id, reason)])

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


async def _exec_reaction_role_add(bot, guild_id, payload, actor):
    """Add a reaction-role mapping: react on a live message and store the pair.

    Payload: ``{"channel_id", "message_id", "role_id"}`` (snowflake strings) plus
    ``"emoji"``. ``guild_id`` is authoritative (the claimed row, written under the
    dashboard's manage-guild gate); EVERYTHING else is re-validated here against
    the live gateway and NEVER trusted: the guild must be present, the channel
    must exist in THIS guild, the role must be a real assignable role of it, and
    the emoji must be non-empty, and the ``actor`` (the resolved Member who asked
    for this from the dashboard, see the module docstring) must outrank the role
    they are publishing. Only then do we fetch the message and add the
    reaction (a failure there -- gone message, missing add-reactions permission,
    a bad emoji -- yields a short code, never a stack).

    On success it upserts ``reaction_roles`` (keyed on (message_id, emoji), so a
    re-add just repoints the role) under the AUTHORITATIVE ``guild_id`` -- the
    upsert only ever touches a row already owned by THIS guild, so a message id
    claimed by another server yields ``message_claimed_elsewhere`` instead of a
    silent cross-tenant overwrite -- then live-patches the ReactionRoles cog's
    in-memory ``cache`` -- CRUCIAL, because ``on_raw_reaction_add`` reads that
    cache, not the table, on every reaction.
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

    me = guild.me
    if me is None:
        return {"ok": False, "error": "guild_unavailable"}

    role = guild.get_role(role_id)
    if role is None:
        return {"ok": False, "error": "bad_role"}
    # The publishing gate, both halves (_role_gate_failure): the BOT half mirrors
    # the /buttonrole builder's BuilderView._can_assign, so a dashboard write
    # can't map an emoji to a role Yasuho could never hand out (@everyone, an
    # integration-managed role, one at/above her own top role), and the
    # CONFIGURER half is the one /reactionrole asks - the person who asked for
    # this from the web app must outrank the role they are publishing. Checked
    # BEFORE the reaction is added, so a refused mapping leaves no stray reaction
    # on the message. A mapping names exactly ONE role, so the ``failures`` list
    # can only ever hold one entry here - it is still a list, because the
    # dashboard has ONE shape to handle (module docstring).
    reason = _role_gate_failure(actor, guild, role)
    if reason is not None:
        return _role_refusal([_role_failure(role_id, reason)])

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

    # The upsert is scoped to the AUTHORITATIVE guild_id the same way the
    # remove executor's DELETE is: the primary key is (message_id, emoji) with
    # no guild in it, so an unqualified DO UPDATE would let a manage-guild user
    # of guild B repoint guild A's live mapping by naming a known message id.
    # RETURNING is what distinguishes "written" from "that row is someone
    # else's", and the cache patch below hangs off it - the cache key carries no
    # guild either, so a blind patch would break the victim guild until restart.
    query = """
        INSERT INTO reaction_roles
        (message_id, emoji, role_id, guild_id)
        VALUES
        ($1, $2, $3, $4)
        ON CONFLICT (message_id, emoji) DO UPDATE
            SET role_id = EXCLUDED.role_id
            WHERE reaction_roles.guild_id = EXCLUDED.guild_id
        RETURNING role_id;
        """
    written = await bot.db_pool.fetchval(query, message_id, stored, role_id, guild_id)
    if written is None:
        return {"ok": False, "error": "message_claimed_elsewhere"}

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
    popped - ONLY when that DELETE matched a row, since the cache key carries no
    guild - so ``on_raw_reaction_add`` stops granting immediately. Best-effort, we
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
    result = await bot.db_pool.execute(query, message_id, stored, guild_id)

    # Only evict when the guild-scoped DELETE actually matched a row, exactly
    # like the cog's own reactionrole_remove. The cache key is (message_id,
    # emoji) with NO guild in it, so an unconditional pop would let a
    # manage-guild user of guild B kill guild A's LIVE mapping (its row would
    # survive, so the breakage would last until the next restart).
    cog = bot.get_cog("ReactionRoles")
    if cog is not None and result != "DELETE 0":
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


def _panel_button(role, label, emoji, style):
    """Normalise ONE panel button: bounded label, optional emoji, valid style.

    Shared by the post and edit executors, which take their buttons from the
    SAME payload shape, so one normalisation answers for both: ``style`` coerced
    to a callable ButtonStyle int (1/2/3/4, secondary fallback), an empty label
    falling back to the role name and bounded to 80, a blank emoji dropped.
    Never raises.
    """
    try:
        style = int(style)
    except (TypeError, ValueError):
        style = 2
    if style not in (1, 2, 3, 4):
        style = 2

    if not isinstance(label, str) or not label.strip():
        label = role.name
    label = label[:_MAX_BUTTON_LABEL]

    if not isinstance(emoji, str) or not emoji.strip():
        emoji = None
    else:
        emoji = emoji.strip()

    return {"role_id": role.id, "label": label, "emoji": emoji, "style": style}


def _panel_rows(buttons):
    """The (role_id, label, emoji, style) tuples ``ButtonRoleView`` expects."""
    return [(b["role_id"], b["label"], b["emoji"], b["style"]) for b in buttons]


def _register_panel_view(bot, br, rows, message_id):
    """Re-register a panel's persistent view so its buttons survive a restart.

    Of THIS process only: a restart of the bot rebuilds every panel's view from
    the table in ``ButtonRoles.cog_load``. Shared by the post and edit
    executors - an edit must re-register too, or the in-memory view would keep
    answering with the OLD button set until the next boot. Best effort: a
    failure here is logged, never fatal to an action whose message is already
    live.

    discord.py's view store is keyed on (message_id, custom_id) and
    ``ButtonRoleButton`` spells its own ``br:<role_id>``, so re-registering
    REPLACES the handler of every button that is still on the panel and leaves
    behind the entry of one that was removed from it, until the next boot. That
    entry answers a custom_id the message no longer carries; the cog's own
    re-post/attach path leaves exactly the same one, and the grant behind it
    still goes through Discord, which refuses a role Yasuho may not hand out
    (``ButtonRoleButton.callback`` reports that 403 rather than granting).
    """
    try:
        bot.add_view(br.ButtonRoleView(rows), message_id=message_id)
    except Exception:
        log.exception(
            "dashboard_actions: failed to register button-role view for message %s",
            message_id,
        )


async def _exec_button_panel_post(bot, guild_id, payload, actor):
    """Post an embed + self-assignable role buttons panel into a channel.

    Payload: ``{"channel_id", "embed": {<embed_creator blob>},
    "buttons": [{"role_id", "label"?, "emoji"?, "style"}]}``. ``guild_id`` is
    authoritative (the claimed row, written under the dashboard's manage-guild
    gate); EVERYTHING else is re-validated here against the live gateway and NEVER
    trusted: the guild must be present, the channel must exist in THIS guild, be a
    text channel and be sendable, there must be 1..MAX_BUTTONS buttons, each role
    must be a real role of the guild, and the ``actor`` (the resolved Member who
    asked for this from the dashboard, see the module docstring) must outrank
    every role the panel publishes. Style is coerced to a callable
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
    posted message, so a panel's embed cannot be edited from the dashboard; its
    BUTTONS can, through ``button_panel_edit`` below, which takes the SAME
    ``buttons`` shape this one does and re-renders them onto the same message.
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
    # The dedup runs BEFORE the gate so a role named twice is gated once and
    # named once in ``failures`` - the answer for a repeat is the answer already
    # collected for its first mention.
    seen_roles = set()
    failures = []
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
        if role_id in seen_roles:
            continue  # one button per role, mirroring the primary key
        seen_roles.add(role_id)

        # The publishing gate, both halves (_role_gate_failure): the BOT half
        # mirrors the /buttonrole builder's own guard (BuilderView._can_assign)
        # so a dashboard write can't persist a button for a dead/dangerous role,
        # and the CONFIGURER half is the one /buttonrole asks (module docstring).
        # COLLECTED, not returned: a 25-button panel must name EVERY role it
        # refuses over, so the operator fixes them in one pass instead of
        # discovering them one failed action at a time.
        reason = _role_gate_failure(actor, guild, role)
        if reason is not None:
            failures.append(_role_failure(role_id, reason))
            continue

        buttons.append(
            _panel_button(
                role, entry.get("label"), entry.get("emoji"), entry.get("style")
            )
        )

    # Refused BEFORE anything is sent or persisted - and BEFORE the empty check,
    # so a panel whose every button is refused names its roles rather than
    # answering the unhelpful "no_buttons".
    if failures:
        return _role_refusal(failures)

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
    rows = _panel_rows(buttons)
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
    _register_panel_view(bot, br, rows, msg.id)

    return {
        "ok": True,
        "message_id": str(msg.id),
        "channel_id": str(channel.id),
    }


async def _exec_button_panel_edit(bot, guild_id, payload, actor):
    """Re-render an EXISTING panel's BUTTONS in place, from the payload.

    Payload: ``{"message_id", "channel_id", "buttons": [{"role_id", "label"?,
    "emoji"?, "style"}]}`` (snowflakes as strings). ``buttons`` is the SAME shape
    ``button_panel_post`` accepts, normalised through the same ``_panel_button``:
    ONE shape produced on both sides of the queue, never a second dialect for the
    dashboard to build.

    WHY IT EXISTS: without it, fixing a typo in a label or adding a button means
    delete + re-post, which changes the message id - pinned links break and the
    panel jumps to the bottom of the channel. Editing in place keeps both.

    WHY THE BUTTONS TRAVEL IN THE PAYLOAD - AN ORDERING DECISION, NOT A
    CONVENIENCE. This kind first shipped reading the buttons back out of the
    ``button_roles`` rows, which forced the dashboard to WRITE BEFORE KNOWING:
    its write is synchronous, our verdict is asynchronous, so the rows had to be
    in place before the action could even be enqueued. On EVERY refusal - a gate
    failure, ``bad_role``, ``channel_mismatch``, ``too_many_buttons``,
    ``edit_failed``, any ``actor_*`` - those rows were then left AHEAD of the
    Discord message, and neither side could detect or reconcile the gap. Worse,
    it outlived the fix a restart usually is: ``ButtonRoles.cog_load`` rebuilds
    the persistent view from the TABLE at boot, so the rows ahead of the message
    came back as buttons with no handler behind them.

    Reading the table bought no safety in exchange. Every role is re-validated at
    render anyway, with the same actor and the same two checks, so a role carried
    in the payload passes through exactly the same gate as one read from a row -
    the table was never a second opinion, only a second copy. All it bought was
    the ordering constraint.

    So the dashboard now writes only AFTER our ``ok``, and the divergence moves
    off the ROUTINE path (any refusal at all) onto an EXCEPTIONAL one (its own
    write failing after we already said yes). NEITHER SHAPE IS ATOMIC: this is a
    distributed transaction across two systems with no shared commit, and no
    payload contract can make it one. The asymmetry is the whole point, and the
    RESIDUAL is real and belongs to the writer - after an ``ok`` whose write then
    fails, the message carries buttons the table lacks, so ``cog_load`` rebuilds
    that panel without them and those buttons answer nobody. The dashboard must
    retry the write, or enqueue a corrective action; it must never leave a button
    unbacked.

    THE FLIP IS THE ONE BREAKING CHANGE IN THIS MODULE, and it answers as one.
    Every other dashboard-facing change here has been additive; this one narrows
    an ALREADY-DEPLOYED kind's payload, so an enqueue in the old
    ``{message_id, channel_id}`` shape is now refused. It refuses fail-closed
    (nothing read, nothing edited, nothing written), and with its OWN code:
    ``buttons_missing`` for an absent field, ``no_buttons`` only for a field
    that is there and unusable. An operator on a stale dashboard must not be
    told "add at least one button" for a version mismatch. The contract
    (``.claude/plans/dashboard-executors-contract-panel-edit.md``, section 1)
    carries the same distinction, and has to reach the dashboard side BEFORE it
    implements the old shape.

    THE ROW LOOKUP STAYS - AS AN OWNERSHIP PROOF, AND NOTHING ELSE. Reading
    ``button_roles`` for this message id, scoped to the AUTHORITATIVE guild, is
    what proves the target is a panel OF THIS GUILD. Without it this kind would
    degrade into "edit any message Yasuho ever authored in this guild" - it calls
    ``message.edit`` on whatever id it is handed - which is strictly wider than
    anything the dashboard can do today. So: no rows -> ``panel_not_found``, and
    the stored ``channel_id`` still decides ``channel_mismatch``. What the lookup
    NO LONGER decides: the buttons, their labels/emoji/styles, and their COUNT
    (``MAX_BUTTONS`` now bounds the payload list, exactly as on a post). The rows
    it finds still hold the panel's OLD button set at that instant - expected,
    since the dashboard writes the new one after our ok - and that content is
    read for nothing but those two verdicts.

    THE EMBED IS NOT TOUCHED. Nothing stores it (the post executor renders it
    into the message and keeps no copy), so this kind edits the COMPONENTS ONLY;
    the dashboard shows that as an honest note. ``msg.edit(view=...)`` leaves the
    message's embed exactly as it was.

    BUTTON ORDER is the payload's, like a post's - what the operator arranged is
    what lands on the message. It is not durable, though: ``button_roles`` has no
    ordering column (its key is ``(message_id, role_id)``), so the next time a
    panel is rebuilt FROM THE TABLE - ``ButtonRoles.cog_load`` at boot - the order
    is whatever the read returns. Roles are DE-DUPLICATED, first mention wins,
    because that same key can only ever hold one row per role.

    ``guild_id`` is authoritative (the claimed row, written under the dashboard's
    manage-guild gate) and EVERYTHING else is re-validated against live state: the
    payload's ``channel_id`` must match the stored one (``channel_mismatch``: a
    message cannot change channel, so a disagreement means a stale or crafted
    request, never something to act on), the stored channel must still exist and
    the message must still be fetchable. The three payload-only refusals
    (``buttons_missing``, ``no_buttons``, ``too_many_buttons``) are answered
    BEFORE the lookup: they need no DB round trip, and their verdict comes purely
    from what the caller sent, so answering them first tells that caller nothing
    about whether the message it named is a panel of this guild.

    RE-VALIDATE EVERY ROLE AT RENDER TIME. An edit re-publishes every button it
    renders, not only the ones that changed, so the pair every publishing kind
    applies - ``_role_gate_failure``, bot half then configurer half - runs here on
    EVERY role against the state of NOW. This is load-bearing, not
    belt-and-braces: ``ButtonRoleButton.callback`` grants straight off its
    custom_id with NO rank check and nothing re-examines a published role when it
    changes (no ``on_guild_role_update`` listener anywhere in the bot), so publish
    time is the only gate this path will ever have. Left ungated, the kind would
    also be the gate's back door: post a harmless role past the check, then edit
    the panel to republish a dangerous one unchecked.

    PARTIAL FAILURE REFUSES THE WHOLE EDIT: the message keeps its current
    buttons, the rows are never touched (this executor never writes
    ``button_roles`` at all), and no view is registered. Rendering 4 of 5 buttons
    would publish a panel nobody asked for and hand the dashboard an ``ok`` to
    write 5 rows against, so its panel list would show five buttons for a message
    carrying four with nothing anywhere able to notice. An ``ok`` on a partial
    publish is not a wrong answer, it is an answer that PREVENTS knowing it is
    wrong. Every refused role is named in ``failures`` (module docstring). This
    holds for a MALFORMED entry too, and that is where the edit kind parts with
    the post one: an entry that is not an object is refused here rather than
    skipped, because the writer that will act on our ``ok`` writes from its own
    list, and a count alone would not tell it WHICH of its entries went out.

    REUSES the post executor's seams verbatim rather than growing a second
    renderer: ``_button_roles_module`` (``ButtonRoleView`` + ``MAX_BUTTONS``),
    ``_panel_button`` for the per-button normalisation, ``_panel_rows`` for the
    view's row tuples and ``_register_panel_view`` for the persistence-view
    re-registration - which an edit needs as much as a post does, or the view
    registered in memory would keep answering with the OLD button set until the
    next boot.
    """
    try:
        message_id = int(payload.get("message_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "message_not_found"}
    try:
        channel_id = int(payload.get("channel_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_channel_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}
    me = guild.me
    if me is None:
        # Without her own member object the gate below cannot ask whether Yasuho
        # can hand a role out, and "I could not check" must never render as a
        # per-role refusal that is really a missing cache.
        return {"ok": False, "error": "guild_unavailable"}

    br = _button_roles_module()
    max_buttons = getattr(br, "MAX_BUTTONS", 25)

    # Payload-only verdicts, answered before the DB round trip (docstring): an
    # unusable button list can never render, whatever message it names.
    if "buttons" not in payload:
        # THE FIELD IS ABSENT ENTIRELY, which is the shape this kind shipped
        # with before the buttons moved into the payload. Told apart from an
        # empty list on purpose: "you sent no button" is an operator mistake to
        # be shown as one, while "you sent no BUTTONS FIELD" is a caller on the
        # old contract, and answering both `no_buttons` would have an operator
        # reading "add at least one button" for a version mismatch they cannot
        # fix from the panel editor. Fail-closed either way - nothing is read,
        # nothing is edited.
        return {"ok": False, "error": "buttons_missing"}
    raw_buttons = payload.get("buttons")
    if not isinstance(raw_buttons, list) or not raw_buttons:
        # An empty list is NOT "strip the panel": that is button_panel_delete's
        # job, and it drops the rows too. Refused exactly like a post's.
        return {"ok": False, "error": "no_buttons"}
    if len(raw_buttons) > max_buttons:
        return {"ok": False, "error": "too_many_buttons"}

    # OWNERSHIP + EXISTENCE ONLY. Guild-scoped, exactly like the delete
    # executor's DELETE: the primary key carries no guild, so an unqualified read
    # would let a manage-guild user of guild B re-render (and re-register) guild
    # A's panel by naming its id. The buttons no longer come from here, but this
    # read still has to happen: it is the ONLY proof that the id names a panel of
    # this guild rather than any message Yasuho ever authored in it, and it is
    # where the stored channel comes from. DISTINCT because the row CONTENT is
    # never read any more - only "does it exist" and "which channel".
    rows = await bot.db_pool.fetch(
        "SELECT DISTINCT channel_id "
        "FROM button_roles "
        "WHERE message_id = $1 AND guild_id = $2;",
        message_id,
        guild_id,
    )
    if not rows:
        return {"ok": False, "error": "panel_not_found"}

    # The PK is (message_id, role_id), so nothing in the schema forces every row
    # of one message to agree on channel_id. Reading rows[0] would make the
    # verdict depend on which row came back first, so a split set is refused
    # outright rather than judged on a coin toss.
    if len(rows) != 1:
        return {"ok": False, "error": "channel_mismatch"}
    stored_channel_id = rows[0]["channel_id"]
    if channel_id != stored_channel_id:
        return {"ok": False, "error": "channel_mismatch"}

    # Publish-time re-validation of every role, and the whole-or-nothing rule:
    # collect, never drop, never render a subset. Dedup runs BEFORE the gate, as
    # on a post, so a role named twice is gated once and named once.
    seen_roles = set()
    buttons = []
    failures = []
    for entry in raw_buttons:
        if not isinstance(entry, dict):
            # REFUSED, not skipped - and this is the one place the edit kind
            # deliberately parts with the post one, which drops such an entry
            # (line above ``_role_gate_failure`` in _exec_button_panel_post).
            # The post executor writes the rows ITSELF, from what it rendered,
            # so a dropped entry leaves message and table agreeing; here the
            # WRITER writes them, from ITS OWN list, after our ok - so rendering
            # 4 of the 5 entries it sent would hand it an ok to write 5 rows
            # against, the exact divergence this kind exists to prevent. An
            # entry that is not an object carries no id to name, so it answers
            # like any other unnameable one: bare ``bad_role``.
            return {"ok": False, "error": "bad_role"}
        try:
            role_id = int(entry.get("role_id"))
        except (TypeError, ValueError):
            # Nothing to name: the value was not an id, and it is never echoed
            # back unparsed.
            return {"ok": False, "error": "bad_role"}
        role = guild.get_role(role_id)
        if role is None:
            # Not a role of this guild (deleted, or never one). Not a gate
            # refusal - there is nothing left to judge - so it keeps the module's
            # existing ``bad_role`` code rather than inventing a third
            # ``failures`` reason, but it names the id, and it still refuses the
            # WHOLE edit rather than rendering the panel minus that button.
            return {"ok": False, "error": "bad_role", "role_id": str(role_id)}
        if role_id in seen_roles:
            continue  # one button per role, mirroring the primary key
        seen_roles.add(role_id)
        reason = _role_gate_failure(actor, guild, role)
        if reason is not None:
            failures.append(_role_failure(role_id, reason))
            continue
        buttons.append(
            _panel_button(
                role, entry.get("label"), entry.get("emoji"), entry.get("style")
            )
        )

    if failures:
        # Nothing edited, nothing written: the message keeps its current buttons,
        # and this executor never writes button_roles at all.
        return _role_refusal(failures)

    # No empty-set check here, unlike the post executor: nothing in the loop
    # above can DROP an entry any more. A non-object refuses, an unusable id
    # refuses, and every other entry either renders a button or lands in
    # ``failures`` - the only ``continue`` left is a repeat of a role already
    # answered for. A non-empty list with no failures therefore always leaves at
    # least one button behind.

    # get_channel_or_thread, not get_channel: a panel can have been attached to a
    # message living in a thread (the /buttonrole attach flow), and this kind
    # only ever EDITS a message that is already there - it never sends one, so
    # the post executor's text-channel + send-permission gates do not apply.
    channel = guild.get_channel_or_thread(stored_channel_id)
    if channel is None:
        return {"ok": False, "error": "channel_not_found"}
    try:
        message = await channel.fetch_message(message_id)
    except Exception:
        return {"ok": False, "error": "message_not_found"}

    view_rows = _panel_rows(buttons)
    try:
        await message.edit(view=br.ButtonRoleView(view_rows))
    except Exception:
        log.exception(
            "dashboard_actions: failed to edit button-role panel %s", message_id
        )
        # The rows are untouched either way, so the panel is still exactly what
        # the table says it is - the edit simply did not land.
        return {"ok": False, "error": "edit_failed"}

    _register_panel_view(bot, br, view_rows, message_id)

    return {
        "ok": True,
        "message_id": str(message_id),
        "channel_id": str(stored_channel_id),
        "buttons": len(view_rows),
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
    """Normalise a payload's option list through the cog's own shared helper.

    The dashboard serialises every snowflake as a STRING (never a JS number, to
    dodge 2^53 precision loss). ``role_menus.normalize_options`` runs each
    role_id through ``tools.snowflake.coerce_id``, so it already accepts that
    STRING spelling and normalises it to an int - on top of all the real work
    (drop/dedup/cap-at-25, label/emoji/description/temp). Kept as a named seam
    (this is where a payload becomes trusted option data) but with no widening
    of its own to duplicate. Never raises.
    """
    return role_menus.normalize_options(raw)


async def _exec_role_menu_post(bot, guild_id, payload, actor):
    """Post a self-role select menu into a channel + persist + register its view.

    Payload: ``{"channel_id": "<snowflake>", "config": {<menu config>}}``.
    ``guild_id`` is authoritative (the claimed row, written under the dashboard's
    manage-guild gate); EVERYTHING else is re-validated here against the live
    gateway and NEVER trusted: the guild must be present, the channel must exist in
    THIS guild, be a text channel and be sendable, the guild must be under
    MAX_MENUS_PER_GUILD, the option list (normalised through the SAME
    ``role_menus.normalize_options`` helper the cog uses) must be non-empty AND at
    least one kept option's role must be a real role of this guild (foreign/gone
    roles are filtered out; an all-foreign list is rejected), and the ``actor``
    (the resolved Member who asked for this from the dashboard, see the module
    docstring) must outrank every role that survives that filter. Title/description are
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

    # belongs-to-guild + assignability defence: keep only options whose role is a
    # real role of THIS guild that Yasuho could actually hand out (not @everyone,
    # not integration-managed, below her top role) - the same guard the reaction
    # and button executors apply, and the one the /rolemenu picker now runs. An
    # unassignable option would 403 on every pick with the failure swallowed at
    # the grant site, so the member just sees nothing happen. A crafted payload
    # naming only foreign/gone/unassignable roles is rejected wholesale rather
    # than posting a menu that can do nothing.
    #
    # The CONFIGURER half (module docstring) is applied to the options that
    # SURVIVE that filter, and REFUSES the whole action rather than dropping the
    # offending option: a role above the actor is not a stale entry to clean up
    # silently, it is a privilege the actor asked for and must be told about
    # (/rolemenu refuses at the picker the same way). Nothing is posted or
    # persisted before this loop.
    #
    # The two halves are spelled out here rather than taken from
    # _role_gate_failure BECAUSE they answer differently on this kind: the bot
    # half FILTERS (a stale option is dropped, as it always was) where the shared
    # helper refuses. Only the actor half refuses here, so ``role_not_assignable``
    # is not a code this kind can return - and every option above the actor is
    # COLLECTED so a 25-option menu names them all in one answer.
    kept = []
    failures = []
    for o in options:
        role = guild.get_role(o["role_id"])
        if role is None or not modchecks.bot_can_assign_role(role, guild):
            continue
        if modchecks.self_assignable_role_error(actor, guild, role):
            failures.append(_role_failure(o["role_id"], "role_above_actor"))
            continue
        kept.append(o)
    # Before the emptiness check: a menu whose every option is above the actor
    # must name those roles, not answer ``bad_role_all``.
    if failures:
        return _role_refusal(failures)
    options = kept
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
    failure there is cosmetic and never affects the ``ok`` result. When (and ONLY
    when) a row matched, the message id is also dropped from the RoleMenus cog's
    in-memory ``_menu_ids`` set (parity with the cog's own on_raw_message_delete
    pruning) - that set is not guild-keyed, so evicting on a miss would be a
    cross-guild write.
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

    # Everything below is conditional on the guild-scoped DELETE having matched:
    # _menu_ids is keyed by message id alone, with NO guild in it, so evicting on
    # a miss would let a manage-guild user of guild B unhook guild A's LIVE menu
    # from on_raw_message_delete pruning (its row would survive) - the same
    # cross-tenant shape the reaction-role cache pop has.
    if rows:
        cog = bot.get_cog("RoleMenus")
        if cog is not None and hasattr(cog, "_menu_ids"):
            cog._menu_ids.discard(message_id)

        # Best-effort: strip the select off the message. Never let a hiccup here
        # fail the delete (the row is already gone).
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


# Autoroom hub ids are 8-hex strings (``tools/autoroom.default_hub``); the cap is
# a sanity bound so a crafted payload can never carry an unbounded string into the
# cog's linear scan over the guild's hubs.
_MAX_HUB_ID_LEN = 64

# Per-guild serialisation of the autoroom read-modify-write.
#
# Both autoroom executors LOAD the guild's hub list, mutate it and SAVE it back
# into the single ``autorooms`` JSONB blob. Every notification is handled in its
# own task (``_on_notify`` creates one per notify), so two autoroom actions for
# the SAME guild can interleave: the second one's save is computed from a list
# read before the first one's save and silently drops the first hub - while its
# category and voice trigger stay alive on Discord, orphaned. This lock makes
# each guild's load -> act -> re-read sequence atomic within this process.
#
# Deliberately NOT ``TemporaryRooms._locks``: that one gates the voice-join hot
# path, and holding it across the seconds a channel-creation round trip takes
# would stall every join-to-create in the guild.
#
# The mapping is unbounded on purpose. It grows one ``asyncio.Lock`` (a few
# hundred bytes, and no event-loop binding at all while uncontended) per guild
# that has ever run a dashboard autoroom action - an operator-driven set far
# smaller than the guild count. A BoundedLRU would be worse than useless here:
# evicting an entry while its lock is HELD hands the next caller a brand-new
# lock and silently destroys the mutual exclusion this exists to provide.
_AUTOROOM_LOCKS = defaultdict(asyncio.Lock)


def _hub_text(payload, key):
    """Return ``payload[key]`` stripped, or ``None`` when it is not usable.

    Server-side bound for the free-text fields of a hub (label, category name,
    join-to-create channel name, room-name template): each must be a non-empty
    string of at most ``CHANNEL_NAME_LIMIT`` (100) characters - Discord's
    channel-name cap, which the cog would otherwise silently truncate. Rejecting
    rather than truncating keeps what the dashboard displays and what Discord
    actually gets identical. Never raises.
    """
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > autoroom.CHANNEL_NAME_LIMIT:
        return None
    return value


async def _exec_autoroom_hub_create(bot, guild_id, payload):
    """Create a join-to-create voice hub: real Discord channels + saved config.

    Payload: ``{"label", "category_name", "hub_name", "template", "user_limit"}``.
    This runs through the QUEUE rather than as a plain dashboard settings write
    because creating a hub creates actual Discord channels (a category + a voice
    trigger) and updates the TemporaryRooms cog's in-memory ``_hub_index`` -
    neither of which the Node process can do. (Editing an existing hub's fields
    IS a direct settings write on the dashboard side; the ``autorooms``
    cache-invalidation kind in ``cogs/system/dashboard_sync.py`` picks that up.)

    ``guild_id`` is authoritative (the claimed row, written under the dashboard's
    manage-guild gate); EVERYTHING else is re-validated here and NEVER trusted:
    the four text fields must be non-empty and within Discord's 100-char channel
    name limit, ``user_limit`` must be an int in 0..99 (0 = unlimited, Discord's
    voice cap is 99) - all checked BEFORE the cog is touched, so a bad payload
    never creates a channel - and the guild must be present with the cog loaded.
    The per-guild ``MAX_HUBS`` cap is enforced before anything is created, exactly
    as ``_exec_role_menu_post`` gates on ``MAX_MENUS_PER_GUILD``.

    ``TemporaryRooms._add_hub`` returns a TRANSLATED human string on EVERY path
    (created, refused by one of its budget checks, or "something went wrong while
    creating the hub's channels"), so success is detected STRUCTURALLY rather than
    by matching that text: only the created path appends the hub and saves, so
    exactly one new hub id appears in the reloaded list. The cog's message is
    passed back as ``message`` so the dashboard can show WHY a refusal happened
    (which budget was hit) - it is bot-authored copy, never a stack or a secret,
    and it is rendered under the guild's configured language (the cog translates
    against the ambient locale, which a background task would otherwise leave at
    the default).

    Because the whole thing is a read-modify-write of one JSONB blob it runs
    under the guild's ``_AUTOROOM_LOCKS`` entry, after dropping the settings LRU
    entry for the guild (the dashboard's Node process writes the same key, and a
    NOTIFY missed during a listener reconnect would leave the cache stale).

    NOTE on the at-least-once delivery of the queue: a crash landing AFTER
    ``_add_hub`` but BEFORE the status write makes the boot reconciliation replay
    this action, and the replay creates a SECOND real category + voice trigger
    pair - not a cosmetic duplicate. It is bounded by the ``MAX_HUBS`` gate
    below, which is re-read from the (freshly invalidated) settings on every run.
    """
    label = _hub_text(payload, "label")
    if label is None:
        return {"ok": False, "error": "bad_label"}
    category_name = _hub_text(payload, "category_name")
    if category_name is None:
        return {"ok": False, "error": "bad_category_name"}
    hub_name = _hub_text(payload, "hub_name")
    if hub_name is None:
        return {"ok": False, "error": "bad_hub_name"}
    template = _hub_text(payload, "template")
    if template is None:
        return {"ok": False, "error": "bad_template"}

    user_limit = payload.get("user_limit")
    if isinstance(user_limit, bool):  # a stray True must never read as 1
        return {"ok": False, "error": "bad_user_limit"}
    try:
        user_limit = int(user_limit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_user_limit"}
    if not 0 <= user_limit <= 99:
        return {"ok": False, "error": "bad_user_limit"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}
    # Same gate the /autoroom group carries (bot_has_permissions(manage_channels)):
    # without it the category/voice creation raises Forbidden inside the cog,
    # which swallows it and returns a failure message - reported here as a code.
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return {"ok": False, "error": "missing_manage_channels"}
    # The cog owns both the Discord side effects and the hub index, so without it
    # loaded the bot simply cannot act on this guild - reported with the SAME code
    # a missing guild uses rather than inventing a code the dashboard can't map.
    cog = bot.get_cog("TemporaryRooms")
    if cog is None:
        return {"ok": False, "error": "guild_unavailable"}

    # The cog's messages are translated against the AMBIENT locale, and this runs
    # on a background queue task with no interaction context, so resolve the
    # guild's language explicitly (the _exec_verify_button_post pattern).
    loc = await i18n.resolve_guild_locale(bot, guild)

    async with _AUTOROOM_LOCKS[guild_id]:
        # Read from Postgres, not from a possibly stale LRU entry: the dashboard
        # writes this same blob, and a NOTIFY emitted while the sync listener was
        # reconnecting is lost. Inside the lock, so a concurrent action of this
        # process cannot re-populate the cache between the drop and the read.
        settings.invalidate_guild(guild_id)

        # The before-picture doubles as the cap gate and as the success diff below.
        before = {hub["id"] for hub in await cog._load_hubs(guild_id)}
        if len(before) >= autoroom.MAX_HUBS:
            return {"ok": False, "error": "too_many_hubs"}

        with i18n.locale(loc):
            message = await cog._add_hub(
                guild,
                label=label,
                category_name=category_name,
                hub_name=hub_name,
                template=template,
                user_limit=user_limit,
            )

        created = [
            hub for hub in await cog._load_hubs(guild_id) if hub["id"] not in before
        ]

    if not created:
        # Refused by a budget check (categories / channel count) or the channel
        # creation failed: nothing was saved, and the cog's message says which.
        return {"ok": False, "error": "create_failed", "message": message}
    # _add_hub APPENDS the new hub before saving, so the last previously-unseen
    # entry is the one THIS call created even if another writer slipped one in.
    hub = created[-1]
    return {
        "ok": True,
        "hub_id": hub["id"],
        "hub_channel_id": str(hub["hub_channel_id"]),
        "message": message,
    }


async def _exec_autoroom_hub_delete(bot, guild_id, payload):
    """Delete a join-to-create hub: its Discord channels + its stored config.

    Payload: ``{"hub_id"}`` (the 8-hex id from the hub dict). Like the create
    path this must run through the queue: ``TemporaryRooms._remove_hub`` deletes
    the hub's trigger channel AND its category with every live temp room inside,
    drops the hub's ``_active`` room set and re-indexes the guild - all live-bot
    work. ``guild_id`` is authoritative (the claimed row), and the hub id is
    re-validated against THIS guild's saved hubs, so a crafted request can never
    reach into another guild's config (the cog only ever looks at the hubs stored
    under the guild it is handed).

    The existence pre-check is what turns the cog's translated "That hub no
    longer exists." into a short machine code, mirroring how the reaction/button
    executors validate a role or channel before acting. The cog's message is
    passed back for display (rendered under the guild's language), as in the
    create executor - and, exactly as there, the whole load -> remove -> save
    sequence runs under the guild's ``_AUTOROOM_LOCKS`` entry with the settings
    LRU dropped first, so it can neither read a stale hub list nor lose a
    concurrent action's write.
    """
    hub_id = payload.get("hub_id")
    if not isinstance(hub_id, str):
        return {"ok": False, "error": "bad_hub_id"}
    hub_id = hub_id.strip()
    if not hub_id or len(hub_id) > _MAX_HUB_ID_LEN:
        return {"ok": False, "error": "bad_hub_id"}

    guild = bot.get_guild(guild_id)
    if guild is None:
        return {"ok": False, "error": "guild_unavailable"}
    # Without manage_channels the cog's channel deletions all raise Forbidden and
    # are swallowed: the config row would be dropped while the category and its
    # live rooms stayed on Discord, and the dashboard would be told ok. Refuse
    # up front instead (parity with the /autoroom group's bot_has_permissions).
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return {"ok": False, "error": "missing_manage_channels"}
    cog = bot.get_cog("TemporaryRooms")
    if cog is None:
        return {"ok": False, "error": "guild_unavailable"}

    loc = await i18n.resolve_guild_locale(bot, guild)

    async with _AUTOROOM_LOCKS[guild_id]:
        settings.invalidate_guild(guild_id)

        hubs = await cog._load_hubs(guild_id)
        if not any(hub["id"] == hub_id for hub in hubs):
            return {"ok": False, "error": "hub_not_found"}

        with i18n.locale(loc):
            message = await cog._remove_hub(guild, hub_id)

    return {"ok": True, "hub_id": hub_id, "message": message}


_EXECUTORS = {
    "verify_button_post": _exec_verify_button_post,
    "reaction_role_add": _exec_reaction_role_add,
    "reaction_role_remove": _exec_reaction_role_remove,
    "button_panel_post": _exec_button_panel_post,
    "button_panel_edit": _exec_button_panel_edit,
    "button_panel_delete": _exec_button_panel_delete,
    "role_menu_post": _exec_role_menu_post,
    "role_menu_delete": _exec_role_menu_delete,
    "autoroom_hub_create": _exec_autoroom_hub_create,
    "autoroom_hub_delete": _exec_autoroom_hub_delete,
    # The five live-player kinds (music_pause / resume / skip / volume / stop)
    # live in cogs/system/dashboard_music_actions.py - they drive the music
    # package's own seams and would double this module's length - but they are
    # MERGED here so the queue keeps exactly ONE kind table to dispatch through:
    # handle_action, the claim, the result write-back and the boot reconciliation
    # are shared verbatim, and a test can register a fake kind the same way.
    **_MUSIC_EXECUTORS,
    # Same deal for the USER-scoped kinds (cogs/system/dashboard_user_actions.py):
    # one registry, one dispatch path. What differs is only which id they are
    # handed - see _USER_KINDS right below.
    **_USER_EXECUTORS,
}

# The kinds whose scope is a USER rather than a guild: they are dispatched with
# ``dashboard_actions.user_id`` (the column the DB CHECK guarantees is the only
# one set on such a row) instead of ``guild_id``.
#
# The house precedent is ``dashboard_sync.USER_KINDS``, and so is the rule it
# encodes: the scope is chosen by the KIND, never by whichever column happens to
# be populated. A ``mydata_export`` row that somehow carries a guild_id would
# otherwise export the data of whoever's snowflake sat in that column; here it
# simply reads a NULL user_id and is refused with ``bad_scope``.
#
# Spelled out as a literal rather than derived from _USER_EXECUTORS so that
# "this kind is user-scoped" is a decision recorded in ONE reviewable place; the
# test suite asserts the two agree, so they cannot drift apart in practice.
_USER_KINDS = frozenset({"mydata_export"})

# The kinds that PUBLISH a role a member can then obtain by clicking, and are
# therefore dispatched with a fourth argument: the ``actor``, the Member behind
# the row's ``requested_by``, resolved once by :func:`_handle_action`. See the
# module docstring for the whole contract (why requested_by is now
# security-bearing, and the three refusal codes a failed resolution yields).
#
# Spelled out as a literal for the same reason :data:`_USER_KINDS` is: "this kind
# carries a privilege decision" is a fact that must be reviewable in ONE place.
# The test suite asserts it agrees EXACTLY with the executors whose signature
# takes an actor, in both directions - registering an actor-taking executor
# without listing it here would dispatch it with three arguments (a TypeError
# swallowed as ``internal_error``), and listing a kind whose executor does not
# take one would do the mirror image.
#
# All five are guild-scoped: the actor is resolved against the row's SCOPE id via
# ``bot.get_guild``, so a kind in both this set and _USER_KINDS would look a guild
# up by a user snowflake, find nothing and refuse - fail-closed, but nonsense, and
# a test keeps the two sets disjoint.
_ACTOR_KINDS = frozenset(
    {
        "verify_button_post",
        "reaction_role_add",
        "button_panel_post",
        # RE-publishes stored roles, so it is gated exactly like the post kind.
        # Leaving it out would be the whole gate's back door: post a harmless
        # role past the gate, rewrite the rows, edit to republish unchecked.
        "button_panel_edit",
        "role_menu_post",
    }
)


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

    BOTH scope columns are returned; :func:`_scope_id` picks the one this row's
    kind is entitled to read. Exactly one of them is non-NULL (the DB CHECK), so
    the other is only ever the answer to "was this row written for the scope its
    kind expects?".

    ``requested_by`` comes back too: for the kinds in :data:`_ACTOR_KINDS` it is
    the ACTOR whose rank gates the publication, so leaving it out of the RETURNING
    is exactly how the gate would silently stop existing.
    """
    return await pool.fetchrow(
        "UPDATE dashboard_actions "
        "SET status = 'running', updated_at = now() "
        "WHERE id = $1 AND status = 'pending' "
        "RETURNING guild_id, user_id, kind, payload, requested_by",
        action_id,
    )


def _scope_id(kind, claimed):
    """Return the id ``kind`` acts on, or ``None`` when the row's scope is wrong.

    The KIND decides which column is read - ``user_id`` for a kind in
    :data:`_USER_KINDS`, ``guild_id`` for every other - so a kind can never be
    made to act on the scope it was not written for (the ``dashboard_sync``
    rule). Combined with the DB CHECK "exactly one column is set", a mismatch
    always surfaces as ``None`` here rather than as an action on the wrong thing.

    The lookup is guarded rather than bare: a row that does not carry the column
    at all (an older query shape mid-deploy, a hand-rolled double) must be
    refused like any other bad scope, not raise inside the dispatcher.
    """
    column = "user_id" if kind in _USER_KINDS else "guild_id"
    try:
        value = claimed[column]
    except (KeyError, IndexError, TypeError):
        return None
    return value


def _actor_id(claimed):
    """Return the row's ``requested_by`` as a positive int, or ``None``.

    Guarded exactly like :func:`_scope_id`: a row that does not carry the column
    at all (an older query shape mid-deploy, a hand-rolled double) must read as
    "no actor" - which the caller REFUSES - rather than raise in the dispatcher.
    ``coerce_id`` is what rejects a NULL, a 0/negative id and a stray boolean.
    """
    try:
        value = claimed["requested_by"]
    except (KeyError, IndexError, TypeError):
        return None
    return coerce_id(value)


async def _resolve_actor(guild, actor_id):
    """Resolve ``actor_id`` to a Member of ``guild``. Returns ``(member, code)``.

    Exactly one of the two is set: ``(member, None)`` on success, ``(None,
    "<code>")`` on a refusal. The codes are the module docstring's contract with
    the dashboard: ``actor_missing`` (no usable id on the row), ``actor_left_guild``
    (PROVEN absent) and ``actor_unverified`` (could not be established).

    THE SPARSE-CACHE RULE (tools/modchecks, at length): Yasuho runs with
    ``chunk_guilds_at_startup=False``, so a ``get_member`` miss means UNKNOWN, not
    absent - deciding on it would wave through exactly the staff member nobody has
    spoken to recently. A miss therefore costs at most ONE
    ``GET /guilds/{id}/members/{user}``, and only ``NotFound`` is a negative answer
    we trust: Forbidden, 5xx, a timeout, a torn-down session, even a fetch that
    somehow yields nothing all leave the rank UNVERIFIABLE and are REFUSED.
    """
    if actor_id is None:
        return None, "actor_missing"

    member = guild.get_member(actor_id)
    if member is not None:
        return member, None

    try:
        member = await guild.fetch_member(actor_id)
    except discord.NotFound:
        return None, "actor_left_guild"
    except Exception:
        log.warning(
            "dashboard_actions: could not resolve actor %s in guild %s; refusing",
            actor_id,
            getattr(guild, "id", None),
            exc_info=True,
        )
        return None, "actor_unverified"

    if member is None:
        return None, "actor_unverified"
    return member, None


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


@contextlib.contextmanager
def _inflight(action_id):
    """Mark ``action_id`` as being handled by this process for the block.

    A refcount rather than a set: a live notify and the boot sweep can both
    enter for the same id (only one wins the claim), and the loser leaving must
    not clear the mark the winner still needs. Always paired in ``finally``, so
    a crashing executor cannot leave a permanent mark that would make the row
    unrecoverable at the next boot.
    """
    _INFLIGHT_ACTIONS[action_id] = _INFLIGHT_ACTIONS.get(action_id, 0) + 1
    try:
        yield
    finally:
        remaining = _INFLIGHT_ACTIONS.get(action_id, 1) - 1
        if remaining > 0:
            _INFLIGHT_ACTIONS[action_id] = remaining
        else:
            _INFLIGHT_ACTIONS.pop(action_id, None)


async def handle_action(bot, action_id):
    """Claim, dispatch and finalise one action. Never raises.

    Returns the terminal status (``'done'`` / ``'failed'``) it wrote, or
    ``None`` when there was nothing to do (already claimed/processed, or the
    claim itself errored). Shared by both the notify path and reconciliation.

    The executor is handed the id of the scope ITS KIND declares (:func:`_scope_id`),
    so a guild kind is dispatched exactly as it always was and a user kind gets
    the row's ``user_id``; a row whose scope does not match its kind is refused
    as ``bad_scope`` without the executor ever running. A kind in
    :data:`_ACTOR_KINDS` additionally gets the resolved ``requested_by`` Member as
    a fourth argument, and is refused - again without ever running - when that
    actor cannot be established (``actor_missing`` / ``actor_left_guild`` /
    ``actor_unverified``).

    The whole call is marked in :data:`_INFLIGHT_ACTIONS` - from BEFORE the claim
    (the mark must already be up when a concurrent reconcile looks) until after
    the terminal write - so the boot sweep can tell a claim this process is
    working on from one a dead process abandoned.
    """
    with _inflight(action_id):
        return await _handle_action(bot, action_id)


async def _handle_action(bot, action_id):
    """The body of :func:`handle_action`, run under its in-flight mark."""
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

    kind = claimed["kind"]
    payload = _coerce_payload(claimed["payload"])

    executor = _EXECUTORS.get(kind)
    if executor is None:
        await _finalise(pool, action_id, {"ok": False, "error": "unknown_kind"})
        return "failed"

    # The scope the KIND is entitled to, never the one the row happens to carry.
    scope_id = _scope_id(kind, claimed)
    if scope_id is None:
        # A guild kind on a user row, a user kind on a guild row, or a row the
        # CHECK would have rejected. Refused BEFORE the executor is entered, so
        # no executor ever has to defend itself against a None scope.
        await _finalise(pool, action_id, {"ok": False, "error": "bad_scope"})
        return "failed"

    # The ACTOR gate, for the kinds that publish a role. Resolved HERE, before
    # the executor is entered, for the same reason the scope is: an executor of
    # such a kind must never have to defend itself against a missing or
    # unverifiable actor. Every failure below is a REFUSAL with its own code -
    # there is no path where a role publication runs on the bot half alone.
    extra_args = ()
    if kind in _ACTOR_KINDS:
        guild = bot.get_guild(scope_id)
        if guild is None:
            # Same code the executors use for it: without the guild there is
            # neither a member to resolve nor anything to publish into.
            await _finalise(pool, action_id, {"ok": False, "error": "guild_unavailable"})
            return "failed"
        try:
            actor, refusal = await _resolve_actor(guild, _actor_id(claimed))
        except Exception:
            # _resolve_actor swallows its own fetch failures; anything reaching
            # here is unexpected, and unexpected still means UNVERIFIED.
            log.exception(
                "dashboard_actions: actor resolution raised for id=%s", action_id
            )
            actor, refusal = None, "actor_unverified"
        if refusal is not None:
            await _finalise(pool, action_id, {"ok": False, "error": refusal})
            return "failed"
        extra_args = (actor,)

    try:
        result = await executor(bot, scope_id, payload, *extra_args)
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
    ``_ORPHAN_RESET_SECONDS`` and the id is not one this process is handling -
    the listener is attached before this runs, so a live handler of THIS process
    may already hold a ``running`` row, either freshly claimed (recent
    ``updated_at``, stamped by ``_claim``) or still working after the window has
    passed (``mydata_export`` can); both are left alone and only rows orphaned by
    a dead previous process are reset - and (3)
    re-drive every remaining ``pending`` row through the normal atomic claim - so
    a concurrent live notify for the same row still can't double-run it. A
    duplicate is therefore possible only when a crash lands AFTER an executor's
    side effect but BEFORE its status write. Never raises out of a per-row failure.
    """
    pool = bot.db_pool

    # (1) Expire the too-old, EXCEPT ids this process is actively handling. The
    # in-flight guard is the same one step (2) uses and for the same reason: a
    # reconnect-time reconcile (not just boot) can run while a long executor -
    # mydata_export packing and uploading an archive - is still alive on a row
    # older than the window; without the exclusion this step would stamp its
    # live row failed/expired out from under it, the dashboard would show
    # "expired" for work in progress, and a retry would hit the already-taken
    # cooldown. At boot the set is empty, so genuinely orphaned rows still
    # expire. Both bounds are code constants / process state, never user input.
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'failed', result = $2::jsonb, updated_at = now() "
        "WHERE status IN ('pending', 'running') "
        "AND created_at < now() - $1 * INTERVAL '1 minute' "
        "AND NOT (id = ANY($3::bigint[]))",
        _STALE_ACTION_MINUTES,
        json.dumps({"ok": False, "error": "expired"}),
        sorted(_INFLIGHT_ACTIONS),
    )

    # (2) Reset orphaned 'running' rows. Two guards, because neither alone is
    # enough. AGE: the listener is already attached, so a live handler of THIS
    # process may hold a freshly claimed 'running' row (recent updated_at,
    # stamped by _claim); resetting it would let step 3 re-claim and re-run its
    # executor, doubling the side effect. IN-FLIGHT: an executor that runs longer
    # than the grace window (mydata_export packs and uploads an archive) has a
    # claim that is BOTH live and old, which the age guard alone would read as an
    # orphan - so ids this process is actually handling are excluded outright,
    # whatever their age. Both bounds are code constants/process state, never
    # user input, and are still passed as parameters rather than interpolated.
    await pool.execute(
        "UPDATE dashboard_actions "
        "SET status = 'pending', updated_at = now() "
        "WHERE status = 'running' "
        "AND updated_at < now() - $1 * INTERVAL '1 second' "
        "AND NOT (id = ANY($2::bigint[]))",
        _ORPHAN_RESET_SECONDS,
        sorted(_INFLIGHT_ACTIONS),
    )

    # (3) Re-drive everything still pending, oldest first, one at a time. An id
    # this process is already handling is skipped rather than re-entered: the
    # atomic claim would refuse it anyway, this just saves the round trip and
    # keeps the sweep's intent legible.
    rows = await pool.fetch(
        "SELECT id FROM dashboard_actions WHERE status = 'pending' ORDER BY id"
    )
    for row in rows:
        if row["id"] in _INFLIGHT_ACTIONS:
            continue
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
        # True once a LISTEN has been registered at least once, so the
        # supervisor can tell the FIRST connection from a RECONNECT (see
        # _maybe_reconcile: the gap behind a reconnect is a hole in delivery).
        self._connected_once = False
        # The in-flight sweep, so a connect-then-die loop schedules one rather
        # than one per cycle (see _maybe_reconcile).
        self._reconcile_task = None
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
                # Sampled BEFORE the connect: _connect_and_listen sets the flag.
                reconnect = self._connected_once
                await self._connect_and_listen()
                backoff = _BACKOFF_START  # healthy connect resets the backoff
                self._maybe_reconcile(reconnect=reconnect)
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

    def _maybe_reconcile(self, reconnect=False):
        """Schedule the reconciliation sweep as a tracked task, when it is due.

        Due at BOOT (once per process) and after every RECONNECT. The reconnect
        case is the twin of the cache-sync cog's post-reconnect resync and exists
        for the same reason: LISTEN/NOTIFY does not buffer, so an action the
        dashboard INSERTed and notified while this connection was down is lost
        exactly like a restart loses one. Without this it would sit 'pending'
        until the NEXT BOOT - the sweep only ever ran at startup - so a dropped
        socket at 02:00 meant the user's Verify button appeared whenever the bot
        next restarted, or never.

        Re-running the sweep at runtime is safe because it was already written to
        run alongside a LIVE listener: it is scheduled AFTER the new LISTEN is
        attached (so nothing arriving meanwhile is lost), and reconcile's own two
        guards do the rest - a 'running' row is only reset once its claim is
        older than _ORPHAN_RESET_SECONDS AND its id is not in _INFLIGHT_ACTIONS,
        so an executor of THIS process that outlived the gap (or the window) is
        never re-driven, whatever its age. Each call schedules exactly ONE sweep;
        the boot flag is what keeps the first connection from scheduling two.

        And at most one sweep is ever IN FLIGHT: a server that accepts the
        connection then immediately kills it (pgbouncer in transaction mode
        refusing LISTEN, connection churn) cycles about once a second, because
        the backoff resets on every SUCCESSFUL connect - so without this guard a
        wedged server would collect a fresh full sweep every second. Concurrent
        sweeps are correct (every claim is an atomic ``WHERE status='pending'
        RETURNING``), just a self-inflicted storm at the worst moment.

        Decoupled from the watch loop so a large backlog can't delay keepalive.
        """
        task = self._reconcile_task
        if task is not None and not task.done():
            log.debug(
                "dashboard_actions: a reconcile sweep is still running; "
                "not scheduling another"
            )
            return
        if not reconnect:
            if self._reconciled:
                return
            self._reconciled = True
            reason = "boot"
        else:
            reason = "reconnect"
            log.info(
                "dashboard_actions: listen connection re-established after a "
                "live connection; notifications sent during the gap were "
                "dropped by Postgres, so the reconcile sweep is re-running."
            )

        async def _run():
            try:
                await reconcile(self.bot)
            except Exception:
                log.exception(
                    "dashboard_actions: %s reconciliation failed", reason
                )

        self._reconcile_task = self.bot.loop.create_task(_run())
        self._track(self._reconcile_task)

    async def _connect_and_listen(self):
        conn = await asyncpg.connect(self._dsn)
        self._conn = conn
        await conn.add_listener(CHANNEL, self._on_notify)
        # Only now is a notification deliverable to this process; the reconnect
        # marker flips HERE so a failed connect never counts as a connection.
        self._connected_once = True
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
