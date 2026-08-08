# Yasuho - Privacy Policy

_Last updated: August 8, 2026_

Yasuho ("the bot") is a Discord community bot. This document explains what data
the bot processes, what it stores, for how long, and how you can see or delete
it. We collect the minimum needed for each feature, and several features are
strictly opt-in.

## What we store

**Server configuration** (per server, controlled by server managers): prefixes,
welcome/autorole/automod/starboard/leveling/music settings, custom commands and
their responses, role menus. This data belongs to the server, not to a user.

**Moderation records** (per server): warnings and moderation cases created by
that server's moderators (user ID, moderator ID, reason, timestamp). These exist
so that server moderation works; they are removed when the data ages out of the
server's retention window or the server removes the bot.

**Leveling**: your user ID with XP totals, levels, and monthly season results
per server.

**Server statistics**: aggregate counters only (messages per day, joins/leaves
per day). No message content and no per-user activity is stored.

**User preferences**: language, privacy toggles, and similar settings you set
yourself.

**Social profile** (entirely user-created): bio, pronouns, gaming IDs and
external account usernames (AniList, Steam, osu!, Last.fm, Backloggd) that you
explicitly link, with per-field visibility you control. AniList OAuth tokens are
stored encrypted at rest. Public data fetched from those services is cached
briefly to render your profile card.

**Presence / "recently played"** (strictly OPT-IN): if - and only if - you
enable it (`/profile presence gaming on`), the bot stores aggregate play data:
game name, total minutes, last-played timestamp (top games, 30-day display
window). Never a minute-by-minute timeline. Users who have not opted in are
discarded at the event level: nothing is recorded. Spotify listening status is
displayed live from Discord and never stored. Turning the feature off deletes
the collected aggregates immediately.

**Avatar history** (opt-out): past avatars can be listed by an avatar-history
command; `?mydata deleteavatars` permanently deletes yours and disables future
tracking for you.

**Top.gg votes**: if you vote for the bot on top.gg, top.gg tells us that you
did. We store your user ID with the time of your latest vote, your consecutive
vote streak and your lifetime vote count, which is what lets a vote grant a
temporary XP bonus. Nothing else about the vote is stored, and we never poll
top.gg to find out who has voted.

**Content you ask us to keep**: reminder texts, music favorites and playlists.
Kept until you delete them.

**Command usage**: anonymous aggregate counters (command name x day). No user
ID, no server ID.

## What we do NOT do

- We do not store message content. Messages are processed in memory (prefix
  commands, automod filtering, custom command triggers, mini-games) and
  discarded.
- We do not use any data to train machine learning or AI models.
- We do not sell or share data with third parties. External services (AniList,
  MangaDex, Steam, osu!, Last.fm, Backloggd, top.gg) are only queried with
  identifiers you provided, to render features you asked for.

## Where data lives

All data is stored in a private PostgreSQL database on a server operated by the
bot owner, with access limited to the bot process and its operator. Database
backups are encrypted at rest; OAuth tokens are additionally encrypted at the
application level. Backups are kept for disaster recovery and are subject to
the same deletion schedule on restore.

## Retention

- Server statistics aggregates: 90 days.
- Presence aggregates: 30-day display window, deleted entirely on opt-out.
- Dashboard request logs (your own actions on the web dashboard): 30 days after
  completion.
- Anonymous command-usage aggregates: 400 days.
- Top.gg vote record: kept until you delete it. There is no automatic window,
  because the record IS the streak and the lifetime count it exists to show;
  ageing it out would quietly take a reward away. One row per voter, deleted in
  full by `?mydata deleteprofile`.
- Server-scoped data (configuration, moderation records, leveling) is purged on
  a retention schedule after the bot is removed from a server.

## Your rights

- `?mydata export` - receive a complete machine-readable export of everything
  the bot holds about you (rate-limited to once per hour). Also available from
  the web dashboard.
- `?mydata deleteprofile` - permanently delete your profile, gaming IDs, linked
  accounts, visibility choices, collected presence data, and your top.gg vote
  record.
- `?mydata deleteavatars` - permanently delete your avatar history and disable
  future tracking.
- `/connections unlink` - unlink an external account (removes its data and
  visibility immediately).
- `/profile presence gaming off` - stop and forget presence collection.

## Contact

Questions or requests: open an issue at
https://github.com/yaniswav/Yasuho/issues or contact the owner through the
bot's support server listed on its Discord profile.

Changes to this policy will be published at this same address.
