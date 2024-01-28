import discord
from discord.ext import commands
import topgg
from tools.config_loader import config_loader

TOP_GG_TOKEN = config_loader.get('WebsiteTokens', 'topGG')
TOP_GG_PASSWORD = config_loader.get('WebsiteTokens', 'topGGPassword')

class Webstats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dbl_token = TOP_GG_TOKEN
        self.dbl_password = TOP_GG_PASSWORD

        self.dbl_client = topgg.DBLClient(self.bot, self.dbl_token, autopost=True, post_shard_count=True)
        self.webhook_manager = topgg.WebhookManager(self.bot).dbl_webhook("/dblwebhook", TOP_GG_PASSWORD)
        self.webhook_manager.run(55000) 

    @commands.Cog.listener()
    async def on_autopost_success(self):
        print(f"Posted server count ({self.dbl_client.guild_count}), shard count ({self.bot.shard_count})")

    @commands.Cog.listener()
    async def on_dbl_vote(self, data):
        print(f"Received a vote:\n{data}")

    @commands.Cog.listener()
    async def on_dbl_test(self, data):
        print(f"Received a test vote:\n{data}")

async def setup(bot):
    await bot.add_cog(Webstats(bot))
