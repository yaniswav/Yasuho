"""Quote is a message context menu, not a text command.

A context menu receives the target message's content without the Message
Content intent; a text command fetching a linked message would get it empty.
"""

import types

import discord
from discord.ext import commands

from cogs.utility.extras import Extras


async def _bot_with_extras():
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.none())
    await bot.add_cog(Extras(bot))
    return bot


async def test_quote_is_a_guild_only_message_menu_and_no_text_command():
    bot = await _bot_with_extras()
    menu = bot.tree.get_command("Quote", type=discord.AppCommandType.message)
    assert menu is not None and menu.guild_only is True
    assert bot.get_command("quote") is None
    await bot.remove_cog("Extras")
    assert bot.tree.get_command("Quote", type=discord.AppCommandType.message) is None


class _Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _message(guild):
    author = types.SimpleNamespace(
        display_name="Ann", display_avatar=types.SimpleNamespace(url="https://x/a.png")
    )
    return types.SimpleNamespace(
        guild=guild, content="hi <@1> @everyone", created_at=discord.utils.utcnow(),
        author=author, jump_url="https://discord.com/channels/1/2/3",
    )


async def test_quote_replays_without_pinging_anyone():
    bot = await _bot_with_extras()
    guild = types.SimpleNamespace(id=1)
    interaction = types.SimpleNamespace(guild=guild, response=_Response(), locale=None, guild_locale=None, user=None)
    await bot.get_cog("Extras").quote_message(interaction, _message(guild))
    (args, kwargs), = interaction.response.sent
    assert kwargs["embed"].description == "hi <@1> @everyone"
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


async def test_quote_refuses_a_message_from_another_server():
    bot = await _bot_with_extras()
    interaction = types.SimpleNamespace(guild=types.SimpleNamespace(id=1), response=_Response(), locale=None, guild_locale=None, user=None)
    await bot.get_cog("Extras").quote_message(interaction, _message(types.SimpleNamespace(id=2)))
    (args, kwargs), = interaction.response.sent
    assert kwargs.get("ephemeral") is True and "embed" not in kwargs
