"""AFK statuses: a member says they are away, and anyone pinging them is told.

TWO PROPERTIES THE FIRST VERSION DID NOT HAVE, both of them about the fact that
the status is FREE TEXT its author typed once and the bot then re-broadcasts on
a trigger anybody else can pull:

* IT IS PER GUILD. The status is stored against the guild it was set in and is
  only ever announced there. Stored user-globally it was replayed verbatim in
  every other server the member shares with the bot - text written for one
  audience read out to another, which is a cross-tenant leak of somebody's own
  words, not a feature. A row from before the column existed carries no guild
  (NULL): it announces NOWHERE (the closed direction) and is cleared the first
  time its author speaks anywhere.
* IT IS BOUNDED. One notice per MESSAGE, whatever it mentions, plus a per
  (channel, member) window - so a message that mentions an AFK member twenty
  times costs one DB read and one send, not twenty of each, and a mention loop
  cannot be used to wall a channel.

The mention policy (only the AFK member is ever pingable) is the third rule and
is stated at each send below; tests/cogs/test_afk.py asserts all three.

Typography rule: ASCII '-' and '...' only.
"""

import logging

import discord
from discord.ext import commands

from tools.cooldowns import Cooldowns
from tools.formats import random_colour
from tools.i18n import _
from tools.time import human_timedelta

log = logging.getLogger(__name__)

# One notice per (channel, AFK member) per this many seconds. Long enough that a
# mention loop cannot turn one member's status into a wall of bot messages,
# short enough that the next person to genuinely ping them a minute later still
# learns they are away.
NOTICE_COOLDOWN_SECONDS = 60.0

# In-memory, bounded and self-pruning (tools.cooldowns.Cooldowns) - the same
# debounce the music station select and the AniList feed buttons use. Not a
# durable contract: after a restart the first ping in a channel gets its notice,
# which is the harmless direction.
_NOTICE_DEBOUNCE = Cooldowns(NOTICE_COOLDOWN_SECONDS)

# Sentinel for "this member has no AFK status at all", so that a real stored
# NULL guild (a legacy row) is never confused with an absent entry.
_NOT_AFK = object()


class AFK(commands.Cog):
    """Let members set an AFK status and notify others when they are mentioned."""

    def __init__(self, bot):
        self.bot = bot
        # user_id -> the guild id the status was set in (None for a legacy row
        # written before the column existed). A user-keyed PREFILTER for the
        # on_message hot path: one dict lookup decides, with no DB round trip,
        # whether this message can possibly matter. Keyed by user (the table's
        # primary key) so it stays exactly one entry per AFK member.
        self.afk_users = {}

    async def cog_load(self):
        rows = await self.bot.db_pool.fetch("SELECT user_id, guild_id FROM afk")
        self.afk_users = {row["user_id"]: row["guild_id"] for row in rows}

    @commands.hybrid_command()
    @commands.guild_only()
    @discord.app_commands.describe(message="Why you're away (shown to anyone who pings you).")
    async def afk(self, ctx, *, message: str = "AFK"):
        """Set your AFK status, with an optional message."""

        # guild_id travels with the text: this status was written for THIS
        # server and is announced in no other (see _notify_one_mention).
        query = """
            INSERT INTO afk
            (user_id, guild_id, message)
            VALUES
            ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                message = EXCLUDED.message,
                guild_id = EXCLUDED.guild_id,
                since = now();
            """

        await self.bot.db_pool.execute(query, ctx.author.id, ctx.guild.id, message)
        self.afk_users[ctx.author.id] = ctx.guild.id
        embed = discord.Embed(colour=random_colour())
        embed.description = _("{user} you are now AFK: {message}").format(
            user=ctx.author.mention, message=message
        )
        await ctx.send(embed=embed)

    async def _clear_if_back(self, message):
        """(1) The author is back from being AFK - clear the status and say so.

        Scoped to the guild the status was set in, so a member who is AFK in
        server A and chatting in server B stays AFK in A (they have not come
        back THERE) and, just as importantly, costs no write per message in B:
        the in-memory guild id answers that with no round trip at all.
        """
        guild_id = self.afk_users.get(message.author.id, _NOT_AFK)
        if guild_id is _NOT_AFK:
            return
        if guild_id is not None and guild_id != message.guild.id:
            return

        deleted = await self.bot.db_pool.fetchrow(
            "DELETE FROM afk WHERE user_id = $1 "
            "AND guild_id IS NOT DISTINCT FROM $2 "
            "AND now() - since > interval '3 seconds' RETURNING since",
            message.author.id,
            guild_id,
        )
        if not deleted:
            return
        self.afk_users.pop(message.author.id, None)
        await message.channel.send(
            _("Welcome back {user}, you were AFK for {duration}.").format(
                user=message.author.mention,
                duration=human_timedelta(deleted["since"], suffix=False),
            ),
            delete_after=10,
            # No free text here, but the policy is stated on every
            # send in this cog: the only pingable party is the
            # member the line is about.
            allowed_mentions=discord.AllowedMentions(
                users=[message.author], roles=False, everyone=False
            ),
        )

    async def _notify_one_mention(self, message):
        """(2) Notify when an AFK member gets mentioned - AT MOST ONCE.

        The loop stops at the first notice it actually sends, so the work a
        single message can trigger is capped at one DB read and one send no
        matter how many mentions it carries (Discord allows dozens, and the
        same member twenty times over). Everything before that first send is
        pure memory: a dict lookup and a cooldown check.

        A member who is AFK in ANOTHER guild is skipped outright - their status
        does not exist here - and so is a legacy row with no guild recorded,
        because an unknown origin is not a licence to republish somebody's text
        in front of an audience they never wrote it for.
        """
        for user in message.mentions:
            if self.afk_users.get(user.id, _NOT_AFK) != message.guild.id:
                continue

            key = (message.channel.id, user.id)
            if _NOTICE_DEBOUNCE.is_active(key):
                continue
            # Touched BEFORE the await, so two messages processed back to back
            # cannot both slip past the check while the read is in flight. A
            # skipped click never touches it, so a mention loop cannot keep
            # extending the window for everyone else.
            _NOTICE_DEBOUNCE.touch(key)

            r = await self.bot.db_pool.fetchrow(
                "SELECT message, since FROM afk WHERE user_id = $1 AND guild_id = $2",
                user.id,
                message.guild.id,
            )
            if not r:
                continue
            await message.channel.send(
                _("{name} is AFK: {message} ({duration})").format(
                    name=user.display_name,
                    message=r["message"],
                    duration=human_timedelta(r["since"]),
                ),
                # The status text (and the display name) are free text
                # authored by the AFK member, and this line is
                # re-broadcast every single time somebody pings them.
                # With default mentions that turns an AFK status into a
                # ping amplifier aimed at anyone the author names, so
                # only the AFK member - the subject of the sentence -
                # can ever be mentioned here.
                allowed_mentions=discord.AllowedMentions(
                    users=[user], roles=False, everyone=False
                ),
            )
            return

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        # THE PREFILTER, hoisted out of the two handlers that each start with
        # half of it. Both are cheap - a dict lookup, an empty list - but they
        # were being reached by awaiting a coroutine apiece, on every message in
        # every guild, only to be told at once that there was nothing to do.
        # Asked here, the ordinary message (author not AFK, nobody mentioned)
        # leaves this listener having allocated nothing and awaited nothing.
        # ``afk_users`` holds one entry per AFK member fleet-wide, so this stays
        # O(1) at any number of guilds. The handlers keep their own guards:
        # each is still correct called on its own.
        if message.author.id not in self.afk_users and not message.mentions:
            return

        try:
            await self._clear_if_back(message)
            await self._notify_one_mention(message)
        except Exception:
            log.exception("on_message handler failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
