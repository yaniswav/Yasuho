import asyncio
import io
import logging
import random
import re

import discord
from discord.ext import commands
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageSequence
from pyfiglet import figlet_format

from tools import interactions, rendering
from tools.config_loader import config_loader
from tools.cooldowns import Cooldowns
from tools.formats import public_echo, random_colour
from tools.http import TIMEOUT, get_session
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Link / invite detection for ?say
# ---------------------------------------------------------------------------
# BYTE-IDENTICAL to the two grounded patterns the AutoMod engine scans every
# message with (cogs/moderation/automod.py: AutoMod.url_re, AutoMod.invite_re).
# What is a link for automod is a link here, and nothing else is: a bare
# "example.com" is not clickable in a Discord client, so it is not the
# impersonation vector this filter exists for.
#
# COPIED, NOT IMPORTED, on purpose. Every cross-package import in this repo
# reaches for a small leaf helper (cogs/system/events.py -> mute_perms,
# profile's connectors -> cogs.anilist.helpers); these two are class attributes
# of the moderation ENGINE cog, so importing them would drag that cog's whole
# import chain - its DB layer, the Components V2 panel, the warn-escalation
# ladder - behind a ?say, and would take ?say down the day that chain breaks.
# tests/cogs/test_fun.py asserts the pattern SOURCES stay byte-identical with
# automod's, so a divergence is a red test rather than a silent one.
#
# The pattern these replace was anchored ``^...$`` and consulted with
# ``re.match``, i.e. it only ever fired when the WHOLE argument was one bare
# URL. A single leading character ("hi ", ".", a space) defeated it and any
# member could publish arbitrary links UNDER YASUHO'S NAME. Both are unanchored
# and consulted with .search() below.
LINK_RE = re.compile(r"https?://\S+|discord\.gg/\S+", re.IGNORECASE)
INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord(?:\.gg|app\.com/invite|\.com/invite)|discord\.me|discord\.io)"
    r"/[\w-]+",
    re.IGNORECASE,
)


def contains_link(text) -> bool:
    """True when a link or a Discord invite appears ANYWHERE inside ``text``."""

    return bool(LINK_RE.search(str(text)) or INVITE_RE.search(str(text)))


# How long one member must let pass between two ?hug renders.
#
# A hug is ~1s of Pillow work holding one of only TWO bot-wide image slots
# (tools/rendering.py), shared with rank cards, profile cards and serverstats
# charts. The command's ``commands.cooldown(3, 90)`` bounds the member's
# VOLUME but not the BURST: a rate of 3 lets three invocations clear prepare()
# back to back, so all three renders start together, take both slots at once,
# and every other image job in the bot gets nothing but the 5s acquire timeout.
# This spacing closes exactly that, and nothing else - the volume budget above
# is unchanged.
#
# 10 seconds is ~9x one render, so two hugs from the same member can never
# overlap, while casual use is untouched: the 3-per-90s budget still lets
# someone hug three different people inside 30 seconds.
#
# ``max_concurrency(1, user)`` would have been the one-decorator version, but
# MaxConcurrencyReached matches no branch in cogs/system/errors.py and would
# answer a routine throttle with the "report this to the bot owner" embed.
HUG_SPACING = 10.0


# Rock-Paper-Scissors: which emoji beats which (key beats value).
RPS_BEATS = {"✊": "✌", "🖐": "✊", "✌": "🖐"}
RPS_CHOICES = list(RPS_BEATS)


class RPSButton(discord.ui.Button):
    """A single Rock / Paper / Scissors move."""

    def __init__(self, choice: str, label: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label, emoji=choice)
        self.choice = choice

    async def callback(self, interaction: discord.Interaction):
        view: "RPSView" = self.view
        try:
            await view.resolve(interaction, self.choice)
        except Exception:
            log.exception("rps move failed")
            await interactions.notify_failure(
                interaction, _("Something went wrong with that move.")
            )


class RPSView(AuthorView):
    """Rock-Paper-Scissors against Yasuho: three buttons, author-gated."""

    def __init__(self, player: discord.abc.User):
        super().__init__(
            player.id,
            timeout=60,
            deny_message="This isn't your game, start your own with the command!",
        )
        self.player = player
        self.add_item(RPSButton("✊", _("Rock")))
        self.add_item(RPSButton("🖐", _("Paper")))
        self.add_item(RPSButton("✌", _("Scissors")))

    def disable_all(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def resolve(self, interaction: discord.Interaction, user_choice: str):
        """Pick the bot's move, decide the outcome, and reveal it in one edit."""
        bot_choice = random.choice(RPS_CHOICES)

        result = _("Draw")
        if bot_choice != user_choice:
            result = (
                _("You won") if RPS_BEATS[user_choice] == bot_choice else _("You lost")
            )

        description = _(
            "I choose {bot_choice}\nYou choose {user_choice}\n\nResult : `{result}`"
        ).format(bot_choice=bot_choice, user_choice=user_choice, result=result)

        result_em = discord.Embed(
            color=random_colour(),
            timestamp=discord.utils.utcnow(),
            title=_("RPS Game"),
            description=description,
        )
        result_em.set_footer(text=_("Thanks for playing!"))

        self.disable_all()
        self.stop()
        await interaction.response.edit_message(embed=result_em, view=self)

    async def on_timeout(self):
        self.disable_all()
        if self.message is not None:
            timeout_em = discord.Embed(
                color=random_colour(),
                timestamp=discord.utils.utcnow(),
                title=_("RPS Game"),
                description=_("Game timed out! Please try again."),
            )
            try:
                await self.message.edit(embed=timeout_em, view=self)
            except discord.HTTPException:
                log.debug("failed to edit timed-out rps message", exc_info=True)


class Fun(commands.Cog):
    """Fun and entertainment commands."""

    def __init__(self, bot):
        self.bot = bot
        # Per-member spacing between two hug RENDERS. Self-pruning, so it can
        # never grow past roughly twice the number of members still cooling.
        self._hug_spacing = Cooldowns(HUG_SPACING)

    @property
    def hug_colour(self):
        return ImageColor.getcolor("#e94573", "L")

    @commands.command(name="hug")
    @commands.guild_only()
    @commands.cooldown(3, 90, commands.BucketType.user)
    async def give_hug(self, ctx, member: discord.Member = None):
        """Give someone a hug."""
        if not member:
            return await ctx.send(_("You can't hug the air..."))

        # Check-and-set with NO await in between, so it is atomic against the
        # member's own concurrent invocations: two ?hug messages arriving
        # together cannot both find the key inactive. Claimed BEFORE the render
        # and after the "hug the air" refusal, so a mistyped hug costs nothing.
        if self._hug_spacing.is_active(ctx.author.id):
            return await ctx.send(
                _("One hug at a time please, give me a few seconds.")
            )
        self._hug_spacing.touch(ctx.author.id)

        hug_colour = self.hug_colour
        author_name = ctx.author.display_name
        member_name = member.display_name

        def _render():
            font = ImageFont.truetype("ressources/fonts/playtime.ttf", size=20)
            im = Image.open("ressources/images/hug.gif")

            frames = []
            for frame in ImageSequence.Iterator(im):
                # Make a copy of the frame
                frame = frame.copy()

                d = ImageDraw.Draw(frame)
                d.text((30, 296), member_name, font=font, fill=hug_colour)
                d.text((300, 310), author_name, font=font, fill=hug_colour)
                del d

                # Save the modified frame into a BytesIO object
                b = io.BytesIO()
                frame.save(b, format="GIF", optimize=True)
                b.seek(0)
                frames.append(b)

            # Create the final GIF in memory
            final_gif = io.BytesIO()
            with Image.open(frames[0]) as first_frame:
                first_frame.save(
                    final_gif,
                    format="GIF",
                    save_all=True,
                    append_images=[Image.open(frame) for frame in frames[1:]],
                    loop=0,
                    optimize=True,
                )
            final_gif.seek(0)
            return final_gif

        async with ctx.typing():
            try:
                final_gif = await rendering.run_image_job(self.bot, _render)
            except Exception:
                log.exception("Failed to render hug gif")
                return await ctx.send(_("Sorry, I couldn't make the hug image."))
            await ctx.send(file=discord.File(final_gif, filename="hug.gif"))

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def cat(self, ctx):
        """Send a random cat picture."""

        async with ctx.typing():
            try:
                async with get_session(self.bot).get(
                    "https://api.thecatapi.com/v1/images/search/",
                    timeout=TIMEOUT,
                ) as r:
                    res = await r.json()
                    url = res[0]["url"]
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch cat image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def dog(self, ctx):
        """Send a random dog picture."""
        async with ctx.typing():
            try:
                async with get_session(self.bot).get(
                    "https://random.dog/woof.json", timeout=TIMEOUT
                ) as r:
                    res = await r.json()
                    url = res["url"]
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch dog image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def fox(self, ctx):
        """Send a random fox picture."""
        async with ctx.typing():
            try:
                async with get_session(self.bot).get(
                    "https://randomfox.ca/floof/?ref=apilist.fun",
                    timeout=TIMEOUT,
                ) as r:
                    res = await r.json()
                    url = res["image"]
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch fox image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @staticmethod
    async def _delete_invocation(ctx):
        """Delete the ?say message ITSELF, never 'whatever is newest here'.

        This used to be ``ctx.channel.purge(limit=1)``, which deletes the most
        recent message in the channel at that instant - not necessarily this
        command. Any member who posted between the invoke and the purge lost
        their message instead, and the offending ?say survived. Deleting
        ``ctx.message`` by id has no such race.

        Both ways this can fail mean "nothing to do here": NotFound (a
        moderator or automod already took it down - the "only if it still
        exists" case) and Forbidden (no Manage Messages in this channel), and
        both are HTTPException. The warning still goes out either way.
        """

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            log.debug("could not delete the say invocation", exc_info=True)

    async def _refuse_say(self, ctx, value):
        """Take the offending invocation down and answer it with a warning.

        ``value`` is already localised, already formatted and already defused
        by the caller. ``AllowedMentions.none()`` is the other half of
        :func:`tools.formats.public_echo`: the echo cannot forge markup, and
        the send cannot ping. The two always travel together.
        """

        await self._delete_invocation(ctx)
        embed = discord.Embed(
            timestamp=discord.utils.utcnow(),
            color=random_colour(),
        )
        embed.add_field(name=_(":warning: Warning!"), value=value, inline=True)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 10.0, commands.BucketType.user)
    async def say(self, ctx, *, args: str):
        """Make Yasuho repeat your message."""
        message = "".join(args)

        try:
            if ctx.message.mention_everyone:
                # public_echo, not the raw text: the warning embed quotes the
                # offending message back into a message YASUHO signs, so it is
                # markup until it is made inert. Unescaped, a rejected
                # "[free nitro](https://evil.example)" simply became a
                # bot-authored clickable link inside the warning about it.
                await self._refuse_say(
                    ctx,
                    _("Don't mention everyone {author}\n Message : {message}").format(
                        author=ctx.author.mention, message=public_echo(message)
                    ),
                )
                return

            if contains_link(message):
                await self._refuse_say(
                    ctx,
                    _("Please, don't send links {author}\n Message : {message}").format(
                        author=ctx.author.mention, message=public_echo(message)
                    ),
                )
                return

            if "stupid" in message:
                await ctx.send(_("Yes, we know."))
                return

            # The echo itself is deliberately verbatim - repeating the member's
            # text IS the command - but it must not PING. ``mention_everyone``
            # above is False for a member who lacks the permission, so without
            # this an @everyone typed by anyone would have been rung out by the
            # bot, which does have it.
            await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

        except Exception:
            log.exception("Failed to process say command")

    @commands.command()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def bigmoji(self, ctx, *, emoji):
        """Make an emoji bigger."""
        # Verify if the emoji is a custom emoji
        if emoji.startswith(("<:", "<a:")) and emoji.endswith(">"):
            m = re.search(r":(\d+)>$", emoji)
            if m is None:
                return await ctx.send(
                    _("That doesn't look like an emoji I can enlarge.")
                )
            emoji_id = m.group(1)
            extension = "gif" if emoji.startswith("<a:") else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
        else:
            # For other emojis, we use Twemoji (the maxcdn host is long dead;
            # jsdelivr serves the maintained fork's assets).
            emoji_code = "".join(format(ord(char), "x") for char in emoji)
            url = (
                "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/"
                f"{emoji_code}.png"
            )

        embed = discord.Embed(color=random_colour())
        embed.add_field(
            name=_("**Download link**"),
            value=_("**[➡️ URL]({url})**").format(url=url),
        )
        embed.set_image(url=url)
        embed.set_footer(
            text=_("Requested by: {user}").format(user=ctx.author.name),
            icon_url=ctx.author.display_avatar.url,
        )
        embed.timestamp = ctx.message.created_at
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def ascii(self, ctx, *, msg: str):
        "Convert text to ASCII art."
        if not (ctx.invoked_subcommand):
            if msg:
                msg = str(
                    await rendering.run_image_job(
                        self.bot, figlet_format, msg.strip(), font="big"
                    )
                )
                if len(msg) > 2000:
                    await ctx.send(_("*Message too long.*"))
                else:
                    try:
                        await ctx.send(f"```fix\n{msg}\n```")

                    except Exception:
                        log.exception("Failed to send ascii art")
        else:
            await ctx.send(
                _("**Please input text to convert to ascii art. Ex: ``<prefix> ascii stuff``**")
            )

    @commands.command(
        name="ask", aliases=["eight-ball", "ball-8", "8-ball"]
    )
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    async def eight_ball(self, ctx, yesnoquestion=None):
        """Answer a yes/no question."""
        if yesnoquestion is None:
            await ctx.send(_("Ask me a question..."))

        else:
            async with ctx.typing():
                await asyncio.sleep(5)
                possible_responses = config_loader.getlist("EightBall", "Answers")
                await ctx.send(
                    random.choice(possible_responses) + " " + ctx.author.mention
                )
                await ctx.message.add_reaction("🎱")

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def reverse(self, ctx, *, text):
        """Reverse your text."""
        embed = discord.Embed(color=random_colour())
        embed.add_field(name=_("Reversed:"), value=f"```{text[::-1]}```")
        embed.set_footer(
            text=_("Requested by: {user}").format(user=ctx.author),
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed)

    @commands.command()
    @commands.guild_only()
    async def rate(self, ctx, *, thing: commands.clean_content):
        """Rate anything you want, out of 100."""
        async with ctx.typing():
            await asyncio.sleep(2)
            num = random.randint(0, 100)
            deci = random.randint(0, 9)

            if num == 100:
                deci = 0

            await ctx.send(
                _("I'd rate {thing} a **{num}.{deci}/ 100**").format(
                    thing=thing, num=num, deci=deci
                )
            )

    @commands.command(aliases=["howhot", "hot"])
    @commands.guild_only()
    async def hotcalc(self, ctx, *, user: discord.Member = None):
        """Rate how hot a member is."""

        if user is None:
            user = ctx.author

        elif user.id == 228895251576782858:
            s = await ctx.send(
                _("**{user}** is **1000%** hot :heart_eyes: :lips:").format(user=user.mention)
            )
            await s.add_reaction("🇭")
            await s.add_reaction("🇴")
            await s.add_reaction("🇹")
            return

        elif user.id == 295575165931356160:
            await ctx.send(
                _("{user} is hot like a pineapple :pineapple:").format(user=user.name)
            )
            return

        r = random.randint(1, 100)
        hot = r / 1.17

        emoji = "💔"
        if hot > 25:
            emoji = "❤"
        if hot > 50:
            emoji = "💖"
        if hot > 75:
            emoji = "💞"

        await ctx.send(
            _("**{user}** is **{hot:.2f}%** hot {emoji}").format(
                user=user.name, hot=hot, emoji=emoji
            )
        )

    @commands.command(description="Rate how gay a member is.")
    @commands.guild_only()
    async def gaycalc(self, ctx, member: discord.Member = None):
        """Rate how gay a member is."""
        member = member or ctx.author
        y = random.randint(0, 99)
        emj = ""

        for x in range(int(y / 20)):
            emj += ":gay_pride_flag:"

        if member.id in (228895251576782858, 295575165931356160, 447697573118214148, 313353843629096960):
            await ctx.send(_("{member} is **0%** gay 👑").format(member=member.name))
            return

        await ctx.send(
            _("{member} is **{y}.{rand}%** gay {emj}").format(
                member=member.name, y=y, rand=random.randint(0, 99), emj=emj
            )
        )

    @commands.command(aliases=["slots", "bet"])
    @commands.guild_only()
    async def slot(self, ctx):
        """Roll the slot machine."""
        emojis = config_loader.getlist("Slots", "slot_emojis")
        a = random.choice(emojis)
        b = random.choice(emojis)
        c = random.choice(emojis)

        slotmachine = f"**[ {a} {b} {c} ]\n{ctx.author.name}**,"

        if a == b == c:
            await ctx.send(_("{slot} All matching, you won! 🎉").format(slot=slotmachine))
        elif (a == b) or (a == c) or (b == c):
            await ctx.send(_("{slot} 2 in a row, you won! 🎉").format(slot=slotmachine))
        else:
            await ctx.send(_("{slot} No match, you lost 😢").format(slot=slotmachine))

    @commands.command(name="rps", aliases=["shifumi", "pfc"])
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    async def pfc(self, ctx):
        """Play Rock Paper Scissors against Yasuho."""
        em = discord.Embed(
            color=random_colour(),
            timestamp=discord.utils.utcnow(),
            title=_("RPS Game"),
            description=_("Pick your move below: Rock, Paper or Scissors!"),
        )
        em.set_footer(text=_("I'm waiting for you!"))
        view = RPSView(ctx.author)
        view.message = await ctx.send(embed=em, view=view)


async def setup(bot):
    await bot.add_cog(Fun(bot))
