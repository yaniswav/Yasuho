"""Unit tests for :mod:`tools.config_loader`.

Covers the pure ``ConfigLoader._unquote`` helper (one matching quote pair
stripped; unquoted values untouched; mismatched quotes untouched) and the
typed accessors ``getstr`` / ``getlist`` / ``getlistint`` driven off a real
temp ``.ini`` read into a fresh loader. No network, DB, Discord, or Lavalink
is touched: the loader is built empty and fed a local file.
"""

from tools.config_loader import ConfigLoader


def _loader_from_ini(tmp_path, text):
    """Build a ConfigLoader with no bundled files, then read a temp .ini.

    ``ConfigLoader()`` with no file names skips its config/ read loop, giving an
    empty parser. We then load our own file the same way the class does
    (``read(path, encoding='utf-8')``) so behaviour matches production exactly.
    """
    path = tmp_path / "sample.ini"
    path.write_text(text, encoding="utf-8")
    loader = ConfigLoader()
    loader.read(str(path), encoding="utf-8")
    return loader


# ---------------------------------------------------------------------------
# _unquote (pure staticmethod)
# ---------------------------------------------------------------------------


def test_unquote_strips_double_quote_pair():
    assert ConfigLoader._unquote('"hello"') == "hello"


def test_unquote_strips_single_quote_pair():
    assert ConfigLoader._unquote("'hello'") == "hello"


def test_unquote_strips_surrounding_whitespace_before_quotes():
    assert ConfigLoader._unquote('   "hi"   ') == "hi"


def test_unquote_keeps_inner_quotes_only_one_pair():
    # Only a single outer pair is removed; inner quotes survive.
    assert ConfigLoader._unquote('""double""') == '"double"'


def test_unquote_leaves_unquoted_value():
    assert ConfigLoader._unquote("plain") == "plain"


def test_unquote_leaves_value_with_matching_non_quote_ends():
    # First == last but not a quote char -> unchanged.
    assert ConfigLoader._unquote("alpha") == "alpha"


def test_unquote_leaves_mismatched_quote_kinds():
    # Leading double, trailing single -> not a matching pair.
    assert ConfigLoader._unquote("\"hello'") == "\"hello'"


def test_unquote_leaves_only_leading_quote():
    assert ConfigLoader._unquote('"hello') == '"hello'


def test_unquote_leaves_only_trailing_quote():
    assert ConfigLoader._unquote('hello"') == 'hello"'


def test_unquote_single_quote_char_too_short():
    # len < 2 guard: a lone quote is returned as-is, not stripped to "".
    assert ConfigLoader._unquote('"') == '"'


def test_unquote_empty_string():
    assert ConfigLoader._unquote("") == ""


def test_unquote_whitespace_only_collapses_to_empty():
    assert ConfigLoader._unquote("   ") == ""


# ---------------------------------------------------------------------------
# getstr
# ---------------------------------------------------------------------------


def test_getstr_strips_surrounding_quotes(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        "[emojis]\n"
        'yes = "<:yes:1>"\n'
        "single = 'plain'\n"
        "bare = nostrip\n"
        'mismatch = "oops\n',
    )
    assert loader.getstr("emojis", "yes") == "<:yes:1>"
    assert loader.getstr("emojis", "single") == "plain"
    assert loader.getstr("emojis", "bare") == "nostrip"
    # Mismatched (only a leading quote) is left intact.
    assert loader.getstr("emojis", "mismatch") == '"oops'


def test_getstr_preserves_inner_spaces(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        '[section]\nname = "hello world"\n',
    )
    assert loader.getstr("section", "name") == "hello world"


# ---------------------------------------------------------------------------
# getlist
# ---------------------------------------------------------------------------


def test_getlist_splits_and_unquotes_nonempty_lines(tmp_path):
    # A blank continuation line in the middle must be dropped, and each
    # remaining line unquoted independently.
    loader = _loader_from_ini(
        tmp_path,
        "[lists]\n"
        "roles =\n"
        '    "admin"\n'
        "    'mod'\n"
        "\n"
        "    member\n",
    )
    assert loader.getlist("lists", "roles") == ["admin", "mod", "member"]


def test_getlist_single_value(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        '[lists]\nonly = "solo"\n',
    )
    assert loader.getlist("lists", "only") == ["solo"]


def test_getlist_drops_whitespace_only_lines(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        "[lists]\n"
        "items =\n"
        "    one\n"
        "    two\n",
    )
    assert loader.getlist("lists", "items") == ["one", "two"]


# ---------------------------------------------------------------------------
# getlistint
# ---------------------------------------------------------------------------


def test_getlistint_casts_each_entry(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        "[ids]\n"
        "channels =\n"
        '    "111"\n'
        "    222\n"
        "    '333'\n",
    )
    result = loader.getlistint("ids", "channels")
    assert result == [111, 222, 333]
    assert all(isinstance(x, int) for x in result)


def test_getlistint_single_line(tmp_path):
    loader = _loader_from_ini(
        tmp_path,
        "[ids]\nowner = 42\n",
    )
    assert loader.getlistint("ids", "owner") == [42]
