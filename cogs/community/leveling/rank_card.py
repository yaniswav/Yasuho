"""Rank-card customisation: validation, normalisation, storage.

Two halves, deliberately kept in one small module because they describe the
SAME contract from both ends:

* the PURE half (no I/O, no discord, no pool) validates and normalises what a
  guild uploads - ``validate_and_downscale`` turns an arbitrary PNG/JPEG/WebP
  into a bounded WebP cropped to the card's EXACT pixel size, and
  ``validate_accent`` turns arbitrary user input into a packed 0xRRGGBB int;
* the STORAGE half is one-statement queries over the ``rank_cards`` table
  (``fetch_config`` / ``fetch_background`` / ``set_background`` / ``set_accent``
  / the ``clear_*`` trio), taking an asyncpg pool explicitly so nothing here
  needs a bot object, plus their PER-USER twins over ``user_rank_cards``
  (``fetch_user_card`` / ``fetch_user_config`` / ``set_user_*`` / ``clear_user*``,
  lot U1) at the bottom of the file.

The pure half is shared verbatim by both scopes: whether a background arrives
from a server admin or from a member customising their own card, it goes through
the same sniff, the same pre-decode pixel ceiling and the same WebP ladder.

Everything the render seam (``cogs/community/leveling/leveling.py``) and the future
Discord panel (RC2) and the Node dashboard need lives here, so the three
writers cannot drift: the dashboard mirrors these exact statements and the same
caps, then fires ``pg_notify('yasuho_dashboard', {"kind": "rank_card", ...})``
so ``cogs/system/dashboard_sync.py`` drops the bot's cached config.

Why normalise on the way IN rather than on every render: the card is drawn
inside the shared image semaphore (``tools/rendering.run_image_job``, 2 slots
bot-wide). Storing a pre-cropped, pre-bounded WebP means a ``/rank`` render only
ever decodes an image the bot itself produced, at exactly the size it needs -
no hostile dimensions, no repeated LANCZOS resample of a 30 MP upload on the hot
path. A malicious upload is rejected ONCE, at configuration time, by an admin.

Errors are typed (all under :class:`RankCardError`) so RC2 and the dashboard can
map each failure to its own user-facing message without string matching.

TODO-CONTRACT (RC2): every bot-side write to this table MUST go through a cog
seam that invalidates the render cache for that guild
(``Leveling.invalidate_rank_card``) in the same call - the storage functions
below deliberately know nothing about the bot, so a panel that calls
``set_background``/``set_accent``/``clear*`` directly would leave the guild
rendering its previous card until the cache entry ages out. RC2 owes a test
proving the seam invalidates, exactly as the dashboard path is already covered
by tests/cogs/test_dashboard_sync.py.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

# ---------------------------------------------------------------------------
# Card geometry - the single source of truth, shared with the renderer.
# ---------------------------------------------------------------------------
# These MUST stay equal to the dimensions cogs/community/leveling/leveling.py draws at;
# that module imports them from here rather than redeclaring the literals, so a
# future card resize cannot leave stored backgrounds silently mis-cropped.
CARD_WIDTH = 880
CARD_HEIGHT = 240
CARD_SIZE = (CARD_WIDTH, CARD_HEIGHT)
# Corner radius of the card panel; the background is masked to the same shape.
CARD_RADIUS = 30

# ---------------------------------------------------------------------------
# Caps.
# ---------------------------------------------------------------------------
# Largest upload we will even decode. Discord's own non-nitro attachment ceiling
# is 10 MiB, so 8 MiB accepts every realistic wallpaper while refusing a blob
# whose only purpose is to make us allocate.
MAX_SOURCE_BYTES = 8 * 1024 * 1024
# Largest DECODED source we will allocate. The byte cap alone is not a memory
# bound: a highly compressible 8 MiB PNG can expand to tens of megapixels, and
# Pillow's own default ceiling (~89 MP) would let that become a ~270 MB RGB
# buffer in the render executor. 40 MP is comfortably above any real wallpaper
# (an 8K image is 33 MP) and caps the decode at roughly 120 MB.
MAX_SOURCE_PIXELS = 40_000_000
# Hard ceiling on what we are willing to STORE (and therefore re-read on every
# render): a full-width card at quality 80 lands around 60-120 KiB, so 512 KiB
# only ever bites on pathological noise images.
MAX_STORED_BYTES = 512 * 1024
# Quality ladder: the first encode, then ONE degraded retry. If even the second
# pass busts MAX_STORED_BYTES the image is refused rather than degraded into
# mush - at that point it is noise, not a background.
WEBP_QUALITY = 80
WEBP_QUALITY_FALLBACK = 60
# method=6 is Pillow's slowest/best WebP search. Affordable here: this runs once
# per configuration change, never on a render.
_WEBP_METHOD = 6

# Only these three are accepted. Sniffed from the decoded image's own header
# (Pillow's ``Image.format``), NEVER inferred from a filename or from the
# client-supplied content type - a caller can claim anything.
ACCEPTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
# Content types the accepted formats are served under, used ONLY as a cheap
# pre-decode reject for an obviously wrong upload (a .zip, a video). The sniff
# below is what actually decides.
ACCEPTED_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp"}
)

# What every stored background is encoded as; also what set_background records
# in ``rank_cards.background_format``.
STORED_FORMAT = "webp"

# Accent bounds: a packed 0xRRGGBB int, the same shape discord.Colour.value has.
ACCENT_MAX = 0xFFFFFF
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


# ---------------------------------------------------------------------------
# Typed errors.
# ---------------------------------------------------------------------------


class RankCardError(Exception):
    """Base class for every rejection below (one except clause covers all)."""


class SourceTooLarge(RankCardError):
    """The uploaded bytes exceed :data:`MAX_SOURCE_BYTES`."""


class ImageTooLarge(RankCardError):
    """The decoded pixel count exceeds Pillow's decompression-bomb ceiling."""


class UnsupportedFormat(RankCardError):
    """The sniffed image format is not one of :data:`ACCEPTED_FORMATS`."""


class DecodeFailed(RankCardError):
    """The bytes are not a decodable image (truncated, corrupt, not an image)."""


class EncodedTooLarge(RankCardError):
    """Even the degraded re-encode stayed above :data:`MAX_STORED_BYTES`."""


class InvalidAccent(RankCardError):
    """The accent value is not an int (or hex string) inside 0..0xFFFFFF."""


# ---------------------------------------------------------------------------
# Pure half: validation + normalisation.
# ---------------------------------------------------------------------------


def _pixel_ceiling():
    """The strictest decoded-pixel ceiling in force: ours, or Pillow's if lower.

    ``Image.MAX_IMAGE_PIXELS`` only makes Pillow WARN at the limit and raise at
    TWICE it, and an application is free to set it to ``None`` (disabled). Both
    behaviours are wrong for an upload: we honour the configured value strictly
    (anything above it is refused outright, never merely warned about) and we
    never let a disabled/raised Pillow setting lift our own
    :data:`MAX_SOURCE_PIXELS` allocation bound.
    """
    pillow_ceiling = Image.MAX_IMAGE_PIXELS
    if pillow_ceiling is None:
        return MAX_SOURCE_PIXELS
    return min(MAX_SOURCE_PIXELS, pillow_ceiling)


def _cover_box(source_width, source_height):
    """Return the crop box that fills the card without distorting the source.

    Classic "cover": scale by the LARGER of the two ratios so both card
    dimensions are covered, then take the centred rectangle of the source whose
    aspect ratio already matches the card. Returned in SOURCE coordinates so the
    caller can hand it straight to ``Image.resize(..., box=...)``, which crops
    and resamples in one pass (no intermediate full-size resize).
    """
    target_ratio = CARD_WIDTH / CARD_HEIGHT
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        # Source is wider than the card: keep full height, crop the sides.
        crop_width = source_height * target_ratio
        left = (source_width - crop_width) / 2
        return (left, 0.0, left + crop_width, float(source_height))
    # Source is taller (or equal): keep full width, crop top and bottom.
    crop_height = source_width / target_ratio
    top = (source_height - crop_height) / 2
    return (0.0, top, float(source_width), top + crop_height)


def validate_and_downscale(data, content_type=None):
    """Normalise an uploaded background into ``(webp_bytes, 'webp')``.

    ``data`` is the raw upload; ``content_type`` is the OPTIONAL client-declared
    type, used only as a cheap early reject - the authoritative check is the
    format Pillow sniffs from the bytes themselves.

    The pipeline, in order, so the cheapest rejection always runs first:

    1. byte cap (:data:`MAX_SOURCE_BYTES`) - refuses before any decode;
    2. content-type pre-reject, when one was supplied;
    3. decode, with the format taken from the header and matched against
       :data:`ACCEPTED_FORMATS`, and the pixel count matched against
       :func:`_pixel_ceiling` BEFORE the pixels are loaded (the byte cap alone
       does not bound the ALLOCATION - a compressible PNG expands);
    4. cover-crop + resample to exactly :data:`CARD_SIZE` (one pass);
    5. WebP encode at :data:`WEBP_QUALITY`, retried ONCE at
       :data:`WEBP_QUALITY_FALLBACK` if the result busts
       :data:`MAX_STORED_BYTES`, then refused.

    Pure and blocking (it is Pillow work): callers on the event loop run it
    through ``tools.rendering.run_image_job`` like every other image job.
    Raises a :class:`RankCardError` subclass on every rejection; never returns a
    blob that is not exactly card-sized and under the stored cap.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise DecodeFailed("background must be raw bytes")
    data = bytes(data)
    if not data:
        raise DecodeFailed("empty upload")
    if len(data) > MAX_SOURCE_BYTES:
        raise SourceTooLarge(
            "upload is %d bytes, cap is %d" % (len(data), MAX_SOURCE_BYTES)
        )
    if content_type is not None:
        # Strip any ";charset=..." parameter and normalise before comparing.
        declared = str(content_type).split(";", 1)[0].strip().lower()
        if declared and declared not in ACCEPTED_CONTENT_TYPES:
            raise UnsupportedFormat("unsupported content type %r" % declared)

    try:
        with Image.open(io.BytesIO(data)) as source:
            # Format comes from the decoder that claimed the header - not from
            # an extension, and not from the caller.
            image_format = (source.format or "").upper()
            if image_format not in ACCEPTED_FORMATS:
                raise UnsupportedFormat("unsupported image format %r" % image_format)
            # Dimensions are known from the header alone, so this refuses a
            # decompression bomb BEFORE load() allocates its pixels.
            ceiling = _pixel_ceiling()
            width, height = source.size
            if width < 1 or height < 1:
                raise DecodeFailed("degenerate image dimensions")
            if width * height > ceiling:
                raise ImageTooLarge(
                    "image is %dx%d pixels, ceiling is %d" % (width, height, ceiling)
                )
            source.load()
            # Animated sources (APNG / animated WebP) keep only their first
            # frame: a rank-card background is a still, and load() above has
            # already landed on frame 0.
            resized = source.convert("RGB").resize(
                CARD_SIZE,
                Image.Resampling.LANCZOS,
                box=_cover_box(width, height),
            )
    except RankCardError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise DecodeFailed("could not decode the image: %s" % exc) from exc
    except Image.DecompressionBombError as exc:  # pragma: no cover - belt and braces
        raise ImageTooLarge(str(exc)) from exc

    for quality in (WEBP_QUALITY, WEBP_QUALITY_FALLBACK):
        buffer = io.BytesIO()
        resized.save(buffer, "WEBP", quality=quality, method=_WEBP_METHOD)
        encoded = buffer.getvalue()
        if len(encoded) <= MAX_STORED_BYTES:
            return encoded, STORED_FORMAT
    raise EncodedTooLarge(
        "re-encoded background is %d bytes, cap is %d"
        % (len(encoded), MAX_STORED_BYTES)
    )


def validate_accent(value):
    """Return ``value`` as a packed 0xRRGGBB int, or ``None`` to keep the default.

    ``None`` in means ``None`` out (the caller is clearing the accent). Anything
    else is accepted as an int, or as a hex string in any of the shapes a human
    or a colour picker produces (``'#5865F2'``, ``'5865F2'``, ``'0x5865F2'``).
    ``bool`` is rejected explicitly - it is an ``int`` subclass, and ``True``
    silently becoming the colour 0x000001 is exactly the kind of quiet nonsense
    a typed error exists to stop. Out-of-range or unparseable input raises
    :class:`InvalidAccent`.

    HEX LENGTH IS EXACT, and the 3-digit shorthand EXPANDS - the same convention
    ``discord.Colour.from_str`` (and CSS) uses, so a colour typed into RC2's
    modal means what it means everywhere else in the bot: ``'#FFF'`` is white
    (0xFFFFFF), not 0x000FFF. Any other length is refused rather than
    zero-extended: ``'#12345'`` is a typo, and silently reading it as 0x012345
    would hand the guild a colour it never chose.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidAccent("accent must be a colour, not a boolean")
    if isinstance(value, int):
        accent = value
    elif isinstance(value, str):
        text = value.strip().lstrip("#")
        if text[:2].lower() == "0x":
            # '0x#5865F2' is discord.py's own legacy spelling; accept it too.
            text = text[2:].lstrip("#")
        if len(text) == 3 and all(c in _HEX_DIGITS for c in text):
            text = "".join(c * 2 for c in text)
        if len(text) != 6 or not all(c in _HEX_DIGITS for c in text):
            raise InvalidAccent("accent must be a hex colour like #5865F2 or #58F")
        accent = int(text, 16)
    else:
        raise InvalidAccent("accent must be an int or a hex colour string")
    if accent < 0 or accent > ACCENT_MAX:
        raise InvalidAccent("accent must be between 0 and 0xFFFFFF")
    return accent


def accent_to_rgb(accent):
    """Unpack a stored 0xRRGGBB int into the ``(r, g, b)`` tuple Pillow wants."""
    if accent is None:
        return None
    return ((accent >> 16) & 0xFF, (accent >> 8) & 0xFF, accent & 0xFF)


# ---------------------------------------------------------------------------
# Storage half: one statement each, asyncpg-mono-statement safe.
# ---------------------------------------------------------------------------
# Reads are split in two on purpose (SCALE STORY): the render seam caches the
# tiny METADATA row per guild, and only fetches the background BYTES on the rare
# render that actually has one - so the bot never holds N guilds' worth of image
# blobs in memory.

# The metadata a render needs to decide what to draw, without the blob itself.
CONFIG_QUERY = (
    "SELECT accent, background_format, background IS NOT NULL AS has_background "
    "FROM rank_cards WHERE guild_id = $1;"
)
BACKGROUND_QUERY = "SELECT background FROM rank_cards WHERE guild_id = $1;"


async def fetch_config(pool, guild_id):
    """Return the guild's ``(accent, background_format, has_background)`` row.

    ``None`` when the guild never customised its card - the stock-card path.
    Deliberately does NOT select the blob: this is what the render seam caches.
    """
    return await pool.fetchrow(CONFIG_QUERY, guild_id)


async def fetch_background(pool, guild_id):
    """Return the stored WebP bytes for a guild, or ``None``.

    Read fresh on every render that needs it (never cached): the blob is up to
    ~512 KiB and a render is already gated behind the image semaphore, so one
    extra primary-key lookup is far cheaper than pinning image data in RAM.
    """
    raw = await pool.fetchval(BACKGROUND_QUERY, guild_id)
    return bytes(raw) if raw is not None else None


async def set_background(pool, guild_id, background, background_format=STORED_FORMAT):
    """Store (or replace) a guild's background. ``background`` is already normalised.

    The caller MUST have passed the bytes through :func:`validate_and_downscale`
    first - this is the persistence step, not a second validation layer. Upserts
    so a guild that only ever set an accent keeps it.
    """
    await pool.execute(
        "INSERT INTO rank_cards (guild_id, background, background_format, updated_at) "
        "VALUES ($1, $2, $3, now()) "
        "ON CONFLICT (guild_id) DO UPDATE SET "
        "background = EXCLUDED.background, "
        "background_format = EXCLUDED.background_format, "
        "updated_at = now();",
        guild_id,
        background,
        background_format,
    )


async def set_accent(pool, guild_id, accent):
    """Store (or replace) a guild's accent colour, keeping any background.

    ``accent`` must already have been through :func:`validate_accent` (the
    table's CHECK is the second line of defence, not the first).
    """
    await pool.execute(
        "INSERT INTO rank_cards (guild_id, accent, updated_at) "
        "VALUES ($1, $2, now()) "
        "ON CONFLICT (guild_id) DO UPDATE SET "
        "accent = EXCLUDED.accent, updated_at = now();",
        guild_id,
        accent,
    )


async def clear_background(pool, guild_id):
    """Drop a guild's background, keeping its accent. No-op without a row."""
    await pool.execute(
        "UPDATE rank_cards SET background = NULL, background_format = NULL, "
        "updated_at = now() WHERE guild_id = $1;",
        guild_id,
    )


async def clear_accent(pool, guild_id):
    """Drop a guild's accent, keeping its background. No-op without a row."""
    await pool.execute(
        "UPDATE rank_cards SET accent = NULL, updated_at = now() "
        "WHERE guild_id = $1;",
        guild_id,
    )


async def clear(pool, guild_id):
    """Reset a guild's card entirely by deleting its row (stock card again)."""
    await pool.execute("DELETE FROM rank_cards WHERE guild_id = $1;", guild_id)


# ---------------------------------------------------------------------------
# Per-USER half (lot U1): the same contract, keyed by user_id.
# ---------------------------------------------------------------------------
# The ``user_rank_cards`` table is the guild table's twin: a member picks their
# own background and/or accent ONCE and it follows them into every server that
# allows it. Everything above is reused VERBATIM - validate_and_downscale (same
# sniff, same pre-decode pixel ceiling, same WebP quality ladder, same stored
# cap) and validate_accent - so a member cannot store anything an admin could
# not, and the render only ever decodes a blob this bot itself produced.
#
# WHY A SEPARATE TABLE rather than a nullable guild_id on rank_cards: the two
# rows mean different things (a guild's branding vs a member's own look), they
# are erased by different paths (a guild purge vs tools/privacy's user erasure),
# and the PK is the whole index either way. Splitting them keeps
# tools/retention's guild-side structural guard and tools/privacy's user-side
# one each looking at exactly one table.
#
# READ SHAPE, and why it differs from the guild one above. The guild path splits
# metadata from blob because the CACHE is per guild and long-lived. The user
# path caches only a self-healing "does this member have a row at all?" marker
# (cogs/community/leveling/rank_card_user.py), so the render either does NOTHING
# at all (the overwhelmingly common case) or ONE statement that already carries
# both the accent and the bytes - never two. fetch_user_config is the
# metadata-only read for the surfaces that describe a card without drawing it.

# The guild_settings key that lets a server refuse per-user styles on ITS /rank
# cards. Declared here, next to the storage it gates, because the dashboard is
# an independent writer of the same key and must not invent its own spelling.
# ABSENT MEANS ALLOWED: the feature is on by default, and a guild that never
# heard of it never had to act.
ALLOW_USER_STYLES_KEY = "rank_card_allow_user_styles"
ALLOW_USER_STYLES_DEFAULT = True

# Everything one render needs, in one round trip (see the read-shape note).
USER_CARD_QUERY = "SELECT accent, background FROM user_rank_cards WHERE user_id = $1;"
# What a surface needs to DESCRIBE a card without drawing it: no blob.
USER_CONFIG_QUERY = (
    "SELECT accent, background IS NOT NULL AS has_background, updated_at "
    "FROM user_rank_cards WHERE user_id = $1;"
)


async def fetch_user_card(pool, user_id):
    """Return ``(accent, background_bytes)`` for a member, or ``None``.

    ``None`` means NO ROW - the member never customised their card - which is a
    different answer from a row whose two columns are both NULL, and the caller
    uses that difference to drop its presence marker.
    """
    row = await pool.fetchrow(USER_CARD_QUERY, user_id)
    if row is None:
        return None
    raw = row["background"]
    return row["accent"], (bytes(raw) if raw is not None else None)


async def fetch_user_config(pool, user_id):
    """Return the member's ``(accent, has_background, updated_at)`` row, or ``None``.

    The blob-free read, for surfaces that describe the card (``/rankcard view``)
    rather than draw it.
    """
    return await pool.fetchrow(USER_CONFIG_QUERY, user_id)


async def set_user_background(pool, user_id, background):
    """Store (or replace) a member's background. Already normalised.

    The caller MUST have passed the bytes through :func:`validate_and_downscale`
    first. Upserts, so a member who only ever set an accent keeps it. There is
    no format column to write: the validator only ever emits
    :data:`STORED_FORMAT` (see schema.sql).
    """
    await pool.execute(
        "INSERT INTO user_rank_cards (user_id, background, updated_at) "
        "VALUES ($1, $2, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "background = EXCLUDED.background, updated_at = now();",
        user_id,
        background,
    )


async def set_user_accent(pool, user_id, accent):
    """Store (or replace) a member's accent colour, keeping any background.

    ``accent`` must already have been through :func:`validate_accent` (the
    table's CHECK is the second line of defence, not the first).
    """
    await pool.execute(
        "INSERT INTO user_rank_cards (user_id, accent, updated_at) "
        "VALUES ($1, $2, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "accent = EXCLUDED.accent, updated_at = now();",
        user_id,
        accent,
    )


# Clearing ONE knob: null it when the other knob is still set, and delete the
# row outright when it is not. A row with both columns NULL is indistinguishable
# from no row at all in every read this module has, but it is NOT free: it makes
# ensure_user_rank_card_style mark the member as "has a row" and pay
# USER_CARD_QUERY on every /rank of theirs for the life of the process, to learn
# (None, None) - exactly the case the marker exists to make cost nothing.
#
# One statement, not two, and deterministic despite both sub-statements
# targeting the same key: the DELETE and the UPDATE have MUTUALLY EXCLUSIVE
# predicates on the same snapshot (the other column IS NULL / IS NOT NULL), so
# no row is ever reached by both - the case Postgres leaves unspecified for
# data-modifying CTEs. Returns whether a row SURVIVED, which is what the caller
# writes into the marker (a member who never had a row gets a truthful False
# instead of the optimistic True a bare UPDATE would leave).
_CLEAR_USER_BACKGROUND = (
    "WITH gone AS ("
    "  DELETE FROM user_rank_cards WHERE user_id = $1 AND accent IS NULL"
    "), kept AS ("
    "  UPDATE user_rank_cards SET background = NULL, updated_at = now() "
    "  WHERE user_id = $1 AND accent IS NOT NULL RETURNING user_id"
    ") SELECT EXISTS (SELECT 1 FROM kept);"
)

_CLEAR_USER_ACCENT = (
    "WITH gone AS ("
    "  DELETE FROM user_rank_cards WHERE user_id = $1 AND background IS NULL"
    "), kept AS ("
    "  UPDATE user_rank_cards SET accent = NULL, updated_at = now() "
    "  WHERE user_id = $1 AND background IS NOT NULL RETURNING user_id"
    ") SELECT EXISTS (SELECT 1 FROM kept);"
)


async def clear_user_background(pool, user_id):
    """Drop a member's background; return whether a row survived.

    Keeps their accent when they have one, and deletes the row when they do not
    (see :data:`_CLEAR_USER_BACKGROUND`). No-op returning False without a row.
    """
    return bool(await pool.fetchval(_CLEAR_USER_BACKGROUND, user_id))


async def clear_user_accent(pool, user_id):
    """Drop a member's accent; return whether a row survived.

    Keeps their background when they have one, and deletes the row when they do
    not (see :data:`_CLEAR_USER_ACCENT`). No-op returning False without a row.
    """
    return bool(await pool.fetchval(_CLEAR_USER_ACCENT, user_id))


async def clear_user(pool, user_id):
    """Reset a member's card entirely by deleting their row.

    The same statement tools/privacy.py runs on an erasure request, kept here so
    the two spellings cannot drift.
    """
    await pool.execute("DELETE FROM user_rank_cards WHERE user_id = $1;", user_id)
