"""The limit-reached message: the one extra line (and, on slash, a purchase
button) a refusal reply is allowed to add when a higher tier would actually
help.

M5 of the monetisation plan (.claude/plans/monetisation/4-plan-retenu.md),
"Sollicitation": ``/premium`` toujours accessible; a une limite, message
EPHEMERE, une fois tous les 7 jours par personne et par type de limite (reset
seulement si la personne ouvre /premium); admin -> bouton d'achat; membre ->
"un administrateur peut gerer ca avec /premium". Jamais de MP ni de pub hors
contexte.

WHAT THIS MODULE IS NOT. It never decides WHETHER a limit was reached - every
caller already refused the action on its own terms (the archival/cap checks
tools/premium.py and tools/premium_archive.py already carry) before it ever
reaches here. This module only decides whether ONE more line is worth adding
to a refusal that is happening anyway, and it is the ONLY place that decides
that, so the 7-day rule and the admin/member wording are each written once.

THE 7-DAY CLAIM, AS ONE ATOMIC UPSERT. There is no separate "check" then
"write": :func:`_try_claim` is a single ``INSERT ... ON CONFLICT`` guarded by
a ``WHERE`` clause comparing against the most recent of the specific key's own
row and the ``'*'`` sentinel (see schema.sql's ``premium_upsells`` for why the
sentinel implements "opening /premium resets every key" without needing one
row per key up front). This is deliberately a plain indexed round trip, not an
in-memory LRU: showing an upsell only happens on a refusal, and a refusal is
by definition rare (the guild/user is already failing a cap check it usually
passes) - an LRU would need no eviction logic worth the complexity for
something this infrequent, and a DB-backed claim is naturally correct across
a restart (no "forgot it already nagged this week" after a redeploy) with no
separate cache-invalidation path to keep in sync with ``/premium`` opening.

FAIL CLOSED ON NAGGING. If the claim write raises (pool unavailable, a
transient error), :func:`_try_claim` logs and returns ``False`` - the caller
shows nothing. The failure mode of "we could not confirm we have not already
shown this" must never become "show it anyway": showing nothing costs a sale
opportunity once; showing it every single refusal because the throttle itself
is broken is the nagging the plan explicitly forbids.

CALLERS NEVER BUILD TEXT THEMSELVES. :func:`for_guild_refusal` and
:func:`for_user_refusal` are the only two entry points, and both return
``None`` (nothing to show) or an :class:`Upsell` (``.line`` - the plain text
sentence; ``.button`` - a ``discord.ui.Button`` or ``None``). A caller that
already decided the refusal itself is unconditional (DMs, background tasks,
an already-top-tier guild/user) simply never calls in - there is no "call it
and let it no-op" path for those, which is also why the call-site census test
(tests/tools/test_premium_upsell_sites.py) can assert the import is confined
to actual refusal sites: a background poller importing this module at all
would be a defect worth failing a test over, not something this module can
silently guard against from the inside.

WHY A BUTTON NEEDS A CONFIGURED SKU. ``discord.ui.Button(style=
discord.ButtonStyle.premium, sku_id=...)`` is Discord's own premium purchase
button (confirmed against the installed discord.py 2.7.1: ``ButtonStyle.premium``
exists and ``Button.__init__`` takes ``sku_id``) - it renders Discord's native
purchase sheet for that SKU and nothing else can substitute for it. Today's
sales are CLOSED (the plan's SKUs do not exist yet), so
:data:`tools.premium.YASUHO_PLUS_SKU`/``COMFORT_PACK_SKU`` read ``None`` and
every button here is simply omitted - the text line still shows, pointing at
``/premium`` the same way the plan's member-facing text always has.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

import discord

from tools import premium
from tools.i18n import _

log = logging.getLogger(__name__)

# The plan's own number: "une fois tous les 7 jours par personne et par type
# de limite".
UPSELL_COOLDOWN = datetime.timedelta(days=7)

# A virtual limit_key, never a real one (every real key below is a plain
# identifier like "guild_playlists" with no asterisk) - see schema.sql's
# premium_upsells comment for what it means and tests/tools/test_premium_upsell.py
# for the "opening /premium resets every key" regression test built on it.
ALL_KEYS_SENTINEL = "*"


@dataclasses.dataclass(frozen=True)
class Upsell:
    """What a refusal site is allowed to add: one line, and maybe a button."""

    line: str
    button: discord.ui.Button | None = None

    def view(self):
        """A one-item ``discord.ui.View`` for the button, or ``None``.

        A caller passes this straight to ``view=`` on its send/response call
        when it is not None - never constructed when ``button`` is None, so a
        send that has no button to show never gets an empty view either.
        """
        if self.button is None:
            return None
        view = discord.ui.View(timeout=None)
        view.add_item(self.button)
        return view


_CLAIM_SQL = """
WITH latest AS (
    SELECT COALESCE(MAX(shown_at), '-infinity'::timestamptz) AS last_shown
    FROM premium_upsells
    WHERE user_id = $1 AND limit_key IN ($2, $4)
)
INSERT INTO premium_upsells (user_id, limit_key, shown_at)
SELECT $1, $2, now()
FROM latest
WHERE now() - latest.last_shown >= $3
ON CONFLICT (user_id, limit_key) DO UPDATE SET shown_at = now()
RETURNING shown_at
"""


async def _try_claim(pool, user_id, limit_key, *, cooldown=UPSELL_COOLDOWN):
    """Atomically claim the right to show ``limit_key`` to ``user_id`` now.

    Returns ``True`` exactly once per 7-day window per (user_id, limit_key) -
    OR once per 7 days since that person last opened ``/premium``, whichever
    is more recent (the ``WHERE ... IN ($2, $4)`` CTE above reads both rows
    and keeps the newer ``shown_at``). ``False`` on a DB error: see the module
    docstring's "FAIL CLOSED ON NAGGING" paragraph - this is the one place
    that rule is enforced.
    """
    try:
        row = await pool.fetchrow(
            _CLAIM_SQL,
            int(user_id),
            limit_key,
            cooldown,
            ALL_KEYS_SENTINEL,
        )
    except Exception:
        log.exception(
            "premium_upsell: claim failed for key=%s; showing nothing", limit_key
        )
        return False
    return row is not None


async def mark_premium_opened(pool, user_id):
    """Record that ``user_id`` just opened ``/premium`` - resets every key.

    Called from cogs/system/premium_panel.py's ``/premium`` handler, every
    time, before it renders anything (so a render failure never leaves the
    person "not yet reset"). A write failure is logged and swallowed: at
    worst, the next refusal's claim still succeeds on its own key's schedule,
    which is no worse than /premium having never been opened at all - it is
    never grounds to show MORE than the plan allows, so there is nothing to
    fail closed against here.
    """
    try:
        await pool.execute(
            "INSERT INTO premium_upsells (user_id, limit_key, shown_at) "
            "VALUES ($1, $2, now()) "
            "ON CONFLICT (user_id, limit_key) DO UPDATE SET shown_at = now()",
            int(user_id),
            ALL_KEYS_SENTINEL,
        )
    except Exception:
        log.exception("premium_upsell: failed to record a /premium open")


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
# "raise" is the ordinary numeric-cap wording ("Yasuho+ raises this limit to
# {benefit}"); "unlock" is for a binary capability with no number to show
# (music_247 today - Yasuho+ does not raise a 24/7 ceiling, it turns the
# feature on at all).

_ADMIN_RAISE = _("Yasuho+ raises this limit to {benefit}. See /premium.")
_ADMIN_UNLOCK = _("Yasuho+ unlocks this. See /premium.")
_MEMBER_RAISE = _("A server admin can raise this limit with /premium.")
_MEMBER_UNLOCK = _("A server admin can unlock this with /premium.")
_USER_RAISE = _("Pack Confort raises this limit to {benefit}. See /premium.")


def _guild_line(*, is_admin, kind, benefit):
    if is_admin:
        if kind == "unlock":
            return _ADMIN_UNLOCK
        return _ADMIN_RAISE.format(benefit=benefit)
    if kind == "unlock":
        return _MEMBER_UNLOCK
    return _MEMBER_RAISE


def _user_line(*, benefit):
    return _USER_RAISE.format(benefit=benefit)


def _button(sku_id):
    if sku_id is None:
        return None
    return discord.ui.Button(style=discord.ButtonStyle.premium, sku_id=sku_id)


def invoker_is_admin(member):
    """Whether ``member`` has Manage Server - never raises.

    ``discord.Member.guild_permissions`` itself reads ``member.guild``
    (ownership/administrator/timeout all factor in), so a lightweight stand-in
    with no ``.guild`` - a real shape in this tree's own test doubles, and
    not impossible in production either (a cached member whose guild
    reference went stale) - raises ``AttributeError`` rather than returning
    ``False``. A permission check feeding a sales nudge must never crash a
    refusal reply over that: this reads as "not an admin" (the safer, more
    conservative wording: never a button, never the "raises this limit"
    line) rather than letting the exception escape.
    """
    try:
        return bool(member.guild_permissions.manage_guild)
    except Exception:
        return False


def is_slash_context(ctx_or_interaction):
    """Whether this call is happening on an interaction (slash, or any
    component/modal callback) rather than a plain prefix message - never
    raises. A hybrid ``commands.Context`` carries ``.interaction`` (``None``
    on a prefix invocation); a bare ``discord.Interaction`` has no such
    attribute at all and IS one, which is what the ``isinstance`` branch
    below is for.
    """
    if isinstance(ctx_or_interaction, discord.Interaction):
        return True
    return getattr(ctx_or_interaction, "interaction", None) is not None


def is_guild_already_top_tier(bot, guild_id):
    """Whether ``guild_id`` already has Yasuho+ - the ``already_top_tier``
    a GUILD-scoped call site passes to :func:`for_guild_refusal` when its own
    refusal did not already know the answer (an archived-item refusal, say,
    which only knows "over the cap" and not "which cap").

    Same defensive guard as :func:`tools.premium.resolve_guild_limits`: a
    missing ``bot.premium``, a ``guild_id`` that cannot be read, or the
    resolver itself raising, all read as "not premium" rather than raising or
    - worse - suppressing an upsell that should have shown.
    """
    resolver = getattr(bot, "premium", None)
    if resolver is None or guild_id is None:
        return False
    try:
        return bool(resolver.is_guild_premium(guild_id))
    except Exception:
        log.exception(
            "premium_upsell: failed to resolve guild premium status; "
            "treating as not premium"
        )
        return False


def is_user_already_top_tier(bot, user_id):
    """The Pack Confort twin of :func:`is_guild_already_top_tier`."""
    resolver = getattr(bot, "premium", None)
    if resolver is None or user_id is None:
        return False
    try:
        return bool(resolver.has_comfort_pack(user_id))
    except Exception:
        log.exception(
            "premium_upsell: failed to resolve user premium status; "
            "treating as not premium"
        )
        return False


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def for_guild_refusal(
    bot,
    *,
    limit_key,
    guild_id,
    person_id,
    is_admin,
    already_top_tier,
    benefit=None,
    kind="raise",
    allow_button=True,
):
    """The upsell (or ``None``) for a GUILD-scoped limit refusal.

    Takes ``bot`` (not a bare pool) and resolves ``bot.db_pool`` itself via
    ``getattr`` - the same defensive shape ``tools.premium.resolve_guild_limits``
    already uses for ``bot.premium`` - so a caller whose ``bot`` is a partial
    test double, or genuinely has no pool yet (a cog running before core.py's
    ``setup_hook``), degrades to "show nothing" rather than an
    ``AttributeError`` escaping from a refusal reply's hot path.

    ``already_top_tier`` is the caller's own verdict (``bot.premium.
    is_guild_premium(guild_id)``) that this guild is ALREADY on Yasuho+ and is
    hitting the Yasuho+ ceiling itself - the plan is explicit that nothing
    helps there, so this returns ``None`` before ever touching the database
    (no 7-day slot is spent on a case that can never show anything useful).
    ``is_admin`` is the invoker's own Manage Server permission in this guild -
    never guessed, always passed in by the caller, which already has it from
    whichever ``ctx``/``interaction`` it is handling (see
    :func:`invoker_is_admin` for computing it without risking a crash).

    ``kind="unlock"`` switches to the no-number wording for a binary
    capability (music_247); ``benefit`` is then ignored. ``allow_button=False``
    forces a text-only result even with a configured SKU - the prefix-command
    path (no component support) passes this.
    """
    if already_top_tier or guild_id is None or person_id is None:
        return None
    pool = getattr(bot, "db_pool", None)
    if pool is None or not await _try_claim(pool, person_id, limit_key):
        return None
    line = _guild_line(is_admin=is_admin, kind=kind, benefit=benefit)
    button = _button(premium.YASUHO_PLUS_SKU) if (is_admin and allow_button) else None
    log.info(
        "PREMIUM-UPSELL key=%s scope=guild scope_id=%s shown=1", limit_key, guild_id
    )
    return Upsell(line=line, button=button)


async def for_user_refusal(
    bot,
    *,
    limit_key,
    person_id,
    already_top_tier,
    benefit,
    allow_button=True,
):
    """The upsell (or ``None``) for a USER-scoped limit refusal (favourites,
    reminders). No admin/member split - it is the person's own purchase.

    Takes ``bot``, not a bare pool - see :func:`for_guild_refusal`'s own
    docstring for why.
    """
    if already_top_tier or person_id is None:
        return None
    pool = getattr(bot, "db_pool", None)
    if pool is None or not await _try_claim(pool, person_id, limit_key):
        return None
    line = _user_line(benefit=benefit)
    button = _button(premium.COMFORT_PACK_SKU) if allow_button else None
    log.info(
        "PREMIUM-UPSELL key=%s scope=user scope_id=%s shown=1", limit_key, person_id
    )
    return Upsell(line=line, button=button)
