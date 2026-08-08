"""Purpose: package entry point - exposes the tickets cog to core's extension
discovery (a package whose ``__init__`` defines ``setup`` is loaded whole).

THE support-ticket feature: a panel with one button, and a PRIVATE THREAD per
request. Deliberately one cog in one extension (``?reload config.tickets``
reloads the whole feature), because the pieces below are layers of a single
flow rather than separate features.

Layout - three leaves, then the flow, then the cog:

* guild_config.py - the six ``guild_settings`` keys, their coercers, their
                    bounds and their defaults (no discord, no SQL);
* preflight.py    - which permissions a panel channel needs, and how to name the
                    missing ones (no discord, no SQL);
* storage.py      - the only door to the ``tickets`` table: the guarded open,
                    the per-user open count, the read-back by thread (no
                    discord);
* open.py         - the persistent panel button, the subject modal, and the flow
                    that turns a click into a thread plus a row;
* panel.py        - the ``Tickets`` cog: the ``/ticket`` group and the status
                    card.

What is NOT stored: anything a member typed. The subject goes into the thread's
opening message and never into the database, and the table has no column that
could hold a transcript (schema.sql). Ticket CONTENT lives where its
participants can read it, and dies with the thread.

Lot T2 adds the in-thread controls (under the ``tk:`` DynamicItem namespace,
reserved in open.py) and the inactivity sweep; nothing here runs on a clock.

Hand-off, so the next lot does not have to rediscover either item:

* the sweep must close rows whose THREAD IS GONE, not only idle ones. The open
  flow can leave a committed 'open' row with no thread if the connection drops
  after the INSERT commits (see the ordering note in open.py), and that row holds
  a cap slot with nothing in T1 to release it;
* ``cogs/system/dashboard_sync.VALID_KINDS`` has no ``tickets`` kind. Correct for
  T1 - nothing writes these keys from the dashboard yet, and a kind with no
  writer is dead configuration - but it is what the dashboard contract will need
  the day the ticket settings become editable there.

Typography rule: ASCII '-' and '...' only.
"""

from .panel import Tickets

__all__ = ("Tickets", "setup")


async def setup(bot):
    await bot.add_cog(Tickets(bot))
