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


def role_hierarchy_error(ctx, role):
    """Return a reason string if ctx.author may not manage ``role``, else None.

    The role-management commands (addrole/removerole) are gated only by
    ``manage_roles``, which does not prove the invoker outranks the role they
    are handing out. This mirrors :func:`hierarchy_error` for roles: the guild
    owner or an Administrator may touch any role, but a plain moderator must sit
    strictly above it, and the bot must outrank it too or the edit just fails
    with a confusing silent Forbidden.
    """
    author = ctx.author
    guild = ctx.guild

    if (
        author.id != guild.owner_id
        and not author.guild_permissions.administrator
        and role >= author.top_role
    ):
        return _("You can't manage a role that is equal to or above your highest role.")

    if role >= guild.me.top_role:
        return _("My highest role isn't above that role, so I can't manage it.")

    return None
