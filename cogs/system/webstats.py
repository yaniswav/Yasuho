import logging

import topgg
from discord.ext import commands

from tools.config_loader import config_loader

log = logging.getLogger(__name__)

# fallback=None so a fresh checkout without top.gg config does not crash the
# whole cog at import; the cog then simply skips autopost/webhook setup.
TOP_GG_TOKEN = config_loader.get('WebsiteTokens', 'topGG', fallback=None)
TOP_GG_PASSWORD = config_loader.get('WebsiteTokens', 'topGGPassword', fallback=None)


class Webstats(commands.Cog):
    """Posts server/shard counts to Top.gg and handles vote webhooks."""

    def __init__(self, bot):
        self.bot = bot
        self.dbl_token = TOP_GG_TOKEN
        self.dbl_password = TOP_GG_PASSWORD
        self.dbl_client = None
        self.webhook_manager = None
        self._webhook_task = None

        if not TOP_GG_TOKEN:
            log.info("top.gg not configured; skipping autopost and vote webhook.")
            return

        self.dbl_client = topgg.DBLClient(self.bot, self.dbl_token, autopost=True, post_shard_count=True)
        self.webhook_manager = topgg.WebhookManager(self.bot).dbl_webhook("/dblwebhook", TOP_GG_PASSWORD)
        self._webhook_task = self.webhook_manager.run(55000)

        def _on_webhook_done(task):
            exc = task.exception()
            if exc:
                log.error("webhook server failed to start: %s", exc)

        self._webhook_task.add_done_callback(_on_webhook_done)

    async def cog_unload(self):
        # Close each independently so one failure doesn't block the other, and unload never raises
        if self.dbl_client is not None:
            try:
                await self.dbl_client.close()
            except Exception:
                log.exception("failed to close DBL client")
        if self.webhook_manager is not None:
            try:
                await self.webhook_manager.close()
            except Exception:
                log.exception("failed to close webhook manager")

    @commands.Cog.listener()
    async def on_autopost_success(self):
        log.info("Posted server count (%s), shard count (%s)", self.dbl_client.guild_count, self.bot.shard_count)

    @commands.Cog.listener()
    async def on_dbl_vote(self, data):
        if data.get("type") == "test":
            log.info("Received a test vote:\n%s", data)
            return
        log.info("Received a vote:\n%s", data)


async def setup(bot):
    await bot.add_cog(Webstats(bot))
