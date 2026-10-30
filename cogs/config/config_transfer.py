"""Server configuration export / import and saved templates (lot V5d).

`/config export` hands a manager the bot's configuration of THIS server as a
versioned JSON file; `/config import` (Yasuho+) reads such a file back, shows a
preview and applies it only after the manager confirms; `/config template ...`
(Yasuho+) stores exports server-side and applies them through the SAME preview.
The commands live on the `/config` group (cogs/config/settings.py); this module
holds the logic and owns no cog (no ``setup``).

SCOPE (v1). Only the ``guild_settings`` configuration blobs and keys that already
have a normaliser on read: language, welcome, tickets (all but the panel
channel), music, member milestones, birthdays, suggestions, event reminders,
YouTube alert settings, server statistics channels. NOT included: per-user data,
content tables (custom commands, playlists, reaction/button roles, role menus,
starboards, YouTube follows), anything holding a secret, token or webhook, the
tickets panel channel (it is the on-switch of a panel message only `/ticket
setup` posts), the forum "solved" tags (forum-scoped tag ids, not channels or
roles) and the music default effect / request panels (bot-only writes).

TRUST. Every snowflake in a file refers to the SOURCE server, and the file is
untrusted input. So:

* the file is parsed strictly (size cap, format + version, a known section set,
  every channel/role reference shaped ``{"id", "name"}``) - :func:`parse_document`;
* a reference is looked up ONLY in the target guild (``guild.get_channel`` /
  ``guild.get_role``, never the bot-wide cache): an id that exists there is kept
  if it is the right kind and dropped if not; otherwise it is remapped by EXACT
  name to the single object of the right kind, and dropped when there is none or
  several. ``source_guild_id`` is informational and never read;
* a role is dropped when it has dangerous permissions
  (``role_audit.refuse_dangerous_role``) or fails the feature's own rule
  (@everyone, a birthday role Yasuho cannot hand out, a ping role nobody can
  ping); a channel whose permissions Yasuho (or the manager's own view) lacks
  makes its whole section SKIPPED with the reason;
* the plan is rebuilt from scratch at the confirm click, so everything above is
  re-validated against the guild as it is THEN, and written section by section
  through the feature's own storage (``tools.settings``) plus its follow-up hook
  (timers, milestone arming, birthday role strip), each section isolated so a
  failure reports which sections applied.

A section whose imported values equal the current ones is left untouched, which
is what makes an export imported back into the same server a no-op.

SCALE. Export: one settings-blob read (LRU-cached, shared by every section) and a
few KB of JSON. Import: a file capped at 64 KB, one per guild per 30 s, then at
most ten ``jsonb_set`` writes plus a handful of hook calls on confirm. Templates:
at most ``max_config_templates`` (10) rows per guild, each one capped like a file;
save is one short transaction under a per-guild advisory lock.

Typography rule: ASCII '-' and '...' only.
"""

import dataclasses
import datetime
import io
import json
import logging

import discord

from cogs.community.birthdays import store as birthdays_store
from cogs.community.serverstats import daily as stats_daily
from cogs.community.serverstats import digest as stats_digest
from cogs.community.suggestions import store as suggestions_store
from cogs.config import event_reminders, milestones, welcome, youtube
from cogs.config.tickets import guild_config as tickets_config
from cogs.music import guild_config as music_config
from tools import embed_creator, i18n, premium, premium_upsell, role_audit, settings
from tools.cooldowns import Cooldowns
from tools.formats import format_dt
from tools.i18n import N_, _, ngettext
from tools.snowflake import coerce_id
from tools.views import AuthorView

log = logging.getLogger(__name__)

FORMAT = "yasuho-config"
VERSION = 1
# A fully loaded export (every welcome embed text at its Discord limit, every
# other text field full, even all of it as \u00XX escapes) is ~310 KB worst case
# with 50 max-length gifs; tests/cogs/test_config_transfer.py pins that it fits.
# Only a welcome GIF pool far larger than that (the UI sets no count cap) can
# exceed it, and is then refused whole rather than silently truncated.
MAX_BYTES = 384 * 1024
MAX_NAME = 100  # Discord's channel / role name limit
TEMPLATE_NAME_MAX = 32
IMPORT_COOLDOWN_SECONDS = 30
CONFIRM_TIMEOUT = 300
ROLE_SURFACE = "config_import"
_LOCK_NS = 0x43464754  # 'CFGT'
_INT4_FOLD = 2147483647

# Plan statuses.
UNCHANGED = "unchanged"
CHANGE = "change"
SKIPPED = "skipped"

_TEXT = (discord.ChannelType.text, discord.ChannelType.news)
_SEND = ("view_channel", "send_messages")
_POST = ("view_channel", "send_messages", "embed_links")

# One per guild, shared by import and template apply: a preview is the costly part.
_import_cooldown = Cooldowns(IMPORT_COOLDOWN_SECONDS)


class DocumentError(Exception):
    """The file (or stored template) is not a usable export; ``reason`` says why."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Section registry
# ---------------------------------------------------------------------------


def _role_usable(guild, role, member):
    return not role.is_default()


def _birthday_role_usable(guild, role, member):
    return not birthdays_store.role_problem(guild, role)


def _ping_role_usable(guild, role, member):
    # The event-reminder panel's own rule: a role the manager could ping by hand.
    me = guild.me
    return not role.is_default() and (
        role.mentionable
        or (
            member.guild_permissions.mention_everyone
            and me is not None
            and me.guild_permissions.mention_everyone
        )
    )


@dataclasses.dataclass(frozen=True)
class Ref:
    """A channel or role id inside a section, and what the target must offer."""

    field: str
    kind: str  # "channel" | "role"
    channel_types: tuple = _TEXT
    perms: tuple = _SEND  # what Yasuho needs in the channel
    member_perms: tuple = ("view_channel",)  # what the importing manager needs there
    usable: object = _role_usable  # (guild, role, member) -> bool
    # True only for a role Yasuho HANDS OUT: that is what refuse_dangerous_role
    # guards. A pinged or privilege role (ticket support, DJ, suggestion staff)
    # is normally a moderator role and must survive a round trip.
    grant: bool = False


@dataclasses.dataclass(frozen=True)
class Section:
    name: str
    label: str
    clean: object  # untrusted dict -> fresh, fully-typed dict (pure)
    refs: tuple = ()
    keys: tuple = ()  # flat guild_settings keys (absent = default) ...
    blob_key: str = None  # ... or ONE blob key
    after: object = None  # async (bot, guild, old, new) follow-up of a write
    texts: tuple = ()  # paths of free text shown (excerpted) in the preview


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def _clean_embed(raw):
    raw = raw if isinstance(raw, dict) else {}
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    footer = raw.get("footer") if isinstance(raw.get("footer"), dict) else {}
    colour = raw.get("color")
    fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
    return {
        "title": _text(raw.get("title"), embed_creator.LIMIT_TITLE),
        "description": _text(raw.get("description"), embed_creator.LIMIT_DESC),
        "color": colour if type(colour) is int and 0 <= colour <= 0xFFFFFF else None,
        "author": {
            "name": _text(author.get("name"), embed_creator.LIMIT_AUTHOR),
            "icon": _text(author.get("icon"), 1024),
        },
        "footer": {
            "text": _text(footer.get("text"), embed_creator.LIMIT_FOOTER),
            "icon": _text(footer.get("icon"), 1024),
        },
        "thumbnail": _text(raw.get("thumbnail"), 1024),
        "image": _text(raw.get("image"), 1024),
        "fields": [
            {
                "name": _text(f.get("name"), embed_creator.LIMIT_FIELD_NAME),
                "value": _text(f.get("value"), embed_creator.LIMIT_FIELD_VALUE),
                "inline": f.get("inline") is True,
            }
            for f in fields[: embed_creator.LIMIT_FIELDS]
            if isinstance(f, dict)
        ],
    }


def _clean_welcome(blob):
    """The welcome blob, strictly typed (``welcome._merge_defaults`` trusts shapes)."""
    blob = blob if isinstance(blob, dict) else {}
    gifs = blob.get("gifs") if isinstance(blob.get("gifs"), list) else []
    return welcome._merge_defaults(
        {
            "channel_id": blob.get("channel_id"),
            "enabled": bool(blob.get("enabled", False)),
            "ping": bool(blob.get("ping", True)),
            "card": bool(blob.get("card", True)),
            "random_gif": bool(blob.get("random_gif", False)),
            # The welcome UI's own rule (AddGifModal): an http(s) URL of at most
            # 1024 characters, and NO count cap - so none here either.
            "gifs": [g for g in gifs if embed_creator.is_url(g) and len(g) <= 1024],
            "card_greeting": blob.get("card_greeting"),
            "card_subtitle": blob.get("card_subtitle"),
            "card_color": blob.get("card_color"),
            "embed": _clean_embed(blob.get("embed")),
        }
    )


def _clean_language(raw):
    return {"locale": i18n.normalize(raw.get("locale")) if isinstance(raw.get("locale"), str) else None}


def _clean_tickets(raw):
    tc = tickets_config
    hours = tc.coerce_count(
        raw.get(tc.KEY_INACTIVITY_HOURS),
        minimum=tc.MIN_INACTIVITY_HOURS,
        maximum=tc.MAX_INACTIVITY_HOURS,
        default=None,
    )
    return {
        tc.KEY_SUPPORT_ROLE: coerce_id(raw.get(tc.KEY_SUPPORT_ROLE)),
        tc.KEY_LOG_CHANNEL: coerce_id(raw.get(tc.KEY_LOG_CHANNEL)),
        tc.KEY_MAX_OPEN_PER_USER: tc.coerce_count(
            raw.get(tc.KEY_MAX_OPEN_PER_USER),
            minimum=tc.MIN_OPEN_PER_USER,
            maximum=premium.GUILD_CEILINGS["max_tickets_open_per_user"],
            default=None,
        ),
        tc.KEY_INACTIVITY_HOURS: None if hours is None else tc.snap_inactivity_hours(hours),
        tc.KEY_PANEL_MESSAGE: tc.coerce_text(
            raw.get(tc.KEY_PANEL_MESSAGE), limit=tc.MAX_PANEL_MESSAGE_LENGTH
        ),
        tc.KEY_RATING: True if raw.get(tc.KEY_RATING) is True else None,
    }


_MUSIC_BOOLS = (
    music_config.KEY_AUTOPLAY,
    music_config.KEY_VOTESKIP,
    music_config.KEY_SPONSORBLOCK,
    music_config.KEY_FAIR_QUEUE,
    music_config.KEY_DJ_PRIORITY,
)


def _clean_music(raw):
    mc = music_config
    values = {key: mc.coerce_bool(raw.get(key), None) for key in _MUSIC_BOOLS}
    values[mc.KEY_DEFAULT_VOLUME] = mc.coerce_volume(raw.get(mc.KEY_DEFAULT_VOLUME))
    values[mc.KEY_DJ_ROLE] = mc.coerce_role_id(raw.get(mc.KEY_DJ_ROLE))
    values[mc.KEY_MEMBER_CAP] = mc.coerce_member_cap(raw.get(mc.KEY_MEMBER_CAP))
    return values


_STATS_KEYS = (stats_digest.KEY_DIGEST_CHANNEL, stats_daily.KEY_DAILY_CHANNEL)


def _clean_stats(raw):
    return {key: coerce_id(raw.get(key)) for key in _STATS_KEYS}


async def _after_milestones(bot, guild, old, new):
    cog = bot.get_cog("Milestones")
    if cog is not None and new["enabled"] and new["channel_id"]:
        await cog.arm(guild)  # never replays a milestone already passed


async def _after_birthdays(bot, guild, old, new):
    cog = bot.get_cog("Birthdays")
    if cog is None:
        return
    await cog.reconcile(guild.id)
    was = old["role_id"] if old["enabled"] else None
    now = new["role_id"] if new["enabled"] else None
    if was and was != now:
        await cog.strip_role(guild, was, new)


async def _after_suggestions(bot, guild, old, new):
    cog = bot.get_cog("Suggestions")
    if cog is not None:
        await cog.reconcile_digest(guild.id)


async def _after_event_reminders(bot, guild, old, new):
    cog = bot.get_cog("EventReminders")
    if cog is not None:
        await cog.reconcile_guild(guild)


SECTIONS = {
    s.name: s
    for s in (
        Section("language", N_("Language"), _clean_language, keys=("locale",)),
        Section(
            "welcome",
            N_("Welcome"),
            _clean_welcome,
            refs=(Ref("channel_id", "channel", (discord.ChannelType.text,), _POST),),
            blob_key="welcome",
            texts=(
                ("embed", "title"),
                ("embed", "description"),
                ("embed", "author", "name"),
                ("embed", "footer", "text"),
                ("embed", "fields"),
                ("card_greeting",),
                ("card_subtitle",),
            ),
        ),
        Section(
            "tickets",
            N_("Tickets"),
            _clean_tickets,
            refs=(
                Ref(tickets_config.KEY_SUPPORT_ROLE, "role"),
                Ref(tickets_config.KEY_LOG_CHANNEL, "channel", perms=_POST),
            ),
            keys=tuple(k for k in tickets_config.KEY_ORDER if k != tickets_config.KEY_PANEL_CHANNEL),
            texts=((tickets_config.KEY_PANEL_MESSAGE,),),
        ),
        Section(
            "music",
            N_("Music"),
            _clean_music,
            refs=(Ref(music_config.KEY_DJ_ROLE, "role"),),
            keys=(
                music_config.KEY_DEFAULT_VOLUME,
                *_MUSIC_BOOLS,
                music_config.KEY_DJ_ROLE,
                music_config.KEY_MEMBER_CAP,
            ),
        ),
        Section(
            "milestones",
            N_("Member milestones"),
            milestones.normalise,
            refs=(Ref("channel_id", "channel"),),
            blob_key=milestones.SETTINGS_KEY,
            after=_after_milestones,
            texts=(("message",),),
        ),
        Section(
            "birthdays",
            N_("Birthdays"),
            birthdays_store.normalise,
            refs=(
                Ref("channel_id", "channel"),
                Ref("role_id", "role", usable=_birthday_role_usable, grant=True),
            ),
            blob_key=birthdays_store.SETTINGS_KEY,
            after=_after_birthdays,
            texts=(("message",),),
        ),
        Section(
            "suggestions",
            N_("Suggestions"),
            suggestions_store.normalise,
            refs=(Ref("channel_id", "channel", perms=_POST), Ref("staff_role_id", "role")),
            blob_key=suggestions_store.SETTINGS_KEY,
            after=_after_suggestions,
            texts=(("custom_statuses",),),
        ),
        Section(
            "event_reminders",
            N_("Event reminders"),
            event_reminders.normalise,
            refs=(
                Ref("channel_id", "channel"),
                Ref("role_id", "role", usable=_ping_role_usable),
            ),
            blob_key=event_reminders.SETTINGS_KEY,
            after=_after_event_reminders,
        ),
        Section(
            "youtube",
            N_("YouTube alerts"),
            youtube._config_from,
            refs=(Ref("role_id", "role"),),
            blob_key=youtube.SETTINGS_KEY,
            texts=(("message",),),
        ),
        Section(
            "serverstats",
            N_("Server statistics"),
            _clean_stats,
            # /serverstats digest|daily set hold the manager to CONFIGURER_PERMISSIONS
            # in the channel (a recurring post is still their post): mirrored here.
            refs=tuple(
                Ref(key, "channel", perms=_POST, member_perms=stats_digest.CONFIGURER_PERMISSIONS)
                for key in _STATS_KEYS
            ),
            keys=_STATS_KEYS,
        ),
    )
}


# ---------------------------------------------------------------------------
# Storage (each section through tools.settings, the features' own seam)
# ---------------------------------------------------------------------------

# A flat section in ONE statement, so a failure never half-applies it: the set
# keys are merged in and the unset ones DELETED (absent means default, the rule
# of tickets/guild_config.set_key) together.
_WRITE_KEYS_SQL = (
    "INSERT INTO guild_settings (guild_id, settings) VALUES ($1, $2::jsonb) "
    "ON CONFLICT (guild_id) DO UPDATE SET "
    "settings = (guild_settings.settings - $3::text[]) || $2::jsonb"
)


async def read_section(pool, guild_id, spec):
    """``(configured, values)``: the section as stored, cleaned."""
    if spec.blob_key:
        raw = await settings.get_guild(pool, guild_id, spec.blob_key, None)
        return raw is not None, spec.clean(raw)
    values = spec.clean({key: await settings.get_guild(pool, guild_id, key) for key in spec.keys})
    return any(v is not None for v in values.values()), values


async def write_section(pool, guild_id, spec, values):
    if spec.blob_key:
        await settings.set_guild(pool, guild_id, spec.blob_key, values)
        return
    present = {key: values[key] for key in spec.keys if values.get(key) is not None}
    absent = [key for key in spec.keys if key not in present]
    try:
        await pool.execute(_WRITE_KEYS_SQL, guild_id, json.dumps(present), absent)
    finally:
        # Out-of-band write: drop the cached blob (read-through, so only a re-read).
        settings.invalidate_guild(guild_id)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _lookup(guild, ref, object_id):
    return guild.get_role(object_id) if ref.kind == "role" else guild.get_channel(object_id)


def _visible(ref, found, member):
    """Whether ``member`` may learn this object's name (roles are public)."""
    return ref.kind == "role" or found.permissions_for(member).view_channel


async def build_export(pool, guild, member, *, now=None):
    """The export document of ``guild`` (only the sections it configured).

    A channel the exporter cannot see keeps its id but not its name.
    """
    sections = {}
    for spec in SECTIONS.values():
        configured, values = await read_section(pool, guild.id, spec)
        if not configured:
            continue
        for ref in spec.refs:
            object_id = values.get(ref.field)
            if object_id is not None:
                found = _lookup(guild, ref, object_id)
                values[ref.field] = {
                    "id": str(object_id),
                    "name": found.name if found is not None and _visible(ref, found, member) else None,
                }
        sections[spec.name] = values
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now.isoformat(),
        "source_guild_id": str(guild.id),
        "sections": sections,
    }


def dump(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Parse (strict: the file is untrusted)
# ---------------------------------------------------------------------------


def _parse_ref(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"id", "name"}:
        raise DocumentError("malformed")
    object_id = coerce_id(raw.get("id"))
    name = raw.get("name")
    if object_id is None or not (
        name is None
        or (isinstance(name, str) and len(name) <= MAX_NAME and "\n" not in name and "\r" not in name)
    ):
        raise DocumentError("malformed")
    return {"id": object_id, "name": name}


def _check_strings(value):
    """ValueError on a NUL or a lone surrogate in any key or string (JSON allows
    both as escapes; Postgres jsonb refuses the first, utf-8 the second)."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("NUL")
        value.encode("utf-8")  # a lone surrogate raises UnicodeEncodeError (a ValueError)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_strings(key)
            _check_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_strings(item)


def parse_document(data):
    """``{section: values}`` from an export's bytes, or :class:`DocumentError`.

    References come back as ``{"id": int, "name": str | None}`` (or ``None``);
    every other value is left for the section's own ``clean``. Nothing here
    looks at ``source_guild_id``.
    """
    if len(data) > MAX_BYTES:
        raise DocumentError("too_large")
    try:
        document = json.loads(data.decode("utf-8-sig"))
        _check_strings(document)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DocumentError("malformed") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise DocumentError("malformed")
    version = document.get("version")
    if type(version) is not int or version != VERSION:
        raise DocumentError("version")
    sections = document.get("sections")
    if not isinstance(sections, dict) or set(sections) - set(SECTIONS):
        raise DocumentError("malformed")
    parsed = {}
    for name, values in sections.items():
        if not isinstance(values, dict):
            raise DocumentError("malformed")
        values = dict(values)
        for ref in SECTIONS[name].refs:
            values[ref.field] = _parse_ref(values.get(ref.field))
        parsed[name] = values
    return parsed


# ---------------------------------------------------------------------------
# Plan: remap + re-validate against the TARGET guild
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Note:
    """What happened to one reference: kept / remapped / dropped (with why)."""

    field: str
    kind: str  # "channel" | "role"
    outcome: str  # "remapped" | "unmapped" | "wrong_type" | "dangerous" | "unusable"
    name: str = None  # the source name
    target: object = None  # the target object (remapped, dangerous, unusable)


@dataclasses.dataclass
class SectionPlan:
    name: str
    status: str
    old: dict
    new: dict
    changed: tuple = ()
    notes: list = dataclasses.field(default_factory=list)
    problem: tuple = None  # ("bot", channel, missing) | ("member", channel)


def _kind_ok(ref, found):
    return ref.kind == "role" or getattr(found, "type", None) in ref.channel_types


def _candidates(guild, ref, member):
    if ref.kind == "role":
        return guild.roles
    # Only channels the manager can see: a name match must never reveal one.
    return [
        c
        for c in guild.channels
        if getattr(c, "type", None) in ref.channel_types and _visible(ref, c, member)
    ]


def resolve_ref(guild, member, ref, raw):
    """``(target_id or None, Note or None)`` for one parsed reference."""
    if raw is None:
        return None, None
    found = _lookup(guild, ref, raw["id"])
    if found is not None and not _visible(ref, found, member):
        found = None  # hidden from this manager: as if it did not exist
    note = None
    if found is not None:
        if not _kind_ok(ref, found):
            return None, Note(ref.field, ref.kind, "wrong_type", raw["name"], found)
    else:
        matches = [o for o in _candidates(guild, ref, member) if raw["name"] and o.name == raw["name"]]
        if len(matches) != 1:
            return None, Note(ref.field, ref.kind, "unmapped", raw["name"])
        found = matches[0]
        note = Note(ref.field, ref.kind, "remapped", raw["name"], found)
    if ref.kind == "role":
        if ref.grant and role_audit.refuse_dangerous_role(
            found, surface=ROLE_SURFACE, guild_id=guild.id
        ):
            return None, Note(ref.field, ref.kind, "dangerous", raw["name"], found)
        if not ref.usable(guild, found, member):
            return None, Note(ref.field, ref.kind, "unusable", raw["name"], found)
    return found.id, note


def _channel_problem(guild, member, spec, values):
    for ref in spec.refs:
        if ref.kind != "channel" or values.get(ref.field) is None:
            continue
        channel = guild.get_channel(values[ref.field])
        me = guild.me
        missing = stats_digest.missing_permissions(
            channel.permissions_for(me) if me is not None else None, ref.perms
        )
        if missing:
            return ("bot", channel, missing)
        missing = stats_digest.missing_permissions(channel.permissions_for(member), ref.member_perms)
        if missing:
            return ("member", channel, missing)
    return None


async def build_plan(pool, guild, member, parsed):
    """One :class:`SectionPlan` per section of the file, against the guild NOW."""
    plans = []
    for spec in SECTIONS.values():
        if spec.name not in parsed:
            continue
        _configured, old = await read_section(pool, guild.id, spec)
        values = dict(parsed[spec.name])
        notes = []
        for ref in spec.refs:
            values[ref.field], note = resolve_ref(guild, member, ref, values[ref.field])
            if note is not None:
                notes.append(note)
        new = spec.clean(values)
        if new == old:
            plans.append(SectionPlan(spec.name, UNCHANGED, old, new, notes=notes))
            continue
        changed = tuple(k for k in new if new.get(k) != old.get(k))
        problem = _channel_problem(guild, member, spec, new)
        plans.append(
            SectionPlan(spec.name, SKIPPED if problem else CHANGE, old, new, changed, notes, problem)
        )
    return plans


async def apply_plan(bot, guild, plans):
    """Write every CHANGE section; ``[(plan, "applied" | "failed" | status)]``."""
    results = []
    for plan in plans:
        if plan.status != CHANGE:
            results.append((plan, plan.status))
            continue
        spec = SECTIONS[plan.name]
        try:
            await write_section(bot.db_pool, guild.id, spec, plan.new)
        except Exception:
            log.exception("config import: section %s failed for guild %s", plan.name, guild.id)
            results.append((plan, "failed"))
            continue
        results.append((plan, "applied"))
        if spec.after is not None:
            try:
                await spec.after(bot, guild, plan.old, plan.new)
            except Exception:
                # The write is durable; the feature's own on_ready reconcile catches up.
                log.exception("config import: follow-up of %s failed for guild %s", plan.name, guild.id)
    return results


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _mention(kind, target):
    return ("<@&{}>" if kind == "role" else "<#{}>").format(target.id)


def _plain(name):
    return discord.utils.escape_markdown(name or "?")


def _note_line(note):
    field = "`{}`".format(note.field)
    if note.outcome == "remapped":
        return _("{field}: {name} -> {target} (matched by name)").format(
            field=field, name=_plain(note.name), target=_mention(note.kind, note.target)
        )
    if note.outcome == "dangerous":
        return _("{field}: {target} dropped, it has powerful permissions").format(
            field=field, target=_mention(note.kind, note.target)
        )
    if note.outcome == "unusable":
        return _("{field}: {target} dropped, it can't be used for this").format(
            field=field, target=_mention(note.kind, note.target)
        )
    if note.outcome == "wrong_type":
        return _("{field}: {name} dropped, it is not the right kind here").format(
            field=field, name=_plain(note.name)
        )
    return _("{field}: {name} dropped, nothing with that name here").format(
        field=field, name=_plain(note.name)
    )


def _section_label(name):
    label = SECTIONS[name].label
    return _(label)


def _plan_line(plan):
    section = _section_label(plan.name)
    if plan.status == UNCHANGED:
        return _("**{section}**: no change").format(section=section)
    if plan.status == SKIPPED:
        kind, channel = plan.problem[0], plan.problem[1]
        if kind == "bot":
            return _("**{section}**: skipped, I need {permissions} in {channel}").format(
                section=section,
                permissions=stats_digest.describe_permissions(plan.problem[2]),
                channel=_mention("channel", channel),
            )
        return _("**{section}**: skipped, you need {permissions} in {channel}").format(
            section=section,
            permissions=stats_digest.describe_permissions(plan.problem[2]),
            channel=_mention("channel", channel),
        )
    return _("**{section}**: will be updated ({keys})").format(
        section=section, keys=", ".join("`{}`".format(k) for k in plan.changed)
    )


def _clip(lines):
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3997] + "..."


EXCERPT = 80


def _dig(values, path):
    for step in path:
        values = values.get(step) if isinstance(values, dict) else None
    return values


def _flatten(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " | ".join(filter(None, (_flatten(item) for item in value)))
    if isinstance(value, dict):
        return " ".join(filter(None, (_flatten(item) for item in value.values() if isinstance(item, str))))
    return ""


def excerpt(text):
    """One line, at most EXCERPT characters, markdown and mentions neutralised."""
    text = " ".join(text.split())
    if len(text) > EXCERPT:
        text = text[:EXCERPT] + "..."
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _text_lines(plan):
    """The changed free text of a section, excerpted, so the manager sees what
    will be posted (format only, no prose: ``field``: "text")."""
    if plan.status != CHANGE:
        return []
    lines = []
    for path in SECTIONS[plan.name].texts:
        new = _flatten(_dig(plan.new, path))
        if new and new != _flatten(_dig(plan.old, path)):
            lines.append('- `{}`: "{}"'.format(".".join(path), excerpt(new)))
    return lines


def _detail_lines(plan):
    return ["- " + _note_line(note) for note in plan.notes] + _text_lines(plan)


def render_preview(plans):
    lines = []
    for plan in plans:
        lines.append(_plan_line(plan))
        lines.extend(_detail_lines(plan))
    return _clip(lines)


def render_results(results):
    """Per-section outcome plus the remaps / drops as rebuilt AT THE CLICK."""
    lines = []
    for plan, outcome in results:
        section = _section_label(plan.name)
        if outcome == "applied":
            lines.append(_("**{section}**: applied").format(section=section))
        elif outcome == "failed":
            lines.append(_("**{section}**: failed, try again later").format(section=section))
        else:
            lines.append(_plan_line(plan))
        lines.extend("- " + _note_line(note) for note in plan.notes)
    return _clip(lines)


def describe_error(reason):
    if reason == "too_large":
        return _("That file is too large (the limit is {kb} KB).").format(kb=MAX_BYTES // 1024)
    if reason == "version":
        return _("That export comes from a version of Yasuho this one can't read.")
    return _("That file is not a Yasuho configuration export.")


# ---------------------------------------------------------------------------
# Premium + the confirm view
# ---------------------------------------------------------------------------


def import_allowed(bot, guild_id):
    return premium.resolve_guild_limits(bot, guild_id).config_import


async def _refuse_interaction(bot, interaction, limit_key):
    upsell = await premium_upsell.for_guild_refusal(
        bot,
        limit_key=limit_key,
        guild_id=interaction.guild.id,
        person_id=interaction.user.id,
        is_admin=premium_upsell.invoker_is_admin(interaction.user),
        already_top_tier=premium_upsell.is_guild_already_top_tier(bot, interaction.guild.id),
        kind="unlock",
    )
    text = _("Configuration import and templates are a Yasuho+ feature.")
    kwargs = {"ephemeral": True}
    if upsell is not None:
        text += "\n" + upsell.line
        view = upsell.view()
        if view is not None:
            kwargs["view"] = view
    await interaction.response.send_message(text, **kwargs)


class ImportConfirmView(AuthorView):
    """Apply / Cancel under a preview. Everything is re-checked at the click."""

    def __init__(self, bot, guild, author_id, parsed):
        super().__init__(author_id, timeout=CONFIRM_TIMEOUT)
        self.bot = bot
        self.guild = guild
        self.parsed = parsed
        self._done = False
        self.apply_button.label = _("Apply")
        self.cancel_button.label = _("Cancel")

    def _close(self):
        self._done = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply_button(self, interaction, button):
        if self._done:
            return await interaction.response.defer()
        if interaction.guild is None or interaction.guild.id != self.guild.id:
            return await interaction.response.defer()
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                _("You need the Manage Server permission to do that."), ephemeral=True
            )
        if not import_allowed(self.bot, self.guild.id):
            return await _refuse_interaction(self.bot, interaction, "config_import")
        self._close()
        await interaction.response.defer()
        # Rebuilt from scratch: every id, permission and role rule is re-validated NOW.
        plans = await build_plan(self.bot.db_pool, interaction.guild, interaction.user, self.parsed)
        results = await apply_plan(self.bot, interaction.guild, plans)
        embed = discord.Embed(title=_("Import finished"), description=render_results(results))
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction, button):
        if self._done:
            return await interaction.response.defer()
        self._close()
        await interaction.response.edit_message(
            content=_("Import cancelled. Nothing was changed."), embed=None, view=self
        )


# ---------------------------------------------------------------------------
# Command bodies (the /config group in cogs/config/settings.py delegates here)
# ---------------------------------------------------------------------------


async def _refuse_premium(bot, ctx, limit_key):
    await premium_upsell.refuse_in_context(
        bot,
        ctx,
        _("Configuration import and templates are a Yasuho+ feature."),
        limit_key=limit_key,
        kind="unlock",
    )


def _spend_cooldown(guild_id):
    if _import_cooldown.is_active(guild_id):
        return False
    _import_cooldown.touch(guild_id)
    return True


async def _send_preview(bot, ctx, parsed):
    plans = await build_plan(bot.db_pool, ctx.guild, ctx.author, parsed)
    if not plans or all(p.status == UNCHANGED for p in plans):
        return await ctx.send(
            _("This configuration matches the current one: there is nothing to import."),
            ephemeral=True,
        )
    embed = discord.Embed(
        title=_("Import preview"),
        description=render_preview(plans),
    )
    embed.set_footer(text=_("Nothing is saved until you press Apply."))
    view = ImportConfirmView(bot, ctx.guild, ctx.author.id, parsed)
    view.message = await ctx.send(
        embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


async def _refuse_prefix(ctx):
    """True (and answered) on the prefix path: there is no ephemeral there, and an
    export or a preview names this server's channels and roles."""
    if ctx.interaction is not None:
        return False
    await ctx.send(_("Please use the slash command for this, so the answer stays private."))
    return True


async def export_command(bot, ctx):
    if await _refuse_prefix(ctx):
        return
    await ctx.defer(ephemeral=True)
    document = await build_export(bot.db_pool, ctx.guild, ctx.author)
    await ctx.send(
        _("Here is this server's configuration. Load it with `/config import`."),
        file=discord.File(io.BytesIO(dump(document)), filename="yasuho-config.json"),
        ephemeral=True,
    )


async def import_command(bot, ctx, attachment):
    if await _refuse_prefix(ctx):
        return
    if not import_allowed(bot, ctx.guild.id):
        return await _refuse_premium(bot, ctx, "config_import")
    if not _spend_cooldown(ctx.guild.id):
        return await ctx.send(
            _("Please wait a moment before importing a configuration again."), ephemeral=True
        )
    await ctx.defer(ephemeral=True)
    try:
        if attachment.size > MAX_BYTES:
            raise DocumentError("too_large")
        parsed = parse_document(await attachment.read())
    except DocumentError as error:
        return await ctx.send(describe_error(error.reason), ephemeral=True)
    await _send_preview(bot, ctx, parsed)


# -- templates ---------------------------------------------------------------


def clean_template_name(name):
    name = " ".join((name or "").split())
    return name if 0 < len(name) <= TEMPLATE_NAME_MAX else None


async def save_template(pool, guild_id, name, payload, cap):
    """``"saved"`` / ``"replaced"`` / ``"full"``, the count checked under a lock."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", _LOCK_NS, guild_id % _INT4_FOLD)
            exists = await conn.fetchval(
                "SELECT 1 FROM config_templates WHERE guild_id = $1 AND name = $2", guild_id, name
            )
            if not exists:
                count = await conn.fetchval(
                    "SELECT count(*) FROM config_templates WHERE guild_id = $1", guild_id
                )
                if count >= cap:
                    return "full"
            await conn.execute(
                "INSERT INTO config_templates (guild_id, name, payload) VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT (guild_id, name) DO UPDATE SET payload = EXCLUDED.payload, created_at = now()",
                guild_id,
                name,
                payload,
            )
    return "replaced" if exists else "saved"


async def load_template(pool, guild_id, name):
    return await pool.fetchval(
        "SELECT payload FROM config_templates WHERE guild_id = $1 AND name = $2", guild_id, name
    )


async def list_templates(pool, guild_id):
    return await pool.fetch(
        "SELECT name, created_at FROM config_templates WHERE guild_id = $1 ORDER BY name", guild_id
    )


async def delete_template(pool, guild_id, name):
    return (
        await pool.fetchval(
            "DELETE FROM config_templates WHERE guild_id = $1 AND name = $2 RETURNING 1",
            guild_id,
            name,
        )
        is not None
    )


async def template_save_command(bot, ctx, name):
    cap = premium.resolve_guild_limits(bot, ctx.guild.id).max_config_templates
    if cap <= 0:
        return await _refuse_premium(bot, ctx, "max_config_templates")
    clean = clean_template_name(name)
    if clean is None:
        return await ctx.send(
            _("A template name is 1 to {n} characters.").format(n=TEMPLATE_NAME_MAX), ephemeral=True
        )
    await ctx.defer(ephemeral=True)
    payload = dump(await build_export(bot.db_pool, ctx.guild, ctx.author))
    if len(payload) > MAX_BYTES:
        return await ctx.send(describe_error("too_large"), ephemeral=True)
    outcome = await save_template(bot.db_pool, ctx.guild.id, clean, payload.decode("utf-8"), cap)
    if outcome == "full":
        return await ctx.send(
            ngettext(
                "This server already has {n} template. Delete one first.",
                "This server already has {n} templates. Delete one first.",
                cap,
            ).format(n=cap),
            ephemeral=True,
        )
    text = (
        _("Replaced the template **{name}**.")
        if outcome == "replaced"
        else _("Saved the template **{name}**.")
    )
    await ctx.send(text.format(name=discord.utils.escape_markdown(clean)), ephemeral=True)


async def template_apply_command(bot, ctx, name):
    if await _refuse_prefix(ctx):
        return
    if not import_allowed(bot, ctx.guild.id):
        return await _refuse_premium(bot, ctx, "config_import")
    if not _spend_cooldown(ctx.guild.id):
        return await ctx.send(
            _("Please wait a moment before importing a configuration again."), ephemeral=True
        )
    await ctx.defer(ephemeral=True)
    clean = clean_template_name(name)
    payload = await load_template(bot.db_pool, ctx.guild.id, clean) if clean else None
    if payload is None:
        return await ctx.send(
            _("No template is called **{name}**.").format(
                name=discord.utils.escape_markdown(name or "")
            ),
            ephemeral=True,
        )
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        parsed = parse_document(raw.encode("utf-8"))
    except DocumentError:
        return await ctx.send(
            _("That template can no longer be read. Delete it and save it again."),
            ephemeral=True,
        )
    await _send_preview(bot, ctx, parsed)


async def template_list_command(bot, ctx):
    await ctx.defer(ephemeral=True)
    rows = await list_templates(bot.db_pool, ctx.guild.id)
    if not rows:
        return await ctx.send(
            _("This server has no configuration templates yet. Save one with `/config template save`."),
            ephemeral=True,
        )
    lines = [
        "**{}** - {}".format(discord.utils.escape_markdown(row["name"]), format_dt(row["created_at"], "R"))
        for row in rows
    ]
    embed = discord.Embed(title=_("Configuration templates"), description=_clip(lines))
    await ctx.send(embed=embed, ephemeral=True)


async def template_delete_command(bot, ctx, name):
    await ctx.defer(ephemeral=True)
    clean = clean_template_name(name)
    shown = discord.utils.escape_markdown(clean or name or "")
    if clean and await delete_template(bot.db_pool, ctx.guild.id, clean):
        return await ctx.send(_("Deleted the template **{name}**.").format(name=shown), ephemeral=True)
    await ctx.send(_("No template is called **{name}**.").format(name=shown), ephemeral=True)
