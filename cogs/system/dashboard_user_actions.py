"""Dashboard -> bot action queue: the executors whose scope is a USER.

Companion module to ``cogs/system/dashboard_actions.py`` (which owns the queue:
the dedicated LISTEN connection, the atomic claim, the boot reconciliation and
the result write-back) and sibling of ``dashboard_music_actions.py``. Same
handler contract - ``async handler(bot, scope_id, payload) -> result dict`` - and
the registry stays SINGLE-SOURCED: ``dashboard_actions`` merges :data:`EXECUTORS`
into its own ``_EXECUTORS`` at import time, so there is exactly one kind table.
The dependency only ever points this way (this module never imports
``dashboard_actions``), so no cycle is possible.

What makes this module different from every other executor group: the second
argument is a USER id, not a guild id. Those rows carry ``user_id`` instead of
``guild_id`` in ``dashboard_actions`` (the DB CHECK makes "exactly one scope" a
schema fact) and ``dashboard_actions._USER_KINDS`` - which MUST list every kind
below, guarded by a test - is what tells the queue to hand the executor the user.
The dashboard writes such a row under "this is your own session", not under its
``requireManageGuild`` gate, and the same rule applies to the status polling it
does afterwards: an action row must only ever be read back by the user it names.

Design:

* NEVER reimplement anything that touches personal data. ``mydata_export`` drives
  the exact functions ``?mydata export`` drives -
  ``privacy.collect_user_export`` -> ``privacy.build_export_archives`` (through
  the shared image-job ceiling) -> DM - so there is ONE definition of what a
  personal-data archive contains, and a table added to the export is added for
  both callers at once.
* The rate limit is a DB clock, not a process-local bucket:
  ``privacy.claim_export_slot`` is THE gate for both callers (see its docstring),
  so the dashboard cannot be used to dodge the Discord cooldown or the reverse.
  It is claimed BEFORE anything is collected or packed: the archive is the most
  expensive job the bot has, and building one only to discard it would make the
  limiter a paper one.
* Results are machine-facing: short, stable, NEVER localised identifiers, and
  never a stack or a secret. Failures report under ``reason`` (the shape the
  dashboard contracted for), with ``retryAfter`` alongside ``cooldown``.
"""

from __future__ import annotations

import logging

import discord

from tools import i18n, privacy, rendering
from tools.i18n import _

log = logging.getLogger(__name__)


async def _resolve_user(bot, user_id):
    """Return the ``discord.User`` for ``user_id``, or ``None``. Never raises.

    Two-step by cost: the in-memory ``bot.get_user`` is free; ``bot.fetch_user``
    is a REST call, reached ONLY on a cache miss. That cost split is what lets the
    caller keep the documented resolve-then-cooldown order on a cache hit (resolve
    is free there) while consulting the cooldown before the REST on a miss - see
    :func:`_exec_mydata_export` for why the miss path reorders.
    """
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except Exception:
        log.warning("dashboard_user_actions: could not resolve user %s", user_id)
        return None


async def _export_note(bot, user_id):
    """The one line of context that goes out with the first archive part.

    A DM triggered from the dashboard arrives with no message at all otherwise -
    an unexplained file drop from a bot, which is exactly what a user is taught
    not to open. The Discord command already says what it sent, so this is the
    same courtesy on the other path.

    Localised in the RECIPIENT's locale: there is no invocation to inherit one
    from here (the queue runs in the background), so it is resolved from their
    own preference. Never fatal - a failed lookup falls back to the default
    catalog rather than costing somebody the export they are owed.
    """
    locale = i18n.DEFAULT_LOCALE
    try:
        locale = await i18n.resolve_locale(bot, user_id=user_id)
    except Exception:
        log.warning(
            "dashboard_user_actions: locale lookup failed for %s", user_id
        )
    with i18n.locale(locale):
        return _(
            "Here is the personal-data export you requested from the dashboard."
        )


async def _exec_mydata_export(bot, user_id, payload):
    """DM a user their personal-data archive. Payload: ``{}``.

    ``user_id`` is authoritative (it comes from the claimed row); there is
    nothing else to validate, which is exactly why the payload is empty - the
    scope IS the request, so no crafted field can point the export at somebody
    else's data.

    Order is load-bearing:

    1. resolve the user, but the resolve/cooldown order flips by COST. On an
       in-memory cache hit resolving is free, so the documented order stands:
       resolve, then claim (never burn the honest user's hourly slot on a doomed
       attempt - see :func:`_resolve_user`). On a cache MISS resolving would cost
       a REST fetch, so the cooldown is claimed FIRST and a rate-limited abusive
       request is refused before that REST call ever happens;
    2. claim the shared cooldown slot - refused means ``cooldown`` with the exact
       ``retryAfter`` in seconds, and NOTHING has been read or built at that
       point;
    3. only then collect and pack, through the SAME two functions the Discord
       command uses, with the packing handed to the shared image-job executor so
       a big avatar history cannot block the event loop;
    4. DM every part, the first one carrying a line of context (see
       :func:`_export_note`).

    ``dm_closed`` is reported for ``discord.Forbidden`` alone - the one failure
    the user can fix themselves (closed DMs / not sharing a server with the bot);
    anything else is ``failed``, with the detail logged server-side and never put
    in ``result``. A part-way failure still reports the failure: some parts may
    have landed, and the dashboard telling the user "delivered" when part 2 of 3
    never arrived would be a lie.

    The slot stays consumed on every one of those failures - see
    ``privacy.claim_export_slot`` for why releasing it would be the abusable
    direction.
    """
    # Whether to resolve BEFORE or AFTER the cooldown depends on whether
    # resolving is FREE. The documented order is resolve-then-cooldown so a
    # doomed attempt never burns the honest user's one export per hour (see
    # _resolve_user). That reasoning holds only while resolving costs nothing: an
    # in-memory cache HIT (bot.get_user) is free, so on a hit we keep the
    # documented order EXACTLY - resolve, then claim.
    #
    # A cache MISS is the abuse signal. Resolving then needs a REST bot.fetch_user
    # (see _resolve_user), and a throwaway account that shares no guild with the
    # bot misses the cache on every queued request. So on a miss we consult the
    # cooldown FIRST: a doomed/abusive request that is already rate-limited pays
    # the cheap DB check instead of a REST round-trip. The tradeoff is that a
    # granted-then-unresolvable attempt on the miss path consumes the slot - but
    # that path is exactly the uncached, abuse-shaped one, and the honest
    # cache-hit path (where get_user cannot "resolve to nobody") is byte-for-byte
    # unchanged.
    user = bot.get_user(user_id)
    resolve_via_rest = user is None

    granted, retry_after = await privacy.claim_export_slot(bot.db_pool, user_id)
    if not granted:
        return {"ok": False, "reason": "cooldown", "retryAfter": retry_after}

    if resolve_via_rest:
        user = await _resolve_user(bot, user_id)
        if user is None:
            return {"ok": False, "reason": "failed"}

    try:
        data, avatar_rows = await privacy.collect_user_export(bot.db_pool, user_id)
        archives = await rendering.run_image_job(
            bot,
            privacy.build_export_archives,
            data,
            avatar_rows,
            # timeout=None for the same reason the prefix/slash export gives
            # (cogs/community/usersettings.py's mydata_export): the hourly slot
            # is already claimed and is not released on failure, so waiting for
            # a busy pool is strictly better than burning the user's hour.
            timeout=None,
        )
    except Exception:
        log.exception(
            "dashboard_user_actions: failed to build the export for %s", user_id
        )
        return {"ok": False, "reason": "failed"}

    note = await _export_note(bot, user_id)
    try:
        for index, (filename, archive) in enumerate(archives):
            # The note rides the FIRST part only: repeating it on every part of a
            # multi-part archive would be noise, and sending it as its own
            # message would be one more thing that can fail on its own.
            await user.send(
                content=note if index == 0 else None,
                file=discord.File(archive, filename=filename),
            )
    except discord.Forbidden:
        return {"ok": False, "reason": "dm_closed"}
    except Exception:
        log.exception(
            "dashboard_user_actions: failed to deliver the export for %s", user_id
        )
        return {"ok": False, "reason": "failed"}

    return {"ok": True, "delivered": "dm"}


# Merged into ``dashboard_actions._EXECUTORS`` at import time, so the queue keeps
# ONE kind table. EVERY kind here is USER-scoped, so it must also appear in
# ``dashboard_actions._USER_KINDS`` - a test asserts the two agree, because a
# kind listed here but not there would be handed a guild id (in practice NULL on
# a user row) and refused.
EXECUTORS = {
    "mydata_export": _exec_mydata_export,
}
