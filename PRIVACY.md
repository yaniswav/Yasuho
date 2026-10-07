# Yasuho - Privacy Policy

_Last updated: October 7, 2026_

Yasuho ("the bot") is a Discord community bot. This document explains what data
the bot processes, what it stores, for how long, and how you can see or delete
it. We collect the minimum needed for each feature, and several features are
strictly opt-in.

## Who is responsible

The data controller is **Horizon Vista**, the trade name of Yanis, a sole
proprietor (entrepreneur individuel) in France. Contact for any privacy
question or request: **azellaxmc@gmail.com**.

## What we store

**Server configuration** (per server, controlled by server managers): prefixes,
welcome/autorole/automod/starboard/leveling/music settings, custom commands and
their responses, role menus. This data belongs to the server, not to a user.

**Moderation records** (per server): warnings and moderation cases created by
that server's moderators (user ID, moderator ID, reason, timestamp). These exist
so that server moderation works. There is no automatic expiry: a warning is kept
until a moderator removes it (`?delwarn`, or the remove button on the warn
list), and every moderation record is kept until the server removes the bot,
after which that server's data is purged on the schedule below.

**Dashboard change log** (per server): if a server manager uses the web
dashboard, each configuration change made there is recorded - which setting was
changed, the acting manager's user ID, and when. This is the server's audit
trail of who changed what, not the manager's own record: like moderation
records, the manager who made a change can see it in their own data export but
cannot erase it, and the whole log dies with the server when the bot is removed
(it is also aged out on the schedule below meanwhile).

**Support tickets** (per server): metadata only - the ticket number, the private
thread's ID, who opened it, which staff member claimed it, who closed it, when
it was opened and closed, and whether it is still open. The bot never stores the
conversation: it has no database column for it, and none of it is written
anywhere the bot keeps.

Transcripts. If - and only if - a server manager has set a ticket log channel,
the bot uploads a plain-text transcript of the thread to **that server's own
channel** when the ticket closes, so the staff team keeps a record of what was
agreed. The file is built in memory, sent once, and never stored by the bot;
from then on it lives in that Discord channel, under that server's control, and
it outlives the thread. It contains what was said in the ticket, so a server
should point that setting at a staff-only channel - the bot warns when the
channel it is pointed at is readable by everyone. A server with no ticket log
channel gets no transcript at all. The closing message in the thread tells you
which of the two happened, every time: that no transcript was saved, or that
one was just saved to that server's ticket log.

**Leveling**: your user ID with XP totals, levels, and monthly season results
per server. If you customise your own rank card (`/rankcard`), the accent colour
you chose and the background image you uploaded are stored under your user ID -
the image re-encoded and cropped to the card, never the original file. Servers
can refuse personal card styles on their own cards; that setting displays
nothing and deletes nothing. Clearing it (`/rankcard clear`) or erasing your data
removes both.

**Server statistics**: aggregate counters only (messages per day, joins/leaves
per day). No message content and no per-user activity is stored. They are shown
over up to 90 days, or up to 365 days on a server with Yasuho+.

**User preferences**: language, privacy toggles, and similar settings you set
yourself.

**Social profile** (entirely user-created): bio, pronouns, gaming IDs and
external account usernames (AniList, Steam, osu!, Last.fm, Backloggd) that you
explicitly link, with per-field visibility you control. AniList OAuth tokens are
stored encrypted at rest. Public data fetched from those services is cached
briefly to render your profile card.

**AniList / MangaDex alerts** (strictly OPT-IN): if you turn on new-episode
alerts (`/anilist airing`) or new-chapter alerts (`/anilist chapters`), we store
your Discord user ID, your AniList numeric user ID and the date you opted in,
which is what lets the alert poller read your PUBLIC list without using your
token. Turning an alert off stops the DMs and keeps the row so you can turn it
back on later; `?mydata deleteprofile` deletes the row itself.

**Presence / "recently played"** (strictly OPT-IN): if - and only if - you
enable it (`/profile presence gaming on`), the bot stores aggregate play data:
game name, total minutes, last-played timestamp (top games, 30-day window - see
Retention below). Never a minute-by-minute timeline. Users who have not opted
in are discarded at the event level: nothing is recorded. Spotify listening status is
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

**Premium purchases**: premium perks (Yasuho+ for a server, Pack Confort for
you) are bought through Discord, which tells us about each purchase. We keep a
copy of what Discord reports: which offer, the server ID or your user ID it
belongs to, when it starts and ends, and whether it was cancelled or refunded.
Payment details never reach us: Discord handles billing. If the bot owner gives
premium as a gift, we record who received it, the note the owner wrote, and
when it starts, ends or is revoked.

**Limit notes**: when you reach a limit that a premium offer would raise, the
bot may add one line about it to its reply, at most once every 7 days per kind
of limit. To keep that promise we store your user ID, which limit, and when the
line was last shown.

**Content you ask us to keep**: reminder texts, music favorites and playlists,
kept until you delete them; and your AFK status message, deleted as soon as you
are back.

**Command usage**: anonymous aggregate counters (command name x day). No user
ID, no server ID.

## What we do NOT do

- We do not store your conversations. Messages are processed in memory (prefix
  commands, automod filtering, custom command triggers, mini-games) and
  discarded. The only text we keep is text explicitly given to a command for
  that purpose, all listed above: reminder texts, AFK status messages,
  moderation case reasons, and custom command replies written by server
  managers. A deleted message is held in memory for at most 15 minutes so
  moderators can use the snipe command, and is never written to disk. Our logs
  never contain message text.
- We do not use any data to train machine learning or AI models.
- We do not sell or share data with third parties. External services are only
  contacted to render a feature you asked for, and only with what that feature
  needs:
  - **AniList, MangaDex, Steam, osu!, Last.fm, Backloggd, top.gg** receive the
    identifiers you gave us (a username, a numeric id) so we can fetch what
    they publish about you.
  - **Music sources.** When you use a music command, the words you type - your
    search terms, or the link you paste - are sent to our audio backend and on
    to the service that can answer them: **YouTube**, **Spotify**,
    **SoundCloud**, **Bandcamp**, **Twitch**, **Vimeo**, and any direct URL you
    ask us to play. Those searches are what makes music work; they are not
    stored by the bot and are not tied to your identity at those services (we
    hold no account of yours there), but the search itself does leave the bot.
    Lyrics, when you ask for them, are fetched the same way. If you would
    rather a query never leave, do not run the command.

## Where data lives

All data is stored in a private PostgreSQL database on a privately owned
server located in France (no cloud hosting provider), with access limited to
the bot process and its operator. The web dashboard runs on a second privately
owned machine on the same private network; it has no database of its own, does
not keep your Discord login token (it is used once to sign you in, then
discarded), and keeps your session only in a signed cookie in your browser. The
database itself is not encrypted at rest (the server's disk is not encrypted);
database backups are encrypted (GPG), and OAuth tokens are additionally
encrypted at the application level. Backups are kept for disaster recovery and are subject to
the same deletion schedule on restore.

## Why we use it (legal bases)

- **To provide the features you or your server ask for** (commands, music,
  reminders, leveling, profiles, tickets, premium perks you bought): necessary
  to provide the service described in our Terms of Service.
- **Server configuration and moderation records**: the legitimate interest of
  each server's managers in running and moderating their community, and ours
  in operating the bot.
- **Opt-in features** (presence / "recently played", AniList and MangaDex
  alerts): your consent, which you can withdraw at any time with the commands
  listed below; withdrawing does not affect what was done before.
- **Security and abuse prevention** (rate limits, anti-abuse checks, technical
  logs without message text): our legitimate interest in keeping the service
  safe and available.

## Who receives data

We do not sell data. Apart from the operator, data only reaches:

- **Discord** (Discord Inc., United States), which runs the platform the bot
  lives on: everything the bot shows you goes through Discord, and premium
  purchases are processed by Discord. Discord acts under its own privacy policy.
- **The external services you use through a feature**, listed in "What we do
  NOT do" above (AniList, MangaDex, Steam, osu!, Last.fm, Backloggd, top.gg,
  music sources): each receives only what that feature needs and acts under its
  own privacy policy.

Several of these services are based outside the European Union, mostly in the
United States. Data sent to them is transferred there under their own
safeguards (for example the EU-US Data Privacy Framework or standard
contractual clauses, depending on the service). The bot's own database stays
in France.

## Retention

- Server statistics aggregates: 90 days. On a server with Yasuho+, 365 days,
  and the 90-to-365-day part is still kept for up to 365 days after Yasuho+
  ends (shown again if it comes back, never shown while it is off). Nothing is
  kept past 365 days.
- Premium purchase records and premium gifts: kept while they are active, then
  deleted 400 days after they end (the statistics rule above needs to know when
  a server's Yasuho+ ended).
- Limit notes: 14 days.
- Presence aggregates: 30 days. A game you have not played for 30 days drops
  off your profile, and a whole aggregate nothing has added to for 30 days is
  emptied by the daily cleanup - so the 30 days is what we keep, not only what
  we show. Opting out deletes it immediately and entirely.
- Dashboard request logs (your own actions on the web dashboard): 30 days after
  completion.
- Dashboard change log (server configuration changes made on the web
  dashboard): 90 days, and purged in full when the bot is removed from the
  server.
- Anonymous command-usage aggregates: 400 days.
- Technical logs: the bot's logs and the dashboard's service logs, 30 days; the
  dashboard's web access logs (IP address, browser user-agent, requested page,
  date), 15 days. Logs never contain message text, and Discord IDs appear in
  them only to trace an error.
- Top.gg vote record: kept until you delete it. There is no automatic window,
  because the record IS the streak and the lifetime count it exists to show;
  ageing it out would quietly take a reward away. One row per voter, deleted in
  full by `?mydata deleteprofile`.
- Server-scoped data (configuration, moderation records, leveling) is purged on
  a retention schedule after the bot is removed from a server.

## Your rights

Under the GDPR you have the right to access your data, to have it corrected or
erased, to restrict or object to its use, to receive it in a portable format,
and to withdraw a consent you gave. Most of this is available directly through
the commands below; for anything else, write to azellaxmc@gmail.com. We answer
within one month. If you think your rights are not respected, you can lodge a
complaint with the French data protection authority, the CNIL
(https://www.cnil.fr).

- `?mydata export` - receive a complete machine-readable export of everything
  the bot holds about you (rate-limited to once per hour). Also available from
  the web dashboard. Records that involve someone else are limited to your own
  half of them: if you moderated a member, the export states which server, which
  case number, what action you took and when, never who the member was or what
  reason was written about them.
- `?mydata deleteprofile` - permanently delete your profile, gaming IDs, linked
  accounts, visibility choices, collected presence data, your top.gg vote
  record, your personal rank card, your AniList link (the stored OAuth token),
  your AniList / MangaDex alert opt-ins, your limit notes and our copy of your
  own premium purchases. A purchase that is still active on Discord is copied
  again at the next check, because the perk depends on it. A gift the bot owner
  gave you stays in the owner's record of gifts until it is deleted on the
  schedule above.
- `?mydata deleteavatars` - permanently delete your avatar history and disable
  future tracking.
- `/connections unlink` - unlink an external account (removes its data and
  visibility immediately).
- `?anilist logout` - unlink your AniList account on its own: the stored OAuth
  token is deleted immediately.
- `/profile presence gaming off` - stop and forget presence collection.

## Contact

Privacy questions or requests: **azellaxmc@gmail.com**. Do not post personal
data in a public issue. For anything else, open an issue at
https://github.com/yaniswav/Yasuho/issues or reach the owner through the bot's
support server listed on its Discord profile.

Changes to this policy will be published at this same address.
