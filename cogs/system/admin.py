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

from tools import backup
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# Repo root: this file is cogs/system/admin.py, so three levels up.
REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKUPS_DIR = os.path.join(REPO_DIR, "backups")


class UpdateSelect(discord.ui.Select):
    """Pick which (changed) cogs to reload; all are pre-selected by default."""

    def __init__(self, cogs):
        options = [
            discord.SelectOption(label=c[:100], value=c, default=True)
            for c in cogs[:25]
        ]
        super().__init__(
            placeholder=_("Cogs to reload"),
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction):
        try:
            self.view.selected = set(self.values)
            await interaction.response.defer()
        except Exception:
            log.exception("update select failed")


class UpdateView(AuthorView):
    """Owner panel to choose what to hot-reload after a pull."""

    def __init__(self, bot, author_id, cogs, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This panel isn't for you."
        )
        self.bot = bot
        self.selected = set(cogs)
        if cogs:
            self.add_item(UpdateSelect(cogs))

    async def _reload(self, interaction, exts):
        if not exts:
            return await interaction.response.send_message(
                _("Nothing selected."), ephemeral=True
            )
        ok, fail = [], []
        for e in exts:
            try:
                await self.bot.reload_extension(e)
                ok.append(e)
            except commands.ExtensionError as err:
                log.exception("update: reload failed for %s", e)
                fail.append(f"{e} ({type(err).__name__})")
        lines = []
        if ok:
            lines.append(
                _("Reloaded: {items}").format(items=", ".join(f"`{e}`" for e in ok))
            )
        if fail:
            lines.append(
                _("Failed: {items}").format(items=", ".join(f"`{e}`" for e in fail))
            )
        await interaction.response.send_message(
            "\n".join(lines) or _("Nothing reloaded."), ephemeral=True
        )

    @discord.ui.button(
        label="Reload selected", style=discord.ButtonStyle.success, emoji="🔄", row=1
    )
    async def reload_selected(self, interaction, button):
        try:
            await self._reload(interaction, sorted(self.selected))
        except Exception:
            log.exception("update reload_selected failed")

    @discord.ui.button(
        label="Reload everything", style=discord.ButtonStyle.secondary, emoji="♻️", row=1
    )
    async def reload_all(self, interaction, button):
        try:
            await self._reload(interaction, list(self.bot.extensions))
        except Exception:
            log.exception("update reload_all failed")

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.danger, emoji="✖️", row=1
    )
    async def close(self, interaction, button):
        try:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()
        except Exception:
            log.exception("update close failed")


class Admin(commands.Cog):
    """Owner-only administrative commands (sync, eval, extension management)."""

    def __init__(self, bot):
        self.bot = bot
        self._last_result = None

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
    async def i18ndump(self, ctx):
        """Dump live slash command descriptions/choices for i18n extraction.

        These are the exact strings Discord shows in the command picker. Run this
        on the host (the full command tree must be loaded), commit the generated
        locales/command_strings.py, then extract + translate + compile so the
        app_commands.Translator can localize the slash UI.
        """
        strings = set()
        for cmd in self.bot.tree.walk_commands():
            if getattr(cmd, "description", None):
                strings.add(str(cmd.description))
            for param in getattr(cmd, "parameters", None) or []:
                if getattr(param, "description", None):
                    strings.add(str(param.description))
                for choice in getattr(param, "choices", None) or []:
                    if getattr(choice, "name", None):
                        strings.add(str(choice.name))

        # Skip discord.py's default placeholder for a parameter with no
        # describe() (a lone fancy ellipsis): it is not a real translatable
        # string and would trip the no-fancy-typography rule in the catalogs.
        ordered = sorted(s for s in strings if s and s != "\u2026")
        path = os.path.join(REPO_DIR, "locales", "command_strings.py")
        lines = [
            '"""Auto-generated by ?i18ndump: slash command strings for i18n extraction.',
            "",
            "Do not edit by hand; re-run ?i18ndump after changing command metadata.",
            "Wrapped in N_ (a no-op marker) so pybabel collects them; the slash",
            "Translator (tools/translator.py) translates them at sync time.",
            '"""',
            "from tools.i18n import N_",
            "",
            "_COMMAND_STRINGS = [",
        ]
        for s in ordered:
            esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append(f'    N_("{esc}"),')
        lines.append("]")
        try:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("\n".join(lines) + "\n")
        except Exception:
            log.exception("Failed to write command_strings.py")
            return await ctx.send(_("Failed to write the command strings file."))
        await ctx.send(
            _("Wrote {count} command strings to locales/command_strings.py.").format(
                count=len(ordered)
            )
        )

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

            scope = _("globally") if spec is None else _("to the current guild.")
            await ctx.send(
                _("Synced {count} commands {scope}").format(
                    count=len(synced), scope=scope
                )
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

        await ctx.send(
            _("Synced the tree to {done}/{total}.").format(done=ret, total=len(guilds))
        )

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
        }

        env.update(globals())
        # Set after update(globals()) so the eval "_" (last result) is not
        # shadowed by the module-level gettext "_" pulled in from globals().
        env["_"] = self._last_result

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
                return await ctx.send(
                    _("Reloaded {count} extensions, {fail} fail.").format(
                        count=v, fail=e
                    )
                )

            await ctx.message.add_reaction("\u2705")
            await ctx.send(
                _("Reloaded {count} extensions, {fail} fail.").format(count=v, fail=e)
            )
            return

        try:
            await self.bot.reload_extension(
                self._resolve_ext(extension) or f"cogs.{extension}"
            )

        except commands.ExtensionError as e:
            log.exception("Couldn't reload extension: %s", extension)

            if ctx.interaction:
                await ctx.interaction.response.send_message(
                    _("Couldn't reload `{ext}`: {error}").format(
                        ext=extension, error=e
                    ),
                    ephemeral=True,
                )
            else:
                await ctx.send(
                    _("Couldn't reload `{ext}`: {error}").format(
                        ext=extension, error=e
                    )
                )

        else:
            log.info("Reloaded extension: %s", extension)

            if ctx.interaction:
                await ctx.interaction.response.send_message(
                    _("Reloaded `{ext}`").format(ext=extension), ephemeral=True
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
                name=_("Error!"),
                value=_("{ext} cannot be loaded! \n**[{error}]**").format(
                    ext=extension, error=error
                ),
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
                name=_("Error!"),
                value=_("{ext} cannot be unloaded! \n**[{error}]**").format(
                    ext=extension, error=error
                ),
                inline=True,
            )

            if ctx.interaction:
                return await ctx.interaction.response.send_message(
                    embed=embed, ephemeral=True
                )

            await ctx.send(embed=embed)


    @commands.command(hidden=True)
    @commands.is_owner()
    async def backup(self, ctx):
        """Take an on-demand pg_dump now, then rotate old dumps.

        Same seam as the startup backup. Owner-only; the reply names the file,
        its size and how many old dumps were rotated. On failure it replies
        briefly without leaking the DSN or any pg_dump internals.
        """
        async with ctx.typing():
            result = await backup.run_backup(
                config_loader.get("Database", "PostgreSQL"), BACKUPS_DIR
            )
        if not result.ok:
            log.warning("?backup failed: %s", result.error)
            return await ctx.send("Backup failed. Check the logs.")
        await ctx.send(
            "Backup saved: `{name}` ({size}), {deleted} old dump(s) rotated.".format(
                name=os.path.basename(result.path),
                size=backup.human_size(result.size or 0),
                deleted=result.deleted,
            )
        )

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
        """Pull the latest code, then pick which cogs to hot-reload (interactive).

        Pass a cog name to pull and reload just that one. core.py / tools changes
        are flagged as needing a restart (they cannot be hot-reloaded).
        """
        if extension is not None:
            await self._git("pull", "--ff-only")
            ext = self._resolve_ext(extension) or f"cogs.{extension}"
            try:
                await self.bot.reload_extension(ext)
                return await ctx.send(
                    _("Pulled and reloaded `{ext}`.").format(ext=ext)
                )
            except commands.ExtensionError as err:
                return await ctx.send(
                    _("Could not reload `{ext}`: {error}").format(ext=ext, error=err)
                )

        async with ctx.typing():
            before = await self._git("rev-parse", "HEAD")
            pull_out = await self._git("pull", "--ff-only")
            after = await self._git("rev-parse", "HEAD")

        moved = before != after
        changed = (
            [
                f.strip()
                for f in (
                    await self._git("diff", "--name-only", before, after)
                ).splitlines()
                if f.strip()
            ]
            if moved
            else []
        )

        cogs, restart = [], []
        for f in changed:
            if not f.endswith(".py"):
                continue
            mod = f[:-3].replace("/", ".")
            ext = next(
                (x for x in self.bot.extensions if mod == x or mod.startswith(x + ".")),
                None,
            )
            if ext and ext not in cogs:
                cogs.append(ext)
            elif not ext and not f.startswith("cogs/"):
                restart.append(f)

        embed = discord.Embed(title=_("Update"), colour=random_colour())
        if not moved:
            embed.description = (
                _("Already up to date.")
                if "up to date" in pull_out.lower()
                else _("Nothing pulled:\n```\n{output}\n```").format(
                    output=pull_out[:400]
                )
            )
        else:
            pulled = await self._git(
                "log", "--oneline", "--no-decorate", f"{before}..{after}"
            )
            embed.add_field(
                name=_("Pulled"),
                value=_("`{before}` -> `{after}`\n```\n{log}\n```").format(
                    before=before[:7],
                    after=after[:7],
                    log=(pulled or _("(no log)"))[:800],
                ),
                inline=False,
            )
        if cogs:
            embed.add_field(
                name=_("Changed cogs"),
                value="\n".join(f"`{c}`" for c in cogs),
                inline=False,
            )
        if restart:
            embed.add_field(
                name=_("Restart needed (not hot-reloadable)"),
                value="\n".join(f"`{f}`" for f in restart),
                inline=False,
            )
        embed.set_footer(
            text=_("Pick the cogs to reload below, or reload everything.")
        )

        view = UpdateView(self.bot, ctx.author.id, cogs)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(Admin(bot))
