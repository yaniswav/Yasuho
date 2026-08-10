"""``AutoMod.on_message`` must not do permission work for a guild with automod off.

``on_message`` runs for EVERY message in EVERY guild, and the overwhelming
majority of guilds never turn a single automod feature on. It used to read
``Member.guild_permissions`` first - a property that folds the permission bits of
every one of the author's roles on each access - and only then look at whether
antilink / antispam / antiinvite were enabled at all. That fold was pure waste on
the busiest listener in the bot.

The order is now: enabled gate first (two in-process cache reads), permission
check second. These tests pin BOTH halves - that the fold is skipped when the
feature set is empty, and that it still happens (and still lets moderators
through) the moment anything is on - because the swap is only safe if the
outcome is unchanged.

No database, no Discord: the settings reads are stubbed and the author counts its
own ``guild_permissions`` accesses.
"""

import types

from cogs.moderation import automod


class _Author:
    """Message author that COUNTS how often its permissions are folded."""

    def __init__(self, *, manage_messages=False):
        self.bot = False
        self.id = 7
        self.mention = "<@7>"
        self.roles = []
        self._manage_messages = manage_messages
        self.permission_reads = 0

    @property
    def guild_permissions(self):
        self.permission_reads += 1
        return types.SimpleNamespace(manage_messages=self._manage_messages)


def _message(author, content="hello"):
    return types.SimpleNamespace(
        author=author,
        guild=types.SimpleNamespace(id=42),
        channel=types.SimpleNamespace(id=1, parent_id=None),
        content=content,
    )


def _cog(monkeypatch, *, antilink=False, antispam=False, antiinvite=False):
    """An AutoMod whose settings answer from memory, counting nothing else."""
    cog = automod.AutoMod.__new__(automod.AutoMod)
    cog.bot = types.SimpleNamespace(db_pool=object())
    cog._settings = automod._SettingsCache()
    cog._settings[42] = {"antilink": antilink, "antispam": antispam}
    cog._spam = {}

    async def _get_guild(_pool, _guild_id, key, default=None):
        assert key == "antiinvite"
        return antiinvite

    monkeypatch.setattr(automod.settings, "get_guild", _get_guild)
    return cog


# ---------------------------------------------------------------------------
# The regression: no feature on -> no permission fold.
# ---------------------------------------------------------------------------


async def test_message_in_a_guild_with_automod_off_never_folds_permissions(
    monkeypatch,
):
    cog = _cog(monkeypatch)
    author = _Author()

    await cog.on_message(_message(author))

    assert author.permission_reads == 0


async def test_a_bot_or_dm_message_still_leaves_before_anything(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)

    from_bot = _Author()
    from_bot.bot = True
    await cog.on_message(_message(from_bot))
    assert from_bot.permission_reads == 0

    in_dm = _Author()
    message = _message(in_dm)
    message.guild = None
    await cog.on_message(message)
    assert in_dm.permission_reads == 0


# ---------------------------------------------------------------------------
# ... and the outcome is unchanged once a feature IS on.
# ---------------------------------------------------------------------------


async def test_a_feature_being_on_does_check_permissions(monkeypatch):
    cog = _cog(monkeypatch, antilink=True)
    author = _Author()
    exempt_calls = []

    async def _is_exempt(_message):
        exempt_calls.append(_message)
        return True

    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author))

    assert author.permission_reads == 1
    # The gate passed, so the message went on to the exemption check.
    assert len(exempt_calls) == 1


async def test_a_moderator_is_still_never_auto_moderated(monkeypatch):
    """The bypass moved AFTER the gate; it must still bypass."""
    cog = _cog(monkeypatch, antilink=True)
    author = _Author(manage_messages=True)
    violations = []

    async def _handle_violation(*_args, **_kwargs):
        violations.append(_kwargs)

    async def _is_exempt(_message):  # pragma: no cover - must never be reached
        raise AssertionError("a manage_messages author must return before this")

    cog._handle_violation = _handle_violation
    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author, content="https://example.com"))

    assert author.permission_reads == 1
    assert violations == []


async def test_antiinvite_alone_is_enough_to_open_the_gate(monkeypatch):
    """The invite toggle lives in a DIFFERENT store than antilink/antispam."""
    cog = _cog(monkeypatch, antiinvite=True)
    author = _Author()

    async def _is_exempt(_message):
        return True

    cog._is_exempt = _is_exempt

    await cog.on_message(_message(author))

    assert author.permission_reads == 1
