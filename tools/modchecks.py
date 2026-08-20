"""Shared moderation authorization checks (role hierarchy / self / owner).

discord.py's has_permissions only proves the INVOKER holds a permission, not
that the target is actually actionable. This stops a moderator from punishing
themselves, the guild owner, or anyone whose top role is equal to or above their
own (unless they own the guild), and flags when the bot itself is not high enough
to act. Returning a user-facing reason (or None) keeps the call sites a two-liner.

MEMBER RESOLUTION IS PART OF THE CHECK. Yasuho runs with
``chunk_guilds_at_startup=False`` (core.py), so ``guild.get_member`` is a SPARSE
cache: a miss means "unknown", NOT "not in this server". Deciding on a miss is
therefore a fail-OPEN bug - the guard would wave through exactly the staff member
nobody has spoken to recently. Anything holding a bare ``User``/``Object`` (a
``discord.User`` annotation, a massban id) must resolve the target first with
:func:`hierarchy_error_resolved` (one target) or :func:`resolve_guild_members`
(a lot), and an unresolvable target is REFUSED.
"""

from __future__ import annotations

import logging

import discord

from tools.i18n import _

log = logging.getLogger(__name__)

# query_members is a gateway op (REQUEST_GUILD_MEMBERS) capped at 100 ids per
# request by Discord; discord.py clamps ``limit`` to 100 as well.
MEMBER_QUERY_CHUNK = 100


def _unverifiable_error():
    """The fail-closed reason used when a target's rank cannot be established."""

    return _(
        "I couldn't check that user's rank in this server right now, so I "
        "won't act on them. Try again in a moment."
    )


def hierarchy_error_with_member(ctx, target, member):
    """The hierarchy check with the target's Member ALREADY resolved.

    ``member`` is the target's :class:`discord.Member` in this guild, or None
    when the target has been PROVEN absent from it (a hackban by id). Never pass
    a plain ``guild.get_member`` miss as None - see the module docstring: that is
    the fail-open hole this split exists to close. Callers that legitimately hold
    a resolved Member (a ``discord.Member`` annotation, whose converter already
    did the lookup) can go through :func:`hierarchy_error` instead.
    """
    author = ctx.author
    guild = ctx.guild

    if target.id == author.id:
        return _("You can't do that to yourself.")

    if member is None:
        return None  # proven not in the guild: no hierarchy to compare

    if member.id == guild.owner_id:
        return _("You can't action the server owner.")

    # The invoker must outrank the target, unless they own the guild.
    if author.id != guild.owner_id and member.top_role >= author.top_role:
        return _("You can't action someone whose role is equal to or above yours.")

    # The bot must also outrank the target to act on them.
    if member.top_role >= guild.me.top_role:
        return _("My highest role isn't above that member, so I can't act on them.")

    return None


def hierarchy_error(ctx, target):
    """Return a reason string if ctx.author may not action target, else None.

    CACHE-ONLY: safe when ``target`` is already a :class:`discord.Member` (the
    ``discord.Member`` converter resolved it, so there is nothing left to miss).
    For a bare User or Object use :func:`hierarchy_error_resolved` - on this bot
    a cache miss does not mean the target is absent from the guild.
    """
    member = (
        target
        if isinstance(target, discord.Member)
        else ctx.guild.get_member(target.id)
    )
    return hierarchy_error_with_member(ctx, target, member)


async def hierarchy_error_resolved(ctx, target):
    """:func:`hierarchy_error`, resolving an uncached target before deciding.

    Used by the single-target punishments annotated ``discord.User``
    (ban/kick/tempban): their converter happily yields a User for someone who IS
    in the guild but is simply not cached, and the old cache-only check then
    returned None - letting a moderator ban or kick staff ranked above them.

    Cost: at most ONE ``GET /guilds/{id}/members/{user}`` per invocation, and
    only on a cache miss. ``NotFound`` is the one negative answer we trust (the
    target really is not in the guild, so a hackban by id stays legal); every
    other failure - Forbidden, 5xx, timeout - leaves the rank UNKNOWN and is
    refused, because "I could not check" must never read as "go ahead".
    """
    guild = ctx.guild

    if isinstance(target, discord.Member):
        return hierarchy_error_with_member(ctx, target, target)

    cached = guild.get_member(target.id)
    if cached is not None:
        return hierarchy_error_with_member(ctx, target, cached)

    try:
        member = await guild.fetch_member(target.id)
    except discord.NotFound:
        member = None  # proven absent from the guild
    except Exception:
        log.warning(
            "Could not resolve member %s in guild %s for a hierarchy check",
            target.id,
            getattr(guild, "id", None),
            exc_info=True,
        )
        return _unverifiable_error()

    return hierarchy_error_with_member(ctx, target, member)


async def resolve_guild_members(guild, user_ids):
    """Resolve many ids at once for a bulk hierarchy decision.

    Returns ``(found, unresolved)``: ``found`` maps id -> Member for the ids that
    ARE in the guild, ``unresolved`` is the set of ids whose membership could not
    be established. An id in neither is proven absent (a legitimate hackban).

    Cost: cache hits are free; the misses go out as ``query_members``, a GATEWAY
    request (REQUEST_GUILD_MEMBERS) batched :data:`MEMBER_QUERY_CHUNK` ids at a
    time. A worst-case massban of 200 uncached ids is therefore TWO gateway
    round-trips, not 200 REST calls - which is why the bulk path does not reuse
    :func:`hierarchy_error_resolved`'s per-target ``fetch_member``. Results are
    NOT cached (``cache=False``): a raid-cleanup lot must not grow the member
    cache of every guild it touches. A chunk that raises (timeout, closed socket)
    marks its whole chunk unresolved, so the caller fails closed on it.
    """
    found = {}
    missing = []
    for uid in dict.fromkeys(user_ids):  # de-duplicated, order preserved
        member = guild.get_member(uid)
        if member is not None:
            found[uid] = member
        else:
            missing.append(uid)

    unresolved = set()
    for start in range(0, len(missing), MEMBER_QUERY_CHUNK):
        chunk = missing[start : start + MEMBER_QUERY_CHUNK]
        try:
            members = await guild.query_members(
                user_ids=chunk, limit=MEMBER_QUERY_CHUNK, cache=False
            )
        except Exception:
            log.warning(
                "Member query failed for %d id(s) in guild %s; treating them as "
                "unresolved",
                len(chunk),
                getattr(guild, "id", None),
                exc_info=True,
            )
            unresolved.update(chunk)
            continue
        for member in members:
            found[member.id] = member

    return found, unresolved


def bot_can_assign_role(role, guild):
    """Whether Yasuho could actually add/remove ``role`` in ``guild`` right now.

    Not @everyone, not managed by an integration, and strictly below the bot's
    top role - the same hierarchy shape :func:`role_hierarchy_error` checks for
    a human invoker, but asked purely about the BOT with no invoker involved:
    used to gate role grants that Yasuho hands out on its own (a level-up
    reward, a season champion role), never in response to a moderator's
    command. Shared home for a check that started life duplicated in
    cogs/community/leveling/level_rewards.py (level rewards) and cogs/community/leveling/seasons.py
    (the season champion role) - both import it from here now.
    """
    me = guild.me
    return (
        me is not None
        and not role.is_default()
        and not role.managed
        and role < me.top_role
    )


def role_hierarchy_error_for(author, guild, role):
    """Return a reason string if ``author`` may not manage ``role``, else None.

    The member/guild-shaped core of :func:`role_hierarchy_error`, split out so
    the component surfaces (a modal submit, a builder select) can run the exact
    same check: they only ever hold an ``Interaction`` (``interaction.user``),
    never a ``commands.Context``, and duplicating the comparison per builder is
    how the self-role builders ended up with no configurer guard at all.
    """
    if (
        author.id != guild.owner_id
        and not author.guild_permissions.administrator
        and role >= author.top_role
    ):
        return _("You can't manage a role that is equal to or above your highest role.")

    if role >= guild.me.top_role:
        return _("My highest role isn't above that role, so I can't manage it.")

    return None


def role_hierarchy_error(ctx, role):
    """Return a reason string if ctx.author may not manage ``role``, else None.

    The role-management commands (addrole/removerole) are gated only by
    ``manage_roles``, which does not prove the invoker outranks the role they
    are handing out. This mirrors :func:`hierarchy_error` for roles: the guild
    owner or an Administrator may touch any role, but a plain moderator must sit
    strictly above it, and the bot must outrank it too or the edit just fails
    with a confusing silent Forbidden.
    """
    return role_hierarchy_error_for(ctx.author, ctx.guild, role)


def self_assignable_role_error(author, guild, role):
    """Return a reason string if ``role`` may not be PUBLISHED as self-assignable.

    The self-role surfaces (reaction roles, button panels, role menus) ask a
    strictly bigger question than addrole does: the configurer must outrank the
    role (:func:`role_hierarchy_error_for`) AND Yasuho must be able to hand it
    out at all (:func:`bot_can_assign_role`). Position alone is not enough here
    - @everyone sits below everyone and an integration-managed role (a Nitro
    booster role, another bot's own role) can sit below both parties, yet
    neither can ever be granted: publishing one yields a button/emoji/option
    whose grant 403s forever, and the failure is swallowed at the grant site, so
    the member just sees nothing happen. Refusing at publish time is the only
    place the configurer can be told.

    The two halves are kept as separate functions because addrole/removerole
    want the hierarchy half ALONE (a moderator legitimately removes a managed
    role from someone), and the automatic grants (level rewards, season
    champion) want the bot half alone (no invoker exists).
    """
    err = role_hierarchy_error_for(author, guild, role)
    if err:
        return err

    if not bot_can_assign_role(role, guild):
        return _(
            "I can't assign that role - it's either managed by an "
            "integration or above my highest role."
        )

    return None
