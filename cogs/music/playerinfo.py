"""Reading a sonolink ``Player``'s guild - one answer, in one place.

sonolink makes ``Player.guild`` a PROPERTY that raises ``RuntimeError`` - not
``AttributeError`` - while the player has no guild attached yet (verified in the
installed ``sonolink/gateway/player/_base.py``). Two consequences that every
caller in this package has to live with:

* ``getattr(player, "guild", None)`` does NOT protect anybody. ``getattr`` only
  swallows ``AttributeError``, so the RuntimeError comes straight back out of
  it. A line that looks defensive is not.
* The guild is reachable the whole time anyway. The voice channel and the home
  text channel both carry it, and neither of them raises.

This module exists because that fact was rediscovered the hard way. Five
separate "the player's guild id" helpers had grown across the package - in
``effects``, ``lyrics``, ``sponsorblock``, ``voteskip`` and ``views`` - and they
disagreed: two wrapped the property in ``try``/``except``, two read it
unprotected, one derived it from the channels. The failure the unprotected shape
produces is silent (an exception inside a listener or a per-guild lock, and the
room simply never gets its panel), so the copies that were wrong stayed wrong.
One helper, one shape, and a "make these consistent" pass can no longer
reintroduce the raise.

A second sonolink pitfall, unrelated but equally silent: ``Playable`` (in
``sonolink/models/track.py``) defines ``__len__`` - the track length in
milliseconds - and no ``__bool__``. Python falls back from ``__bool__`` to
``__len__``, so ``bool(player.current)`` is really ``player.current.length
!= 0``: a genuinely playing track whose length is 0 (a stream mid-probe, or
the partially built track from the cold-restore race - see views.py's
"length-less tracks") is FALSY. Code that asks "is something playing?" with
``if player.current:`` or ``if not player.current:`` gets that case wrong.
Always compare to ``None`` instead: ``player.current is None`` /
``player.current is not None``.
"""

from __future__ import annotations

import typing

__all__ = ["guild_id_of"]


def guild_id_of(player: typing.Any) -> typing.Optional[int]:
    """The id of the guild a ``player`` is playing in, or ``None``.

    Derivation order, and each step is deliberate:

    1. the VOICE CHANNEL's guild. A connected player always has one, it is the
       guild the player is playing in by definition, and reading it cannot
       raise.
    2. the HOME text channel's guild. Covers a player that is mid-move or
       already disconnected, where ``channel`` can be ``None`` while the panel
       and the bookkeeping still need to know which guild this was.
    3. ``player.guild`` itself, and ONLY as a last resort, inside a bare
       ``except``. This is the read that can raise ``RuntimeError`` on an
       unattached player (see the module docstring), so it is both last and
       guarded: a caller must never have to know that.

    Never raises, and returns ``None`` only when no route answered - which the
    callers all treat as "skip the guild-keyed part of this work", never as a
    failure.
    """
    guild = getattr(getattr(player, "channel", None), "guild", None)
    if guild is None:
        guild = getattr(getattr(player, "home", None), "guild", None)
    if guild is None:
        try:
            guild = player.guild
        except Exception:
            return None
    return getattr(guild, "id", None)
