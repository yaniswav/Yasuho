"""``?premiumadmin`` owner controls, the ENTITLEMENT_* gateway handlers and
the periodic reconciliation loop.

M3a+ of the monetisation plan (.claude/plans/monetisation/4-plan-retenu.md)
added the owner-only ``?premiumadmin`` surface (named ``?premium`` at the
time - see the M3c paragraph below for why it moved): the owner asked, in as
many words, to be able to grant premium to a friend's server or to a user
themself - before any store is open and whether or not a SKU is ever
configured. It writes ``premium_grants`` (tools/premium.py) and then the
bot's live cache, in that order, so a grant or a revoke is never visible in
memory before it is durable.

M3b (this lot) adds what keeps the OTHER half of the projection -
``premium_entitlements``, Discord's own SKU purchases - honest without ever
needing a restart:

* three gateway listeners (``on_entitlement_create/update/delete``) that
  write every event into ``premium_entitlements``
  (:func:`tools.premium.upsert_entitlement_event`) and then refresh only
  that one guild/user's slice of ``bot.premium``
  (:meth:`tools.premium.EntitlementCache.refresh_entitlement_scope`) - see
  :meth:`Premium._handle_entitlement_event` for the shared body and the
  module docstring of tools/premium.py's "EVENT ORDERING" section for the
  ordering guarantee;
* a periodic reconciliation loop (:attr:`Premium.reconcile_entitlements`)
  that lists every entitlement Discord has on record for this application
  and makes ``premium_entitlements`` match it exactly -
  :func:`tools.premium.reconcile` - then reloads the whole cache
  (:meth:`tools.premium.EntitlementCache.load`) on a complete pass. See
  :meth:`Premium._reconcile_once`.

M3c (this lot) adds three things on top:

* ``?premiumadmin testbuy server|user`` / ``?premiumadmin testclear`` - the
  owner's own end-to-end round trip through Discord's TEST-entitlement
  surface (``Client.create_entitlement`` / ``Entitlement.delete``), still
  owner-only and still prefix-only. Deliberately neither writes
  ``premium_entitlements`` itself: ``create_entitlement`` fires the exact
  same ``ENTITLEMENT_CREATE`` gateway event a real purchase would, so the M3b
  listeners above do the actual write - these two commands only ever ask
  Discord to create or delete a test entitlement, which is what makes them a
  genuine test of the WHOLE purchase -> event -> cache -> ``/premium``
  status path rather than a shortcut around it. See
  :meth:`Premium.premium_testbuy_server`/:meth:`premium_testbuy_user`/
  :meth:`premium_testclear`.
* a "PREMIUM-" prefixed INFO line for every premium state change (an
  ENTITLEMENT_* event landing, a complete reconciliation pass, an owner
  grant/revoke/testbuy/testclear) - one greppable technical audit trail, no
  payment data, no more personal data than the ids already named above. See
  :meth:`Premium._handle_entitlement_event` (now logging which of the three
  events it was), the end of :meth:`Premium._reconcile_once`, and the end of
  each grant/revoke/testbuy/testclear command below.
* the owner-only group itself is RENAMED from ``?premium`` to
  ``?premiumadmin`` (every leaf below: grant, revoke, list, check, testbuy,
  testclear), freeing the ``premium`` token for the public entry point - see
  the next paragraph. This is purely a rename: the gate shape (
  ``@commands.is_owner()`` on every leaf, ``cog_check`` on top) and every
  leaf's behaviour are unchanged.

The public, no-gate entry point this lot also adds - ``/premium`` (a hybrid
command, so ``?premium`` works too) - lives in its own cog,
cogs/system/premium_panel.py, precisely so it is never subject to this cog's
owner-only ``cog_check``. It could not be named that before this lot's own
rename above: the token was already this group's, and a hybrid or app
command sharing it would have collided in
tests/test_command_tree_hygiene.py's ROOT namespace (verified directly
against that guard - it conflates every prefix-root AND app-command-root
name into one bucket, a real non-issue for two separate Discord registries
but exactly the guard's job to catch for anything else). Renaming THIS
group out of the way, rather than carving out an exemption in that guard,
keeps the guard's promise intact for every other command.

Prefix-only by design for the ``?premiumadmin`` command group, with no
app_command anywhere: an owner-only control surface has no business in the
public slash picker, and a hybrid command's subcommands would need their
OWN checks on both invocation paths (see
tests/test_hybrid_gating_hygiene.py's docstring for why a group's check does
not protect them) for no benefit here. ``@commands.is_owner()`` on every
leaf is the real gate - ``cog_check`` is a second, cog-wide layer on top, the
same belt-and-suspenders cogs/system/admin.py and cogs/system/retention.py
already use. The gateway listeners and the reconciliation loop need no such
gate - nothing about them is a user-invoked command.

M5 (this lot) DMs the bot owner (Team-aware: every admin/developer Team
member if the application is Team-owned, same resolution
``discord.ext.commands.Bot.is_owner`` itself uses - see
:meth:`Premium._resolve_owner_ids`) one short, plain-English message for
every genuine sale-relevant transition, EXACTLY ONCE:

* a NEW entitlement (:meth:`Premium._handle_entitlement_event`'s own
  before/after read, or :func:`tools.premium.reconcile`'s ``new_rows`` when
  the gateway event that should have announced it was missed - labelled
  "(found by reconciliation)");
* an entitlement ENDING (a refund or a cancellation - the same event/
  reconciliation split, via ``tools.premium.reconcile``'s ``ended_rows``).

Both are derived from a DATABASE TRANSITION (a before/after snapshot of the
one row, never from "an event arrived") by
:func:`tools.premium.classify_entitlement_transition` - see that function's
own docstring for the exact rule and why it makes a replayed/duplicated
event, or reconciliation re-reading a row it already knew about, a no-op
rather than a second DM. An owner grant (``?premiumadmin grant``) is
deliberately NOT one of these - it never touches ``premium_entitlements``
at all, so no transition is ever derived for it, and no DM is ever sent.

RENEWALS ARE DELIBERATELY NOT HANDLED - VERDICT AND SOURCES. Discord does
not surface a successful subscription renewal as any kind of entitlement
event: per Discord's own "Entitlement Update Events" and "Implementing App
Subscriptions" documentation (docs.discord.com/developers/events/gateway-
events and .../monetization/implementing-app-subscriptions, both read
2026-10-07), a subscription entitlement is granted with ``ends_at`` left
``NULL`` for as long as it keeps renewing, and ``ENTITLEMENT_UPDATE`` fires
ONLY once, when the subscription actually ends (carrying the ``ends_at``
that marks when it stopped) - as of the October 1, 2024 change, there is no
longer even an ``ENTITLEMENT_UPDATE`` on a successful renewal. The only
Discord-side signal for a renewal is ``SUBSCRIPTION_UPDATE`` (a DIFFERENT
resource - ``discord.Subscription``, current_period_start/current_period_end
- confirmed present as ``on_subscription_create/update/delete`` in this
repo's installed discord.py, ``discord/state.py``'s
``parse_subscription_*``), which this cog does not listen for: wiring it up
would mean standing up an entirely separate event family and its own
period-tracking state for a label ("renewed") the owner did not ask for and
that carries no entitlement/cache consequence of its own - out of scope for
this lot. Per this lot's own brief ("if they cannot be detected reliably,
say so and skip renewals"), renewals are skipped outright rather than
guessed at from ``ends_at`` moving (it never does, while a subscription is
actively renewing).

Delivery (:meth:`Premium._dispatch_sale_dm`/:meth:`Premium._deliver_sale_dm`)
is always a background task, fire-and-forget from the gateway listener's or
the reconciliation loop's point of view: a slow or failing DM (closed DMs,
``Forbidden``, any other ``HTTPException``) is caught, logged as a single
``PREMIUM-OWNER-DM-FAILED reason=...`` line, and never raised - it must
never slow down or break the write path that found the transition. A
successful send logs ``PREMIUM-OWNER-DM kind=new|ended|reconcile-summary``.
A single reconciliation pass finding more than
:data:`RECONCILE_DM_STORM_THRESHOLD` transitions at once (a long outage, or
the very first pass ever run against an existing customer base) collapses
to ONE summary DM instead of one per transition - see
:meth:`Premium._notify_reconcile_transitions`.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from tools import premium, premium_usage
from tools.formats import format_dt, random_colour
from tools.i18n import _
from tools.time import ShortTime

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()

# Sentinel for "the pre-write read of an entitlement's prior state failed" -
# see _read_prior_deleted_flag below. Deliberately distinct from both `None`
# (confirmed: no row existed yet) and a real row (confirmed: it existed),
# since a caller that cannot tell which of those two is true must skip the
# sale notification for this one event rather than guess - a false "new
# sale" DM, or a missed one, are both worse than silence here, and a
# genuinely missed one still self-heals via the next reconciliation pass's
# own "(found by reconciliation)" label.
_UNKNOWN_PRIOR_STATE = object()

# M5 (owner-DM-on-sale): if a single reconciliation pass finds more than
# this many sale-relevant transitions at once (a long gap since the last
# successful pass, or the very first pass ever run against an existing
# customer base), send ONE summary DM instead of one per transition - see
# Premium._notify_reconcile_transitions.
RECONCILE_DM_STORM_THRESHOLD = 5

# discord.EntitlementType -> a short, plain-English label for the owner DM
# (M5). Keyed by the enum's own int VALUE (not the enum itself) because the
# stored/returned row carries entitlement_type as a plain int (see
# tools.premium._coerce_entitlement) - never re-wrapped back into the enum
# on the way out, so this module must not assume it is one either.
_ENTITLEMENT_TYPE_LABELS = {
    discord.EntitlementType.purchase.value: "purchase",
    discord.EntitlementType.premium_subscription.value: "premium subscription",
    discord.EntitlementType.developer_gift.value: "developer gift",
    discord.EntitlementType.test_mode_purchase.value: "test-mode purchase",
    discord.EntitlementType.free_purchase.value: "free purchase",
    discord.EntitlementType.user_gift.value: "user gift",
    discord.EntitlementType.premium_purchase.value: "premium purchase",
    discord.EntitlementType.application_subscription.value: "application subscription",
}


# Discord's "An entitlement has already been granted for this resource".
_ALREADY_GRANTED = 40074


def _is_test_entitlement(entitlement_type, starts_at):
    """True for a Discord TEST entitlement. A test-mode purchase says so in
    its type, but a test entitlement created through the API
    (?premiumadmin testbuy) carries the SKU's ordinary type (e.g.
    application subscription); what marks it is the missing start date -
    discord.py documents starts_at as "not present when using test
    entitlements". Accepts the enum or its int value."""
    entitlement_type = getattr(entitlement_type, "value", entitlement_type)
    return (
        entitlement_type == discord.EntitlementType.test_mode_purchase.value
        or starts_at is None
    )


def _entitlement_type_label(entitlement_type):
    return _ENTITLEMENT_TYPE_LABELS.get(entitlement_type, f"type {entitlement_type}")


def _sale_product_name(sku_id):
    """"Yasuho+"/"Pack Confort" for whichever product's SKU this is, from
    this module's OWN already-parsed [Premium] config - never re-reads
    bot.ini (see tools.premium's own "[Premium] SKU configuration" section).
    Falls back to a bare SKU id for a row naming neither (should not happen
    in production - reconcile/the gateway handlers both narrow to the
    configured catalog - but a DM must never crash over a fallback label)."""
    if sku_id == premium.YASUHO_PLUS_SKU:
        return "Yasuho+"
    if sku_id == premium.COMFORT_PACK_SKU:
        return "Pack Confort"
    return f"SKU {sku_id}"


def _sale_scope_desc(bot, scope_type, guild_id, user_id):
    """"server <name> (<id>)" from the bot's OWN guild cache if it has the
    guild, else just "<id>" (never an await, never a fetch - a cache miss is
    not worth delaying a fire-and-forget DM for); "<mention> (<id>)" for a
    user scope, no fetch needed since a mention never requires the user's
    name to resolve correctly client-side."""
    if scope_type == "guild":
        guild = bot.get_guild(guild_id) if guild_id is not None else None
        name = guild.name if guild is not None else str(guild_id)
        return f"server {name} ({guild_id})"
    return f"<@{user_id}> ({user_id})"


def _build_sale_dm(bot, kind, row, *, found_by_reconciliation=False):
    """Plain-English (never tools.i18n._()) owner DM text for one
    sale-relevant transition - ``kind`` is ``"new"`` or ``"ended"``, ``row``
    is whatever tools.premium.upsert_entitlement_event/upsert_entitlement/
    load_active_entitlement_rows returned (a plain dict or an asyncpg.Record
    - both support the same ``row["column"]`` reads used below).

    Deliberately plain text, not an embed and not wrapped in tools.i18n._():
    this is a technical notification for the bot owner ALONE, the same
    "owner diagnostic, plain text" posture ?premiumadmin usage already takes
    above, never a member-facing string that would need translating.
    """
    product = _sale_product_name(row["sku_id"])
    scope_desc = _sale_scope_desc(bot, row["scope_type"], row["guild_id"], row["user_id"])
    entitlement_type = row["entitlement_type"]
    if kind == "new":
        head = "New sale"
        if found_by_reconciliation:
            head += " (found by reconciliation)"
        ends_at = row["ends_at"]
        ends_str = format_dt(ends_at) if ends_at else "never"
        lines = [
            f"{head}: {product} for {scope_desc}",
            f"Type: {_entitlement_type_label(entitlement_type)}",
            f"Ends: {ends_str}",
        ]
    else:
        lines = [
            f"Ended (refund or cancellation): {product} for {scope_desc}",
            f"Type: {_entitlement_type_label(entitlement_type)}",
        ]
    text = "\n".join(lines)
    if _is_test_entitlement(entitlement_type, row.get("starts_at")):
        text = "TEST - " + text
    return text

# The two scope keywords ?premiumadmin list/check accept, mapped to
# tools.premium's scope_type strings.
_SCOPE_TYPE = {"server": "guild", "user": "user"}

# How often the periodic reconciliation (self.reconcile_entitlements) lists
# Discord's own entitlement ledger and resyncs premium_entitlements against
# it. The gateway listeners are the real-time path; this is the safety net
# for whatever they missed (a gateway reconnect window, a dropped event) -
# see tools.premium.reconcile's own docstring for the fail-safe algorithm.
# 6h is comfortably inside the 48h GRACE window tools.premium.is_active
# already grants a confirmed-but-not-yet-resynced subscription end, so a
# missed event is caught and corrected well before that grace would matter,
# while staying far below any rate-limit concern for a handful of REST pages
# every few hours (see the lot's report for the full scale story).
RECONCILE_INTERVAL_HOURS = 6


def _parse_duration_and_reason(rest):
    """Split "[duration] [reason...]" into ``(expires_at_or_None, reason_or_None)``.

    ``rest`` is the raw text after the id argument. The FIRST whitespace-
    separated token is tried against :class:`tools.time.ShortTime` (the same
    parser ``?remind`` uses for "2d"/"12h"/...); if it parses, it is consumed
    as the duration and everything after it is the reason. If it does not
    parse (or ``rest`` is empty), there is no duration - the grant is
    permanent - and the WHOLE of ``rest`` is the reason. This mirrors
    reminders.py's own ShortTime-first, text-second split.
    """
    rest = (rest or "").strip()
    if not rest:
        return None, None
    token, _sep, remainder = rest.partition(" ")
    match = ShortTime.compiled.fullmatch(token)
    if match is not None and match.group(0):
        try:
            expires_at = ShortTime(token).dt
        except commands.BadArgument:
            return None, rest
        return expires_at, (remainder.strip() or None)
    return None, rest


def _embed(title):
    return discord.Embed(title=title, colour=random_colour())


# Discord's embed size limits this cog must respect BY CONSTRUCTION, not by
# luck: a description over 4096 characters, or one field value over 1024,
# is rejected by the API (discord.HTTPException on send), not by
# discord.Embed itself at construction time. `reason` is free text the owner
# types (bounded only by a Discord message's own ~2000-char limit) and
# `?premiumadmin list`/`?premiumadmin check` otherwise join an unbounded NUMBER of rows
# into one string, so either a long reason or enough active rows can cross
# either budget on their own. Both constants below leave headroom under the
# real ceiling rather than target it exactly.
_REASON_CLIP = 120
_DESCRIPTION_BUDGET = 4000
_FIELD_BUDGET = 1000


def _clip_reason(reason):
    """Shorten a free-text reason so one row can never dominate a line budget."""
    if not reason or len(reason) <= _REASON_CLIP:
        return reason
    return reason[:_REASON_CLIP] + "..."


def _join_within_budget(lines, budget):
    """Join ``lines`` with newlines, stopping before the result would cross
    ``budget`` characters and naming how many were left out instead - the
    same "+N more" shape ``?premiumadmin list``'s row-count cap already uses, so
    an overlong field value never reaches discord.py's HTTP call at all."""
    kept = []
    total = 0
    for index, line in enumerate(lines):
        total += len(line) + 1  # +1 for the joining newline
        if total > budget:
            remaining = len(lines) - index
            kept.append(_("{count} more not shown.").format(count=remaining))
            break
        kept.append(line)
    return "\n".join(kept)


class Premium(commands.Cog):
    """Owner-only gifting of Yasuho+ / Pack Confort, the ENTITLEMENT_* gateway
    handlers, and the periodic reconciliation loop against Discord's own
    entitlement ledger."""

    def __init__(self, bot):
        self.bot = bot
        # Strong references to in-flight owner-DM background tasks (M5), so
        # they are never garbage-collected mid-flight - discarded by their
        # own done callback once finished. A plain set, never awaited on
        # unload: cog_unload only cancels the reconciliation loop (see
        # below), the same "a DM must never block anything" posture that
        # makes every DM send fire-and-forget in the first place.
        self._pending_owner_dms = set()

    async def cog_load(self):
        # Starting the task HERE rather than in __init__ is deliberate, and
        # is why this cog's own task-starting convention differs from
        # cogs/system/retention.py's and cogs/anilist/airing.py's (both start
        # their tasks.loop directly in __init__): tests/cogs/test_premium.py
        # constructs ``Premium(bot)`` directly against a plain stand-in bot
        # (no ``wait_until_ready``, no ``entitlements``) for every one of its
        # ?premiumadmin command tests, never through ``bot.add_cog`` - starting a
        # task eagerly in __init__ would have tried to use attributes that
        # stand-in does not have the moment ANY of those tests constructs the
        # cog. ``cog_load`` is the hook discord.py itself awaits from
        # ``add_cog`` (see discord/ext/commands/cog.py), and only from there -
        # so direct construction in a test stays exactly as inert as it was
        # before this lot, while production (core.py's real ``add_cog`` call)
        # starts the loop exactly once, right after the cog attaches.
        self.reconcile_entitlements.start()

    def cog_unload(self):
        self.reconcile_entitlements.cancel()

    async def cog_check(self, ctx):
        # Second layer on top of @commands.is_owner() on every leaf below -
        # see the module docstring for why both exist.
        return await self.bot.is_owner(ctx.author)

    # -- group ---------------------------------------------------------

    @commands.group(name="premiumadmin", hidden=True, invoke_without_command=True)
    @commands.is_owner()
    async def premium_group(self, ctx):
        await ctx.send_help(ctx.command)

    # -- grant -----------------------------------------------------------

    @premium_group.group(name="grant", invoke_without_command=True)
    @commands.is_owner()
    async def premium_grant(self, ctx):
        """Gift a perk: ?premiumadmin grant server <guild_id> or grant user <user_id>"""
        await ctx.send_help(ctx.command)

    @premium_grant.command(name="server")
    @commands.is_owner()
    async def premium_grant_server(self, ctx, guild_id: int, *, rest: str = ""):
        """Gift Yasuho+ to a server: ?premiumadmin grant server <guild_id> [duration] [reason...]

        DELIBERATELY allows a guild_id the bot is not currently a member of
        (no ``self.bot.get_guild`` check): the plan's own "gift a friend's
        server... before any store is open" scenario includes pre-granting
        before the bot has even been invited there (a pre-launch/preorder
        gift) - the row simply sits unused until the guild exists from the
        bot's point of view, exactly like a grant surviving the bot leaving
        and later rejoining a guild already does. What IS rejected is a
        value that cannot be a real Discord id at all (zero or negative) -
        see the check right below.
        """
        if guild_id <= 0:
            await ctx.send(
                _("Server id must be a positive number, not `{value}`.").format(
                    value=guild_id
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        expires_at, reason = _parse_duration_and_reason(rest)
        grant_id = await premium.create_grant(
            self.bot.db_pool,
            product=premium.PRODUCT_YASUHO_PLUS,
            scope_type="guild",
            guild_id=guild_id,
            reason=reason,
            granted_by=ctx.author.id,
            expires_at=expires_at,
        )
        await self.bot.premium.refresh_grant_scope(
            self.bot.db_pool, "guild", guild_id=guild_id
        )
        log.info(
            "PREMIUM-GRANT product=%s scope=guild scope_id=%s grant_id=%s "
            "by=%s",
            premium.PRODUCT_YASUHO_PLUS,
            guild_id,
            grant_id,
            ctx.author.id,
        )
        until = format_dt(expires_at) if expires_at else _("permanent")
        await ctx.send(
            _(
                "Granted **Yasuho+** to server `{guild_id}` (grant #{grant_id}, "
                "{until})."
            ).format(guild_id=guild_id, grant_id=grant_id, until=until),
            allowed_mentions=NO_MENTIONS,
        )

    @premium_grant.command(name="user")
    @commands.is_owner()
    async def premium_grant_user(self, ctx, user_id: int, *, rest: str = ""):
        """Gift the Pack Confort to a user: ?premiumadmin grant user <user_id> [duration] [reason...]"""
        if user_id <= 0:
            await ctx.send(
                _("User id must be a positive number, not `{value}`.").format(
                    value=user_id
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        expires_at, reason = _parse_duration_and_reason(rest)
        grant_id = await premium.create_grant(
            self.bot.db_pool,
            product=premium.PRODUCT_COMFORT_PACK,
            scope_type="user",
            user_id=user_id,
            reason=reason,
            granted_by=ctx.author.id,
            expires_at=expires_at,
        )
        await self.bot.premium.refresh_grant_scope(
            self.bot.db_pool, "user", user_id=user_id
        )
        log.info(
            "PREMIUM-GRANT product=%s scope=user scope_id=%s grant_id=%s "
            "by=%s",
            premium.PRODUCT_COMFORT_PACK,
            user_id,
            grant_id,
            ctx.author.id,
        )
        until = format_dt(expires_at) if expires_at else _("permanent")
        await ctx.send(
            _(
                "Granted the **Pack Confort** to user `{user_id}` (grant "
                "#{grant_id}, {until})."
            ).format(user_id=user_id, grant_id=grant_id, until=until),
            allowed_mentions=NO_MENTIONS,
        )

    # -- revoke ------------------------------------------------------------

    @premium_group.command(name="revoke")
    @commands.is_owner()
    async def premium_revoke(self, ctx, grant_id: int):
        """Revoke one grant by id: ?premiumadmin revoke <grant_id>"""
        # Looked up BEFORE the revoke, by its own id only - one row, not a
        # filtered list - purely to learn which scope's cache entry needs a
        # refresh after a successful revoke; the revoke itself does not need
        # it (revoke_grant takes the id alone).
        row = await self.bot.db_pool.fetchrow(
            "SELECT scope_type, guild_id, user_id FROM premium_grants "
            "WHERE id = $1",
            grant_id,
        )
        revoked = await premium.revoke_grant(
            self.bot.db_pool, grant_id, revoked_by=ctx.author.id
        )
        if not revoked:
            await ctx.send(
                _(
                    "No active grant #{grant_id} found (already revoked, "
                    "expired, or it never existed)."
                ).format(grant_id=grant_id),
                allowed_mentions=NO_MENTIONS,
            )
            return
        scope_label, scope_id = None, None
        if row is not None:
            if row["scope_type"] == "guild":
                scope_label, scope_id = "guild", row["guild_id"]
                await self.bot.premium.refresh_grant_scope(
                    self.bot.db_pool, "guild", guild_id=row["guild_id"]
                )
            else:
                scope_label, scope_id = "user", row["user_id"]
                await self.bot.premium.refresh_grant_scope(
                    self.bot.db_pool, "user", user_id=row["user_id"]
                )
        log.info(
            "PREMIUM-REVOKE grant_id=%s scope=%s scope_id=%s by=%s",
            grant_id,
            scope_label,
            scope_id,
            ctx.author.id,
        )
        await ctx.send(
            _("Revoked grant #{grant_id}.").format(grant_id=grant_id),
            allowed_mentions=NO_MENTIONS,
        )

    # -- list ----------------------------------------------------------

    @premium_group.command(name="list")
    @commands.is_owner()
    async def premium_list(self, ctx, scope: str = None, target_id: int = None):
        """List active grants: ?premiumadmin list [server|user <id>]"""
        if scope is not None and scope not in _SCOPE_TYPE:
            await ctx.send(
                _("Scope must be `server` or `user`, not `{scope}`.").format(
                    scope=scope
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        kwargs = {}
        if scope == "server":
            kwargs["scope_type"] = "guild"
            kwargs["guild_id"] = target_id
        elif scope == "user":
            kwargs["scope_type"] = "user"
            kwargs["user_id"] = target_id
        rows = await premium.list_grants(self.bot.db_pool, **kwargs)
        embed = _embed(_("Active premium grants"))
        if not rows:
            embed.description = _("No active grant matches.")
        else:
            lines = []
            for row in rows[:25]:
                scope_label = "server" if row["scope_type"] == "guild" else "user"
                scope_id = row["guild_id"] if row["scope_type"] == "guild" else row["user_id"]
                until = format_dt(row["expires_at"]) if row["expires_at"] else _("permanent")
                lines.append(
                    _("#{id} - {product} - {scope} `{scope_id}` - {until}").format(
                        id=row["id"],
                        product=row["product"],
                        scope=scope_label,
                        scope_id=scope_id,
                        until=until,
                    )
                )
            # Two independent caps, never traded against each other: at most
            # 25 ROWS shown (the footer below counts whatever that excludes),
            # and within those 25 the DESCRIPTION's own character budget
            # (_join_within_budget) - a `reason` is not shown here, so a long
            # one cannot inflate a list row, but 25 ids/dates alone are still
            # worth guarding on principle.
            embed.description = _join_within_budget(lines, _DESCRIPTION_BUDGET)
            if len(rows) > 25:
                embed.set_footer(
                    text=_("{count} more not shown.").format(count=len(rows) - 25)
                )
        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # -- check -----------------------------------------------------------

    @premium_group.command(name="check")
    @commands.is_owner()
    async def premium_check(self, ctx, scope: str, target_id: int):
        """Show effective premium status and why: ?premiumadmin check server|user <id>"""
        if scope not in _SCOPE_TYPE:
            await ctx.send(
                _("Scope must be `server` or `user`, not `{scope}`.").format(
                    scope=scope
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        scope_type = _SCOPE_TYPE[scope]
        embed = _embed(_("Premium status"))

        if scope_type == "guild":
            is_premium = self.bot.premium.is_guild_premium(target_id)
            entitlement_rows = await self.bot.db_pool.fetch(
                "SELECT entitlement_id, sku_id, entitlement_type, starts_at, ends_at "
                "FROM premium_entitlements "
                "WHERE guild_id = $1 AND deleted = FALSE",
                target_id,
            )
            sku = premium.YASUHO_PLUS_SKU
            grants = await premium.list_grants(
                self.bot.db_pool, scope_type="guild", guild_id=target_id
            )
        else:
            is_premium = self.bot.premium.has_comfort_pack(target_id)
            entitlement_rows = await self.bot.db_pool.fetch(
                "SELECT entitlement_id, sku_id, entitlement_type, starts_at, ends_at "
                "FROM premium_entitlements "
                "WHERE user_id = $1 AND deleted = FALSE",
                target_id,
            )
            sku = premium.COMFORT_PACK_SKU
            grants = await premium.list_grants(
                self.bot.db_pool, scope_type="user", user_id=target_id
            )

        active_entitlements = [
            row
            for row in entitlement_rows
            if sku is not None
            and int(row["sku_id"]) == sku
            and premium.is_active(row)
        ]

        embed.add_field(
            name=_("Effective"),
            value=_("Premium") if is_premium else _("Free"),
            inline=False,
        )
        if active_entitlements:
            lines = []
            for row in active_entitlements:
                until = format_dt(row["ends_at"]) if row["ends_at"] else _("permanent")
                line = _("Discord entitlement #{id}, {until}").format(
                    id=row["entitlement_id"], until=until
                )
                # The id is what ?premiumadmin testclear takes.
                if _is_test_entitlement(row["entitlement_type"], row["starts_at"]):
                    line += " - TEST"
                lines.append(line)
            embed.add_field(
                name=_("Entitlement"),
                value=_join_within_budget(lines, _FIELD_BUDGET),
                inline=False,
            )
        if grants:
            lines = []
            for row in grants:
                until = format_dt(row["expires_at"]) if row["expires_at"] else _("permanent")
                reason = _clip_reason(row["reason"]) or _("(no reason given)")
                lines.append(
                    _("#{id} - {until} - {reason}").format(
                        id=row["id"], until=until, reason=reason
                    )
                )
            embed.add_field(
                name=_("Owner grant(s)"),
                value=_join_within_budget(lines, _FIELD_BUDGET),
                inline=False,
            )
        if not active_entitlements and not grants:
            embed.add_field(name=_("Why"), value=_("Nothing active."), inline=False)

        await ctx.send(embed=embed, allowed_mentions=NO_MENTIONS)

    # -- usage (L4: .claude/plans/monetisation/) -------------------------
    #
    # Owner-only diagnostic for setting limits from DATA rather than
    # guesses: tools/premium_usage.py runs one bounded aggregate query per
    # premium-raisable resource (percentiles computed IN SQL, never a
    # per-row Python loop) and this just renders the result. Deliberately
    # PLAIN ENGLISH, never tools.i18n._() - this is a fixed-width table for
    # the owner deciding where to set a limit, not a member-facing reply
    # (the same "owner diagnostic, plain text" posture cogs/system/admin.py's
    # ?eval already takes - see its own command for the precedent).

    @premium_group.command(name="usage")
    @commands.is_owner()
    async def premium_usage_report(self, ctx):
        """Real usage of every premium-raisable limit, from the current
        database: ?premiumadmin usage"""
        try:
            rows_by_key = await premium_usage.fetch_all(self.bot.db_pool)
        except Exception:
            log.exception("premiumadmin usage: query failed")
            await ctx.send("Could not read usage from the database.")
            return
        for chunk in premium_usage.render_report(rows_by_key):
            await ctx.send(f"```\n{chunk}\n```", allowed_mentions=NO_MENTIONS)

    # -- test purchases (M3c) -------------------------------------------
    #
    # Deliberately write NOTHING to premium_entitlements themselves.
    # Client.create_entitlement fires the exact same ENTITLEMENT_CREATE
    # gateway event a real purchase would (and Entitlement.delete the exact
    # same ENTITLEMENT_DELETE a refund/revoke would), so the M3b listeners
    # above do the actual write - these commands only ever ask Discord to
    # create/delete a TEST entitlement, which is what makes them a genuine
    # end-to-end test of purchase -> event -> cache -> /premium status
    # rather than a shortcut that skips the parts worth testing.

    @premium_group.group(name="testbuy", invoke_without_command=True)
    @commands.is_owner()
    async def premium_testbuy(self, ctx):
        await ctx.send_help(ctx.command)

    async def _find_new_test_entitlement(self, sku_id, *, guild_id=None, user_id=None):
        """Best-effort id of the entitlement :meth:`Client.create_entitlement`
        just made, for the confirmation message's ``?premiumadmin testclear`` hint.

        ``create_entitlement`` itself returns nothing - Discord's own API
        gives back no body for it - so this re-lists Discord's own
        entitlements for the exact sku+scope just created and returns the
        highest id (newest) TEST entitlement found, or ``None`` if the
        listing does not show it yet (eventual consistency on Discord's
        side; the gateway event lands and is handled regardless - the caller
        falls back to pointing the owner at ``?premiumadmin check``).
        """
        kwargs = {
            "skus": [discord.Object(id=sku_id)],
            "exclude_deleted": False,
            "limit": 5,
        }
        if guild_id is not None:
            kwargs["guild"] = discord.Object(id=guild_id)
        if user_id is not None:
            kwargs["user"] = discord.Object(id=user_id)
        best_id = None
        async for entitlement in self.bot.entitlements(**kwargs):
            if not _is_test_entitlement(entitlement.type, entitlement.starts_at):
                continue
            if best_id is None or entitlement.id > best_id:
                best_id = entitlement.id
        return best_id

    @premium_testbuy.command(name="server")
    @commands.is_owner()
    async def premium_testbuy_server(self, ctx, guild_id: int):
        """Create a TEST Yasuho+ entitlement for a server: ?premiumadmin testbuy server <guild_id>"""
        sku_id = premium.YASUHO_PLUS_SKU
        if sku_id is None:
            await ctx.send(
                _(
                    "No `yasuho_plus_sku` is configured in `[Premium]` yet - "
                    "there is nothing to test-buy."
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        try:
            await self.bot.create_entitlement(
                discord.Object(id=sku_id),
                discord.Object(id=guild_id),
                discord.EntitlementOwnerType.guild,
            )
        except discord.HTTPException as error:
            if error.code != _ALREADY_GRANTED:
                raise
            existing = await self._find_new_test_entitlement(sku_id, guild_id=guild_id)
            await ctx.send(
                _(
                    "Discord says this {scope} already has this product "
                    "(error 40074). Clear the TEST entitlement first: "
                    "`?premiumadmin testclear {entitlement_id}`."
                ).format(scope="server", entitlement_id=existing or "<id>"),
                allowed_mentions=NO_MENTIONS,
            )
            return
        log.info(
            "PREMIUM-TESTBUY sku=%s scope=guild scope_id=%s by=%s",
            sku_id,
            guild_id,
            ctx.author.id,
        )
        entitlement_id = await self._find_new_test_entitlement(
            sku_id, guild_id=guild_id
        )
        if entitlement_id is None:
            await ctx.send(
                _(
                    "Created a TEST **Yasuho+** entitlement for server "
                    "`{guild_id}`. Its id was not visible yet when I checked "
                    "- use `?premiumadmin check server {guild_id}` in a moment."
                ).format(guild_id=guild_id),
                allowed_mentions=NO_MENTIONS,
            )
            return
        await ctx.send(
            _(
                "Created TEST entitlement #{entitlement_id} (**Yasuho+**) "
                "for server `{guild_id}`. Clear it later with `?premiumadmin "
                "testclear {entitlement_id}`."
            ).format(entitlement_id=entitlement_id, guild_id=guild_id),
            allowed_mentions=NO_MENTIONS,
        )

    @premium_testbuy.command(name="user")
    @commands.is_owner()
    async def premium_testbuy_user(self, ctx, user_id: int):
        """Create a TEST Pack Confort entitlement for a user: ?premiumadmin testbuy user <user_id>"""
        sku_id = premium.COMFORT_PACK_SKU
        if sku_id is None:
            await ctx.send(
                _(
                    "No `comfort_pack_sku` is configured in `[Premium]` yet "
                    "- there is nothing to test-buy."
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        try:
            await self.bot.create_entitlement(
                discord.Object(id=sku_id),
                discord.Object(id=user_id),
                discord.EntitlementOwnerType.user,
            )
        except discord.HTTPException as error:
            if error.code != _ALREADY_GRANTED:
                raise
            existing = await self._find_new_test_entitlement(sku_id, user_id=user_id)
            await ctx.send(
                _(
                    "Discord says this {scope} already has this product "
                    "(error 40074). Clear the TEST entitlement first: "
                    "`?premiumadmin testclear {entitlement_id}`."
                ).format(scope="user", entitlement_id=existing or "<id>"),
                allowed_mentions=NO_MENTIONS,
            )
            return
        log.info(
            "PREMIUM-TESTBUY sku=%s scope=user scope_id=%s by=%s",
            sku_id,
            user_id,
            ctx.author.id,
        )
        entitlement_id = await self._find_new_test_entitlement(
            sku_id, user_id=user_id
        )
        if entitlement_id is None:
            await ctx.send(
                _(
                    "Created a TEST **Pack Confort** entitlement for user "
                    "`{user_id}`. Its id was not visible yet when I checked "
                    "- use `?premiumadmin check user {user_id}` in a moment."
                ).format(user_id=user_id),
                allowed_mentions=NO_MENTIONS,
            )
            return
        await ctx.send(
            _(
                "Created TEST entitlement #{entitlement_id} (**Pack "
                "Confort**) for user `{user_id}`. Clear it later with "
                "`?premiumadmin testclear {entitlement_id}`."
            ).format(entitlement_id=entitlement_id, user_id=user_id),
            allowed_mentions=NO_MENTIONS,
        )

    @premium_group.command(name="testclear")
    @commands.is_owner()
    async def premium_testclear(self, ctx, entitlement_id: int):
        """Delete a TEST entitlement by id: ?premiumadmin testclear <entitlement_id>"""
        try:
            entitlement = await self.bot.fetch_entitlement(entitlement_id)
        except discord.NotFound:
            await ctx.send(
                _("No entitlement #{entitlement_id} exists.").format(
                    entitlement_id=entitlement_id
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        except discord.HTTPException:
            await ctx.send(
                _(
                    "Could not fetch entitlement #{entitlement_id} from "
                    "Discord."
                ).format(entitlement_id=entitlement_id),
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not _is_test_entitlement(entitlement.type, entitlement.starts_at):
            await ctx.send(
                _(
                    "Entitlement #{entitlement_id} is not a TEST entitlement "
                    "(type `{type}`) - refusing to delete a real purchase."
                ).format(
                    entitlement_id=entitlement_id, type=entitlement.type.name
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        await entitlement.delete()
        log.info(
            "PREMIUM-TESTCLEAR entitlement=%s by=%s",
            entitlement_id,
            ctx.author.id,
        )
        await ctx.send(
            _(
                "Deleted TEST entitlement #{entitlement_id}. The matching "
                "ENTITLEMENT_DELETE event will clear it from the cache."
            ).format(entitlement_id=entitlement_id),
            allowed_mentions=NO_MENTIONS,
        )

    # -- owner DM on sale/refund (M5) ------------------------------------
    #
    # See the module docstring's own "M5" paragraph for the feature and the
    # sourced renewal verdict. Everything here is additive: it reads a
    # before/after snapshot the write paths below already make durable, and
    # a failure anywhere in this section is caught and logged, never raised
    # - a DM must never slow down or break the gateway listener or the
    # reconciliation loop that found the transition.

    async def _read_prior_deleted_flag(self, entitlement_id):
        """The stored ``deleted`` flag for ``entitlement_id`` BEFORE this
        event's own write - ``None`` if no row exists yet (a genuinely new
        id), a row if one does, or :data:`_UNKNOWN_PRIOR_STATE` if the read
        itself failed. The caller skips any sale notification for this one
        event on that last case rather than guess - see the sentinel's own
        comment for why that is the safe direction.
        """
        try:
            return await self.bot.db_pool.fetchrow(
                "SELECT deleted FROM premium_entitlements WHERE entitlement_id = $1",
                entitlement_id,
            )
        except Exception:
            log.exception(
                "premium: failed to read entitlement %s's prior state; sale "
                "notification skipped for this event (self-heals via the "
                "next reconciliation pass if it was genuinely new)",
                entitlement_id,
            )
            return _UNKNOWN_PRIOR_STATE

    async def _resolve_owner_ids(self):
        """Every id this bot currently resolves as an owner - mirrors
        ``discord.ext.commands.Bot.is_owner`` exactly (``discord/ext/
        commands/bot.py``): the bot owner alone, or every admin/developer
        Team member if the application is Team-owned, with no ``user`` to
        check against since this is resolving the SET, not answering "is
        this one person an owner". Caches onto ``bot.owner_id``/
        ``bot.owner_ids`` exactly like ``is_owner`` itself does (the same
        attributes, the same shape), so this - or any later
        ``?premiumadmin`` command's own owner check - never re-fetches
        ``application_info`` once either is set.
        """
        owner_id = getattr(self.bot, "owner_id", None)
        if owner_id:
            return {owner_id}
        owner_ids = getattr(self.bot, "owner_ids", None)
        if owner_ids:
            return set(owner_ids)
        application_info = getattr(self.bot, "application_info", None)
        if application_info is None:
            return set()
        app = await application_info()
        if app.team:
            ids = {
                member.id
                for member in app.team.members
                if member.role
                in (discord.TeamMemberRole.admin, discord.TeamMemberRole.developer)
            }
            self.bot.owner_ids = ids
            return ids
        self.bot.owner_id = app.owner.id
        return {app.owner.id}

    async def _send_one_owner_dm(self, owner_id, text, *, kind):
        try:
            user = self.bot.get_user(owner_id)
            if user is None:
                user = await self.bot.fetch_user(owner_id)
            await user.send(text)
        except discord.Forbidden:
            log.warning(
                "PREMIUM-OWNER-DM-FAILED reason=forbidden owner=%s", owner_id
            )
            return
        except discord.HTTPException as error:
            log.warning(
                "PREMIUM-OWNER-DM-FAILED reason=http-%s owner=%s",
                error.status,
                owner_id,
            )
            return
        except Exception:
            log.exception(
                "PREMIUM-OWNER-DM-FAILED reason=unexpected owner=%s", owner_id
            )
            return
        log.info("PREMIUM-OWNER-DM kind=%s", kind)

    async def _deliver_owner_dm(self, text, *, kind):
        """The background task body for an already-built DM (the
        reconciliation storm summary below) - resolve the owner(s), then
        send to each, one failure never stopping delivery to the others."""
        try:
            owner_ids = await self._resolve_owner_ids()
        except Exception:
            log.exception("PREMIUM-OWNER-DM-FAILED reason=owner-resolve-error")
            return
        if not owner_ids:
            log.warning("PREMIUM-OWNER-DM-FAILED reason=no-owner-resolved")
            return
        for owner_id in owner_ids:
            await self._send_one_owner_dm(owner_id, text, kind=kind)

    async def _deliver_sale_dm(self, kind, row, *, found_by_reconciliation=False):
        """The background task body :meth:`_dispatch_sale_dm` schedules.

        Builds the DM text INSIDE the task (never synchronously in the
        caller) and catches a build failure the exact same way a send
        failure is caught - a malformed row, or a bot stand-in missing an
        attribute :func:`_build_sale_dm` reads (``get_guild`` and similar),
        must never propagate out of the gateway listener or the
        reconciliation loop that found this transition any more than a
        closed-DMs ``Forbidden`` would.
        """
        try:
            text = _build_sale_dm(
                self.bot, kind, row, found_by_reconciliation=found_by_reconciliation
            )
        except Exception:
            log.exception("PREMIUM-OWNER-DM-FAILED reason=build-error")
            return
        await self._deliver_owner_dm(text, kind=kind)

    def _dispatch_sale_dm(self, kind, row, *, found_by_reconciliation=False):
        """Send one sale-relevant DM in a background task - fire-and-forget
        from the caller's point of view (the gateway listener below, or the
        reconciliation loop), so a slow or failing DM never delays or
        breaks the write path that found this transition.
        ``self._pending_owner_dms`` holds a strong reference while the task
        is in flight (never garbage-collected mid-send) and discards it the
        moment the task finishes, success or not.
        """
        task = asyncio.ensure_future(
            self._deliver_sale_dm(
                kind, row, found_by_reconciliation=found_by_reconciliation
            )
        )
        self._pending_owner_dms.add(task)
        task.add_done_callback(self._pending_owner_dms.discard)

    def _notify_reconcile_transitions(self, new_rows, ended_rows):
        """One DM per transition :func:`tools.premium.reconcile`'s own pass
        found - unless there are more than :data:`RECONCILE_DM_STORM_THRESHOLD`
        of them, in which case this sends ONE summary DM instead (a long
        outage, or the very first pass ever run against an existing
        customer base, must not flood the owner with one DM per row)."""
        total = len(new_rows) + len(ended_rows)
        if total == 0:
            return
        if total > RECONCILE_DM_STORM_THRESHOLD:
            text = (
                f"Premium reconciliation found {total} changes: "
                f"{len(new_rows)} new sale(s), {len(ended_rows)} ended. "
                "See the PREMIUM-RECONCILE log line for detail."
            )
            task = asyncio.ensure_future(
                self._deliver_owner_dm(text, kind="reconcile-summary")
            )
            self._pending_owner_dms.add(task)
            task.add_done_callback(self._pending_owner_dms.discard)
            return
        for row in new_rows:
            self._dispatch_sale_dm("new", row, found_by_reconciliation=True)
        for row in ended_rows:
            self._dispatch_sale_dm("ended", row)

    # -- ENTITLEMENT_* gateway handlers (M3b) ---------------------------
    #
    # Real-time path for a purchase/renewal/cancellation/refund. The
    # periodic reconciliation loop below is the safety net for whatever one
    # of these misses (a gateway reconnect window, a dropped delivery) - the
    # two are independent and either alone keeps the projection eventually
    # correct.

    def _foreign_application(self, entitlement):
        """True if ``entitlement`` names a DIFFERENT application than ours,
        or if either id is unknown (fail-safe: never process what we cannot
        positively attribute to our own application).

        Defence in depth, not the primary guard: Discord's own gateway only
        ever dispatches entitlement events for OUR application in the first
        place, so this should never actually fire in production - but a row
        that somehow named someone else's application must never be written
        into our own projection, and must never make a later reconciliation
        pass mark one of OUR rows deleted on its account (see
        tools.premium.reconcile's own "foreign application" paragraph).
        """
        application_id = getattr(self.bot, "application_id", None)
        entitlement_application_id = getattr(entitlement, "application_id", None)
        if application_id is None or entitlement_application_id is None:
            return True
        return int(entitlement_application_id) != int(application_id)

    async def _handle_entitlement_event(self, entitlement, *, force_deleted, kind):
        """Shared body of all three ``on_entitlement_*`` listeners below.

        Writes first (:func:`tools.premium.upsert_entitlement_event`), the
        matching ONE-scope cache refresh only after that write actually
        succeeds - never the other way, and never skipped on failure by
        accident: a DB error here is caught, logged, and returns, leaving
        ``bot.premium`` exactly as it was (stale in the direction the next
        gateway event or the next reconciliation pass corrects, never wrong
        in the dangerous direction of granting something that was never
        written down). A cache-refresh failure AFTER a successful write is
        caught and logged separately - the row is durable either way, and
        the next reconciliation pass (at most RECONCILE_INTERVAL_HOURS away)
        reloads the whole cache regardless, so this self-heals without
        needing its own retry here.

        ``kind`` is ``"create"``/``"update"``/``"delete"`` - purely for the
        PREMIUM-EVENT audit line below (M3c); it changes no behaviour, which
        is still driven entirely by ``force_deleted`` and the payload itself.
        """
        entitlement_id = getattr(entitlement, "id", "?")
        if self._foreign_application(entitlement):
            log.debug(
                "premium: ignoring entitlement %s for a different/unknown "
                "application",
                entitlement_id,
            )
            return
        # Read BEFORE the write below - see tools.premium.classify_entitlement_
        # transition's own docstring for why a before/after snapshot (not the
        # event's own create/update/delete kind) is what decides "new sale"/
        # "ended" for the owner DM (M5): a replayed/duplicated event reads the
        # exact same before/after pair and converges to the same verdict
        # (None, every time after the first). A read failure here never
        # blocks the write - it only means this one event skips the sale
        # notification (see _read_prior_deleted_flag's own docstring).
        try:
            entitlement_id_int = int(entitlement_id)
        except (TypeError, ValueError):
            entitlement_id_int = None
        previous = (
            await self._read_prior_deleted_flag(entitlement_id_int)
            if entitlement_id_int is not None
            else _UNKNOWN_PRIOR_STATE
        )
        try:
            row = await premium.upsert_entitlement_event(
                self.bot.db_pool, entitlement, force_deleted=force_deleted
            )
        except Exception:
            log.exception(
                "premium: failed to persist entitlement %s; cache left "
                "untouched (will self-heal on the next event or "
                "reconciliation pass)",
                entitlement_id,
            )
            return
        # The durable write above is the actual state change; log it as such
        # regardless of whether the cache refresh below (a purely in-memory,
        # self-healing step) succeeds.
        log.info(
            "PREMIUM-EVENT %s entitlement=%s sku=%s scope=%s scope_id=%s",
            kind,
            row["entitlement_id"],
            row["sku_id"],
            row["scope_type"],
            row["guild_id"] if row["scope_type"] == "guild" else row["user_id"],
        )
        if previous is not _UNKNOWN_PRIOR_STATE:
            transition = premium.classify_entitlement_transition(
                is_new=previous is None,
                prev_deleted=bool(previous["deleted"]) if previous is not None else False,
                now_deleted=row["deleted"],
            )
            if transition is not None:
                self._dispatch_sale_dm(transition, row)
        try:
            if row["scope_type"] == "guild":
                await self.bot.premium.refresh_entitlement_scope(
                    self.bot.db_pool, "guild", guild_id=row["guild_id"]
                )
            else:
                await self.bot.premium.refresh_entitlement_scope(
                    self.bot.db_pool, "user", user_id=row["user_id"]
                )
        except Exception:
            log.exception(
                "premium: entitlement %s stored but its cache refresh "
                "failed; will self-heal on the next reconciliation pass",
                row["entitlement_id"],
            )

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement):
        await self._handle_entitlement_event(
            entitlement, force_deleted=False, kind="create"
        )

    @commands.Cog.listener()
    async def on_entitlement_update(self, entitlement):
        # A refund or an ended, non-renewing subscription arrives THROUGH
        # THIS event, not a dedicated one - Discord reports both as an
        # entitlement update (often with ``deleted: true``) per the plan's
        # own "a refund follows the expiry path" rule. No extra branch is
        # needed here: upsert_entitlement_event already OR-preserves
        # whatever ``deleted`` value the payload carries forever once it is
        # True, so trusting this event's own field is exactly correct.
        await self._handle_entitlement_event(
            entitlement, force_deleted=False, kind="update"
        )

    @commands.Cog.listener()
    async def on_entitlement_delete(self, entitlement):
        # force_deleted=True: the payload's own `deleted` field is not
        # trusted here - receiving THIS event at all is the signal, and this
        # also makes the write INSERT an already-deleted row if the delete
        # happens to arrive before its own create (out-of-order gateway
        # delivery) rather than finding nothing to update and losing the
        # delete - see upsert_entitlement_event's own docstring for why this
        # is deliberately NOT just tools.premium.mark_deleted.
        await self._handle_entitlement_event(
            entitlement, force_deleted=True, kind="delete"
        )

    # -- periodic reconciliation (M3b) ----------------------------------

    def _configured_skus(self):
        """``discord.Object`` wrappers for whichever of the two catalog SKUs
        is configured, for ``bot.entitlements(skus=...)`` below. Narrowing
        to our own catalog (rather than leaving ``skus`` unset, which would
        list EVERY entitlement for the application) keeps each reconciliation
        pass's REST traffic proportional to what we actually sell, not to
        whatever else might exist on the application - see the lot's report
        for the full scale story. Returns ``None`` (meaning "no filter",
        discord.py's own default) only in the dev/test posture where NEITHER
        SKU is configured yet, since an empty list and ``None`` are not the
        same thing to that endpoint.
        """
        sku_ids = [
            sku_id
            for sku_id in (premium.YASUHO_PLUS_SKU, premium.COMFORT_PACK_SKU)
            if sku_id is not None
        ]
        if not sku_ids:
            return None
        return [discord.Object(id=sku_id) for sku_id in sku_ids]

    async def _reconcile_once(self):
        """One reconciliation pass: list, diff, (maybe) reload the cache.

        Exposed as its own method, separate from the ``tasks.loop`` wrapper
        below, so a test can drive exactly one pass with a fake bot and no
        scheduler - the same split cogs/anilist/airing.py's
        ``_poll_airing``/``_tick`` already uses.
        """
        application_id = getattr(self.bot, "application_id", None)
        if application_id is None:
            log.warning(
                "premium: reconciliation skipped, application_id not set yet"
            )
            return
        configured_skus = self._configured_skus()
        entitlements = self.bot.entitlements(
            limit=None,
            skus=configured_skus,
            exclude_ended=False,
            exclude_deleted=True,
        )
        # sku_ids MUST be the same filter just handed to bot.entitlements()
        # above - tools.premium.reconcile's own docstring ("sku_ids") spells
        # out why a mismatch here would wrongly mark deleted every row for a
        # sku this pass never actually listed (e.g. right after an owner
        # changes [Premium] yasuho_plus_sku/comfort_pack_sku in bot.ini: the
        # OLD sku's rows must be left alone, not read as "gone").
        sku_ids = (
            None
            if configured_skus is None
            else [sku.id for sku in configured_skus]
        )
        result = await premium.reconcile(
            self.bot.db_pool,
            entitlements,
            application_id=application_id,
            sku_ids=sku_ids,
        )
        if result is None:
            # The fail-safe path already logged inside tools.premium.reconcile.
            # NOTHING was marked deleted, and premium_entitlements was left
            # exactly as it stood - so the cache reload below MUST NOT run:
            # it would just re-read the same (unharmed) rows, but running it
            # on principle here would blur the line between "a pass that
            # changed nothing because there was nothing to change" and "a
            # pass that was aborted", which is exactly the distinction this
            # fail-safe exists to preserve.
            return
        try:
            await self.bot.premium.load(self.bot.db_pool)
        except Exception:
            log.exception(
                "premium: reconciliation completed but the cache reload "
                "failed; the database is correct, bot.premium will catch up "
                "on the next pass or event"
            )
            return
        log.info(
            "PREMIUM-RECONCILE seen=%s upserted=%s missing=%s",
            result["seen"],
            result["upserted"],
            result["missing"],
        )
        # M5: .get(..., []) rather than result[...] - a test double standing
        # in for tools.premium.reconcile (several already exist above this
        # cog's own test suite) may return the bare three-key dict reconcile
        # had before this lot; treated as "nothing to announce", never a
        # KeyError.
        self._notify_reconcile_transitions(
            result.get("new_rows", []), result.get("ended_rows", [])
        )

    @tasks.loop(hours=RECONCILE_INTERVAL_HOURS)
    async def reconcile_entitlements(self):
        # Fully wrapped: an unexpected error must never kill the loop - same
        # posture as cogs/anilist/airing.py's _poll_airing.
        try:
            await self._reconcile_once()
        except Exception:
            log.exception("premium: reconciliation tick failed")

    @reconcile_entitlements.before_loop
    async def _before_reconcile(self):
        # The FIRST tick (only) waits here; every later tick already runs
        # against a ready bot by construction (the loop only reaches its
        # next iteration after this coroutine - and then the previous
        # tick's body - have returned). This is also why reconciliation runs
        # "once after ready, then periodically" without a separate one-shot
        # call: the loop's own first iteration IS that first post-ready run.
        await self.bot.wait_until_ready()

    @reconcile_entitlements.error
    async def _reconcile_error(self, error):
        log.exception(
            "premium: reconciliation loop crashed; restarting", exc_info=error
        )
        self.reconcile_entitlements.restart()


async def setup(bot):
    await bot.add_cog(Premium(bot))
