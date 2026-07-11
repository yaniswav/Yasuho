"""Reusable discord.ui.View base classes.

This module hosts the shared View building blocks that were previously
copy-pasted across the cogs. Keeping a single canonical implementation means
the author gating and timeout cleanup behave identically everywhere and only
have to be fixed in one place.
"""

from __future__ import annotations

import discord

from tools import i18n
from tools.i18n import N_, _

# Deny wordings used as AuthorView.deny_message across the cogs. They are stored
# on the view at construction time (outside the clicker's task) and translated
# at send time in interaction_check, so the literals are registered here with N_
# to be extractable. Add any new deny wording here so it gets translated.
_DENY_STRINGS = [
    N_("This menu isn't for you."),
    N_("This panel isn't for you."),
    N_("This prompt isn't for you."),
    N_("This profile editor isn't for you."),
    N_("This isn't your game, start your own with the command!"),
]


class AuthorView(discord.ui.View):
    """A View that only its originating author may interact with.

    Subclasses add their own components (buttons, selects, modals) exactly as
    they would on a plain :class:`discord.ui.View`. This base only supplies two
    behaviours:

    * ``interaction_check`` rejects anyone other than ``author_id`` with an
      ephemeral ``deny_message``.
    * ``on_timeout`` disables every child and edits the bound ``message`` so the
      components stop responding once the View expires.

    Both ``timeout`` and ``deny_message`` are overridable per instance. Assign
    the sent message to ``self.message`` (e.g. ``view.message = await ctx.send(...)``)
    so the timeout cleanup has something to edit.

    Subclasses MAY extend either hook and should call ``super()`` to keep the
    base behaviour, for example::

        async def interaction_check(self, interaction):
            if not await super().interaction_check(interaction):
                return False
            ...  # extra checks
            return True

        async def on_timeout(self):
            ...  # extra cleanup
            await super().on_timeout()
    """

    def __init__(self, author_id, *, timeout=180, deny_message="This menu isn't for you."):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None
        self._deny_message = deny_message

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localize.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            # Translate in the clicker's locale (the stored wording is a
            # registered N_ literal, see _DENY_STRINGS).
            await interaction.response.send_message(
                _(self._deny_message), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# Component types a LayoutView disables on timeout (buttons + every select
# flavour; ChannelSelect is NOT a subclass of ui.Select, so list it explicitly).
_DISABLEABLE = (discord.ui.Button, discord.ui.Select, discord.ui.ChannelSelect)


class AuthorLayoutView(discord.ui.LayoutView):
    """A Components V2 LayoutView gated to its originating author.

    LayoutView cannot subclass :class:`AuthorView` (that is a plain
    ``discord.ui.View``), so the author gate and locale resolution AuthorView
    normally supplies are reimplemented here: :meth:`interaction_check` applies
    the clicker's locale then rejects anyone but ``author_id`` (using the same
    registered deny wording, see ``_DENY_STRINGS``), and :meth:`on_timeout`
    disables every control and edits the bound ``message`` in place. Subclasses
    assemble their own :class:`~discord.ui.Container` and set ``self.message`` so
    the timeout cleanup has something to edit.
    """

    def __init__(self, author_id, *, timeout=180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None

    async def interaction_check(self, interaction):
        # Component callbacks run in their own task where get_context never set
        # the locale; resolve it here so this check AND the callback localize.
        await i18n.apply_interaction_locale(interaction)
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This panel isn't for you."), ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        """Disable every button/select in the layout (walks nested ActionRows)."""

        for child in self.walk_children():
            if isinstance(child, _DISABLEABLE):
                child.disabled = True

    async def on_timeout(self):
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LocaleModal(discord.ui.Modal):
    """A Modal whose submit callback runs in the interaction's resolved locale.

    Modal submit callbacks run in their own task, where ``Yasuho.get_context``
    never set the i18n locale; resolving it in ``interaction_check`` makes the
    modal's ``_()`` calls localize for the submitter. Subclass this instead of
    ``discord.ui.Modal`` for any modal with user-facing (translatable) text.

    Subclasses that need their own ``interaction_check`` should call
    ``super().interaction_check(interaction)`` to keep the locale resolution.
    """

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True
