from __future__ import annotations

import logging

import discord

from .formats import random_colour
from .views import AuthorView

log = logging.getLogger(__name__)


def paginate_lines(lines, *, title=None, colour=None, per_page=10):
    """Chunk a list of strings into a list of embeds, one per page.

    Returns at least one embed (a placeholder when ``lines`` is empty) so the
    result can always be handed straight to :class:`Paginator`.
    """
    if colour is None:
        colour = random_colour()
    if not lines:
        return [discord.Embed(title=title, description="Nothing to show.", colour=colour)]

    embeds = []
    for start in range(0, len(lines), per_page):
        chunk = lines[start : start + per_page]
        embeds.append(
            discord.Embed(title=title, description="\n".join(chunk), colour=colour)
        )
    return embeds


class Paginator(AuthorView):
    """A reusable button paginator over a list of embeds.

    Usage::

        embeds = paginate_lines(lines, title="Leaderboard")
        await Paginator(embeds, author_id=ctx.author.id).start(ctx)

    ``author_id`` is optional: when None the paginator is public (anyone may
    page), so interaction_check is overridden to keep that looser gate while
    still inheriting the shared on_timeout cleanup.
    """

    def __init__(self, embeds, *, author_id=None, timeout=120):
        super().__init__(author_id, timeout=timeout)
        self.embeds = list(embeds) or [discord.Embed(description="Nothing to show.")]
        self.index = 0
        self._sync()

    def _sync(self):
        """Refresh button states and the page footer for the current index."""
        at_start = self.index == 0
        at_end = self.index == len(self.embeds) - 1
        self.first_page.disabled = self.prev_page.disabled = at_start
        self.next_page.disabled = self.last_page.disabled = at_end
        self.embeds[self.index].set_footer(
            text=f"Page {self.index + 1}/{len(self.embeds)}"
        )

    async def interaction_check(self, interaction):
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def start(self, ctx):
        """Send the first page. A single page is sent without navigation buttons."""
        self._sync()
        if len(self.embeds) <= 1:
            self.message = await ctx.send(embed=self.embeds[0])
        else:
            self.message = await ctx.send(embed=self.embeds[0], view=self)
        return self.message

    async def _go(self, interaction, index):
        self.index = max(0, min(index, len(self.embeds) - 1))
        self._sync()
        try:
            await interaction.response.edit_message(
                embed=self.embeds[self.index], view=self
            )
        except Exception:
            log.exception("paginator navigation failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "Couldn't turn the page, please try again.", ephemeral=True
                    )
                except Exception:
                    log.exception("paginator navigation failed")

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction, button):
        await self._go(interaction, 0)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction, button):
        await self._go(interaction, self.index - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction, button):
        await self._go(interaction, self.index + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction, button):
        await self._go(interaction, len(self.embeds) - 1)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_page(self, interaction, button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            log.exception("paginator navigation failed")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "Couldn't close the menu, please try again.", ephemeral=True
                    )
                except Exception:
                    log.exception("paginator navigation failed")
        finally:
            self.stop()
