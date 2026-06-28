
import logging

import aiohttp
import discord
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)

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
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f"https://api.nasa.gov/planetary/apod?api_key={NASA_KEY}") as resp:

                cont = await resp.json()

                embed = discord.Embed(
                    color=random_colour(),
                    timestamp=ctx.message.created_at,
                    title="Astronomy Picture of the Day",
                    description=f'`{cont["title"]}` ● `{cont["date"]}`'
                    f'\n\n{cont["explanation"]}',
                )

                if not cont["url"].endswith(("gif", "png", "jpg")):
                    embed.add_field(
                        name="**🔴 Watch**",
                        value=f"**[➢ Watch this!]({cont['url']})**")
                else:
                    embed.set_image(url=cont["url"])

                try:
                    embed.add_field(
                        name="**🖼 Download**",
                        value=f'**[➢ HD Download]({cont["hdurl"]})**',
                    )
                except KeyError:
                    pass

                embed.set_footer(
                    text=f"APOD Requested by {ctx.author.name}",
                    icon_url=ctx.author.display_avatar.url,
                )

                if ctx.interaction:
                    return await ctx.interaction.response.send_message(embed=embed, ephemeral=True)

                await ctx.send(
                    embed=embed
                )

    @commands.hybrid_command()
    @commands.guild_only()
    async def weather(self, ctx, city: str):
        """Shows the current weather for a given city."""
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_KEY}&units=metric') as r:

                res = await r.json()

                if str(res.get("cod")) != "200":
                    return await ctx.send(res.get("message", "City not found."))

                city_json = res["name"]
                embed = discord.Embed(title=f"Temperature in {city_json}", color=random_colour(), timestamp=discord.utils.utcnow())
                weather = res["weather"][0]["main"]
                weather_desc = res["weather"][0]["description"]
                current_temp = res['main']["temp"]
                feel_like = res['main']["feels_like"]
                temp_min = res['main']["temp_min"]
                temp_max = res['main']["temp_max"]
                pressure = res['main']["pressure"]
                humidity = res['main']["humidity"]
                clouds = res['clouds']["all"]

                embed.add_field(name="Weather Informations",
                                value=f"```Weather status: {weather}, {weather_desc}\nCurrent temperature: {current_temp}°C\nFeel like: {feel_like}°C\nMinimum temperature: {temp_min}°C\nMax temperature {temp_max}°C\nPressure: {pressure}hPa\nHumidity: {humidity}%\nClouds: {clouds}%\n```", inline=True)

                if ctx.interaction:
                    return await ctx.interaction.response.send_message(embed=embed, ephemeral=True)

                await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Meta(bot))
