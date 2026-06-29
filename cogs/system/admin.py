import asyncio
import io
import logging
import os
import shlex
import subprocess
import textwrap
import traceback
from contextlib import redirect_stdout
from typing import Literal, Optional

import discord
from discord.ext import commands

from tools.formats import random_colour

log = logging.getLogger(__name__)

# Repo root: this file is cogs/system/admin.py, so three levels up.
REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


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
            if ctx.interaction:
                await ctx.interaction.response.defer(ephemeral=True)

            v, e = 0, 0
            for ext in list(self.bot.extensions):
                try:
                    await self.bot.reload_extension(ext)
                    log.info("Reloaded extension: %s", ext)
                    v += 1
                except commands.ExtensionError:
                    log.exception("Couldn't reload extension: %s", ext)
                    e += 1

            if ctx.interaction:
                return await ctx.send(f"Reloaded {v} extensions, {e} fail.")

            await ctx.message.add_reaction("\u2705")
            await ctx.send(f"Reloaded {v} extensions, {e} fail.")
            return

        try:
            await self.bot.reload_extension(
                self._resolve_ext(extension) or f"cogs.{extension}"
            )

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
            await self.bot.unload_extension(
                self._resolve_ext(extension) or f"cogs.{extension}"
            )
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


    def _resolve_ext(self, name):
        """Resolve a short cog name to a currently-loaded extension path."""
        if name in self.bot.extensions:
            return name
        if f"cogs.{name}" in self.bot.extensions:
            return f"cogs.{name}"
        matches = [
            e
            for e in self.bot.extensions
            if e == name or e.endswith("." + name) or e.split(".")[-1] == name
        ]
        return matches[0] if len(matches) == 1 else None

    async def _git(self, *args):
        """Run a git command in the repo and return its combined output."""
        cmd = "git -C " + shlex.quote(REPO_DIR) + " " + " ".join(
            shlex.quote(a) for a in args
        )
        out, err = await self.run_process(cmd)
        return (out + err).strip()

    @commands.command(hidden=True, name="update", aliases=["pull"])
    @commands.is_owner()
    async def update(self, ctx, extension: str = None):
        """Pull the latest code from the remote and hot-reload the changed cogs.

        With no argument, reloads exactly the cogs whose files changed. Pass a
        cog name to reload only that one. core.py / tools changes need a restart.
        """
        async with ctx.typing():
            before = await self._git("rev-parse", "HEAD")
            pull_out = await self._git("pull", "--ff-only")
            after = await self._git("rev-parse", "HEAD")

        if before == after:
            if "up to date" in pull_out.lower():
                return await ctx.send("Already up to date - nothing to reload.")
            return await ctx.send(f"Nothing pulled:\n```\n{pull_out[:1500]}\n```")

        changed = [
            f.strip()
            for f in (await self._git("diff", "--name-only", before, after)).splitlines()
            if f.strip()
        ]
        pulled = await self._git("log", "--oneline", "--no-decorate", f"{before}..{after}")

        embed = discord.Embed(title="Update", colour=random_colour())
        embed.add_field(
            name="Pulled",
            value=f"`{before[:7]}` -> `{after[:7]}`\n```\n{(pulled or '(no log)')[:900]}\n```",
            inline=False,
        )

        if extension:
            targets, restart = {self._resolve_ext(extension) or f"cogs.{extension}"}, []
        else:
            targets, restart = set(), []
            for f in changed:
                if not f.endswith(".py"):
                    continue
                mod = f[:-3].replace("/", ".")
                ext = next(
                    (x for x in self.bot.extensions if mod == x or mod.startswith(x + ".")),
                    None,
                )
                if ext:
                    targets.add(ext)
                elif not f.startswith("cogs/"):
                    restart.append(f)

        ok, fail = [], []
        for ext in sorted(targets):
            try:
                await self.bot.reload_extension(ext)
                ok.append(ext)
            except commands.ExtensionError as err:
                log.exception("update: reload failed for %s", ext)
                fail.append(f"{ext} ({type(err).__name__})")

        if ok:
            embed.add_field(
                name="Reloaded", value="\n".join(f"`{e}`" for e in ok), inline=False
            )
        if fail:
            embed.add_field(
                name="Failed", value="\n".join(f"`{e}`" for e in fail), inline=False
            )
        if restart:
            embed.add_field(
                name="Restart needed (not hot-reloadable)",
                value="\n".join(f"`{f}`" for f in restart),
                inline=False,
            )
        if not (ok or fail or restart):
            embed.add_field(name="Reloaded", value="No cog files changed.", inline=False)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Admin(bot))
