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

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)

# Cap outbound HTTP calls so a slow or hung endpoint can't block an interaction.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

regex = re.compile(
    r"^(?:http|ftp)s?://"  # http:// or https://
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|"  # domain...
    r"localhost|"  # localhost...
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # ...or ip
    r"(?::\d+)?"  # optional port
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)


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
            return await ctx.send("You can't hug the air...")

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
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as cs:
                    async with cs.get('https://api.thecatapi.com/v1/images/search/') as r:
                        res = await r.json()
                        url=(res[0]['url'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch cat image")
                await ctx.send(':warning: **ERROR !**', delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def dog(self, ctx):
        """ Sends a random dog picture"""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as cs:
                    async with cs.get('https://random.dog/woof.json') as r:
                        res = await r.json()
                        url=(res['url'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch dog image")
                await ctx.send(':warning: **ERROR !**', delete_after=3)

    @commands.command()
    @commands.guild_only()
    @commands.cooldown(1.0, 5.0, commands.BucketType.user)
    async def fox(self, ctx):
        """ Sends a random dog picture"""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as cs:
                    async with cs.get('https://randomfox.ca/floof/?ref=apilist.fun') as r:
                        res = await r.json()
                        url=(res['image'])
                await ctx.send(url)
            except Exception:
                log.exception("Failed to fetch fox image")
                await ctx.send(':warning: **ERROR !**', delete_after=3)

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
                    name=":warning: Warning!",
                    value=f"Don't mention everyone {ctx.message.author.mention}\n Message : {message}",
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
                    name=":warning: Warning!",
                    value=f"Please, don't send links {ctx.message.author.mention}\n Message : {message}",
                    inline=True,
                )
                await ctx.send(embed=embed)
                return

            elif "stupid" in message:
                await ctx.send("Yes, we know.")

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
            name="**Download link**",
            value=f"**[➡️ URL]({url})**",
        )
        embed.set_image(url=url)
        embed.set_footer(
            text=f"Requested by: {ctx.author.name}", icon_url=ctx.author.display_avatar.url
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
                    await ctx.send("*Message too long.*")
                else:
                    try:
                        await ctx.send(f"```fix\n{msg}\n```")

                    except Exception:
                        log.exception("Failed to send ascii art")
        else:
            await ctx.send(
                "**Please input text to convert to ascii art. Ex: ``<prefix> ascii stuff``**"
            )

    @commands.command(
        name="ask", aliases=["eight-ball", "ball-8", "8-ball"]
    )
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    async def eight_ball(self, ctx, yesnoquestion=None):
        """Answer to a yes/no quesiton."""
        if yesnoquestion is None:
            await ctx.send("Ask me a question...")

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
        embed.add_field(name="Reversed:", value=f"```{text[::-1]}```")
        embed.set_footer(
            text=f"Requested by: {ctx.author}", icon_url=ctx.author.display_avatar.url
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

            await ctx.send(f"I'd rate {thing} a **{num}.{deci}/ 100**")

    @commands.command(aliases=["howhot", "hot"])
    @commands.guild_only()
    async def hotcalc(self, ctx, *, user: discord.Member = None):
        """Returns a random percent for how hot is a discord user"""

        if user is None:
            user = ctx.author

        elif user.id == 228895251576782858:
            s = await ctx.send(
                f"**{user.mention}** is **1000%** hot :heart_eyes: :lips:"
            )
            await s.add_reaction("🇭")
            await s.add_reaction("🇴")
            await s.add_reaction("🇹")
            return

        elif user.id == 295575165931356160:
            await ctx.send(f"{user.name} is hot like a pineapple :pineapple:")
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

        await ctx.send(f"**{user.name}** is **{hot:.2f}%** hot {emoji}")

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
            await ctx.send(f"{member.name} is **0%** gay 👑")
            return

        await ctx.send(f"{member.name} is **{y}.{random.randint(0, 99)}%** gay {emj}")

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
            await ctx.send(f"{slotmachine} All matching, you won! 🎉")
        elif (a == b) or (a == c) or (b == c):
            await ctx.send(f"{slotmachine} 2 in a row, you won! 🎉")
        else:
            await ctx.send(f"{slotmachine} No match, you lost 😢")

    @commands.command(name="rps", aliases=["shifumi", "pfc"])
    @commands.guild_only()
    @commands.cooldown(1.0, 3.0, commands.BucketType.user)
    async def pfc(self, ctx):
        """Play Rock Paper Scissors with Yasuho!"""
        em = discord.Embed(
            color=random_colour(),
            timestamp=discord.utils.utcnow(),
            title="RPS Game",
            description="**I choose ** :grey_question: \n\nReact with:\n✊ for `Rock`\n🖐 for `Paper`\n✌ for `Scissors`",
        )
        em.set_footer(text="I'm waiting for you!")
        rps = await ctx.send(embed=em)
        responses = ["✊", "🖐", "✌"]
        bot_response = random.choice(responses)
        await rps.add_reaction("✊")
        await rps.add_reaction("🖐")
        await rps.add_reaction("✌")

        def check(reaction, user):
            return (
                user == ctx.author
                and str(reaction.emoji) in responses
                and reaction.message.id == rps.id
            )

        try:
            reaction, user = await self.bot.wait_for(
                "reaction_add", timeout=60.0, check=check
            )
        except asyncio.TimeoutError:
            await rps.edit(content="Game timed out! Please try again.", embed=None)
            return

        user_choice = str(reaction.emoji)
        result = "Draw"

        if bot_response != user_choice:
            win_conditions = {"✊": "✌", "🖐": "✊", "✌": "🖐"}
            result = (
                "You won" if win_conditions[user_choice] == bot_response else "You lost"
            )

        response_desc = (
            f"I choose {bot_response}\nYou choose {user_choice}\n\nResult : `{result}`"
        )
        result_em = discord.Embed(
            color=random_colour(),
            timestamp=discord.utils.utcnow(),
            title="RPS Game",
            description=response_desc,
        )
        result_em.set_footer(text="Thanks for playing!")
        await rps.edit(embed=result_em)


async def setup(bot):
    await bot.add_cog(Fun(bot))
