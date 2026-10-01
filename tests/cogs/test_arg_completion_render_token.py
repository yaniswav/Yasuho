"""``_CompletionView._render_token`` must round-trip through a REAL StringView.

discord.ext.commands.view.StringView.get_quoted_word only unescapes a
backslash when it sits immediately before the closing quote character;
every other backslash (including a run of several) passes through
completely literally. The old renderer doubled every backslash before
quoting, which StringView never collapses back - a value with a backslash
plus a space or a quote round-tripped to the wrong string.
"""

import pytest
from discord.ext.commands.view import StringView

from cogs.system.arg_completion import _CompletionView, _Field, _UnrepresentableValue


def _render(value):
    field = _Field("text", "str", True, False, None)
    return _CompletionView._render_token(None, field, value)


def _reparse(token):
    """Feed one rendered token through a real StringView, as get_quoted_word would."""
    view = StringView(token)
    view.skip_ws()
    return view.get_quoted_word()


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "has a space",
        'has a "quote" inside',
        "back\\slash in the middle",
        "ends right before a quote\\\" marker",
        "multiple\\\\backslashes\\\\here",
    ],
)
def test_render_then_reparse_is_identity(value):
    token = _render(value)
    assert _reparse(token) == value


def test_backslash_before_quote_round_trips():
    value = 'a\\"b'  # literal: a, backslash, quote, b
    token = _render(value)
    assert _reparse(token) == value


def test_trailing_backslash_with_a_space_is_refused_not_corrupted():
    # Needs quoting (has a space) AND ends in a backslash: the trailing
    # backslash would sit right before our closing quote and get eaten as
    # an escape, so the renderer must refuse rather than hand back a token
    # that corrupts on re-parse.
    value = "needs space\\"
    with pytest.raises(_UnrepresentableValue):
        _render(value)
