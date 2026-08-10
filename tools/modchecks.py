"""Shared moderation authorization checks (role hierarchy / self / owner).

discord.py's has_permissions only proves the INVOKER holds a permission, not
that the target is actually actionable. This stops a moderator from punishing
themselves, the guild owner, or anyone whose top role is equal to or above their
own (unless they own the guild), and flags when the bot itself is not high enough
to act. Returning a user-facing reason (or None) keeps the call sites a two-liner.
"""

from __future__ import annotations

import discord

from tools.i18n import _


def hierarchy_error(ctx, target):
    """Return a reason string if ctx.author may not action target, else None.

    ``target`` may be a Member or a bare User (e.g. a hackban by id). A User who
    is not in the guild has no role to compare, so only the self-check applies.
    """
    author = ctx.author
    guild = ctx.guild

    if target.id == author.id:
        return _("You can't do that to yourself.")

    member = (
        target
        if isinstance(target, discord.Member)
        else guild.get_member(target.id)
    )
    if member is None:
        return None  # not in the guild: no hierarchy to compare

    if member.id == guild.owner_id:
        return _("You can't action the server owner.")

    # The invoker must outrank the target, unless they own the guild.
    if author.id != guild.owner_id and member.top_role >= author.top_role:
        return _("You can't action someone whose role is equal to or above yours.")

    # The bot must also outrank the target to act on them.
    if member.top_role >= guild.me.top_role:
        return _("My highest role isn't above that member, so I can't act on them.")

    return None


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
