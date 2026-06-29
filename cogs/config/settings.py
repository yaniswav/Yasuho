import logging

import discord
from discord.ext import commands

from tools import settings
from tools.formats import random_colour

log = logging.getLogger(__name__)

# Sentinel for "we tried to read this feature's state but the lookup failed".
# Distinct from None, which legitimately means "not configured".
_UNKNOWN = object()

# Features the panel can route the admin to. Leveling is toggled in-panel; the
# rest each point at their dedicated setup command.
GUIDANCE = {
    "prefix": (
        "Use `/prefix set <prefix>` to change the command prefix for this "
        "server."
    ),
    "autorole": (
        "Use `/autorole set <role>` to choose the role new members receive on "
        "join."
    ),
    "automod": (
        "Use `/automod` to open the interactive AutoMod control panel "
        "(custom + native filters)."
    ),
    "modlog": (
        "Use `/modlog` to open the interactive mod-log control panel "
        "(channel + event toggles)."
    ),
    "starboard": (
        "Use `/starboard set <channel> [threshold]` to set up the starboard."
    ),
    "welcome": (
        "Use `/welcome set <channel> <message>` to configure welcome messages. "
        "Placeholders: {user}, {server}, {count}."
    ),
}


def _onoff(value):
    """Render a tri-state boolean (True / False / unknown) as a labelled dot."""

    if value is _UNKNOWN:
        return "❔ Unknown"
    return "🟢 Enabled" if value else "🔴 Disabled"


# ----------------------------------------------------------------------
# Interactive server-settings panel (discord.ui)
# ----------------------------------------------------------------------
class ConfigSelect(discord.ui.Select):
    """Pick a feature: Leveling toggles in place, others point to a command."""

    def __init__(self, panel):
        self.panel = panel
        leveling = panel.state.get("leveling")
        if leveling is _UNKNOWN:
            lvl_desc = "Current state unknown"
        elif leveling:
            lvl_desc = "Currently enabled - select to disable"
        else:
            lvl_desc = "Currently disabled - select to enable"

        options = [
            discord.SelectOption(
                label="Leveling",
                value="leveling",
                emoji="📈",
                description=lvl_desc[:100],
            ),
            discord.SelectOption(
                label="Prefix",
                value="prefix",
                emoji="💬",
                description="Change the command prefix",
            ),
            discord.SelectOption(
                label="Auto-role",
                value="autorole",
                emoji="🎭",
                description="Role granted to new members",
            ),
            discord.SelectOption(
                label="AutoMod",
                value="automod",
                emoji="🛡️",
                description="Open the AutoMod control panel",
            ),
            discord.SelectOption(
                label="Mod-log",
                value="modlog",
                emoji="📝",
                description="Open the mod-log control panel",
            ),
            discord.SelectOption(
                label="Starboard",
                value="starboard",
                emoji="⭐",
                description="Configure the starboard",
            ),
            discord.SelectOption(
                label="Welcome",
                value="welcome",
                emoji="👋",
                description="Configure welcome messages",
            ),
        ]
        super().__init__(
            placeholder="Configure a feature...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        await self.panel.handle_select(interaction, self.values[0])


class ConfigPanel(discord.ui.View):
    """Author-restricted overview of every server feature, with quick controls."""

    def __init__(self, cog, author_id, guild, state, timeout=180):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.guild = guild
        self.state = state
        self.message = None
        self.add_item(ConfigSelect(self))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This panel isn't for you.", ephemeral=True
            )
            return False
        return True

    # -- rendering ------------------------------------------------------
    def build_embed(self):
        state = self.state
        embed = discord.Embed(
            title=f"⚙️ Configuration · {self.guild.name}"[:256],
            description=(
                "An overview of every feature for this server. Use the menu "
                "below to toggle **Leveling** or jump to a feature's setup "
                "command."
            ),
            colour=random_colour(),
        )
        icon = getattr(self.guild, "icon", None)
        if icon is not None:
            embed.set_thumbnail(url=icon.url)

        embed.add_field(name="💬 Prefix", value=f"`{state['prefix']}`", inline=True)
        embed.add_field(
            name="📈 Leveling", value=_onoff(state["leveling"]), inline=True
        )

        role_id = state["autorole"]
        embed.add_field(
            name="🎭 Auto-role",
            value=(f"🟢 <@&{role_id}>" if role_id else "🔴 Not set up"),
            inline=True,
        )

        starboard = state["starboard"]
        if starboard is _UNKNOWN:
            starboard_value = "❔ Unknown"
        elif starboard is None:
            starboard_value = "🔴 Not set up"
        else:
            channel_id, threshold = starboard
            starboard_value = f"🟢 <#{channel_id}> · {threshold} ⭐"
        embed.add_field(name="⭐ Starboard", value=starboard_value, inline=True)

        automod = state["automod"]
        if automod is _UNKNOWN:
            automod_value = "❔ Unknown"
        elif automod is None:
            automod_value = "🔴 Not set up"
        else:
            antilink, antispam = automod
            automod_value = (
                f"Anti-link {'🟢' if antilink else '🔴'} · "
                f"Anti-spam {'🟢' if antispam else '🔴'}"
            )
        embed.add_field(name="🛡️ AutoMod", value=automod_value, inline=True)

        modlog = state["modlog"]
        if modlog is _UNKNOWN:
            modlog_value = "❔ Unknown"
        elif modlog:
            modlog_value = f"🟢 <#{modlog}>"
        else:
            modlog_value = "🔴 Not set up"
        embed.add_field(name="📝 Mod-log", value=modlog_value, inline=True)

        welcome = state["welcome"]
        if welcome is _UNKNOWN:
            welcome_value = "❔ Unknown"
        elif welcome:
            welcome_value = f"🟢 <#{welcome}>"
        else:
            welcome_value = "🔴 Not set up"
        embed.add_field(name="👋 Welcome", value=welcome_value, inline=True)

        embed.set_footer(
            text="Only you can use these controls · times out after 3 min"
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

    async def _error(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong.", ephemeral=True
                )
        except discord.HTTPException:
            pass

    # -- callbacks ------------------------------------------------------
    async def handle_select(self, interaction, key):
        if key == "leveling":
            await self._toggle_leveling(interaction)
            return

        guidance = GUIDANCE.get(
            key, "Use the matching command to configure this feature."
        )
        try:
            # Re-render first so the dropdown resets, then send the tip.
            await self._rerender(interaction)
            await interaction.followup.send(guidance, ephemeral=True)
        except Exception:
            log.exception("Config panel guidance failed")
            await self._error(interaction)

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
            await self._error(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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

        query = """
            INSERT INTO prefixes
            (guild_id, prefix)
            VALUES
            ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET prefix = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, prefix, prefix)
        self.bot.prefixes[ctx.guild.id] = prefix
        embed = discord.Embed(
            title="Server prefix", colour=random_colour()
        )
        embed.add_field(name="Prefix has been set to:", value=f"`{prefix}`")
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
            title="Server prefix", colour=random_colour()
        )
        embed.add_field(name="Current server prefix", value=f"`{prefix}`")
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

        query = """
            INSERT INTO autorole
            (guild_id, role_id)
            VALUES
            ($1, $2) ON CONFLICT (guild_id) DO UPDATE SET role_id = $3;
            """

        await self.bot.db_pool.execute(query, ctx.guild.id, role.id, role.id)
        self.bot.autoroles[ctx.guild.id] = role.id
        embed = discord.Embed(
            title="Auto-role role", colour=random_colour()
        )
        embed.add_field(name="Auto-role has been set to:", value=f"<@&{role.id}>")
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
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(
                name="Auto-role has been remove from the guild", value="​"
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
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(name="Current auto-role", value=f"<@&{role}>")
            await ctx.send(embed=embed)

        else:
            embed = discord.Embed(
                title="Auto-role", colour=random_colour()
            )
            embed.add_field(name="Current auto-role", value="`None`")
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
            state["welcome"] = await pool.fetchval(
                "SELECT channel_id FROM welcome WHERE guild_id = $1", gid
            )
        except Exception:
            log.exception("Config panel: failed to read welcome config")
            state["welcome"] = _UNKNOWN

        return state

    @commands.hybrid_group(name="config")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def config(self, ctx):
        """Open the interactive server-settings panel."""

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
            title="Leveling",
            description=(
                f"Leveling {'enabled' if mode else 'disabled'} for this server."
            ),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
