"""YouTube helpers for the upload alerts: input parsing, feed parsing, fetching.

Pure and network-light on purpose (the cog is cogs/config/youtube.py).

Only two outbound requests exist, both to ``www.youtube.com`` with a URL built
HERE from a fixed host (never from user text), redirects refused, bodies capped:

* the public per-channel Atom feed ``/feeds/videos.xml?channel_id=UC...`` (no API
  key; the 15 latest uploads; sends ``Cache-Control: max-age=900`` but no
  ETag / Last-Modified, so there is no conditional GET to make);
* for an ``@handle`` / ``/c/`` / ``/user/`` input, ONE page fetch whose
  ``<link rel="canonical">`` carries the channel id. From some regions YouTube
  answers 302 to a consent page; the ``SOCS=CAI`` cookie skips it.

Typography rule: ASCII '-' and '...' only.
"""

import datetime
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from tools.http import TIMEOUT

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
PAGE_URL = "https://www.youtube.com"
ALLOWED_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
# Sent on the page fetch only (consent bypass, see the module docstring).
PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Yasuho Discord bot)",
    "Cookie": "SOCS=CAI",
    "Accept-Language": "en",
}
FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Yasuho Discord bot)"}
MAX_FEED_BYTES = 512 * 1024
# Channel pages run 1.4-2.6 MB with the canonical link around 825 KB: the cap is a
# safety net, the read stops at the match.
MAX_PAGE_BYTES = 3 * 1024 * 1024

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# YouTube handles allow Unicode letters and digits plus . _ -
_HANDLE_RE = re.compile(r"^@[\w.-]{3,30}$")
_SEGMENT_RE = re.compile(r"^[\w.%-]{1,100}$")
_CANONICAL_RE = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'
)
_CANONICAL_BYTES_RE = re.compile(_CANONICAL_RE.pattern.encode())

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class FeedGone(Exception):
    """The channel does not exist (404 / 410): stop asking for a long while."""


class FeedError(Exception):
    """Any other failure (network, status, malformed body): retry with backoff."""


@dataclass(frozen=True)
class Entry:
    video_id: str
    title: str
    url: str
    published: datetime.datetime
    is_short: bool


@dataclass(frozen=True)
class Feed:
    title: str
    entries: tuple


def parse_channel_input(text):
    """Classify what a member typed: ``("id", UC...)``, ``("path", "/@h")`` or None.

    Accepts a bare channel id, ``@handle`` and youtube.com URLs
    (``/channel/UC..``, ``/@handle``, ``/c/name``, ``/user/name``). Any other
    host (or a port, or credentials in the URL) is refused here, before any
    request exists.
    """

    text = (text or "").strip()
    if CHANNEL_ID_RE.match(text):
        return ("id", text)
    if _HANDLE_RE.match(text):
        return ("path", "/" + text)
    candidate = text if "://" in text else "https://" + text
    try:
        parts = urllib.parse.urlsplit(candidate)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or host not in ALLOWED_HOSTS:
        return None
    if port is not None or parts.username or parts.password:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    first = segments[0]
    if _HANDLE_RE.match(urllib.parse.unquote(first)) and first.startswith("@"):
        return ("path", "/" + first)
    if len(segments) >= 2 and first == "channel":
        if CHANNEL_ID_RE.match(segments[1]):
            return ("id", segments[1])
        return None
    if len(segments) >= 2 and first in ("c", "user") and _SEGMENT_RE.match(segments[1]):
        return ("path", "/%s/%s" % (first, segments[1]))
    return None


def extract_channel_id(page_html):
    """The canonical channel id of a channel page, or None."""

    match = _CANONICAL_RE.search(page_html or "")
    return match.group(1) if match else None


def _parse_time(text):
    value = datetime.datetime.fromisoformat((text or "").strip())
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def parse_feed(xml_text):
    """Parse the Atom feed body into a :class:`Feed`; raises FeedError if malformed.

    A DOCTYPE / ENTITY declaration is refused outright: the real feed never
    carries one, and it is the only door to entity-expansion tricks.
    """

    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise FeedError("feed carries a DTD")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError("feed is not XML") from exc
    entries = []
    for node in root.findall("a:entry", _NS):
        try:
            video_id = node.findtext("yt:videoId", namespaces=_NS)
            link = node.find("a:link", _NS)
            href = link.get("href") if link is not None else ""
            entries.append(
                Entry(
                    video_id=video_id,
                    title=(node.findtext("a:title", namespaces=_NS) or "").strip(),
                    url="https://www.youtube.com/watch?v=" + video_id,
                    published=_parse_time(node.findtext("a:published", namespaces=_NS)),
                    is_short="/shorts/" in (href or ""),
                )
            )
        except (TypeError, ValueError):
            continue  # one malformed entry never hides the others
    return Feed(title=(root.findtext("a:title", namespaces=_NS) or "").strip(), entries=tuple(entries))


async def _read_capped(response, limit):
    """Read at most ``limit`` bytes; past it, raise FeedError."""

    body = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        body += chunk
        if len(body) > limit:
            raise FeedError("body too large")
    return body.decode("utf-8", "replace")


async def fetch_feed(session, channel_id):
    """GET and parse one channel's feed. FeedGone on 404/410, FeedError otherwise."""

    if not CHANNEL_ID_RE.match(channel_id):
        raise FeedGone(channel_id)
    try:
        async with session.get(
            FEED_URL,
            params={"channel_id": channel_id},
            headers=FEED_HEADERS,
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            if response.status in (404, 410):
                raise FeedGone(channel_id)
            if response.status != 200:
                raise FeedError("HTTP %s" % response.status)
            return parse_feed(await _read_capped(response, MAX_FEED_BYTES))
    except (FeedGone, FeedError):
        raise
    except Exception as exc:
        raise FeedError(str(exc)) from exc


async def _scan_for_channel_id(response):
    """Read the page chunk by chunk and stop at the canonical link (or the cap)."""

    buf = b""
    total = 0
    async for chunk in response.content.iter_chunked(16384):
        total += len(chunk)
        # Keep a tail so a match split across two chunks is still found.
        buf = buf[-512:] + chunk
        match = _CANONICAL_BYTES_RE.search(buf)
        if match:
            return match.group(1).decode()
        if total > MAX_PAGE_BYTES:
            break
    return None


async def resolve_path(session, path):
    """Channel id behind an ``/@handle`` style path, or None when none is found."""

    try:
        async with session.get(
            PAGE_URL + path,
            headers=PAGE_HEADERS,
            timeout=TIMEOUT,
            allow_redirects=False,
        ) as response:
            if response.status in (404, 410):
                return None
            if response.status != 200:
                raise FeedError("HTTP %s" % response.status)
            return await _scan_for_channel_id(response)
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError(str(exc)) from exc
