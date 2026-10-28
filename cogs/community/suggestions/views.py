"""Suggestion box components: the persistent buttons, the status picker, the modals.

The posted message carries three ``discord.ui.DynamicItem`` buttons (``sg:up``,
``sg:dn``, ``sg:st``), so votes and the staff menu survive restarts with no stored
view. The ids are suggestion row ids; the guild is re-checked in SQL on every vote.
Staff authority is re-read from the live member and config on every step (button,
select and modal submit), never from anything captured earlier.

Typography rule: ASCII '-' and '...' only.
"""

import discord

from . import render, store
from tools.i18n import _
from tools.views import AuthorView, LocaleDynamicItem, LocaleModal, LocaleView

UP_TEMPLATE = r"sg:up:(?P<id>\d+)"
DOWN_TEMPLATE = r"sg:dn:(?P<id>\d+)"
STATUS_TEMPLATE = r"sg:st:(?P<id>\d+)"


def _cog(interaction):
    return interaction.client.get_cog("Suggestions")


async def _unavailable(interaction):
    await interaction.response.send_message(
        _("Suggestions are unavailable right now."), ephemeral=True
    )


class VoteUpButton(LocaleDynamicItem[discord.ui.Button], template=UP_TEMPLATE):
    """Persistent upvote: a member's own vote, toggled."""

    def __init__(self, suggestion_id, count=0, disabled=False):
        self.suggestion_id = int(suggestion_id)
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=render.UP,
                label=str(count),
                disabled=disabled,
                custom_id="sg:up:{id}".format(id=int(suggestion_id)),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.vote(interaction, self.suggestion_id, 1)


class VoteDownButton(LocaleDynamicItem[discord.ui.Button], template=DOWN_TEMPLATE):
    """Persistent downvote: a member's own vote, toggled."""

    def __init__(self, suggestion_id, count=0, disabled=False):
        self.suggestion_id = int(suggestion_id)
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                emoji=render.DOWN,
                label=str(count),
                disabled=disabled,
                custom_id="sg:dn:{id}".format(id=int(suggestion_id)),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.vote(interaction, self.suggestion_id, -1)


class StatusButton(LocaleDynamicItem[discord.ui.Button], template=STATUS_TEMPLATE):
    """Persistent staff menu button, visible to all and usable by staff only."""

    def __init__(self, suggestion_id):
        self.suggestion_id = int(suggestion_id)
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=_("Set status"),
                custom_id="sg:st:{id}".format(id=int(suggestion_id)),
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["id"]))

    async def callback(self, interaction):
        cog = _cog(interaction)
        if cog is None:
            return await _unavailable(interaction)
        await cog.open_status_menu(interaction, self.suggestion_id)


DYNAMIC_ITEMS = (VoteUpButton, VoteDownButton, StatusButton)


class SuggestionView(LocaleView):
    """The buttons riding a posted suggestion (dispatch goes through the registry)."""

    def __init__(self, suggestion_id, up=0, down=0, closed=False):
        super().__init__(timeout=None)
        self.add_item(VoteUpButton(suggestion_id, up, closed))
        self.add_item(VoteDownButton(suggestion_id, down, closed))
        self.add_item(StatusButton(suggestion_id))


class _StatusSelect(discord.ui.Select):
    def __init__(self, picker, options):
        self._owner = picker
        super().__init__(placeholder=_("Choose a status..."), options=options)

    async def callback(self, interaction):
        await self._owner.cog.pick_status(interaction, self._owner.suggestion_id, self.values[0])


class StatusPicker(AuthorView):
    """Ephemeral status menu for one staff member."""

    def __init__(self, cog, author_id, suggestion_id, statuses):
        super().__init__(author_id, timeout=120, deny_message="This menu isn't for you.")
        self.cog = cog
        self.suggestion_id = suggestion_id
        self.add_item(
            _StatusSelect(
                self,
                [
                    discord.SelectOption(label=render.status_label(key)[:100], value=key)
                    for key in statuses
                ],
            )
        )


class ReasonModal(LocaleModal):
    """Optional short reason; opened only by the staff-gated :class:`StatusPicker`."""

    def __init__(self, cog, suggestion_id, status):
        super().__init__(title=_("Set status"))
        self.cog = cog
        self.suggestion_id = suggestion_id
        self.status = status
        self.reason = discord.ui.TextInput(
            label=_("Reason (optional)"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=store.REASON_MAX,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction):
        await self.cog.apply_status(
            interaction, self.suggestion_id, self.status, (self.reason.value or "").strip()
        )


class SuggestModal(LocaleModal):
    """Any member may submit; the suggestion belongs to ``interaction.user``."""

    def __init__(self, cog):
        super().__init__(title=_("New suggestion"))
        self.cog = cog
        self.headline = discord.ui.TextInput(
            label=_("Title"), min_length=1, max_length=store.TITLE_MAX
        )
        self.details = discord.ui.TextInput(
            label=_("Details"),
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=store.DETAILS_MAX,
        )
        self.add_item(self.headline)
        self.add_item(self.details)

    async def on_submit(self, interaction):
        await self.cog.submit(
            interaction, self.headline.value.strip(), self.details.value.strip()
        )
