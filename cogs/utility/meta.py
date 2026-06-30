
import logging

import aiohttp
import discord
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.i18n import _

log = logging.getLogger(__name__)

# Cap external HTTP calls so a slow or hung endpoint can't block an interaction.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

NASA_KEY = config_loader.get('APITokens', 'nasaKey')
WEATHER_KEY = config_loader.get('APITokens', 'weatherKey')


class Meta(commands.Cog):
    """Miscellaneous informational commands (NASA APOD, weather)."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command()
    async def apod(self, ctx):
        """
        Shows Astronomy Picture of the Day.
        """
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as cs:
                    async with cs.get(
                        f"https://api.nasa.gov/planetary/apod?api_key={NASA_KEY}"
                    ) as resp:
                        if resp.status != 200:
                            return await ctx.send(
                                _(
                                    "Could not fetch the Astronomy Picture of the Day right now."
                                )
                            )
                        cont = await resp.json()
            except Exception:
                log.exception("APOD fetch failed")
                return await ctx.send(
                    _("Could not fetch the Astronomy Picture of the Day right now.")
                )

            url = cont.get("url") or ""
            embed = discord.Embed(
                color=random_colour(),
                timestamp=ctx.message.created_at,
                title=_("Astronomy Picture of the Day"),
                description=f'`{cont.get("title", "Unknown")}` ● `{cont.get("date", "")}`'
                f'\n\n{cont.get("explanation", "")}',
            )

            if url and not url.endswith(("gif", "png", "jpg")):
                embed.add_field(
                    name="**🔴 Watch**",
                    value=f"**[➢ Watch this!]({url})**")
            elif url:
                embed.set_image(url=url)

            hdurl = cont.get("hdurl")
            if hdurl:
                embed.add_field(
                    name="**🖼 Download**",
                    value=f'**[➢ HD Download]({hdurl})**',
                )

            embed.set_footer(
                text=_("APOD Requested by {user}").format(user=ctx.author.name),
                icon_url=ctx.author.display_avatar.url,
            )

            await ctx.send(embed=embed)

    @commands.hybrid_command()
    @commands.guild_only()
    async def weather(self, ctx, city: str):
        """Shows the current weather for a given city."""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as cs:
                    async with cs.get(
                        f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_KEY}&units=metric'
                    ) as r:
                        res = await r.json()
            except Exception:
                log.exception("Weather fetch failed")
                return await ctx.send(_("Could not fetch the weather right now."))

            if str(res.get("cod")) != "200":
                return await ctx.send(res.get("message", _("City not found.")))

            main = res.get("main") or {}
            weather_list = res.get("weather") or [{}]
            weather_main = weather_list[0].get("main", "Unknown")
            weather_desc = weather_list[0].get("description", "")
            clouds = (res.get("clouds") or {}).get("all", "?")

            city_json = res.get("name", city)
            embed = discord.Embed(title=_("Temperature in {city}").format(city=city_json), color=random_colour(), timestamp=discord.utils.utcnow())
            current_temp = main.get("temp", "?")
            feel_like = main.get("feels_like", "?")
            temp_min = main.get("temp_min", "?")
            temp_max = main.get("temp_max", "?")
            pressure = main.get("pressure", "?")
            humidity = main.get("humidity", "?")

            embed.add_field(name=_("Weather Informations"),
                            value=_("```Weather status: {status}, {desc}\nCurrent temperature: {temp}°C\nFeel like: {feels}°C\nMinimum temperature: {tmin}°C\nMax temperature {tmax}°C\nPressure: {pressure}hPa\nHumidity: {humidity}%\nClouds: {clouds}%\n```").format(status=weather_main, desc=weather_desc, temp=current_temp, feels=feel_like, tmin=temp_min, tmax=temp_max, pressure=pressure, humidity=humidity, clouds=clouds), inline=True)

            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Meta(bot))
