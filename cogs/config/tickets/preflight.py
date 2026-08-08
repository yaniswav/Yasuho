"""What the bot must be allowed to do on a panel channel, and how to say what is
missing.

A ticket room is a PRIVATE THREAD on the panel channel, which is the whole
reason this list exists: a private thread inherits the parent channel's
permissions, so nothing here grants visibility - Discord does, by only showing
the thread to its members and to staff holding ``manage_threads``. What the bot
needs is the ability to CREATE that thread, talk inside it, and manage it later.

The check is deliberately performed twice, against the same list: once at
``/ticket setup`` (so a server manager is told immediately, in the shape
``cogs/config/verification.py`` uses) and once at click time (because a role can
be edited, or a channel overwrite added, long after setup - the panel button
must never fail with a raw Forbidden in front of a member asking for help).

A THIRD check, against a different list and with a different verdict, asks
whether the configured support ROLE can reach the threads it will be pinged in
(:data:`SUPPORT_ROLE_PERMISSIONS`). That one only warns.

Returning ATTRIBUTE NAMES rather than prose keeps this module pure and
testable with a plain permissions stand-in; the human labels are marked with
``N_`` here and translated at use through :func:`describe`, the standard
module-constant-then-translate-at-use pattern (see tools.i18n.mark).

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

from tools.i18n import N_, _

# Needed to put the panel itself in the channel.
PANEL_PERMISSIONS = ("view_channel", "send_messages", "embed_links")

# Needed to run tickets there. ``manage_threads`` is what lets support staff and
# the bot see and manage every ticket thread (and is what lot T2's close/archive
# controls will need); ``read_message_history`` is what lets the bot read a
# thread's last activity, which the inactivity sweep is built on.
THREAD_PERMISSIONS = (
    "create_private_threads",
    "send_messages_in_threads",
    "manage_threads",
    "read_message_history",
)

# The full set ``/ticket setup`` demands before it will write any configuration.
SETUP_PERMISSIONS = PANEL_PERMISSIONS + THREAD_PERMISSIONS

# The subset a CLICK needs. Posting the panel is already done by then, so a
# missing ``embed_links`` must not stop somebody opening a ticket.
OPEN_PERMISSIONS = ("view_channel",) + THREAD_PERMISSIONS

# What the SUPPORT ROLE itself needs on the panel channel, checked against
# ``channel.permissions_for(role)``.
#
# This is the one permission set that is NOT about the bot, and it exists because
# of an asymmetry that is easy to miss: mentioning a USER adds them to a private
# thread, mentioning a ROLE does not add anybody. Staff reach a ticket only by
# holding ``manage_threads`` on the parent channel (which is what makes every
# private thread there visible to them), so a support role without it is pinged
# into rooms it cannot open - a ticket nobody answers.
#
# WARNED about, never refused: a server may run staff access some other way (a
# category-level overwrite the check does see, or a human who adds staff by hand),
# and refusing setup over it would block a working configuration.
SUPPORT_ROLE_PERMISSIONS = ("view_channel", "manage_threads")

# Human labels, in English, translated at render time. Keyed by the exact
# ``discord.Permissions`` attribute name so a typo in a list above surfaces as a
# missing key rather than as a silently unchecked permission (see
# :func:`describe`, which falls back to the raw name and is guarded by a test).
_LABELS = {
    "view_channel": N_("View Channel"),
    "send_messages": N_("Send Messages"),
    "embed_links": N_("Embed Links"),
    "create_private_threads": N_("Create Private Threads"),
    "send_messages_in_threads": N_("Send Messages in Threads"),
    "manage_threads": N_("Manage Threads"),
    "read_message_history": N_("Read Message History"),
}


def missing_permissions(permissions, required=SETUP_PERMISSIONS):
    """Names in ``required`` that ``permissions`` does not grant, in order.

    ``permissions`` is whatever ``channel.permissions_for(guild.me)`` returned.
    A permission the object does not even carry counts as MISSING rather than as
    granted: the safe direction, and the one that surfaces a discord.py rename
    instead of silently skipping the check. Pure.
    """
    if permissions is None:
        return list(required)
    return [name for name in required if not getattr(permissions, name, False)]


def describe(names):
    """Render permission attribute names as a translated, comma-joined list.

    Falls back to the raw attribute name for anything unlabelled, so a list that
    grows a permission without a label degrades to a slightly uglier - but still
    correct and actionable - message instead of raising in front of a user.

    The label is bound to a LOCAL before ``_()`` sees it: pybabel's extractor
    reads ``_(...)`` calls literally, and a subscript expression inside one is
    the shape that makes it complain (the same reason help.py translates its
    ``N_`` category blurbs through a plain variable).
    """
    parts = []
    for name in names:
        label = _LABELS.get(name)
        parts.append(_(label) if label else name)
    return ", ".join(parts)
