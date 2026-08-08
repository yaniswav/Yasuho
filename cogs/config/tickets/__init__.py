"""Purpose: package entry point - exposes the tickets cog to core's extension
discovery (a package whose ``__init__`` defines ``setup`` is loaded whole).

THE support-ticket feature: a panel with one button, a PRIVATE THREAD per
request, and the two controls that end it. ONE extension (``?reload
config.tickets`` reloads the whole feature), because the pieces below are layers
of a single flow rather than separate features. It carries two cogs, split along
the only line that matters here: ``Tickets`` is what a server ADMIN configures
and costs nothing until somebody clicks, while ``TicketLifecycle`` is what
LISTENS and what runs on a clock. Keeping the clock out of the configuration cog
is what lets the panel side stay a zero-cost feature.

Layout - four leaves, then the flows, then the cogs:

* guild_config.py - the six ``guild_settings`` keys, their coercers, their
                    bounds and their defaults (no discord, no SQL);
* preflight.py    - which permissions a panel channel needs, and how to name the
                    missing ones (no discord, no SQL);
* transcripts.py  - a closing ticket's messages rendered to a plain-text file in
                    memory (no SQL, no settings, no i18n - see below);
* storage.py      - the only door to the ``tickets`` table: the guarded open, the
                    per-user open count, the read-back by thread, the atomic
                    claim, the exactly-once close, the sweep window (no discord);
* open.py         - the persistent panel button, the subject modal, and the flow
                    that turns a click into a thread plus a row;
* lifecycle.py    - the ``tk:`` in-thread controls, the three ways a ticket ends,
                    and the hourly backstop sweep (the ``TicketLifecycle`` cog);
* panel.py        - the ``Tickets`` cog: the ``/ticket`` group and the status
                    card.

What the BOT never stores: anything a member typed. The subject goes into the
thread's opening message and never into the database, and the table has no
column that could hold a transcript (schema.sql). A CLOSE may render that
content once, into memory, and upload it to the guild's own log channel if it
configured one - transcripts.py has no pool and writes no file, so the guarantee
is structural rather than a promise. Note what that upload means, because it is
the one place ticket content outlives its thread: the file then lives in that
SERVER's channel, under that server's control. PRIVACY.md says so, and both
configuration surfaces name the transcript and warn when the chosen log channel
is one everybody can read.

Both of T1's hand-offs are answered in lifecycle.py: the sweep closes rows whose
THREAD IS GONE (a cache miss past the guild's window, which covers the committed
'open' row the open flow can leave behind), and the auto-archive listener turns
Discord's own archive into the inactivity signal, so no ticket needs a timer -
the guild's ``tickets_inactivity_hours`` IS the thread's auto-archive duration.

The dashboard side is wired: ``cogs/system/dashboard_sync.VALID_KINDS`` carries
the guild-scoped ``tickets_config`` kind, which evicts this guild's settings
blob and nothing else (there is no derived tickets cache). The contract the
other team writes against is ``.claude/plans/dashboard-tickets-contract.md``.

Typography rule: ASCII '-' and '...' only.
"""

from .lifecycle import TicketLifecycle
from .panel import Tickets

__all__ = ("TicketLifecycle", "Tickets", "setup")


async def setup(bot):
    await bot.add_cog(Tickets(bot))
    await bot.add_cog(TicketLifecycle(bot))
