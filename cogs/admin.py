import asyncio
import io
import logging
import subprocess
import textwrap
import traceback
from contextlib import redirect_stdout
from typing import Literal, Optional

import discord
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)


class Admin(commands.Cog):
    """Owner-only administrative commands (sync, eval, extension management)."""

    def __init__(self, bot):
        self.bot = bot
        self._last_result = None
        self.sessions = set()

    async def run_process(self, command: str) -> list[str]:
        try:
            process = await asyncio.create_subprocess_shell(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            result = await process.communicate()
        except NotImplementedError:
            process = subprocess.Popen(
                command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            result = await self.bot.loop.run_in_executor(None, process.communicate)

        return [output.decode() for output in result]

    def cleanup_code(self, content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith("```") and content.endswith("```"):
            return "\n".join(content.split("\n")[1:-1])

        # remove `foo`
        return content.strip("` \n")

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    def get_syntax_error(self, e: SyntaxError) -> str:
        if e.text is None:
            return f"```py\n{e.__class__.__name__}: {e}\n```"
        return f'```py\n{e.text}{"^":>{e.offset}}\n{e.__class__.__name__}: {e}```'

    @commands.command(hidden=True)
    @commands.is_owner()
    @commands.guild_only()
    async def sync(
        self,
        ctx,
        guilds: commands.Greedy[discord.Object],
        spec: Optional[Literal["~", "*", "^"]] = None,
    ) -> None:
        """Synchronizes the application command tree globally or to guilds."""
        if not guilds:
            if spec == "~":
                synced = await self.bot.tree.sync(guild=ctx.guild)
            elif spec == "*":
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
            elif spec == "^":
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                synced = []
            else:
                synced = await self.bot.tree.sync()

            await ctx.send(
                f"Synced {len(synced)} commands {'globally' if spec is None else 'to the current guild.'}"
            )
            return

        ret = 0
        for guild in guilds:
            try:
                await self.bot.tree.sync(guild=guild)
            except discord.HTTPException:
                pass
            else:
                ret += 1

        await ctx.send(f"Synced the tree to {ret}/{len(guilds)}.")

    @commands.command(hidden=True, name="eval")
    @commands.is_owner()
    async def _eval(self, ctx: commands.Context, *, body: str):
        """Evaluates a code"""

        env = {
            "bot": self.bot,
            "ctx": ctx,
            "channel": ctx.channel,
            "author": ctx.author,
            "guild": ctx.guild,
            "message": ctx.message,
            "_": self._last_result,
        }

        env.update(globals())

        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'

        try:
            exec(to_compile, env)
        except Exception as e:
            return await ctx.send(f"```py\n{e.__class__.__name__}: {e}\n```")

        func = env["func"]
        try:
            with redirect_stdout(stdout):
                ret = await func()
        except Exception:
            value = stdout.getvalue()
            await ctx.send(f"```py\n{value}{traceback.format_exc()}\n```")
        else:
            value = stdout.getvalue()
            try:
                await ctx.message.add_reaction("\u2705")
            except Exception:
                log.exception("Failed to add success reaction")

            if ret is None:
                if value:
                    await ctx.send(f"```py\n{value}\n```")
            else:
                self._last_result = ret
                await ctx.send(f"```py\n{value}{ret}\n```")

    @commands.hybrid_command(hidden=True, name="reload", aliases=["rl"])
    @commands.is_owner()
    async def reload(self, ctx, extension=None):
        """Reloads a single extension, or all configured extensions."""
        if extension is None:
            v, e = 0, 0
            for ext in config_loader.getlist("Extension", "Extensions"):
                try:
                    await self.bot.reload_extension(f"{ext}")
                    log.info("Reloaded extension: %s", ext)
                    v += 1
                except commands.ExtensionError:
                    log.exception("Couldn't reload extension: %s", ext)
                    e += 1

            if ctx.interaction:
                return await ctx.interaction.response.send_message(
                    f"Reloaded {v} extensions, {e} fail.", ephemeral=True
                )

            await ctx.message.add_reaction("\u2705")
            await ctx.send(f"Reloaded {v} extensions, {e} fail.")
            return

        try:
            await self.bot.reload_extension(f"cogs.{extension}")

        except commands.ExtensionError as e:
            log.exception("Couldn't reload extension: %s", extension)

            if ctx.interaction:
                await ctx.interaction.response.send_message(
                    f"Couldn't reload `{extension}`: {e}", ephemeral=True
                )
            else:
                await ctx.send(f"Couldn't reload `{extension}`: {e}")

        else:
            log.info("Reloaded extension: %s", extension)

            if ctx.interaction:
                await ctx.interaction.response.send_message(
                    f"Reloaded `{extension}`", ephemeral=True
                )
                return

            await ctx.message.add_reaction("\u2705")

    @commands.command(hidden=True)
    @commands.is_owner()
    async def load(self, ctx, extension):
        """Loads an extension by name."""
        try:
            await self.bot.load_extension(f"cogs.{extension}")
            await ctx.message.add_reaction("\u2705")

        except Exception as error:
            embed = discord.Embed(color=random_colour())
            embed.add_field(
                name="Error!",
                value=f"{extension} cannot be loaded! \n**[{error}]**",
                inline=True,
            )

            if ctx.interaction:
                return await ctx.interaction.response.send_message(
                    embed=embed, ephemeral=True
                )

            await ctx.send(embed=embed)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def unload(self, ctx, extension):
        """Unloads an extension by name."""
        try:
            await self.bot.unload_extension(f"cogs.{extension}")
            await ctx.message.add_reaction("\u2705")

        except Exception as error:
            embed = discord.Embed(color=random_colour())
            embed.add_field(
                name="Error!",
                value=f"{extension} cannot be unloaded! \n**[{error}]**",
                inline=True,
            )

            if ctx.interaction:
                return await ctx.interaction.response.send_message(
                    embed=embed, ephemeral=True
                )

            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Admin(bot))
