
import logging

import aiohttp
import discord
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour
from tools.http import TIMEOUT
from tools.i18n import _

log = logging.getLogger(__name__)

# fallback=None so a fresh checkout without these keys does not crash the cog at
# import; apod falls back to NASA's DEMO_KEY below, and weather fails gracefully.
NASA_KEY = config_loader.get('APITokens', 'nasaKey', fallback=None)
WEATHER_KEY = config_loader.get('APITokens', 'weatherKey', fallback=None)

# NASA's public DEMO_KEY works (with tighter rate limits) when no real key is
# configured, so /apod degrades gracefully instead of always failing when the
# nasaKey slot still holds the template placeholder.
if not NASA_KEY or NASA_KEY.startswith("YOUR_"):
    NASA_KEY = "DEMO_KEY"


class WeatherView(discord.ui.LayoutView):
    """Current weather as a Components V2 layout.

    A coloured container pairs the OpenWeather condition icon (as a Section
    thumbnail accessory) with a header, then lists the readings below a
    separator. The view is display-only, so it carries no interactive
    components and never times out.
    """

    def __init__(self, *, city, condition, icon_url, readings, timeout=None):
        super().__init__(timeout=timeout)

        container = discord.ui.Container(accent_colour=random_colour())

        header = _("## {city}\n{condition}").format(
            city=city, condition=condition
        )
        if icon_url:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(icon_url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(readings))

        self.add_item(container)


class Meta(commands.Cog):
    """Miscellaneous informational commands (NASA APOD, weather)."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command()
    async def apod(self, ctx):
        """
        Show NASA's Astronomy Picture of the Day.
        """
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as cs:
                    async with cs.get(
                        f"https://api.nasa.gov/planetary/apod?api_key={NASA_KEY}"
                    ) as resp:
                        if resp.status != 200:
                            log.warning("APOD API returned HTTP %s", resp.status)
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
    @discord.app_commands.describe(city="The city to look up.")
    async def weather(self, ctx, city: str):
        """Show the current weather for a given city."""
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession(timeout=TIMEOUT) as cs:
                    async with cs.get(
                        "https://api.openweathermap.org/data/2.5/weather",
                        params={"q": city, "appid": WEATHER_KEY, "units": "metric"},
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
            icon = weather_list[0].get("icon")
            clouds = (res.get("clouds") or {}).get("all", "?")

            city_json = res.get("name", city)
            current_temp = main.get("temp", "?")
            feel_like = main.get("feels_like", "?")
            temp_min = main.get("temp_min", "?")
            temp_max = main.get("temp_max", "?")
            pressure = main.get("pressure", "?")
            humidity = main.get("humidity", "?")

            condition = _("{status} - {desc}").format(
                status=weather_main, desc=weather_desc
            )
            readings = _(
                "**Temperature:** `{temp}C`\n"
                "**Feels like:** `{feels}C`\n"
                "**Min / Max:** `{tmin}C` / `{tmax}C`\n"
                "**Humidity:** `{humidity}%`\n"
                "**Clouds:** `{clouds}%`\n"
                "**Pressure:** `{pressure}hPa`"
            ).format(
                temp=current_temp,
                feels=feel_like,
                tmin=temp_min,
                tmax=temp_max,
                humidity=humidity,
                clouds=clouds,
                pressure=pressure,
            )
            icon_url = (
                f"https://openweathermap.org/img/wn/{icon}@2x.png" if icon else None
            )

            view = WeatherView(
                city=city_json,
                condition=condition,
                icon_url=icon_url,
                readings=readings,
            )
            await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Meta(bot))
