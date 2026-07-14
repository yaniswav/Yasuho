import logging

import discord
from discord.ext import commands

from tools import db, settings
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.interactions import notify_failure, refresh_layout
from tools.views import AuthorLayoutView

log = logging.getLogger(__name__)

# Sentinel for "we tried to read this feature's state but the lookup failed".
# Distinct from None, which legitimately means "not configured".
_UNKNOWN = object()

# Features the panel can route the admin to. Leveling is toggled in-panel; the
# rest each point at their dedicated setup command. Each value is N_-marked for
# extraction and translated at the use site via _(...) (see handle_select).
GUIDANCE = {
    "prefix": N_(
        "Use `/prefix set <prefix>` to change the command prefix for this "
        "server."
    ),
    "autorole": N_(
        "Use `/autorole set <role>` to choose the role new members receive on "
        "join."
    ),
    "automod": N_(
        "Use `/automod` to open the interactive AutoMod control panel "
        "(custom + native filters)."
    ),
    "modlog": N_(
        "Use `/modlog` to open the interactive mod-log control panel "
        "(channel + event toggles)."
    ),
    "starboard": N_(
        "Use `/starboard set <channel> [threshold]` to set up the starboard."
    ),
    "welcome": N_(
        "Use `/welcome set <channel> <message>` to configure welcome messages. "
        "Placeholders: {user}, {server}, {count}."
    ),
}


def _onoff(value):
    """Render a tri-state boolean (True / False / unknown) as a labelled dot."""

    if value is _UNKNOWN:
        return "❔ " + _("Unknown")
    return ("🟢 " + _("Enabled")) if value else ("🔴 " + _("Disabled"))


# ----------------------------------------------------------------------
# Edit a LayoutView panel in place with view=-only (no embed/content)
# ----------------------------------------------------------------------
async def _refresh_layout(interaction, message, view):
    """Edit a LayoutView panel in place with ``view=`` only (no embed/content).

    A Components V2 message carries its content inside the view and Discord
    rejects an ``embed=`` on such an edit. Tries the live interaction edit
    first, then falls back to editing the stored message when the interaction
    was already answered (e.g. a deferred modal submit).
    """

    await refresh_layout(interaction, message, view, surface="config panel")


# ----------------------------------------------------------------------
# Interactive server-settings panel (Components V2)
# ----------------------------------------------------------------------
class ConfigSelect(discord.ui.Select):
    """Pick a feature: Leveling toggles in place, others point to a command."""

    def __init__(self, panel):
        self.panel = panel
        leveling = panel.state.get("leveling")
        if leveling is _UNKNOWN:
            lvl_desc = _("Current state unknown")
        elif leveling:
            lvl_desc = _("Currently enabled - select to disable")
        else:
            lvl_desc = _("Currently disabled - select to enable")

        options = [
            discord.SelectOption(
                label=_("Leveling"),
                value="leveling",
                emoji="📈",
                description=lvl_desc[:100],
            ),
            discord.SelectOption(
                label=_("Prefix"),
                value="prefix",
                emoji="💬",
                description=_("Change the command prefix"),
            ),
            discord.SelectOption(
                label=_("Auto-role"),
                value="autorole",
                emoji="🎭",
                description=_("Role granted to new members"),
            ),
            discord.SelectOption(
                label=_("AutoMod"),
                value="automod",
                emoji="🛡️",
                description=_("Open the AutoMod control panel"),
            ),
            discord.SelectOption(
                label=_("Mod-log"),
                value="modlog",
                emoji="📝",
                description=_("Open the mod-log control panel"),
            ),
            discord.SelectOption(
                label=_("Starboard"),
                value="starboard",
                emoji="⭐",
                description=_("Configure the starboard"),
            ),
            discord.SelectOption(
                label=_("Welcome"),
                value="welcome",
                emoji="👋",
                description=_("Configure welcome messages"),
            ),
        ]
        super().__init__(
            placeholder=_("Configure a feature..."),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction):
        await self.panel.handle_select(interaction, self.values[0])


class ConfigPanel(AuthorLayoutView):
    """Author-restricted overview of every server feature, with quick controls.

    A single Components V2 :class:`~discord.ui.Container` in the house style
    established by the welcome/Twitch panels: a Section (guild icon thumbnail
    accessory + summary lines) holds every feature's current state, and one
    ActionRow carries the feature select. Re-renders are view=-only edits (see
    ``_refresh_layout``); the deny wording matches AuthorLayoutView's default
    ("This panel isn't for you.", the same wording the old AuthorView-based
    panel used explicitly), so it is left unset here.
    """

    def __init__(self, cog, author_id, guild, state, timeout=180):
        super().__init__(author_id, timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.state = state
        self._build()

    # -- rendering ------------------------------------------------------
    def _build(self):
        state = self.state
        container = discord.ui.Container(accent_colour=random_colour())

        role_id = state["autorole"]
        autorole_value = (
            f"🟢 <@&{role_id}>" if role_id else "🔴 " + _("Not set up")
        )

        starboard = state["starboard"]
        if starboard is _UNKNOWN:
            starboard_value = "❔ " + _("Unknown")
        elif starboard is None:
            starboard_value = "🔴 " + _("Not set up")
        else:
            channel_id, threshold = starboard
            starboard_value = f"🟢 <#{channel_id}> · {threshold} ⭐"

        automod = state["automod"]
        if automod is _UNKNOWN:
            automod_value = "❔ " + _("Unknown")
        elif automod is None:
            automod_value = "🔴 " + _("Not set up")
        else:
            antilink, antispam = automod
            automod_value = (
                _("Anti-link")
                + f" {'🟢' if antilink else '🔴'} · "
                + _("Anti-spam")
                + f" {'🟢' if antispam else '🔴'}"
            )

        modlog = state["modlog"]
        if modlog is _UNKNOWN:
            modlog_value = "❔ " + _("Unknown")
        elif modlog:
            modlog_value = f"🟢 <#{modlog}>"
        else:
            modlog_value = "🔴 " + _("Not set up")

        welcome = state["welcome"]
        if welcome is _UNKNOWN:
            welcome_value = "❔ " + _("Unknown")
        elif welcome:
            welcome_value = f"🟢 <#{welcome}>"
        else:
            welcome_value = "🔴 " + _("Not set up")

        field_lines = [
            "**💬 {label}:** `{value}`".format(
                label=_("Prefix"), value=state["prefix"]
            ),
            "**📈 {label}:** {value}".format(
                label=_("Leveling"), value=_onoff(state["leveling"])
            ),
            "**🎭 {label}:** {value}".format(
                label=_("Auto-role"), value=autorole_value
            ),
            "**⭐ {label}:** {value}".format(
                label=_("Starboard"), value=starboard_value
            ),
            "**🛡️ {label}:** {value}".format(
                label=_("AutoMod"), value=automod_value
            ),
            "**📝 {label}:** {value}".format(
                label=_("Mod-log"), value=modlog_value
            ),
            "**👋 {label}:** {value}".format(
                label=_("Welcome"), value=welcome_value
            ),
        ]

        header_text = discord.ui.TextDisplay(
            "### ⚙️ "
            + _("Configuration")
            + f" · {self.guild.name}"
            + "\n"
            + _(
                "An overview of every feature for this server. Use the menu "
                "below to toggle **Leveling** or jump to a feature's setup "
                "command."
            )
            + " "
            + _("Full leveling setup lives in `/levelconfig`.")
            + "\n\n"
            + "\n".join(field_lines)
        )

        icon = getattr(self.guild, "icon", None)
        if icon is not None:
            container.add_item(
                discord.ui.Section(
                    header_text, accessory=discord.ui.Thumbnail(icon.url)
                )
            )
        else:
            container.add_item(header_text)

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(ConfigSelect(self)))
        container.add_item(
            discord.ui.TextDisplay(
                "-# "
                + _("Only you can use these controls")
                + " · "
                + _("times out after 3 min")
            )
        )
        self.add_item(container)

    async def _rerender(self, interaction):
        """Rebuild from current state so the select's labels stay accurate."""

        new = ConfigPanel(
            self.cog, self.author_id, self.guild, dict(self.state)
        )
        new.message = self.message
        self.stop()
        await _refresh_layout(interaction, self.message, new)

    # -- callbacks ------------------------------------------------------
    async def handle_select(self, interaction, key):
        if key == "leveling":
            await self._toggle_leveling(interaction)
            return

        guidance = _(
            GUIDANCE.get(
                key, N_("Use the matching command to configure this feature.")
            )
        )
        try:
            # Re-render first so the dropdown resets, then send the tip.
            await self._rerender(interaction)
            await interaction.followup.send(guidance, ephemeral=True)
        except Exception:
            log.exception("Config panel guidance failed")
            await notify_failure(interaction)

    async def _toggle_leveling(self, interaction):
        try:
            current = self.state.get("leveling")
            new_value = True if current is _UNKNOWN else not bool(current)
            await self.cog._set_leveling_enabled(self.guild.id, new_value)
            self.state["leveling"] = new_value
            await self._rerender(interaction)
        except Exception:
            log.exception("Config panel leveling toggle failed")
            await notify_failure(interaction)


class Settings(commands.Cog):
    """Server configuration: prefix, auto-role, and the feature panel."""

    def __init__(self, bot):
        self.bot = bot

    async def _set_leveling_enabled(self, guild_id, enabled):
        """Persist a leveling on/off toggle through the Leveling cog.

        The Leveling cog owns the level_config table and the hot-path config cache,
        so the toggle is delegated to it in one call: it writes the row (the new
        source of truth) AND refreshes the in-memory map so the change applies on
        the very next message, no restart. Cross-cog by name is the house seam (see
        the many bot.get_cog call sites); if the Leveling cog is not loaded there is
        nothing to level, so this is a guarded no-op.
        """
        cog = self.bot.get_cog("Leveling")
        if cog is not None:
            await cog.set_enabled(guild_id, bool(enabled))

    @commands.hybrid_group()
    @commands.guild_only()
    async def prefix(self, ctx):
        """Manage the command prefix: set or view it."""


        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @prefix.command(name="set")
    @commands.cooldown(1.0, 15.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(prefix="The new command prefix (max 10 characters).")
    async def set_prefix(self, ctx, prefix: str):
        """Set the command prefix for your guild."""

        # Reject a blank or whitespace-only prefix (which would break parsing)
        # and cap the length; a trailing space is kept on purpose (e.g. "y ").
        if not prefix.strip():
            return await ctx.send(_("The prefix can't be blank."))
        if len(prefix) > 10:
            return await ctx.send(
                _("The prefix can't be longer than 10 characters.")
            )

        await db.upsert_guild_value(
            self.bot.db_pool, "prefixes", "prefix", ctx.guild.id, prefix
        )
        self.bot.prefixes[ctx.guild.id] = prefix
        embed = discord.Embed(
            title=_("Server prefix"), colour=random_colour()
        )
        embed.add_field(name=_("Prefix has been set to:"), value=f"`{prefix}`")
        await ctx.send(embed=embed)

    @prefix.command(name="current", aliases=["list", "info"])
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def list_prefix(self, ctx):
        """List the available prefixes for your guild."""

        query = """

            SELECT prefix FROM prefixes
            WHERE guild_id = $1;

            """

        # The DB only stores *custom* prefixes now, so an unconfigured guild
        # gets None back - fall back to the bot-wide default for display.
        prefix = (
            await self.bot.db_pool.fetchval(query, ctx.guild.id)
        ) or self.bot.default_prefix
        embed = discord.Embed(
            title=_("Server prefix"), colour=random_colour()
        )
        embed.add_field(name=_("Current server prefix"), value=f"`{prefix}`")
        await ctx.send(embed=embed)

    @commands.hybrid_group(aliases=["auto-role"])
    @commands.guild_only()
    async def autorole(self, ctx):
        """Manage the auto-role: set, remove, or view it."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @autorole.command(name="set")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.describe(role="The role to grant new members automatically.")
    async def autorole_set(self, ctx, role: discord.Role):
        """Assign an auto-role to your guild."""

        await db.upsert_guild_value(
            self.bot.db_pool, "autorole", "role_id", ctx.guild.id, role.id
        )
        self.bot.autoroles[ctx.guild.id] = role.id
        embed = discord.Embed(
            title=_("Auto-role role"), colour=random_colour()
        )
        embed.add_field(
            name=_("Auto-role has been set to:"), value=f"<@&{role.id}>"
        )
        await ctx.send(embed=embed)

    @autorole.command(name="remove")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_rm(self, ctx):
        """Remove the auto-role from your guild."""

        query = """DELETE FROM autorole WHERE guild_id = $1 ;"""

        try:
            await self.bot.db_pool.execute(query, ctx.guild.id)
            self.bot.autoroles.pop(ctx.guild.id, None)
            embed = discord.Embed(
                title=_("Auto-role"), colour=random_colour()
            )
            embed.add_field(
                name=_("Auto-role has been remove from the guild"), value="​"
            )
            await ctx.send(embed=embed)

        except Exception:
            log.exception("Failed to remove auto-role")

    @autorole.command(name="info", aliases=["current"])
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_info(self, ctx):
        """Show the auto-role for your guild."""

        query = """

            SELECT role_id FROM autorole
            WHERE guild_id = $1;

            """

        role = await self.bot.db_pool.fetchval(query, ctx.guild.id)

        if role is not None:

            embed = discord.Embed(
                title=_("Auto-role"), colour=random_colour()
            )
            embed.add_field(name=_("Current auto-role"), value=f"<@&{role}>")
            await ctx.send(embed=embed)

        else:
            embed = discord.Embed(
                title=_("Auto-role"), colour=random_colour()
            )
            embed.add_field(name=_("Current auto-role"), value="`None`")
            await ctx.send(embed=embed)

    # -- config panel ---------------------------------------------------
    async def _config_state(self, guild):
        """Collect every feature's current state, each lookup guarded."""

        pool = self.bot.db_pool
        gid = guild.id
        state = {}

        state["prefix"] = self.bot.prefixes.get(gid) or self.bot.default_prefix
        state["autorole"] = self.bot.autoroles.get(gid)

        try:
            # Read the authoritative in-memory answer from the Leveling cog, which
            # already resolved level_config with the legacy JSONB fallback at load;
            # _UNKNOWN if that cog is not loaded (nothing to level).
            leveling_cog = self.bot.get_cog("Leveling")
            state["leveling"] = (
                leveling_cog.is_enabled(gid)
                if leveling_cog is not None
                else _UNKNOWN
            )
        except Exception:
            log.exception("Config panel: failed to read leveling setting")
            state["leveling"] = _UNKNOWN

        try:
            row = await pool.fetchrow(
                "SELECT channel_id, threshold FROM starboard "
                "WHERE guild_id = $1",
                gid,
            )
            state["starboard"] = (
                (row["channel_id"], row["threshold"]) if row else None
            )
        except Exception:
            log.exception("Config panel: failed to read starboard config")
            state["starboard"] = _UNKNOWN

        try:
            row = await pool.fetchrow(
                "SELECT antilink, antispam FROM automod WHERE guild_id = $1",
                gid,
            )
            state["automod"] = (
                (bool(row["antilink"]), bool(row["antispam"]))
                if row is not None
                else None
            )
        except Exception:
            log.exception("Config panel: failed to read automod config")
            state["automod"] = _UNKNOWN

        try:
            state["modlog"] = await pool.fetchval(
                "SELECT channel_id FROM modlog WHERE guild_id = $1", gid
            )
        except Exception:
            log.exception("Config panel: failed to read modlog config")
            state["modlog"] = _UNKNOWN

        try:
            # Welcome stores its state as a JSONB blob via settings.set_guild;
            # the legacy ``welcome`` table is no longer written, so read the blob
            # the same way the Welcome cog does (matches starboard/automod/modlog).
            blob = await settings.get_guild(pool, gid, "welcome", None)
            state["welcome"] = (
                blob.get("channel_id") if blob and blob.get("enabled") else None
            )
        except Exception:
            log.exception("Config panel: failed to read welcome config")
            state["welcome"] = _UNKNOWN

        return state

    @commands.hybrid_group(name="config", aliases=["setup"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config(self, ctx):
        """Open the interactive server-settings panel (also: setup)."""

        if ctx.invoked_subcommand is not None:
            return

        state = await self._config_state(ctx.guild)
        view = ConfigPanel(self, ctx.author.id, ctx.guild, state)
        view.message = await ctx.send(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
