import logging

import discord
from discord.ext import commands

from tools import db, settings
from tools.embed_creator import notify_failure
from tools.formats import random_colour
from tools.i18n import N_, _
from tools.views import AuthorView

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
# Interactive server-settings panel (discord.ui)
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
            row=0,
        )

    async def callback(self, interaction):
        await self.panel.handle_select(interaction, self.values[0])


class ConfigPanel(AuthorView):
    """Author-restricted overview of every server feature, with quick controls."""

    def __init__(self, cog, author_id, guild, state, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.cog = cog
        self.guild = guild
        self.state = state
        self.add_item(ConfigSelect(self))

    # -- rendering ------------------------------------------------------
    def build_embed(self):
        state = self.state
        embed = discord.Embed(
            title=("⚙️ " + _("Configuration") + f" · {self.guild.name}")[:256],
            description=_(
                "An overview of every feature for this server. Use the menu "
                "below to toggle **Leveling** or jump to a feature's setup "
                "command."
            ),
            colour=random_colour(),
        )
        icon = getattr(self.guild, "icon", None)
        if icon is not None:
            embed.set_thumbnail(url=icon.url)

        embed.add_field(
            name="💬 " + _("Prefix"),
            value=f"`{state['prefix']}`",
            inline=True,
        )
        embed.add_field(
            name="📈 " + _("Leveling"),
            value=_onoff(state["leveling"]),
            inline=True,
        )

        role_id = state["autorole"]
        embed.add_field(
            name="🎭 " + _("Auto-role"),
            value=(f"🟢 <@&{role_id}>" if role_id else "🔴 " + _("Not set up")),
            inline=True,
        )

        starboard = state["starboard"]
        if starboard is _UNKNOWN:
            starboard_value = "❔ " + _("Unknown")
        elif starboard is None:
            starboard_value = "🔴 " + _("Not set up")
        else:
            channel_id, threshold = starboard
            starboard_value = f"🟢 <#{channel_id}> · {threshold} ⭐"
        embed.add_field(
            name="⭐ " + _("Starboard"), value=starboard_value, inline=True
        )

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
        embed.add_field(
            name="🛡️ " + _("AutoMod"), value=automod_value, inline=True
        )

        modlog = state["modlog"]
        if modlog is _UNKNOWN:
            modlog_value = "❔ " + _("Unknown")
        elif modlog:
            modlog_value = f"🟢 <#{modlog}>"
        else:
            modlog_value = "🔴 " + _("Not set up")
        embed.add_field(
            name="📝 " + _("Mod-log"), value=modlog_value, inline=True
        )

        welcome = state["welcome"]
        if welcome is _UNKNOWN:
            welcome_value = "❔ " + _("Unknown")
        elif welcome:
            welcome_value = f"🟢 <#{welcome}>"
        else:
            welcome_value = "🔴 " + _("Not set up")
        embed.add_field(
            name="👋 " + _("Welcome"), value=welcome_value, inline=True
        )

        embed.set_footer(
            text=(
                _("Only you can use these controls")
                + " · "
                + _("times out after 3 min")
            )
        )
        return embed

    async def _rerender(self, interaction):
        """Rebuild from current state so the select's labels stay accurate."""

        new = ConfigPanel(
            self.cog, self.author_id, self.guild, dict(self.state)
        )
        new.message = self.message
        self.stop()
        await interaction.response.edit_message(
            embed=new.build_embed(), view=new
        )

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
            await settings.set_guild(
                self.cog.bot.db_pool,
                self.guild.id,
                "leveling_enabled",
                new_value,
            )
            self.state["leveling"] = new_value
            await self._rerender(interaction)
        except Exception:
            log.exception("Config panel leveling toggle failed")
            await notify_failure(interaction)


class Settings(commands.Cog):
    """Server configuration: prefix, auto-role, and the feature panel."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group()
    @commands.guild_only()
    async def prefix(self, ctx):
        """Prefix related commands."""


        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @prefix.command(name="set")
    @commands.cooldown(1.0, 15.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def set_prefix(self, ctx, prefix: str):
        """Assign a Prefix to Yasuho for use in your guild."""

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
        """Auto-role related commands."""

        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @autorole.command(name="set")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def autorole_set(self, ctx, role: discord.Role):
        """Assign an auto role to your guild."""

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
        """Remove auto role from your guild."""

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
        """Auto-role of your guild."""

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
            state["leveling"] = bool(
                await settings.get_guild(pool, gid, "leveling_enabled", False)
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
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @config.command(name="leveling")
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    @commands.has_permissions(manage_guild=True)
    async def config_leveling(self, ctx, mode: bool):
        """Enable or disable the leveling system for this server."""

        await settings.set_guild(
            self.bot.db_pool, ctx.guild.id, "leveling_enabled", mode
        )
        embed = discord.Embed(
            title=_("Leveling"),
            description=(
                _("Leveling enabled for this server.")
                if mode
                else _("Leveling disabled for this server.")
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
