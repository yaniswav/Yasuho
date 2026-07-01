"""Unit tests for the Tic-Tac-Toe engine in ``cogs/fun/games.py``.

These cover the pure game logic of :class:`cogs.fun.games.TicTacToeView`:

* ``_winner``    - a staticmethod scanning ``WIN_LINES`` (each row, column,
  both diagonals, and the no-winner case).
* ``check_state``- winner detection plus draw-when-full and mid-game ``None``.
* ``best_move``  - the bot AI: take an immediate win, else block the human's
  immediate win, else fall back to a random empty cell; ``None`` when full.

The View is built with a minimal stand-in exposing only ``.id`` (all that
``AuthorView.__init__`` needs), and ``.board`` is set directly to drive each
scenario. No network, database, Discord, or Lavalink access is involved.

The file also carries a suite-wide guard (see ``test_no_view_internal_method_collision``)
against re-introducing the production crash where a ``discord.ui.View`` subclass
method shadowed discord.py's internal ``View._refresh(self, components)``.
"""

import importlib
import pkgutil

import discord

from cogs.fun.games import WIN_LINES, TicTacToeView

X = TicTacToeView.X          # "X"  - the human
O = TicTacToeView.BOT_MARK   # "O"  - the bot
DRAW = TicTacToeView.DRAW    # "draw"

# A full board with no three-in-a-row (a genuine draw):
#   X O X
#   X O O
#   O X X
DRAW_BOARD = [X, O, X, X, O, O, O, X, X]


class _Player:
    """Minimal stand-in for ``discord.abc.User`` - only ``.id`` is read."""

    def __init__(self, user_id=123):
        self.id = user_id


def _make_view(user_id=123):
    """Construct a real :class:`TicTacToeView` for instance-method tests."""
    return TicTacToeView(_Player(user_id))


def _line_board(a, b, c, mark=X):
    """A board where only cells ``a``, ``b``, ``c`` hold ``mark``."""
    board = [None] * 9
    board[a] = board[b] = board[c] = mark
    return board


# ---------------------------------------------------------------------------
# _winner  (staticmethod)
# ---------------------------------------------------------------------------


def test_winner_detects_each_row():
    assert TicTacToeView._winner(_line_board(0, 1, 2)) == X
    assert TicTacToeView._winner(_line_board(3, 4, 5)) == X
    assert TicTacToeView._winner(_line_board(6, 7, 8)) == X


def test_winner_detects_each_column():
    assert TicTacToeView._winner(_line_board(0, 3, 6)) == X
    assert TicTacToeView._winner(_line_board(1, 4, 7)) == X
    assert TicTacToeView._winner(_line_board(2, 5, 8)) == X


def test_winner_detects_both_diagonals():
    assert TicTacToeView._winner(_line_board(0, 4, 8)) == X
    assert TicTacToeView._winner(_line_board(2, 4, 6)) == X


def test_winner_returns_the_marking_player():
    # A line owned by O must report O, not X.
    assert TicTacToeView._winner(_line_board(0, 1, 2, mark=O)) == O


def test_winner_none_on_empty_board():
    assert TicTacToeView._winner([None] * 9) is None


def test_winner_none_when_no_line_complete():
    # Marks present but no full line -> None (not a false positive).
    board = [X, O, X, None, None, None, None, None, None]
    assert TicTacToeView._winner(board) is None


def test_win_lines_are_the_eight_expected_lines():
    # Guards the constant the AI relies on: 3 rows, 3 cols, 2 diagonals.
    assert set(WIN_LINES) == {
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    }


# ---------------------------------------------------------------------------
# check_state
# ---------------------------------------------------------------------------


def test_check_state_x_win():
    view = _make_view()
    view.board = [X, X, X, None, None, None, None, None, None]
    assert view.check_state() == X


def test_check_state_o_win():
    view = _make_view()
    view.board = [O, O, O, None, None, None, None, None, None]
    assert view.check_state() == O


def test_check_state_draw_when_full():
    view = _make_view()
    view.board = list(DRAW_BOARD)
    assert view.check_state() == DRAW


def test_check_state_none_mid_game():
    view = _make_view()
    view.board = [X, O, None, None, None, None, None, None, None]
    assert view.check_state() is None


def test_check_state_prefers_winner_over_draw_on_full_board():
    # A full board that also contains a winning line reports the winner, not draw.
    view = _make_view()
    view.board = [X, X, X, O, O, X, O, X, O]  # top row is X, board is full
    assert view.check_state() == X


# ---------------------------------------------------------------------------
# best_move
# ---------------------------------------------------------------------------


def test_best_move_takes_immediate_win():
    view = _make_view()
    # O holds cells 0 and 1; completing at cell 2 wins immediately.
    view.board = [O, O, None, None, None, None, None, None, None]
    assert view.best_move() == 2
    # best_move probes cells in place but must restore the board afterwards.
    assert view.board == [O, O, None, None, None, None, None, None, None]


def test_best_move_blocks_opponent_immediate_win():
    view = _make_view()
    # X threatens to complete the top row at cell 2; O cannot win anywhere,
    # so the bot must block at cell 2.
    view.board = [X, X, None, None, None, None, None, None, None]
    assert view.best_move() == 2
    assert view.board == [X, X, None, None, None, None, None, None, None]


def test_best_move_prefers_winning_over_blocking():
    view = _make_view()
    # O can win at 2 (row 0); X could win at 5 (row 1). Winning beats blocking.
    view.board = [O, O, None, X, X, None, None, None, None]
    assert view.best_move() == 2
    assert view.board == [O, O, None, X, X, None, None, None, None]


def test_best_move_returns_none_on_full_board():
    view = _make_view()
    view.board = list(DRAW_BOARD)
    assert view.best_move() is None
    assert view.board == list(DRAW_BOARD)


def test_best_move_fallback_is_a_valid_empty_cell():
    view = _make_view()
    # No immediate win/block available: a single lone X in the centre.
    view.board = [None, None, None, None, X, None, None, None, None]
    move = view.best_move()
    assert move in {0, 1, 2, 3, 5, 6, 7, 8}
    assert view.board[move] is None  # fallback does not mutate the board


# ---------------------------------------------------------------------------
# Regression guard: no View/Modal subclass may shadow a discord.py internal.
# ---------------------------------------------------------------------------
#
# A production crash was caused by a discord.ui.View subclass defining a method
# named ``_refresh``, which shadowed discord.py's internal
# ``View._refresh(self, components)``; on a MESSAGE_UPDATE discord.py called it
# with a components list and the bot crashed. The fix renamed it to
# ``_rerender``. This guard fails if any View/Modal subclass reintroduces such a
# collision by defining a single-underscore name that also exists on the base.


def _import_all_cogs():
    """Best-effort import of every ``cogs.*`` module so subclasses register.

    The sonolink stub and config bootstrap are already installed by conftest at
    collection time, so modules import cleanly here (and against the real
    sonolink on 3.12+ CI). Any module that still fails is skipped rather than
    failing this guard, so a broken unrelated cog cannot mask the check.
    """
    import cogs

    for module_info in pkgutil.walk_packages(cogs.__path__, cogs.__name__ + "."):
        try:
            importlib.import_module(module_info.name)
        except Exception:
            # An unrelated import failure must not turn this guard red.
            pass


def _all_subclasses(base):
    seen = set()
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


def _single_underscore_internals(base):
    """Names on ``base`` that start with one underscore (not dunder/mangled)."""
    return {
        name
        for name in dir(base)
        if name.startswith("_")
        and not name.startswith("__")
        and not name.startswith("_" + base.__name__ + "__")
    }


def test_no_view_internal_method_collision():
    _import_all_cogs()

    bases = (discord.ui.View, discord.ui.Modal)
    internal_names = set()
    for base in bases:
        internal_names |= _single_underscore_internals(base)

    subclasses = set()
    for base in bases:
        subclasses |= _all_subclasses(base)

    # The base classes themselves are excluded (they legitimately *define* the
    # internals). Only check user-defined subclasses' own namespaces.
    subclasses -= set(bases)

    offenders = {}
    for cls in subclasses:
        collisions = set(vars(cls)) & internal_names
        if collisions:
            offenders[f"{cls.__module__}.{cls.__qualname__}"] = sorted(collisions)

    assert not offenders, (
        "View/Modal subclass(es) shadow a discord.py internal method "
        "(the _refresh crash class): " + repr(offenders)
    )


def test_tictactoeview_does_not_shadow_internals():
    # Focused check on the module under test, independent of the broad sweep.
    internal_names = _single_underscore_internals(discord.ui.View)
    assert not (set(vars(TicTacToeView)) & internal_names)
    # And the specific historical offender is absent.
    assert "_refresh" not in vars(TicTacToeView)
