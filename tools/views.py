"""Reusable discord.ui.View base classes.

This module hosts the shared View building blocks that were previously
copy-pasted across the cogs. Keeping a single canonical implementation means
the author gating and timeout cleanup behave identically everywhere and only
have to be fixed in one place.
"""

from __future__ import annotations

import discord


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
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(self._deny_message, ephemeral=True)
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
