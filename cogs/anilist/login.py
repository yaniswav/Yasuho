"""The ``/anilist login`` account-linking flow: the OAuth PIN modal and its view.

Two pieces only - :class:`LoginView`, the button that opens the prompt, and
:class:`LoginModal`, which collects the PIN AniList showed the user and hands it
to the cog's token exchange. Kept as its own module because it is the one AniList
surface that handles a CREDENTIAL: the code is taken in a modal (never typed into
a channel), never echoed back, and never logged, and that invariant is easier to
hold when the code that carries it is not buried in a thousand-line view file.

Depends on nothing else in the package, so it can be read on its own.
"""

import logging

import discord

from tools import interactions
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Interactive components (discord.ui)
# ----------------------------------------------------------------------
class LoginModal(LocaleModal, title="Enter your AniList code"):
    """Collect the OAuth PIN and finish linking without ever echoing it."""

    code = discord.ui.TextInput(
        label="Code",
        placeholder="Paste the code AniList showed you",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, cog, author_id, login_view=None):
        super().__init__()
        self.cog = cog
        self.author_id = author_id
        self.login_view = login_view

    async def on_submit(self, interaction):
        # Defer first: the token exchange is a network round-trip that can exceed
        # the 3s interaction window, which would otherwise fail the modal submit.
        await interactions.defer(
            interaction, ephemeral=True, thinking=True, surface="anilist login code modal"
        )
        try:
            name = await self.cog._exchange_code(self.author_id, self.code.value)
            if name is None:
                return await interaction.followup.send(
                    _("That code did not work, try `/anilist login` again."),
                    ephemeral=True,
                )
            await interaction.followup.send(
                _("Connected as {name}!").format(name=name), ephemeral=True
            )
            # Once linked, replace the prompt (and its authorize link) with a
            # confirmation and stop the view so nothing lingers in the DM.
            view = self.login_view
            if view is not None and view.message is not None:
                try:
                    await view.message.edit(
                        content=_("✅ Linked as **{name}**.").format(name=name),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
                view.stop()
        except Exception:
            log.exception("AniList login modal failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong linking your account."), ephemeral=True
                )
            except Exception:
                pass


class LoginView(AuthorView):
    """Author-restricted view exposing a modal to enter the OAuth PIN."""

    def __init__(self, cog, author_id, timeout=300):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction, button):
        try:
            await interaction.response.send_modal(
                LoginModal(self.cog, self.author_id, login_view=self)
            )
        except Exception:
            log.exception("AniList login modal launch failed")
            try:
                await interaction.response.send_message(
                    _("Could not open the code form."), ephemeral=True
                )
            except Exception:
                pass
