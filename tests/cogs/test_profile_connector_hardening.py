"""Two P3 fixes on the profile connectors: the parse, and the printed handle.

BACKLOGGD - the parse must not run on the event loop. ``parse_profile`` walks a
document with BeautifulSoup, which is synchronous CPU work bounded only by the
2 MiB page cap: on the loop, a slow parse froze every guild - every command,
every poller, every heartbeat - for as long as it took. It now runs in a thread
behind a two-slot ceiling, the shape ``tools.rendering.run_image_job`` uses (that
function itself cannot be reused: it needs a ``bot``, and a Connector is built by
a bare module import).

STEAM - a persona name printed at the START of a line. Everything else on that
card is either a translated string or ``**bold**``; the fallback "name the
account" line was the one place a third party's own text opened a line, so
``## Gaming IDs`` rendered as a heading standing over somebody else's profile.
It goes through ``profile_views.defuse_lines``, the package's existing answer.

Offline: no network, no Discord, no real page fetched.
"""

import asyncio
import threading
import types

import pytest

from cogs.community.profile import views as profile_views
from cogs.community.profile.connectors import backloggd, base, steam

# ---------------------------------------------------------------------------
# Backloggd: the parse is off the loop, and it is bounded
# ---------------------------------------------------------------------------


class _Container:
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)

    @property
    def item(self):
        return self.items[-1]


async def test_the_parse_runs_off_the_event_loop(monkeypatch):
    """Revert to calling ``parse_profile`` inline and this fails: the spy would
    record the loop's own thread."""

    seen = {}

    def _spy(html):
        seen["thread"] = threading.get_ident()
        return {"display_name": "spy"}

    monkeypatch.setattr(backloggd, "parse_profile", _spy)

    parsed = await backloggd.parse_profile_off_loop("<html></html>")

    assert parsed == {"display_name": "spy"}
    assert seen["thread"] != threading.get_ident()


async def test_a_slow_parse_never_stalls_the_loop(monkeypatch):
    """The point of the fix, stated as behaviour: other work keeps running.

    A parse that blocks its thread for 150ms must not stop the loop from
    scheduling anything in that window - which is exactly what an on-loop
    BeautifulSoup walk did to every guild at once.
    """

    started = threading.Event()

    def _slow(html):
        started.set()
        threading.Event().wait(0.15)
        return {"display_name": "slow"}

    monkeypatch.setattr(backloggd, "parse_profile", _slow)

    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        await backloggd.parse_profile_off_loop("<html></html>")
    finally:
        ticker.cancel()

    assert started.is_set()
    assert ticks >= 3


async def test_the_parse_ceiling_admits_only_its_slots(monkeypatch):
    """Without a ceiling, a burst of links hands the shared default executor as
    many threads as there are callers."""

    inside = 0
    peak = 0
    release = threading.Event()
    lock = threading.Lock()

    def _blocking(html):
        nonlocal inside, peak
        with lock:
            inside += 1
            peak = max(peak, inside)
        release.wait(1.0)
        with lock:
            inside -= 1
        return {"display_name": "x"}

    monkeypatch.setattr(backloggd, "parse_profile", _blocking)

    tasks = [
        asyncio.create_task(backloggd.parse_profile_off_loop("<html></html>"))
        for _ in range(backloggd._PARSE_CONCURRENCY + 2)
    ]
    await asyncio.sleep(0.1)
    running = peak
    release.set()
    await asyncio.gather(*tasks)

    assert running == backloggd._PARSE_CONCURRENCY


async def test_a_saturated_parse_queue_degrades_to_try_later(monkeypatch):
    """The WAIT is what times out (a running thread cannot be cancelled), and a
    caller that never got a slot is told the same "not your fault" as a network
    failure - never an untyped traceback."""

    release = threading.Event()

    def _blocking(html):
        release.wait(1.0)
        return {"display_name": "x"}

    monkeypatch.setattr(backloggd, "parse_profile", _blocking)
    monkeypatch.setattr(backloggd, "_PARSE_ACQUIRE_TIMEOUT", 0.01)

    async def _fetch_html(handle):
        return "<html></html>"

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _fetch_html)

    hoggers = [
        asyncio.create_task(backloggd.parse_profile_off_loop("<html></html>"))
        for _ in range(backloggd._PARSE_CONCURRENCY)
    ]
    await asyncio.sleep(0.05)

    connector = backloggd.BackloggdConnector()
    try:
        with pytest.raises(base.ConnectorUnavailable) as caught:
            await connector._fetch("someone")
        assert caught.value.reason == "remote"
    finally:
        release.set()
        await asyncio.gather(*hoggers)


async def test_the_connector_still_returns_a_real_parse(monkeypatch):
    """The offload is transparent: same payload, same display name."""

    async def _fetch_html(handle):
        return (
            "<html><head><meta property='og:image' content='https://x/y.png'>"
            "</head><body><div class='main-header'>FixtureUser</div></body></html>"
        )

    monkeypatch.setattr(backloggd, "_fetch_profile_html", _fetch_html)

    payload, display_name = await backloggd.BackloggdConnector()._fetch("someone")

    assert display_name == "FixtureUser"
    assert "display_name" not in payload
    assert payload["avatar"] == "https://x/y.png"


# ---------------------------------------------------------------------------
# Steam: a persona name can never open a markdown structure
# ---------------------------------------------------------------------------


def _steam_line(handle):
    return {
        "external_id": "76561197960287930",
        "display_name": handle,
        "payload": {"private": False, "persona_name": handle, "avatar": None},
    }


@pytest.mark.parametrize(
    "hostile",
    ["## Gaming IDs", "# Verified", "### Steam", "-# subtext", "> quoted"],
)
async def test_a_crafted_steam_name_cannot_forge_a_line_structure(hostile):
    """Drop ``defuse_lines`` from that line and this fails: the raw name would
    start the line, which is the only place markdown reads structure."""

    container = _Container()
    await steam._render(
        container, types.SimpleNamespace(label="Steam"), None, _steam_line(hostile), None
    )

    lines = container.item.content.split("\n")
    assert not any(
        line.lstrip().startswith(("#", "-#", ">")) for line in lines[1:]
    ), lines
    # The characters are still all there - defusing never removes anything.
    assert hostile in container.item.content


async def test_a_multi_line_steam_name_is_defused_on_every_line():
    container = _Container()
    await steam._render(
        container,
        types.SimpleNamespace(label="Steam"),
        None,
        _steam_line("harmless\n## Gaming IDs"),
        None,
    )

    assert profile_views._ZERO_WIDTH + "## Gaming IDs" in container.item.content


async def test_an_ordinary_steam_name_is_left_exactly_as_it_is():
    container = _Container()
    await steam._render(
        container, types.SimpleNamespace(label="Steam"), None, _steam_line("Yanis"), None
    )

    assert container.item.content.split("\n")[-1] == "Yanis"
