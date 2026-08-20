"""Third-party text must not become markup in a message the bot signs.

Two reachable holes, both closed by :mod:`cogs.music.safetext`:

* A TRACK TITLE was interpolated straight into the now-playing controller's
  ``## [{title}]({uri})`` header. A title is not ours: on an HTTP-source track it
  is ICY / ID3 metadata written by whoever hosts the file, and a ``]`` in it
  closes the link label early, so everything after it is markdown THEY wrote -
  a masked link to their domain, published publicly by the bot, in someone
  else's server. Same shape in the favourites card and in every queue / history
  line.
* ``/serverplaylist`` echoed a name argument into a PUBLIC message with the
  markdown live and mentions unsuppressed - a name of ``<@id>`` pinged (the
  bot's global allowed_mentions leaves user mentions ON), a name of
  ``[click](https://evil)`` published a link, and on the delete / play / rename
  lookups the argument was never even length-checked.

Everything here is pure or driven on fakes - no Discord, no node, no database.
"""

import re
import types

import discord
import pytest

# music.py must be imported before views.py: it is the package's import entry
# point and views.py imports back from it (see views.py's module docstring).
from cogs.music import music as _music  # noqa: F401
from cogs.music import playlists_shared as ps
from cogs.music import safetext, views

# ---------------------------------------------------------------------------
# What Discord actually renders as a link
# ---------------------------------------------------------------------------

# An UNESCAPED ``[``, a label that consumes ``\x`` escape PAIRS whole, then the
# first ``]`` that is not itself part of a pair, then ``(url)``. This is the
# whole point of the fix: a backslash-escaped bracket is inert markup, so
# asserting on the raw substring ``](`` proves nothing - only the set of urls
# that really become links does. Consuming pairs (rather than a lookbehind for
# a bare backslash) is what makes ``\\`` read the way Discord reads it: an
# escaped BACKSLASH, after which a ``]`` really does close the label.
_LINK = re.compile(r"(?<!\\)\[(?:[^\]\\]|\\.)*\]\(([^)\s]+)\)")


def _link_targets(text):
    """Every url ``text`` would render as a masked link to."""
    return _LINK.findall(text)


def test_the_link_model_itself_is_honest():
    """A sanity check on the assertion tool: it must see a REAL link, and only it."""
    assert _link_targets("[a](https://ok.example)") == ["https://ok.example"]
    assert _link_targets("\\[a](https://evil.example)") == []


# ---------------------------------------------------------------------------
# safetext.link_label - the link-breakout rule
# ---------------------------------------------------------------------------


def test_a_bracket_cannot_close_the_label_early():
    """THE ATTACK, in one line."""
    hostile = "x](https://evil.example) [free nitro"
    header = "## [{0}]({1})".format(
        safetext.link_label(hostile), "https://youtube.com/watch?v=real"
    )
    # Exactly one link renders, and it is the one WE built.
    assert _link_targets(header) == ["https://youtube.com/watch?v=real"]


def test_the_unguarded_header_really_was_exploitable():
    """The counter-test that makes the one above mean something."""
    hostile = "x](https://evil.example) [free nitro"
    unguarded = "## [{0}]({1})".format(hostile, "https://youtube.com/watch?v=real")
    assert "https://evil.example" in _link_targets(unguarded)


def test_the_escapes_are_discords_own_so_a_real_title_still_reads():
    assert safetext.link_label("Song [Remix]") == "Song \\[Remix\\]"


def test_a_newline_cannot_forge_a_heading_inside_the_panel():
    assert "\n" not in safetext.link_label("Nice song\n## Server rules updated")


def test_link_label_is_none_safe_and_stringifies():
    assert safetext.link_label(None) == "None"
    assert safetext.link_label(12) == "12"


# ---------------------------------------------------------------------------
# safetext.public_echo - quoting somebody else's text back
# ---------------------------------------------------------------------------


def test_a_masked_link_is_quoted_as_text_not_published_as_a_link():
    echoed = safetext.public_echo("[click here](https://evil.example)")
    assert _link_targets(echoed) == []
    assert "click here" in echoed  # defused, not deleted


@pytest.mark.parametrize("raw", ["**bold**", "`code`", "~~strike~~", "||spoiler||"])
def test_inline_markdown_is_neutralised(raw):
    assert "\\" in safetext.public_echo(raw)


def test_a_newline_and_a_heading_cannot_survive_together():
    echoed = safetext.public_echo("hello\n## Announcement")
    assert "\n" not in echoed


def test_a_long_value_is_clipped_with_an_ascii_ellipsis():
    echoed = safetext.public_echo("a" * 500, limit=20)
    assert echoed.endswith("...")
    assert len(echoed) == 20


def test_a_short_value_is_left_alone():
    assert safetext.public_echo("Chill Beats") == "Chill Beats"


# ---------------------------------------------------------------------------
# The now-playing controller header (the reported site)
# ---------------------------------------------------------------------------


class _Track:
    def __init__(self, title, uri="https://youtube.com/watch?v=real"):
        self.title = title
        self.author = "Artist"
        self.identifier = "id"
        self.uri = uri
        self.length = 200000
        self.is_stream = False
        self.extras = types.SimpleNamespace(requester=None, radio=False)


class _Queue:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.mode = None


class _Player:
    def __init__(self, track, upcoming=()):
        self.current = track
        self.queue = _Queue(upcoming)
        self.paused = False
        self.volume = 50
        self.position = 0
        self.autoplay = None
        self.radio_genre = None
        self.channel = types.SimpleNamespace(name="General")
        self.home = None
        self.dj = None


def _rendered_text(view):
    """Every TextDisplay string the built layout holds."""
    out = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            out.append(item.content)
    return "\n".join(out)


def _controller(track, upcoming=()):
    cog = types.SimpleNamespace()
    return views.MusicController(cog, _Player(track, upcoming))


def test_the_controller_header_cannot_be_broken_out_of():
    hostile = "x](https://evil.example) [free nitro"
    text = _rendered_text(_controller(_Track(hostile)))
    # The attacker's url never becomes the target of a link; ours still does.
    assert _link_targets(text) == ["https://youtube.com/watch?v=real"]


def test_a_hostile_title_cannot_forge_a_heading_in_the_up_next_block():
    upcoming = [_Track("innocent\n## Moderator notice: click here")]
    text = _rendered_text(_controller(_Track("Now"), upcoming))
    assert "\n## Moderator notice" not in text


def test_an_ordinary_title_is_still_rendered_verbatim():
    text = _rendered_text(_controller(_Track("One More Time")))
    assert "## [One More Time](https://youtube.com/watch?v=real)" in text


# ---------------------------------------------------------------------------
# ... and so does the OTHER half of the link
# ---------------------------------------------------------------------------
# Escaping the label leaves the target untouched, and a url may legally contain
# ``)`` - which is exactly where Discord ends the link. So a track whose URI is
# ``https://evil.example/a)[FREE NITRO](https://evil.example/phish`` still made
# the bot publish a second, entirely attacker-chosen masked link right behind
# the first one. The prerequisite is the same as for the title: an
# attacker-hosted HTTP-source track, whose uri is the requester's own URL and
# passes urlguard because the host is public.

_BREAKOUT_URI = (
    "https://evil.example/a)[FREE NITRO - CLICK](https://evil.example/phish"
)


def test_a_hostile_uri_cannot_open_a_second_link_in_the_header():
    text = _rendered_text(_controller(_Track("Totally Normal Song", _BREAKOUT_URI)))

    assert "https://evil.example/phish" not in _link_targets(text)
    assert len(_link_targets(text)) <= 1


def test_link_target_cannot_return_a_closing_paren():
    assert ")" not in safetext.link_target(_BREAKOUT_URI)


def test_link_target_refuses_a_scheme_discord_would_not_follow():
    """None means 'draw no link', and every call site has a plain-text branch."""
    for hostile in ("javascript:alert(1)", "file:///etc/passwd", "", None, "ytsearch:x"):
        assert safetext.link_target(hostile) is None


def test_link_target_drops_whitespace_that_would_end_the_target():
    assert " " not in safetext.link_target("https://evil.example/a b")
    assert "\n" not in safetext.link_target("https://evil.example/a\nb")


def test_an_ordinary_url_survives_link_target_unchanged():
    for ordinary in (
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/x?t=12&list=PL1",
        "http://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl",
    ):
        assert safetext.link_target(ordinary) == ordinary


def test_a_url_with_real_parentheses_stays_clickable():
    """Percent-encoding rather than refusing: a path that legitimately contains
    parentheses (Wikipedia-style) must still work, decoded by the server."""
    target = safetext.link_target("https://ex.example/Foo_(band)/x")

    assert target == "https://ex.example/Foo_%28band%29/x"


def test_a_title_ending_in_a_backslash_cannot_eat_the_closing_bracket():
    r"""A backslash escapes the NEXT character, so a title ending in one ate the
    ``]`` this module wrote and the link silently stopped being a link. Escaping
    the backslash first is what keeps the label's own escapes ours."""
    text = _rendered_text(_controller(_Track("Song\\")))

    assert _link_targets(text) == ["https://youtube.com/watch?v=real"]


def test_link_label_clips_before_it_escapes():
    """The cap has to govern what the TITLE said, not how many backslashes it
    took to defuse it - otherwise an all-bracket title loses two thirds of its
    text to the escaping and the reader sees a stub."""
    clipped = safetext.link_label("[" * 100, limit=10)

    assert clipped.count("[") == 7  # 10 minus the "..."
    assert clipped.endswith("...")


def test_code_span_cannot_be_closed_from_inside():
    """Inside a code span markdown is already inert and the ONE way out is a
    backtick - so it is removed, not escaped (escaping shows the backslash)."""
    assert "`" not in safetext.code_span("evil`**bold**`")
    assert safetext.code_span("Daft Punk") == "Daft Punk"
    assert "\\" not in safetext.code_span("AC*DC")


# ---------------------------------------------------------------------------
# /serverplaylist echoes: defused AND unable to ping
# ---------------------------------------------------------------------------


def test_echo_name_defuses_and_clips():
    assert ps.echo_name("  Chill   Beats ") == "Chill Beats"
    assert len(ps.echo_name("a" * 4000)) <= ps.MAX_NAME_LEN + 20


def test_echo_name_kills_a_masked_link():
    assert _link_targets(ps.echo_name("[click](https://evil.example)")) == []


class _Ctx:
    def __init__(self, guild_id=99, author_id=7, manager=False):
        self.guild = types.SimpleNamespace(id=guild_id, name="Guild")
        self.author = types.SimpleNamespace(id=author_id)
        self.sends = []
        self.manager = manager

    async def defer(self, *_a, **_kw):
        return None

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))


def _mixin(row=None):
    cog = ps.ServerPlaylistMixin.__new__(ps.ServerPlaylistMixin)
    cog._has_manage_guild = lambda actor: False

    async def fetch(_guild_id, _norm):
        return row

    async def delete(_guild_id, _norm):
        return None

    cog._fetch_guild_playlist = fetch
    cog._delete_guild_playlist = delete
    cog._nodes_available = lambda: True
    return cog


async def test_the_not_found_echo_cannot_ping():
    """THE HOLE: a name argument straight into a public message."""
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_delete.callback(
        _mixin(row=None), ctx, name="<@1234> <@&5678> @everyone"
    )
    (args, kwargs) = ctx.sends[-1]
    mentions = kwargs.get("allowed_mentions")
    assert mentions is not None
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.everyone is False


async def test_the_not_found_echo_is_defused_and_bounded():
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_delete.callback(
        _mixin(row=None), ctx, name="[click](https://evil.example)" + "z" * 4000
    )
    content = ctx.sends[-1][0][0]
    assert len(content) < 200
    assert _link_targets(content) == []


async def test_the_delete_confirmation_defuses_the_stored_name():
    """The stored name is somebody else's text too."""
    row = {"name": "**@everyone** [x](https://evil.example)", "creator_id": 7}
    ctx = _Ctx()
    await ps.ServerPlaylistMixin.serverplaylist_delete.callback(
        _mixin(row=row), ctx, name="whatever"
    )
    (args, kwargs) = ctx.sends[-1]
    assert _link_targets(args[0]) == []
    assert "\\*\\*" in args[0]  # the forged bold is inert too
    assert kwargs["allowed_mentions"].everyone is False


def test_every_serverplaylist_name_echo_passes_allowed_mentions():
    """A structural guard: no send in this module may quote a name unprotected.

    Cheap to add a new reply and forget the pairing, and the failure is silent
    (it only shows up as a ping in production), so the file is checked as text.
    """
    import inspect
    import re

    source = inspect.getsource(ps)
    # Every ctx.send(...) call that interpolates a name must carry the pairing.
    for call in re.findall(r"await ctx\.send\((?:[^()]|\([^()]*\))*\)", source):
        if "name=" not in call and "{name}" not in call:
            continue
        assert "allowed_mentions" in call, call
        assert "echo_name" in call, call


# ---------------------------------------------------------------------------
# The favourites card and the lyrics header draw the same link
# ---------------------------------------------------------------------------
# Both are the same two-halves hazard as the controller header. The favourites
# one is worse in one respect: the title AND the uri are STORED, so rows written
# before this guard existed are still rendered through it here.


def _favourites_text(title, uri):
    owner = types.SimpleNamespace(id=1, display_name="Someone")
    rows = [{"title": title, "author": "Artist", "uri": uri, "encoded": "x"}]
    card = views.FavouritesCard(types.SimpleNamespace(), 1, owner, rows)
    return _rendered_text(card)


def test_a_stored_hostile_uri_cannot_open_a_second_link_in_the_favourites_card():
    text = _favourites_text("Totally Normal Song", _BREAKOUT_URI)

    assert "https://evil.example/phish" not in _link_targets(text)


def test_an_ordinary_favourite_still_links():
    text = _favourites_text("One More Time", "https://youtu.be/abc")

    assert _link_targets(text) == ["https://youtu.be/abc"]


def test_the_lyrics_header_guards_both_halves_too():
    """The synced card posts this header into a PUBLIC, live-updating message."""
    from cogs.music import lyrics

    header = lyrics._track_header(_Track("x](https://evil.example) [nitro",
                                         _BREAKOUT_URI))

    # Exactly ONE link, and it is not a second one of their choosing: the whole
    # hostile uri collapsed into a single (percent-encoded) target. Linking a
    # requester-hosted track's own url is the accepted premise; letting them
    # append a second masked link behind it was the hole.
    targets = _link_targets(header)
    assert len(targets) == 1
    assert "https://evil.example/phish" not in targets
