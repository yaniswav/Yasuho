"""Unit tests for ``tools.rank_card`` - rank-card background/accent storage (RC1).

Two halves, tested the way they are written:

* the PURE half (``validate_and_downscale`` / ``validate_accent`` /
  ``accent_to_rgb``) is exercised against REAL Pillow images, because the whole
  point of that code is what Pillow does with hostile input - a mocked decoder
  would test nothing. Every image here is built in memory, tiny, and never
  touches the disk or the network;
* the STORAGE half is exercised against a recording fake pool: what matters
  there is the exact SQL shape (upsert semantics, ``updated_at`` maintenance,
  asyncpg's one-statement-per-call rule), not that Postgres works.

The real-Postgres side of this table (DDL idempotence, the accent CHECK, the
primary-key plans for both reads) was probed separately in a ROLLBACK
transaction during RC1; this suite is the offline half.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tools import rank_card

# ---------------------------------------------------------------------------
# Helpers: real, tiny images.
# ---------------------------------------------------------------------------


def _encode(image, image_format, **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, image_format, **kwargs)
    return buffer.getvalue()


def _solid(size=(400, 400), colour=(200, 40, 120), image_format="PNG", **kwargs):
    return _encode(Image.new("RGB", size, colour), image_format, **kwargs)


def _noise(size=(1200, 900)):
    """A high-entropy image: WebP cannot compress it, so it exercises the caps."""
    width, height = size
    data = bytes(
        (x * 7 + y * 13 + ((x * y) % 251)) % 256
        for y in range(height)
        for x in range(width)
        for _ in (0, 1, 2)
    )
    return Image.frombytes("RGB", size, data)


# ---------------------------------------------------------------------------
# validate_and_downscale: accepted formats.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image_format, kwargs",
    [("PNG", {}), ("JPEG", {"quality": 90}), ("WEBP", {"quality": 90})],
)
def test_accepts_every_supported_format(image_format, kwargs):
    """PNG, JPEG and WebP all normalise into a card-sized WebP."""
    encoded, stored_format = rank_card.validate_and_downscale(
        _solid(image_format=image_format, **kwargs)
    )
    assert stored_format == rank_card.STORED_FORMAT == "webp"
    assert len(encoded) <= rank_card.MAX_STORED_BYTES
    with Image.open(io.BytesIO(encoded)) as result:
        assert result.format == "WEBP"
        # EXACT card dimensions: the renderer pastes the blob at (0, 0) with no
        # resize of its own, so anything else would shift the whole layout.
        assert result.size == rank_card.CARD_SIZE == (880, 240)


def test_output_is_exact_card_size_for_any_aspect_ratio():
    """A very wide, a very tall and an already-correct source all come out equal."""
    for size in [(2000, 200), (300, 1600), (880, 240), (17, 5)]:
        encoded, _ = rank_card.validate_and_downscale(_solid(size=size))
        with Image.open(io.BytesIO(encoded)) as result:
            assert result.size == rank_card.CARD_SIZE, size


def test_cover_crop_keeps_the_centre_and_never_distorts():
    """Cover-crop takes the CENTRED rectangle, so edge bands are cropped away.

    The source is a 1200x1200 square painted red with a green centre band whose
    aspect ratio already matches the card. Cover-cropping to 880x240 must land
    entirely inside that green band - a stretch (or a top-left crop) would drag
    red into the result.
    """
    source = Image.new("RGB", (1200, 1200), (255, 0, 0))
    band_height = round(1200 / (rank_card.CARD_WIDTH / rank_card.CARD_HEIGHT))
    top = (1200 - band_height) // 2
    source.paste(Image.new("RGB", (1200, band_height), (0, 255, 0)), (0, top))

    encoded, _ = rank_card.validate_and_downscale(_encode(source, "PNG"))
    with Image.open(io.BytesIO(encoded)) as result:
        pixels = result.convert("RGB")
        # Sample well inside the card to stay clear of the resampler's 1px seam
        # at the band edges.
        for point in [(10, 20), (440, 120), (870, 220)]:
            red, green, _blue = pixels.getpixel(point)
            assert green > red, (point, red, green)


def test_cover_box_geometry():
    """The pure crop-box helper: wide sources lose their sides, tall ones their top."""
    left, top, right, bottom = rank_card._cover_box(2000, 200)
    assert (top, bottom) == (0.0, 200.0)  # full height kept
    assert right - left == pytest.approx(200 * (880 / 240))
    assert left == pytest.approx((2000 - (right - left)) / 2)  # centred

    left, top, right, bottom = rank_card._cover_box(880, 2400)
    assert (left, right) == (0.0, 880.0)  # full width kept
    assert bottom - top == pytest.approx(880 / (880 / 240))
    assert top == pytest.approx((2400 - (bottom - top)) / 2)  # centred


# ---------------------------------------------------------------------------
# validate_and_downscale: rejections.
# ---------------------------------------------------------------------------


def test_rejects_source_over_the_byte_cap_without_decoding():
    """The byte cap is checked FIRST, so an oversized blob is never decoded."""
    payload = b"\x89PNG\r\n\x1a\n" + b"\0" * rank_card.MAX_SOURCE_BYTES
    with pytest.raises(rank_card.SourceTooLarge):
        rank_card.validate_and_downscale(payload)


def test_accepts_an_image_below_the_byte_cap():
    """The cap only fires above itself; an ordinary upload sails through."""
    encoded = _solid()
    assert len(encoded) < rank_card.MAX_SOURCE_BYTES
    rank_card.validate_and_downscale(encoded)  # must not raise


@pytest.mark.parametrize("image_format", ["GIF", "BMP", "TIFF"])
def test_rejects_unsupported_image_formats(image_format):
    with pytest.raises(rank_card.UnsupportedFormat):
        rank_card.validate_and_downscale(_solid(image_format=image_format))


def test_rejects_obviously_wrong_content_type_before_decoding():
    with pytest.raises(rank_card.UnsupportedFormat):
        rank_card.validate_and_downscale(_solid(), content_type="application/zip")


def test_content_type_is_a_hint_and_the_sniff_decides():
    """A lying content type cannot smuggle a format in, nor block a valid one."""
    # Declared PNG, actually WebP: the sniff accepts it (both are supported).
    encoded, _ = rank_card.validate_and_downscale(
        _solid(image_format="WEBP"), content_type="image/png"
    )
    assert encoded
    # Declared PNG, actually GIF: the sniff refuses it despite the declaration.
    with pytest.raises(rank_card.UnsupportedFormat):
        rank_card.validate_and_downscale(
            _solid(image_format="GIF"), content_type="image/png"
        )


def test_content_type_parameters_are_tolerated():
    encoded, _ = rank_card.validate_and_downscale(
        _solid(), content_type="image/png; charset=binary"
    )
    assert encoded


@pytest.mark.parametrize(
    "payload", [b"", b"not an image at all", b"\x89PNG\r\n\x1a\ntruncated"]
)
def test_rejects_undecodable_bytes(payload):
    with pytest.raises(rank_card.DecodeFailed):
        rank_card.validate_and_downscale(payload)


def test_rejects_non_bytes_input():
    with pytest.raises(rank_card.DecodeFailed):
        rank_card.validate_and_downscale("/path/to/file.png")


def test_rejects_a_decompression_bomb_by_pixel_count(monkeypatch):
    """A source above Pillow's configured pixel ceiling is refused before load()."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(rank_card.ImageTooLarge):
        rank_card.validate_and_downscale(_solid(size=(400, 400)))


def test_our_own_pixel_ceiling_survives_a_disabled_pillow_setting(monkeypatch):
    """``MAX_IMAGE_PIXELS = None`` (or a raised one) must not lift OUR bound.

    The byte cap alone is not a memory bound - a compressible PNG expands - so
    the allocation ceiling has to be ours, not Pillow's.
    """
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    assert rank_card._pixel_ceiling() == rank_card.MAX_SOURCE_PIXELS
    # An ordinary image still passes with the check disabled upstream.
    encoded, _ = rank_card.validate_and_downscale(_solid(size=(400, 400)))
    assert encoded
    # ...and an oversized one is still refused, by our ceiling alone.
    monkeypatch.setattr(rank_card, "MAX_SOURCE_PIXELS", 1000)
    with pytest.raises(rank_card.ImageTooLarge):
        rank_card.validate_and_downscale(_solid(size=(400, 400)))


def test_pixel_ceiling_takes_the_stricter_of_the_two_bounds(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    monkeypatch.setattr(rank_card, "MAX_SOURCE_PIXELS", 1_000_000)
    assert rank_card._pixel_ceiling() == 10
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000_000_000)
    assert rank_card._pixel_ceiling() == 1_000_000


def test_degrades_quality_once_before_giving_up(monkeypatch):
    """A blob above the stored cap at q80 is retried at q60 and accepted there."""
    source = _noise()
    high = len(_encode(source, "WEBP", quality=rank_card.WEBP_QUALITY, method=6))
    low = len(
        _encode(source, "WEBP", quality=rank_card.WEBP_QUALITY_FALLBACK, method=6)
    )
    assert low < high, "fixture must actually shrink at the lower quality"

    payload = _encode(source, "PNG")
    # Encode the ALREADY card-sized source so the two reference sizes above are
    # the very ones the function will produce.
    monkeypatch.setattr(rank_card, "CARD_SIZE", source.size)
    monkeypatch.setattr(rank_card, "CARD_WIDTH", source.size[0])
    monkeypatch.setattr(rank_card, "CARD_HEIGHT", source.size[1])
    monkeypatch.setattr(rank_card, "MAX_STORED_BYTES", (high + low) // 2)

    encoded, _ = rank_card.validate_and_downscale(payload)
    assert len(encoded) <= rank_card.MAX_STORED_BYTES
    assert len(encoded) == low  # the degraded pass is what got stored


def test_refuses_when_even_the_degraded_encode_is_too_large(monkeypatch):
    monkeypatch.setattr(rank_card, "MAX_STORED_BYTES", 16)
    with pytest.raises(rank_card.EncodedTooLarge):
        rank_card.validate_and_downscale(_encode(_noise((600, 400)), "PNG"))


def test_realistic_upload_stays_well_under_the_stored_cap():
    """A noisy full-size photo-like source still lands inside the cap at q80."""
    encoded, _ = rank_card.validate_and_downscale(_encode(_noise(), "PNG"))
    assert len(encoded) <= rank_card.MAX_STORED_BYTES


def test_every_error_is_a_rank_card_error():
    """One ``except RankCardError`` in RC2 / the dashboard covers every rejection."""
    for error in (
        rank_card.SourceTooLarge,
        rank_card.ImageTooLarge,
        rank_card.UnsupportedFormat,
        rank_card.DecodeFailed,
        rank_card.EncodedTooLarge,
        rank_card.InvalidAccent,
    ):
        assert issubclass(error, rank_card.RankCardError)


# ---------------------------------------------------------------------------
# validate_accent.
# ---------------------------------------------------------------------------


def test_accent_none_round_trips():
    assert rank_card.validate_accent(None) is None


@pytest.mark.parametrize("value", [0, 1, 0x5865F2, 0xFFFFFF])
def test_accent_accepts_ints_inside_the_range(value):
    assert rank_card.validate_accent(value) == value


@pytest.mark.parametrize("value", [-1, 0x1000000, 1 << 40])
def test_accent_rejects_ints_outside_the_range(value):
    with pytest.raises(rank_card.InvalidAccent):
        rank_card.validate_accent(value)


@pytest.mark.parametrize(
    "text", ["#5865F2", "5865F2", "5865f2", "0x5865F2", "0x#5865F2", "  #5865F2  "]
)
def test_accent_accepts_the_usual_hex_shapes(text):
    assert rank_card.validate_accent(text) == 0x5865F2


@pytest.mark.parametrize(
    "text, expected",
    [
        ("#FFF", 0xFFFFFF),
        ("fff", 0xFFFFFF),
        ("#f0a", 0xFF00AA),
        ("#000", 0x000000),
        ("0x58f", 0x5588FF),
    ],
)
def test_accent_expands_the_three_digit_shorthand_like_discord(text, expected):
    """``#FFF`` is WHITE, as it is in CSS and in discord.Colour.from_str.

    Accepting 1..6 digits and zero-extending made it 0x000FFF - a dark blue -
    so a guild picking white through RC2's modal would have got a colour it
    never chose, and the mistake would have been invisible until a member ran
    /rank.
    """
    assert rank_card.validate_accent(text) == expected


def test_accent_agrees_with_discord_colour_from_str():
    """The house convention IS discord's, so a colour means the same thing in
    the rank card, in an embed and in the dashboard's picker."""
    import discord

    for text in ("#FFF", "#f0a", "#000", "0x58f", "#5865F2", "0x5865F2"):
        assert rank_card.validate_accent(text) == discord.Colour.from_str(text).value


@pytest.mark.parametrize(
    "text",
    ["", "#", "zzzzzz", "#12345678", "5865F2F2", "#1", "12", "#1234", "58F2F"],
)
def test_accent_rejects_malformed_strings(text):
    """Only 3 or 6 digits. ``#1`` and ``#12345`` are typos, and reading them as
    0x000001 / 0x012345 would hand back a colour nobody asked for."""
    with pytest.raises(rank_card.InvalidAccent):
        rank_card.validate_accent(text)


@pytest.mark.parametrize("value", [True, False, 1.5, [], {}, object()])
def test_accent_rejects_wrong_types_including_bool(value):
    """``True`` is an int; letting it through would mean the colour 0x000001."""
    with pytest.raises(rank_card.InvalidAccent):
        rank_card.validate_accent(value)


def test_schema_enforces_the_stored_size_cap_in_ddl():
    """The stored-size cap is a CHECK, not just prose in this module.

    The dashboard (a separate Node process) writes this table with its own copy
    of the caps: a blob that slipped past it would be re-read on every /rank of
    that guild, so the bound lives in the DDL too - and the two numbers must not
    drift apart.
    """
    import os

    schema_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "schema.sql"
    )
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema = handle.read()
    assert (
        "octet_length(background) <= %d" % rank_card.MAX_STORED_BYTES in schema
    )


def test_accent_to_rgb():
    assert rank_card.accent_to_rgb(None) is None
    assert rank_card.accent_to_rgb(0x5865F2) == (0x58, 0x65, 0xF2)
    assert rank_card.accent_to_rgb(0) == (0, 0, 0)
    assert rank_card.accent_to_rgb(0xFFFFFF) == (255, 255, 255)


# ---------------------------------------------------------------------------
# Storage half.
# ---------------------------------------------------------------------------


class RecordingPool:
    """Fake asyncpg pool that records every statement and its arguments."""

    def __init__(self, row=None, value=None):
        self.calls = []
        self.row = row
        self.value = value

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.value


def _statements(pool):
    return [query for _method, query, _args in pool.calls]


async def test_fetch_config_reads_metadata_without_the_blob():
    """The cached read must never drag the (up to ~512 KiB) image into memory."""
    pool = RecordingPool(row={"accent": 1, "background_format": "webp"})
    assert await rank_card.fetch_config(pool, 42) == pool.row
    method, query, args = pool.calls[0]
    assert method == "fetchrow" and args == (42,)
    assert "background IS NOT NULL AS has_background" in query
    # The blob column itself is never selected here.
    assert "SELECT accent, background_format, background IS NOT NULL" in query


async def test_fetch_background_returns_plain_bytes():
    """asyncpg hands BYTEA back as memoryview-ish data; callers want real bytes."""
    pool = RecordingPool(value=memoryview(b"webpdata"))
    assert await rank_card.fetch_background(pool, 42) == b"webpdata"
    pool.value = None
    assert await rank_card.fetch_background(pool, 42) is None


async def test_set_background_upserts_and_keeps_the_accent():
    pool = RecordingPool()
    await rank_card.set_background(pool, 42, b"blob")
    _method, query, args = pool.calls[0]
    assert args == (42, b"blob", "webp")
    assert "ON CONFLICT (guild_id) DO UPDATE" in query
    assert "updated_at = now()" in query
    # The accent column is untouched by a background write.
    assert "accent" not in query


async def test_set_accent_upserts_and_keeps_the_background():
    pool = RecordingPool()
    await rank_card.set_accent(pool, 42, 0x5865F2)
    _method, query, args = pool.calls[0]
    assert args == (42, 0x5865F2)
    assert "ON CONFLICT (guild_id) DO UPDATE" in query
    assert "updated_at = now()" in query
    assert "background" not in query


async def test_clear_helpers_are_scoped_as_named():
    pool = RecordingPool()
    await rank_card.clear_background(pool, 42)
    await rank_card.clear_accent(pool, 42)
    await rank_card.clear(pool, 42)
    background_query, accent_query, delete_query = _statements(pool)
    assert "background = NULL" in background_query
    assert "background_format = NULL" in background_query
    assert "accent" not in background_query
    assert "accent = NULL" in accent_query
    assert "background" not in accent_query
    assert delete_query.startswith("DELETE FROM rank_cards")
    for query in (background_query, accent_query):
        assert "updated_at = now()" in query
    assert all(args == (42,) for _method, _query, args in pool.calls)


async def test_every_statement_is_a_single_statement():
    """asyncpg's execute() refuses multi-statement strings with parameters."""
    pool = RecordingPool()
    await rank_card.set_background(pool, 1, b"x")
    await rank_card.set_accent(pool, 1, 2)
    await rank_card.clear_background(pool, 1)
    await rank_card.clear_accent(pool, 1)
    await rank_card.clear(pool, 1)
    await rank_card.fetch_config(pool, 1)
    await rank_card.fetch_background(pool, 1)
    for _method, query, _args in pool.calls:
        assert query.count(";") == 1, query
        assert query.rstrip().endswith(";"), query


def test_queries_target_the_rank_cards_table_only():
    for query in (rank_card.CONFIG_QUERY, rank_card.BACKGROUND_QUERY):
        assert "FROM rank_cards WHERE guild_id = $1" in query
