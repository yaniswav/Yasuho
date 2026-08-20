"""Making third-party text safe to quote into a message the bot signs.

Three hazards, one module, no state - all of them cases of "somebody else wrote
this string and Discord is about to read it as markup":

* :func:`link_label` - text going INSIDE a ``[label](url)`` link. A ``]`` closes
  the label early and everything after it is markup the string's author wrote:
  a track titled ``x](https://evil.example) [free nitro`` renders, in the
  now-playing panel, as a link to THEIR domain under a label of their choosing,
  posted publicly by the bot in somebody else's server. Track titles are fully
  attacker-controlled - the HTTP source manager takes its title straight from
  ICY / ID3 metadata on a file the requester hosts.
* :func:`link_target` - the OTHER half of the same link, and the half escaping
  the label cannot reach. A url may legally contain ``)``, and the first
  unescaped ``)`` is where Discord ends the link: a track whose URI is
  ``https://evil.example/a)[FREE NITRO - CLICK](https://evil.example/phish``
  makes the bot post a second, fully attacker-chosen masked link right after the
  first, and the URI is the requester's own URL (any public host passes
  ``urlguard``). Escaping only the label defuses exactly nothing on this vector.
* :func:`public_echo` - a user-typed argument quoted back into a public message
  (``There's no server playlist called **{name}**.``). Here the whole markdown
  vocabulary is in play, plus mentions, plus length.

REUSE NOTE. :func:`one_line` and :func:`public_echo` are re-exported from
``tools.formats``, which is where they now live: ``cogs.moderation`` needs the
echo rule too (a channel name a moderator typed, quoted back publicly), and a
third hand-written copy is how a rule like this drifts. They are still named
here so every call site in this package reads as one vocabulary. The link
halves stay local: they are the answer to a hazard this package owns.
``cogs.community.profile.presence`` solves the target half differently and
correctly - it VALIDATES a Spotify id and builds the url itself, which is
stronger than escaping and is available to it because it knows the one shape its
urls take. A music track URI can be any public URL, so escaping is the only
option here.
"""

from __future__ import annotations

import typing

from tools.formats import ECHO_LIMIT, one_line, public_echo

__all__ = [
    "ECHO_LIMIT",
    "one_line",
    "public_echo",
    "code_span",
    "link_label",
    "link_target",
]

# The only schemes a link target may carry. Discord only linkifies these anyway,
# and refusing everything else means a hostile ``uri`` can never become a
# clickable target of a scheme we did not think about.
LINK_SCHEMES = ("http://", "https://")

# Every character that is structural inside ``[label](target)``, percent-encoded.
# A url keeps working (a server receives ``%29`` and reads ``)``), and none of
# these can end the link early any more. ``\`` goes first, or encoding it would
# rewrite the ``%5C`` the others just produced.
_TARGET_ESCAPES = (
    ("\\", "%5C"),
    ("(", "%28"),
    (")", "%29"),
    ("[", "%5B"),
    ("]", "%5D"),
    ("<", "%3C"),
    (">", "%3E"),
)


def _clip(text: str, limit: typing.Optional[int]) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def link_label(text: typing.Any, *, limit: typing.Optional[int] = None) -> str:
    """``text``, flattened and escaped, safe inside a markdown link label.

    Three halves are load-bearing. Flattening removes the newline that would let
    the label open a heading. Escaping the brackets keeps the target the only
    structural part of the line - ``\\[`` is Discord's own escape and renders as
    a plain bracket, so a track legitimately titled ``Song [Remix]`` still reads
    exactly right. And the backslash is escaped FIRST, because a title ending in
    one would otherwise escape the closing ``]`` that this module wrote.

    ``limit`` clips BEFORE the escape, for the same reason ``public_echo`` does:
    clipping afterwards can cut a ``\\x`` pair in half and leave a trailing lone
    backslash that eats the closing bracket. Callers pass their cap here rather
    than slicing the result.
    """
    flattened = _clip(one_line(text), limit)
    return (
        flattened.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    )


def code_span(text: typing.Any, *, limit: int = ECHO_LIMIT) -> str:
    """``text`` made safe to drop INSIDE a ``\\`...\\``` code span.

    Inside a code span markdown is already inert - a ``*`` is a star, a
    ``[x](y)`` is text - and the ONE character that can end it is the backtick.
    So the right treatment is the opposite of :func:`public_echo`: escaping is
    both unnecessary and visible (``escape_markdown`` turns every ``*`` into a
    literal ``\\*``, which a code span then renders backslash and all - the
    reason an author with a backtick in their name showed a stray ``\\``). The
    backticks are removed instead, which is the only edit that can change what
    the span means, and everything else is passed through untouched.
    """
    return _clip(one_line(text), limit).replace("`", "")


def link_target(url: typing.Any) -> typing.Optional[str]:
    """``url`` made safe as the TARGET of ``[label](target)``, or ``None``.

    ``None`` means "do not draw a link at all" and every caller has a plain-text
    fallback for it, so a refusal costs a click and never a render.

    Two rules. The scheme must be http(s) - anything else is not a link Discord
    would follow and has no business being interpolated into one. And every
    character that is structural inside the link is percent-encoded, whitespace
    dropped: after this, the string provably cannot contain the ``)`` that ends
    the link, so no second link can be smuggled in behind it. Percent-encoding
    rather than refusing keeps ordinary URLs that legitimately contain
    parentheses (Wikipedia-style paths, some CDN links) clickable and correct.
    """
    text = "".join(str(url or "").split())
    if not text.lower().startswith(LINK_SCHEMES):
        return None
    for char, encoded in _TARGET_ESCAPES:
        text = text.replace(char, encoded)
    return text
