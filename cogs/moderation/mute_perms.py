"""The permission shape of the "Muted" role, defined once.

A mute is NOT enforced by the role's own permission bitfield. Discord computes a
member's base permissions by OR-ing the grants of every role they hold, so a role
whose bitfield is all-deny (value 0) takes nothing away from anybody - it only
declines to add. What actually silences someone is the per-channel OVERWRITE
denying the role, which is why :func:`overwrite_for` is the load-bearing half of
this module and the role factory is documentation.

The hole this module closes: the overwrites denied ``send_messages`` and nothing
else about threads. ``send_messages`` governs the CHANNEL body only - posting
inside a thread is ``send_messages_in_threads``, and opening a new one is
``create_public_threads`` / ``create_private_threads``. A muted member could
therefore start a thread under the channel they had just been muted in and keep
talking, in full view of everyone. Forum channels made it worse: every forum post
IS a thread, so a forum was 100% unmuted, and ``guild.text_channels`` does not
even list forums - the old three loops never touched one.

The second hole: a voice channel carries a TEXT chat as well as audio, and the
overwrites denied ``speak`` alone there - so the muted member simply typed in
the same room. See :data:`VOICE_TEXT_DENIES`.

Keeping the shape here (rather than inline in the mute command) means the three
paths that apply it cannot drift apart - creation (``_ensure_mute_role``), the
new-channel listener (``cogs.system.events.on_guild_channel_create``) and the
admin re-apply (``?mutesync``) all ask this module the same question. That
matters more than it looks: a fix living only in the creation path reaches
exactly zero guilds that already have a Muted role, which is all of them.

:func:`merged` and :func:`needs_update` are what make the re-apply safe and
cheap on a role that has been live for years - see their docstrings.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import discord

# Denied wherever a member can produce text. The three thread bits are the fix:
# without them, `send_messages=False` silences the channel body and nothing else.
TEXT_DENIES = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "add_reactions",
    "send_tts_messages",
)

# Denied wherever a member can produce sound.
VOICE_DENIES = ("speak",)

# A voice (and stage) channel also has a built-in TEXT chat, governed by the
# ordinary text permissions - so denying ``speak`` alone left a muted member
# typing in the very room they had just been silenced in. There are no threads
# under a voice channel, which is why this is not simply TEXT_DENIES.
VOICE_TEXT_DENIES = (
    "send_messages",
    "add_reactions",
    "send_tts_messages",
)


def _deny(*groups):
    """A PermissionOverwrite denying every named permission in ``groups``."""

    denied = {}
    for group in groups:
        denied.update(dict.fromkeys(group, False))
    return discord.PermissionOverwrite(**denied)


def text_overwrite():
    """The deny overwrite for a channel whose members type (text, forum, ...)."""

    return _deny(TEXT_DENIES)


def voice_overwrite():
    """The deny overwrite for a channel whose members speak - AND type.

    Both halves, because a voice channel is two rooms wearing one name: the
    audio and the text chat attached to it. Denying only ``speak`` produced a
    "mute" the member walked around by typing in the same channel.
    """

    return _deny(VOICE_DENIES, VOICE_TEXT_DENIES)


def category_overwrite():
    """Both halves, for a category that children may synchronise from."""

    return _deny(TEXT_DENIES, VOICE_DENIES)


def role_permissions():
    """The bitfield handed to ``create_role``.

    Byte-identical to a bare :class:`discord.Permissions` (every flag here is
    ``False``, so ``.value`` is 0, exactly as it was before the thread bits were
    added). It is spelled out because it names the intent at the creation site,
    and because a reader who assumes this is what enforces the mute needs to see
    the thread permissions in it too.
    """

    return discord.Permissions(
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False,
        send_tts_messages=False,
        speak=False,
    )


def overwrite_for(channel):
    """The right deny overwrite for ``channel``, or None if it takes none.

    Dispatching on TYPE (instead of walking ``guild.text_channels`` then
    ``guild.voice_channels`` then ``guild.categories``) is what pulls forum and
    media channels in: they are neither text nor voice channels to discord.py,
    so the old per-list loops skipped them entirely, and a forum is pure threads.
    Threads themselves are absent on purpose - a thread has no overwrites of its
    own, it inherits its parent's, so denying the parent covers every thread that
    exists in it now or later.
    """

    if isinstance(channel, discord.CategoryChannel):
        return category_overwrite()
    if isinstance(channel, discord.ForumChannel):
        return text_overwrite()
    if isinstance(channel, discord.TextChannel):
        return text_overwrite()
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return voice_overwrite()
    return None


def merged(existing, overwrite):
    """``existing`` with every deny in ``overwrite`` forced on, nothing else lost.

    ``set_permissions(role, overwrite=...)`` REPLACES a channel's overwrite for
    that role, so writing :func:`overwrite_for` straight over an existing one
    would silently drop whatever else a server had put there (a deliberate
    ``view_channel: False`` on a staff-only channel, say). The creation path
    never had this problem - the role is brand new and owns no overwrites - but a
    RE-APPLY path runs against roles that have been live for years, and a
    security fix that quietly widens access somewhere else is not a fix.

    Iterating a :class:`~discord.PermissionOverwrite` yields ``(name, value)``
    for every permission, ``None`` for the ones it does not express; only the
    ones that are set are carried over, from each side, ours last.
    """

    result = discord.PermissionOverwrite()
    for source in (existing, overwrite):
        if source is None:
            continue
        for name, value in source:
            if value is not None:
                setattr(result, name, value)
    return result


def needs_update(existing, overwrite):
    """True when ``existing`` does not already deny everything ``overwrite`` does.

    The cheap half of the re-apply path: a guild whose channels are already
    correct must cost ZERO REST calls to re-sync, or a 500-channel server would
    pay 500 edits to change nothing (and eat the rate limit doing it). Only the
    permissions we assert are compared - anything else on the overwrite is none
    of this module's business.
    """

    if overwrite is None:
        return False
    current = dict(existing or ())
    return any(
        current.get(name) is not value
        for name, value in overwrite
        if value is not None
    )
