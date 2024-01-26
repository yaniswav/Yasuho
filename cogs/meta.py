from typing import Optional
import discord
from discord.ext import commands

import aiohttp
import config
import datetime
import random
import Levenshtein as lv
import requests

class HelpMenu(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Question", value="Question", description='Si tu as une simple question.',
                                 emoji='❔'),
            discord.SelectOption(label="Aide", value="Aide", description='Si tu as besoin de notre aide.', emoji='🔧'),
            discord.SelectOption(label="Report", value="Report", description='Pour signaler un probleme sur le serveur.', emoji='🚫')
        ]
        super().__init__(placeholder="Comment puis-je t'aider ?", min_values=1, max_values=1, options=options,
                         custom_id='menu')

    async def callback(self, interaction: discord.Interaction):
        await ctx.send("bonjour")


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__()

        # Adds the dropdown to our view object.
        self.add_item(HelpMenu())

class HelpCommand(commands.HelpCommand):
    context: commands.Context

    def __init__(self):
        super().__init__(
            command_attrs={
                'cooldown': commands.CooldownMapping.from_cooldown(1, 3.0, commands.BucketType.member),
                'help': 'Shows help about the bot, a command, or a category',
            }
        )

    def get_command_signature(self, command: commands.Command) -> str:
        parent = command.full_parent_name
        if len(command.aliases) > 0:
            aliases = '|'.join(command.aliases)
            fmt = f'[{command.name}|{aliases}]'
            if parent:
                fmt = f'{parent} {fmt}'
            alias = fmt
        else:
            alias = command.name if not parent else f'{parent} {command.name}'
        return f'{alias} {command.signature}'

    async def send_bot_help(self, mapping):
        channel = self.get_destination()
        await channel.send("salut", view=HelpView())

    async def send_command_help(self, command):
        embed = discord.Embed(title=self.get_command_signature(command))
        embed.add_field(name="Help", value=command.help)
        alias = command.aliases
        if alias:
            embed.add_field(name="Aliases", value=", ".join(alias), inline=False)

        channel = self.get_destination()
        await channel.send(embed=embed)

    async def on_help_command_error(self, ctx, error: commands.CommandError):
        if isinstance(error, commands.CommandInvokeError):
            # Ignore missing permission errors
            if isinstance(error.original, discord.HTTPException) and error.original.code == 50013:
                return

            await ctx.send(str(error.original))


class Meta(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
            
        help_command = HelpCommand()
        help_command.cog = self # Instance of YourCog class
        bot.help_command = help_command

    @commands.hybrid_command()
    async def apod(self, ctx):
        """
        Shows Astronomy Picture of the Day.
        """
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f"https://api.nasa.gov/planetary/apod?api_key={config.nasa_key}") as resp:

                cont = await resp.json()

                embed = discord.Embed(
                    color=random.randint(0x000000, 0xFFFFFF),
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
                    icon_url=ctx.author.avatar.url,
                )

                if ctx.interaction:
                    return await ctx.interaction.response.send_message(embed=embed, ephemeral=True)

                await ctx.send(
                    embed=embed
                )

    @commands.hybrid_command()
    @commands.guild_only()
    async def weather(self, ctx, city: str):
        async with aiohttp.ClientSession() as cs:
            async with cs.get(f'https://api.openweathermap.org/data/2.5/weather?q={city}&appid={config.weather_key}&units=metric') as r:
                
                res = await r.json()
                
                cityjson = res["name"]
                embed = discord.Embed(title=f"Temperature in {cityjson}", color=random.randint(
                    0x000000, 0xFFFFFF), timestamp=datetime.datetime.utcnow())
                weather = res["weather"][0]["main"]
                weatherdesc = res["weather"][0]["description"]
                currenttemp = res['main']["temp"]
                feel_like = res['main']["feels_like"]
                temp_min = res['main']["temp_min"]
                temp_max = res['main']["temp_max"]
                pressure = res['main']["pressure"]
                humidity = res['main']["humidity"]
                clouds = res['clouds']["all"]
                
                embed.add_field(name="Weather Informations",
                                value=f"```Weather status: {weather}, {weatherdesc}\nCurrent temperature: {currenttemp}°C\nFeel like: {feel_like}°C\nMinimum temperature: {temp_min}°C\nMax temperature {temp_max}°C\nPressure: {pressure}hPa\nHumidity: {humidity}%\nClouds: {clouds}%\n```", inline=True)
                
                if ctx.interaction:
                    return await ctx.interaction.response.send_message(embed=embed, ephemeral=True)

                await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Meta(bot))
