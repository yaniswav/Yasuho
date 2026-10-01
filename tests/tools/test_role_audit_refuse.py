"""``tools.role_audit.refuse_dangerous_role`` - the apply-time defence in depth.

``?roleaudit`` (tested in test_role_audit.py) catches a guild already
misconfigured; this helper is the second layer, consulted right before Yasuho
APPLIES one of the six stored roles to a member. It must stay pure-cheap (a
bitfield test, zero I/O) because it runs on every member join, and it must log
its refusal exactly once per (guild, role, surface) so a busy guild cannot
flood the log.
"""

import logging
import types

import discord

from tools import role_audit


def _role(role_id, permissions=None):
    return types.SimpleNamespace(
        id=role_id, permissions=permissions or discord.Permissions.none()
    )


def test_dangerous_role_refused_and_logs_once(caplog):
    role = _role(1, discord.Permissions(manage_guild=True))
    with caplog.at_level(logging.WARNING):
        first = role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_AUTOROLE, guild_id=42
        )
    assert first is True
    warnings = [r for r in caplog.records if "ROLE-REFUSED" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert role_audit.SURFACE_AUTOROLE in msg
    assert "42" in msg
    assert "1" in msg
    assert "manage_guild" in msg


def test_second_call_same_triple_does_not_log_again(caplog):
    role = _role(2, discord.Permissions(ban_members=True))
    role_audit.refuse_dangerous_role(
        role, surface=role_audit.SURFACE_MUTEROLE, guild_id=99
    )
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        second = role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_MUTEROLE, guild_id=99
        )
    assert second is True
    assert not [r for r in caplog.records if "ROLE-REFUSED" in r.getMessage()]


def test_different_surface_same_role_logs_again():
    """De-dupe keys on (guild, role, surface) - a new surface is a new key."""
    role = _role(3, discord.Permissions(kick_members=True))
    role_audit.refuse_dangerous_role(
        role, surface=role_audit.SURFACE_AUTOROLE, guild_id=7
    )
    logger = logging.getLogger(role_audit.__name__)
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    logger.addHandler(handler)
    try:
        logger.setLevel(logging.WARNING)
        role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_VERIFY, guild_id=7
        )
    finally:
        logger.removeHandler(handler)
    assert any("ROLE-REFUSED" in r.getMessage() for r in records)


def test_harmless_role_not_refused_and_does_not_log(caplog):
    role = _role(4, discord.Permissions.none())
    with caplog.at_level(logging.WARNING):
        refused = role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_LEVEL_REWARD, guild_id=5
        )
    assert refused is False
    assert not [r for r in caplog.records if "ROLE-REFUSED" in r.getMessage()]


def test_send_messages_only_is_not_dangerous(caplog):
    role = _role(5, discord.Permissions(send_messages=True))
    with caplog.at_level(logging.WARNING):
        refused = role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_TWITCH_LIVE, guild_id=6
        )
    assert refused is False


def test_non_int_permission_value_never_raises_and_is_not_dangerous(caplog):
    """A bare Mock (or any object whose ``.permissions.value`` is not
    int-like) must be treated as harmless, not crash the join/grant path."""
    role = types.SimpleNamespace(
        id=6, permissions=types.SimpleNamespace(value=object())
    )
    with caplog.at_level(logging.WARNING):
        refused = role_audit.refuse_dangerous_role(
            role, surface=role_audit.SURFACE_AUTOROLE, guild_id=7
        )
    assert refused is False
    assert not [r for r in caplog.records if "ROLE-REFUSED" in r.getMessage()]
