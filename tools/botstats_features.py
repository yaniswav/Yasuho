"""Feature adoption for the ``?botstats`` Features page (cogs/system/botstats.py).

Answers "which features do servers actually configure?" from the real config
tables (schema.sql), in ONE bounded statement built from the static
:data:`FEATURES` list - the same shape as ``botstats.build_row_counts_sql``.

Honest stats, same contract as botstats.py: a feature whose count could not be
read is UNKNOWN, never zero; and a count only covers guilds the bot is in right
now (``guild_id = ANY($1)``), so servers that kicked the bot do not inflate the
share. Pure SQL text + pure Python: no Discord, no cog, no pool reference.

Typography rule: ASCII '-' and '...' only.
"""

from __future__ import annotations

import dataclasses

from tools.i18n import N_, _

# A feature is "under-adopted" below this share of the guilds the bot is in.
LOW_ADOPTION = 0.05

# Hard cap on listed features, so a long list can never push the card past
# Discord's text budget.
FEATURE_LINES_LIMIT = 40


@dataclasses.dataclass(frozen=True)
class Feature:
    """One measured feature: the table it lives in and what "uses it" means.

    ``scope`` is "guild" (counted as DISTINCT guild_id, filtered to the guilds
    the bot is in, shown as a share of them) or "user" (counted as DISTINCT
    ``column``, shown as a plain head count: people are not a share of servers).
    ``where`` is a static SQL predicate, never runtime input.
    """

    key: str
    label: str
    table: str
    scope: str = "guild"
    where: str | None = None
    column: str | None = None
    # More (table, where) arms whose guilds also count: ONE feature stored in
    # several places is the UNION of them, still counted once per guild.
    also: tuple = ()


FEATURES = (
    Feature("music_playlists", N_("Server playlists"), "guild_playlists"),
    Feature("music_247", N_("24/7 music"), "music_247"),
    Feature(
        "music_settings",
        N_("Music settings"),
        "guild_settings",
        where="EXISTS (SELECT 1 FROM jsonb_object_keys(settings) k "
        "WHERE k LIKE 'music\\_%')",
    ),
    Feature("tickets", N_("Tickets"), "tickets"),
    Feature("anilist_feeds", N_("AniList feeds"), "anilist_feeds", where="enabled"),
    Feature("anilist_follows", N_("AniList follows"), "anilist_follows"),
    Feature("anilist_subs", N_("AniList channel subs"), "anilist_channel_subs"),
    Feature(
        "mangadex_alerts",
        N_("MangaDex chapter alerts"),
        "anilist_channel_subs",
        where="media_type = 'MANGA'",
    ),
    Feature("airing_roles", N_("Airing ping roles"), "anilist_airing_roles"),
    Feature("watchalongs", N_("Watch-alongs"), "anilist_watchalongs"),
    Feature("leveling", N_("Leveling enabled"), "level_config", where="enabled"),
    Feature("level_rewards", N_("Level role rewards"), "level_rewards"),
    Feature(
        "seasons",
        N_("XP seasons"),
        "level_config",
        where="season_announce OR season_champion_role_id IS NOT NULL",
    ),
    Feature("starboard", N_("Starboard"), "starboard"),
    Feature("welcome", N_("Welcome messages"), "welcome"),
    Feature("birthdays", N_("Birthdays"), "birthdays"),
    Feature("reaction_roles", N_("Reaction roles"), "reaction_roles"),
    Feature("button_roles", N_("Button roles"), "button_roles"),
    Feature("role_menus", N_("Role menus"), "role_menus"),
    Feature("temp_roles", N_("Temporary roles"), "temp_roles"),
    Feature("youtube", N_("YouTube notifications"), "youtube_follows"),
    Feature("twitch", N_("Twitch alerts"), "twitch_alert"),
    # The digest channel is the setting that turns it on (digest.py reads the
    # same key); serverstats_digest_state only appears after a first delivery.
    # ->> is NULL for an absent key and for a JSON null alike.
    Feature(
        "digest",
        N_("Server stats digest"),
        "guild_settings",
        where="settings ->> 'serverstats_digest_channel' IS NOT NULL",
    ),
    # The invite filter is a guild_settings flag (cogs/moderation/automod.py),
    # stored as a JSON boolean: ->> reads 'true' or 'false'.
    Feature(
        "automod",
        N_("Automod"),
        "automod",
        where="antilink OR antispam",
        also=(("guild_settings", "settings ->> 'antiinvite' = 'true'"),),
    ),
    Feature("modlog", N_("Mod log"), "modlog"),
    Feature("suggestions", N_("Suggestions"), "suggestions"),
    Feature("custom_commands", N_("Custom commands"), "custom_commands"),
    Feature("config_templates", N_("Config templates"), "config_templates"),
    Feature(
        "reminders",
        N_("Pending reminders"),
        "timers",
        scope="user",
        where="event = 'reminder'",
        column="extra->>'author_id'",
    ),
    Feature("profiles", N_("User profiles"), "user_profiles", scope="user"),
    Feature("socials", N_("Linked social accounts"), "profile_connections", scope="user"),
    Feature("favourites", N_("Music favourites"), "music_favorites", scope="user"),
    Feature(
        "chapter_dms",
        N_("Chapter DM opt-ins"),
        "anilist_chapter_optins",
        scope="user",
        where="enabled",
    ),
    Feature("votes", N_("top.gg voters"), "topgg_votes", scope="user"),
)


def build_adoption_sql(features):
    """One UNION ALL statement returning ``(key, n)`` per feature.

    ``$1`` is the bigint[] of guild ids the bot is in. Identifiers and
    predicates are interpolated because SQL has no parameter form for them;
    that is safe HERE only: ``features`` is the module-level literal above,
    never a runtime value.
    """
    parts = []
    for feature in features:
        if feature.scope == "guild":
            column = feature.column or "guild_id"
            predicates = ["guild_id = ANY($1::bigint[])"]
        else:
            column = feature.column or "user_id"
            predicates = []
        if feature.where:
            predicates.append("(" + feature.where + ")")
        source = feature.table
        if feature.also:
            arms = ((feature.table, feature.where),) + tuple(feature.also)
            source = "(" + " UNION ".join(
                "SELECT {column} FROM {table}{where}".format(
                    column=column, table=table, where=" WHERE (" + where + ")" if where else ""
                )
                for table, where in arms
            ) + ") AS u"
            predicates = predicates[:1]
        parts.append(
            "SELECT '{key}' AS key, COUNT(DISTINCT {column})::bigint AS n "
            "FROM {table}{where}".format(
                key=feature.key,
                column=column,
                table=source,
                where=" WHERE " + " AND ".join(predicates) if predicates else "",
            )
        )
    return "\nUNION ALL ".join(parts)


ADOPTION = build_adoption_sql(FEATURES)


async def fetch_adoption(pool, guild_ids, *, timeout):
    """``{feature key: n}`` for every feature, in one statement."""
    rows = await pool.fetch(ADOPTION, list(guild_ids), timeout=timeout)
    return {row["key"]: row["n"] for row in rows}


def _percent(count, total):
    if not total:
        return "-"
    return "{:.1f}%".format(min(count, total) * 100 / total)


def _ranked(features, counts):
    """Known counts first (biggest first), unknown ones last, label as tiebreak."""
    def order(feature):
        count = counts.get(feature.key)
        return (count is None, -(count or 0), _(feature.label))

    return sorted(features, key=order)


def render_features(counts, guild_total, *, features=FEATURES, limit=FEATURE_LINES_LIMIT):
    """The Features page: ``[(heading, lines)]`` from plain data only.

    ``counts`` is ``fetch_adoption``'s dict, or ``None`` when the read failed.
    A key missing from it is unknown, not zero.
    """
    if counts is None:
        return [(_("Feature adoption"), [_("Adoption figures are unavailable right now.")])]

    guild_features = [f for f in features if f.scope == "guild"]
    user_features = [f for f in features if f.scope == "user"]

    ranked = _ranked(guild_features, counts)
    lines = []
    for feature in ranked[:limit]:
        count = counts.get(feature.key)
        if count is None:
            lines.append(_("{label} - unknown").format(label=_(feature.label)))
        else:
            lines.append(
                _("{label} - {count} of {total} ({percent})").format(
                    label=_(feature.label),
                    count=count,
                    total=guild_total,
                    percent=_percent(count, guild_total),
                )
            )
    if len(ranked) > limit:
        lines.append(_("... and {count} more").format(count=len(ranked) - limit))

    sections = [(_("Servers using each feature"), lines)]

    people = []
    for feature in _ranked(user_features, counts)[:limit]:
        count = counts.get(feature.key)
        if count is None:
            people.append(_("{label} - unknown").format(label=_(feature.label)))
        else:
            people.append(
                _("{label} - {count}").format(label=_(feature.label), count=count)
            )
    if people:
        sections.append((_("People"), people))

    sections[-1][1].append(
        "-# "
        + _("Counts cover only the {total} servers Yasuho is in right now.").format(
            total=guild_total
        )
    )
    return sections
