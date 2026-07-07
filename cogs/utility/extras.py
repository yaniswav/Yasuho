import logging
import secrets
import string
import unicodedata

import discord
from discord.ext import commands

from tools.formats import random_colour
from tools.i18n import _
from tools.time import human_timedelta

log = logging.getLogger(__name__)


class Extras(commands.Cog):
    """Miscellaneous utility commands that need no database or external services."""

    def __init__(self, bot):
        self.bot = bot
        self.start = discord.utils.utcnow()

    @commands.command()
    async def quote(self, ctx, message: discord.Message):
        """Quote a message into a clean embed."""

        # The Message converter resolves any message the BOT can see; only let
        # the invoker quote one from a channel THEY can read, in this server.
        if ctx.guild is None or message.guild != ctx.guild:
            return await ctx.send(_("I can only quote messages from this server."))
        if not message.channel.permissions_for(ctx.author).read_messages:
            return await ctx.send(
                _("You can't quote a message from a channel you can't see.")
            )

        embed = discord.Embed(
            description=message.content or _("(no text)"),
            colour=random_colour(),
            timestamp=message.created_at,
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(
            name=_("Jump"),
            value=_("[Jump]({url})").format(url=message.jump_url),
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def charinfo(self, ctx, *, characters: str):
        """Show unicode information about the given characters."""

        def to_string(c):
            return f"`\\U{ord(c):08X}` {unicodedata.name(c, 'unknown')} - {c}"

        msg = "\n".join(map(to_string, characters))
        if len(msg) > 1996:
            msg = msg[:1996]

        await ctx.send(f">>> {msg}")

    @commands.command()
    async def password(self, ctx, length: int = 16):
        """Generate a random password and send it to you in DMs."""

        length = max(8, min(length, 64))
        alphabet = string.ascii_letters + string.digits + string.punctuation
        secret = "".join(secrets.choice(alphabet) for _ in range(length))

        try:
            await ctx.author.send(
                _("Here is your password:\n||`{secret}`||").format(secret=secret)
            )
        except discord.Forbidden:
            if ctx.interaction is not None:
                return await ctx.send(
                    _(
                        "I could not DM you, here it is instead:\n||`{secret}`||"
                    ).format(secret=secret),
                    ephemeral=True,
                )
            return await ctx.send(
                _("I could not DM you - please enable direct messages and try again.")
            )
        except Exception:
            log.exception("Failed to DM password")
            return await ctx.send(
                _("Something went wrong while generating your password."),
                ephemeral=True,
            )

        await ctx.send(_("Sent you a DM"), ephemeral=True)

    @commands.command()
    @commands.guild_only()
    async def spotify(self, ctx, member: discord.Member = None):
        """Show what a member is currently listening to on Spotify."""

        member = member or ctx.author

        activity = None
        for act in member.activities:
            if isinstance(act, discord.Spotify):
                activity = act
                break

        if activity is None:
            return await ctx.send(
                _("{member} is not listening to Spotify.").format(
                    member=member.display_name
                )
            )

        embed = discord.Embed(
            title=activity.title,
            colour=random_colour(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name=_("Artist"), value=", ".join(activity.artists))
        embed.add_field(name=_("Album"), value=activity.album)
        _mins, _secs = divmod(int(activity.duration.total_seconds()), 60)
        embed.add_field(name=_("Duration"), value=f"{_mins}:{_secs:02d}")
        embed.set_thumbnail(url=activity.album_cover_url)
        embed.set_author(
            name=member.display_name, icon_url=member.display_avatar.url
        )
        await ctx.send(embed=embed)

    @commands.command()
    async def uptime(self, ctx):
        """Show how long the bot has been running."""

        embed = discord.Embed(
            title=_("Uptime"),
            description=human_timedelta(self.start, suffix=False),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    async def permissions(self, ctx, member: discord.Member = None):
        """List the channel permissions a member currently has."""

        member = member or ctx.author
        perms = ctx.channel.permissions_for(member)
        allowed = [name for name, value in perms if value]

        embed = discord.Embed(
            title=_("Permissions for {member}").format(member=member.display_name),
            description="```\n" + "\n".join(allowed) + "\n```",
            colour=random_colour(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    async def botpermissions(self, ctx):
        """List the channel permissions the bot currently has."""

        perms = ctx.channel.permissions_for(ctx.me)
        allowed = [name for name, value in perms if value]

        embed = discord.Embed(
            title=_("Permissions for {member}").format(member=ctx.me.display_name),
            description="```\n" + "\n".join(allowed) + "\n```",
            colour=random_colour(),
        )
        embed.set_thumbnail(url=ctx.me.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(aliases=["botinvite"])
    async def invite(self, ctx):
        """Get an invite link to add the bot to your server."""

        # Request exactly what Yasuho's features use, never Administrator (a
        # blanket admin invite is a trust red flag and far more than she needs).
        perms = discord.Permissions(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            add_reactions=True,
            external_emojis=True,
            use_application_commands=True,
            manage_messages=True,
            manage_roles=True,
            manage_channels=True,
            manage_nicknames=True,
            kick_members=True,
            ban_members=True,
            moderate_members=True,
            manage_guild=True,
            connect=True,
            speak=True,
            move_members=True,
        )
        url = discord.utils.oauth_url(self.bot.user.id, permissions=perms)
        embed = discord.Embed(
            title=_("Invite me"),
            description=_("[Click here to invite me]({url})").format(url=url),
            colour=random_colour(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Extras(bot))
