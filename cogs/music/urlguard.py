"""SSRF guard for the user-typed queries this cog hands to Lavalink.

WHY THIS EXISTS
---------------
Lavalink runs here with the HTTP source manager ON (``lavalink/application.yml``,
``lavalink.server.sources.http: true``) so that a bare ``.mp3`` link plays. That
source manager fetches whatever URL it is handed, FROM THE LAVALINK HOST - which
is the bot's own box, sitting next to a loopback Postgres, the Lavalink REST port
itself (``127.0.0.1:2333``, whose password lives in that same yml) and, on a
cloud box, the instance metadata service at ``169.254.169.254``.

sonolink decides what reaches ``/loadtracks`` raw with exactly one line
(``gateway/node/_rest.py``)::

    is_url = query.startswith(("http://", "https://"))

Everything else is prefixed into a search (``ytsearch:...``) and is therefore
inert. So an ordinary member typing ``/play http://127.0.0.1:2333/v4/info`` -
no permissions, no voice channel needed - turned the bot into a blind port
scanner and request forger against our own infrastructure. That is the hole this
module closes, bot-side, BEFORE the query is handed over.

WHAT IT REFUSES
---------------
Only URL-SHAPED queries are examined at all (``scheme://...``); a plain text
search is not a URL and is never touched, so ``/play daft punk`` costs nothing
here and behaves byte-identically to before.

* a scheme that is not ``http`` or ``https`` (``file://``, ``gopher://``,
  ``ftp://``, ``dict://``, ...). sonolink already turns those into search text
  today, so this is defence in depth: it stops being free the day that one line
  changes or the ``local`` source manager is flipped on.
* a URL with no host at all (``http:///etc/passwd``).
* a host that resolves to an address the bot must never be aimed at: loopback,
  link-local (metadata!), RFC1918 / unique-local, CGNAT, multicast, reserved,
  unspecified, and the IPv6 wrappers that smuggle one of those in
  (``::ffff:127.0.0.1``, ``2002::/16`` 6to4, ``64:ff9b::/96`` NAT64).

DNS REBINDING is why :func:`check_query` resolves the host itself and checks
EVERY address ``getaddrinfo`` returns, rather than the first: a name answering
``A 1.2.3.4`` **and** ``A 127.0.0.1`` in one reply is the classic bypass, and one
bad address in the set is enough to refuse the whole URL. Obfuscated literals
(``http://2130706433/``, ``http://0177.0.0.1/``) need no special case: they do
not parse as an IP, so they go through the resolver, which normalises them to
127.0.0.1 and they are refused on the address check.

WHAT IT STILL ALLOWS
--------------------
Every ordinary public link: a youtube.com / youtu.be / soundcloud.com /
open.spotify.com / bandcamp URL resolves to a global address and passes. The
guard is a host check, never a domain allow-list, so no new source ever has to
be registered here to keep working.

RESIDUAL RISK (deliberately stated, not fixable from the bot)
------------------------------------------------------------
Lavalink resolves the name AGAIN when it fetches, and follows redirects. A
hostile name can therefore answer "public" to us and "127.0.0.1" to Lavalink a
moment later, or a public URL can 302 into the loopback. This check raises the
bar from "trivially exploitable by any member" to "needs a rebinding or redirect
setup"; the complete fix is ``sources.http: false`` in application.yml (an ops
change, outside this package). Both facts belong in the same sentence whenever
this module is described.

Everything below is pure except :func:`resolve_addresses`, which is the single
``getaddrinfo`` call and is injectable so the whole guard is unit-tested with no
network at all.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import typing
from urllib.parse import urlsplit

# The only schemes Lavalink may be aimed at from a user-typed query.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Reason codes. Returned for LOGS only - every one of them maps to the same
# single user-facing sentence in the cog, deliberately: telling a member WHICH
# check refused their URL turns the bot into an oracle that answers "does
# 10.0.0.7 exist on your network?" one query at a time.
REASON_SCHEME = "scheme"
REASON_NO_HOST = "no-host"
REASON_BLOCKED_ADDRESS = "blocked-address"
REASON_UNRESOLVABLE = "unresolvable"

# NAT64. An address in this prefix carries an IPv4 address in its low 32 bits and
# is the one "is_global == True" way to write 127.0.0.1 as IPv6.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

_IPAddress = typing.Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def is_url_shaped(query: typing.Optional[str]) -> bool:
    """True when ``query`` is written as a URL (``scheme://...``), not free text.

    Deliberately structural, not a validity test: anything with a scheme and a
    ``//`` is claiming to be a URL and gets checked, and anything else is a
    search term this guard has no business touching. Kept here rather than
    imported from ``search.is_url_query`` so this module depends on nothing
    inside the package and can be imported (and tested) on its own.
    """
    text = (query or "").strip()
    scheme, sep, _rest = text.partition("://")
    if not sep or not scheme:
        return False
    head = scheme[0]
    return (head.isascii() and head.isalpha()) and all(
        char.isascii() and (char.isalnum() or char in "+-.") for char in scheme
    )


def split_target(query: str) -> typing.Tuple[str, typing.Optional[str]]:
    """``(scheme, host)`` for a URL-shaped ``query``; ``host`` is None when absent.

    ``urlsplit`` is what strips userinfo (``http://youtube.com@127.0.0.1/`` has
    a HOST of ``127.0.0.1``, not youtube.com - the oldest trick in this family)
    and unwraps the brackets of an IPv6 literal. The scheme is lowercased so
    ``HTTP://`` is the same decision as ``http://``.
    """
    parts = urlsplit((query or "").strip())
    try:
        host = parts.hostname
    except ValueError:
        # A malformed authority (e.g. a bad IPv6 literal) - no host we can vet.
        return parts.scheme.lower(), None
    return parts.scheme.lower(), (host or None)


def is_blocked_address(raw: typing.Any) -> bool:
    """True when ``raw`` is an address the bot host must never be pointed at.

    FAILS CLOSED: anything that does not parse as an IP is refused, because the
    only way a non-address reaches this function is a resolver returning
    something unexpected, and "I could not tell" must never read as "allowed".

    ``is_global`` alone is not enough - it is True for multicast and for NAT64 -
    so the check is the union of every "not the public internet" predicate plus
    the unwrapping below.
    """
    try:
        ip = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return True
    return any(_is_blocked_ip(candidate) for candidate in _candidates(ip))


def _candidates(ip: _IPAddress) -> typing.Iterator[_IPAddress]:
    """``ip`` itself, plus any IPv4 address smuggled inside an IPv6 wrapper.

    ``::ffff:a.b.c.d`` (v4-mapped), ``2002:AABB:CCDD::`` (6to4) and
    ``64:ff9b::a.b.c.d`` (NAT64) all name an IPv4 destination while looking like
    an ordinary IPv6 address; the last one is the only one Python still calls
    global. Judging the wrapper AND its payload means a blocked v4 address stays
    blocked however it is spelled.
    """
    yield ip
    if ip.version != 6:
        return
    for attr in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(ip, attr, None)
        if embedded is not None:
            yield embedded
    teredo = getattr(ip, "teredo", None)
    if teredo is not None:
        yield from teredo
    if ip in _NAT64:
        yield ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


def _is_blocked_ip(ip: _IPAddress) -> bool:
    """The per-address verdict (see :func:`is_blocked_address` for the why)."""
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_unspecified
    )


async def resolve_addresses(host: str) -> typing.List[str]:
    """Every address ``host`` resolves to, as strings (the one I/O call here).

    ``loop.getaddrinfo`` so the lookup never blocks the event loop. A host that
    is already an IP literal is answered by the resolver without a query, so no
    special case is needed for one.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_query(
    query: typing.Optional[str],
    *,
    resolver: typing.Optional[typing.Callable[[str], typing.Awaitable]] = None,
) -> typing.Optional[str]:
    """``None`` when ``query`` is safe to hand to Lavalink, else a reason code.

    The whole guard, in the order that costs the least: a non-URL query returns
    immediately (the overwhelmingly common case - no parse, no lookup), then the
    two pure checks, and only a URL that survives both pays for a DNS lookup.

    ``resolver`` is injectable purely so the tests drive every branch - including
    the multi-answer rebinding case - without a network.
    """
    if not is_url_shaped(query):
        return None

    scheme, host = split_target(query or "")
    if scheme not in ALLOWED_SCHEMES:
        return REASON_SCHEME
    if not host:
        return REASON_NO_HOST

    resolve = resolver or resolve_addresses
    try:
        addresses = await resolve(host)
    except Exception:
        # A name we cannot resolve is a name we cannot vouch for. Refusing also
        # keeps the bot from being a DNS-existence oracle for internal names.
        return REASON_UNRESOLVABLE
    if not addresses:
        return REASON_UNRESOLVABLE
    # EVERY answer, not the first: one poisoned record in the set is the whole
    # point of a rebinding reply.
    if any(is_blocked_address(address) for address in addresses):
        return REASON_BLOCKED_ADDRESS
    return None
