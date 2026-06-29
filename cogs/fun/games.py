import asyncio
import logging
import random
import time

import discord
from discord.ext import commands

from tools.config_loader import config_loader
from tools.formats import random_colour

log = logging.getLogger(__name__)

# All winning index combinations on a flat 3x3 board (index = row * 3 + col).
WIN_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


class TicTacToeButton(discord.ui.Button):
    """A single cell of the Tic-Tac-Toe grid."""

    def __init__(self, x: int, y: int):
        # Empty cells use a zero-width label so the button keeps a square shape.
        super().__init__(style=discord.ButtonStyle.secondary, label="​", row=y)
        self.x = x
        self.y = y
        self.index = y * 3 + x

    async def callback(self, interaction: discord.Interaction):
        view: "TicTacToeView" = self.view

        # Safety: ignore clicks on already-filled cells or finished games.
        if view.board[self.index] is not None or view.is_finished():
            return await interaction.response.defer()

        # 1) Human plays X on the clicked cell.
        view.mark(self.index, view.X, self)
        result = view.check_state()

        # 2) If the game is still going, the bot (O) answers.
        if result is None:
            move = view.best_move()
            if move is not None:
                view.mark(move, view.BOT_MARK, view.button_at(move))
            result = view.check_state()

        # 3) Resolve the outcome and update the board in a single edit.
        content = view.PROMPT
        if result is not None:
            view.disable_all()
            view.stop()
            if result == view.X:
                content = "Tic-Tac-Toe: you win! ❌"
            elif result == view.BOT_MARK:
                content = "Tic-Tac-Toe: I win! ⭕ Better luck next time."
            else:
                content = "Tic-Tac-Toe: it's a draw!"

        await interaction.response.edit_message(content=content, view=view)


class TicTacToeView(discord.ui.View):
    """A 3x3 Tic-Tac-Toe board: the command author (X) versus a simple bot AI (O)."""

    X = "X"
    BOT_MARK = "O"
    DRAW = "draw"
    PROMPT = "Tic-Tac-Toe: you are X"

    def __init__(self, player: discord.abc.User):
        super().__init__(timeout=180)
        self.player = player
        self.message: discord.Message | None = None
        self.board = [None] * 9
        for y in range(3):
            for x in range(3):
                self.add_item(TicTacToeButton(x, y))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the command author may play their own game."""
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "This isn't your game, start your own with the command!",
                ephemeral=True,
            )
            return False
        return True

    def button_at(self, index: int) -> TicTacToeButton:
        for child in self.children:
            if isinstance(child, TicTacToeButton) and child.index == index:
                return child
        return None

    def mark(self, index: int, player: str, button: TicTacToeButton):
        """Place a mark on the board and update the matching button's look."""
        self.board[index] = player
        if button is not None:
            button.disabled = True
            if player == self.X:
                button.style = discord.ButtonStyle.success
                button.label = "X"
                button.emoji = "❌"
            else:
                button.style = discord.ButtonStyle.danger
                button.label = "O"
                button.emoji = "⭕"

    @staticmethod
    def _winner(board) -> str | None:
        for a, b, c in WIN_LINES:
            if board[a] is not None and board[a] == board[b] == board[c]:
                return board[a]
        return None

    def check_state(self) -> str | None:
        """Return 'X'/'O' for a winner, 'draw' if full, otherwise None."""
        winner = self._winner(self.board)
        if winner is not None:
            return winner
        if all(cell is not None for cell in self.board):
            return self.DRAW
        return None

    def best_move(self) -> int | None:
        """Bot AI: take a winning move, else block the human, else play random."""
        empty = [i for i, cell in enumerate(self.board) if cell is None]
        if not empty:
            return None

        # Win if a winning move exists.
        for i in empty:
            self.board[i] = self.BOT_MARK
            if self._winner(self.board) == self.BOT_MARK:
                self.board[i] = None
                return i
            self.board[i] = None

        # Otherwise block the human's winning move.
        for i in empty:
            self.board[i] = self.X
            if self._winner(self.board) == self.X:
                self.board[i] = None
                return i
            self.board[i] = None

        # Fallback: a random empty cell.
        return random.choice(empty)

    def disable_all(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def on_timeout(self):
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="Tic-Tac-Toe: time's up, game over!", view=self
                )
            except discord.HTTPException:
                log.exception("failed to edit timed-out tic-tac-toe message")
            except Exception:
                log.exception("unexpected error editing timed-out tic-tac-toe message")


class Games(commands.Cog):
    """Mini-games to play in chat, such as Tic-Tac-Toe and Sentence Race."""

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(aliases=["ttt"])
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def tictactoe(self, ctx):
        """Play a game of Tic-Tac-Toe against the bot. You are X."""
        view = TicTacToeView(ctx.author)
        view.message = await ctx.send(TicTacToeView.PROMPT, view=view)

    @commands.hybrid_command(aliases=["typerace"])
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def sentencerace(self, ctx):
        """Be the first to retype the given sentence as fast as you can."""
        try:
            sentences = config_loader.getlist("SentenceRace", "minigames_sentences")
        except Exception:
            log.exception("failed to load sentence-race sentences")
            return await ctx.send("The sentence list is unavailable right now.")

        if not sentences:
            return await ctx.send("There are no sentences configured to race with.")

        sentence = random.choice(sentences)

        embed = discord.Embed(
            title="Sentence Race!",
            description=f"Be the first to type out the following sentence:\n\n{sentence}",
            colour=random_colour(),
        )
        embed.set_footer(text="You have 60 seconds. No copy-pasting cheaters!")
        await ctx.send(embed=embed)

        start = time.monotonic()

        def check(message: discord.Message) -> bool:
            return (
                message.channel == ctx.channel
                and not message.author.bot
                and message.content == sentence
            )

        try:
            winner_message = await self.bot.wait_for(
                "message", timeout=60, check=check
            )
        except asyncio.TimeoutError:
            return await ctx.send(
                "Time's up! Nobody managed to finish the sentence in time."
            )

        elapsed = time.monotonic() - start
        word_count = len(sentence.split())
        wpm = word_count / (elapsed / 60) if elapsed > 0 else 0

        result = discord.Embed(
            title="We have a winner!",
            description=f"{winner_message.author.mention} finished the sentence first!",
            colour=random_colour(),
        )
        result.add_field(name="Time", value=f"{elapsed:.2f} seconds", inline=True)
        result.add_field(name="Speed", value=f"~{wpm:.0f} WPM", inline=True)
        result.set_thumbnail(url=winner_message.author.display_avatar.url)
        await ctx.send(embed=result)


async def setup(bot):
    await bot.add_cog(Games(bot))
