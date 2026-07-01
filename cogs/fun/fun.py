import asyncio
import io
import logging
import random
import re

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageSequence
from pyfiglet import figlet_format

from tools import interactions
from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.http import TIMEOUT
from tools.i18n import _
from tools.views import AuthorView

log = logging.getLogger(__name__)

regex = re.compile(
    r"^(?:http|ftp)s?://"  # http:// or https://
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|"  # domain...
    r"localhost|"  # localhost...
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # ...or ip
    r"(?::\d+)?"  # optional port
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)


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

    @property
    def hug_colour(self):
        return ImageColor.getcolor("#e94573", "L")

    @commands.command(name="hug")
    @commands.guild_only()
    @commands.cooldown(3, 90, commands.BucketType.user)
    async def give_hug(self, ctx, member: discord.Member = None):
        """Give a hug to your secret crush ッ"""
        if not member:
            return await ctx.send(_("You can't hug the air..."))

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
            final_gif = await self.bot.loop.run_in_executor(None, _render)
            await ctx.send(file=discord.File(final_gif, filename="hug.gif"))

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def cat(self, ctx):
        """Sends a random cat image"""

        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as cs:
                    async with cs.get('https://api.thecatapi.com/v1/images/search/') as r:
                        res = await r.json()
                        url=(res[0]['url'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch cat image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def dog(self, ctx):
        """ Sends a random dog picture"""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as cs:
                    async with cs.get('https://random.dog/woof.json') as r:
                        res = await r.json()
                        url=(res['url'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch dog image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def fox(self, ctx):
        """ Sends a random fox picture"""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as cs:
                    async with cs.get('https://randomfox.ca/floof/?ref=apilist.fun') as r:
                        res = await r.json()
                        url=(res['image'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch fox image")
                await ctx.send(_(':warning: **ERROR !**'), delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 10.0, commands.BucketType.user)
    async def say(self, ctx, *, args: str):
        """The bot say what you want."""
        message = "".join(args)

        try:
            if ctx.message.mention_everyone:
                await ctx.channel.purge(limit=1)
                embed = discord.Embed(
                    timestamp=discord.utils.utcnow(),
                    color=random_colour(),
                )
                embed.add_field(
                    name=_(":warning: Warning!"),
                    value=_("Don't mention everyone {author}\n Message : {message}").format(
                        author=ctx.message.author.mention, message=message
                    ),
                    inline=True,
                )
                await ctx.send(embed=embed)
                return

            elif re.match(regex, args):
                await ctx.channel.purge(limit=1)
                embed = discord.Embed(
                    timestamp=discord.utils.utcnow(),
                    color=random_colour(),
                )
                embed.add_field(
                    name=_(":warning: Warning!"),
                    value=_("Please, don't send links {author}\n Message : {message}").format(
                        author=ctx.message.author.mention, message=message
                    ),
                    inline=True,
                )
                await ctx.send(embed=embed)
                return

            elif "stupid" in message:
                await ctx.send(_("Yes, we know."))

            else:
                await ctx.send(message)

        except Exception:
            log.exception("Failed to process say command")

    @commands.command()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def bigmoji(self, ctx, *, emoji):
        """Makes big an emoji"""
        # Verify if the emoji is a custom emoji
        if emoji.startswith(("<:", "<a:")) and emoji.endswith(">"):
            m = re.search(r":(\d+)>$", emoji)
            emoji_id = m.group(1)
            extension = "gif" if emoji.startswith("<a:") else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
        else:
            # For other emojis, we use Twemoji
            emoji_code = "".join(format(ord(char), "x") for char in emoji)
            url = f"https://twemoji.maxcdn.com/v/latest/72x72/{emoji_code}.png"

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
    async def ascii(self, ctx, *, msg: str):
        "Convert text to ascii art"
        if not (ctx.invoked_subcommand):
            if msg:
                msg = str(figlet_format(msg.strip(), font="big"))
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
        """Answer to a yes/no question."""
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
        """Gives you reversed text"""
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
        """Rates what you desire"""
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
        """Returns a random percent for how hot is a discord user"""

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

    @commands.command(description="Calculate how gay you are!")
    @commands.guild_only()
    async def gaycalc(self, ctx, member: discord.Member = None):
        """Returns a random percent for how gay is a discord user"""
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
        """Roll the slot machine"""
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
        """Play Rock Paper Scissors with Yasuho!"""
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
