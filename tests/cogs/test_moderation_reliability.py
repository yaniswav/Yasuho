import datetime
import types

from cogs.moderation import moderation


class _Guild:
    def __init__(self):
        self.id = 10
        self.bans = []
        self.unbans = []

    async def ban(self, member, *, reason=None):
        self.bans.append((member.id, reason))

    async def unban(self, member, *, reason=None):
        self.unbans.append((member.id, reason))


class _Reminder:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def create_timer(self, when, event, **extra):
        self.calls.append((when, event, extra))
        if self.error:
            raise self.error
        return {"id": 1}


class _Bot:
    def __init__(self, pool, reminder):
        self.db_pool = pool
        self.reminder = reminder

    def get_cog(self, name):
        if name == "Reminder":
            return self.reminder
        return None


class _Context:
    def __init__(self, guild):
        self.guild = guild
        self.author = types.SimpleNamespace(id=99, mention="<@99>")
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _member():
    return types.SimpleNamespace(
        id=20,
        mention="<@20>",
        display_avatar=types.SimpleNamespace(url="https://example.test/avatar"),
    )


def _duration():
    return types.SimpleNamespace(
        dt=datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1)
    )


async def test_tempban_preflights_scheduler_before_ban(fake_pool, monkeypatch):
    monkeypatch.setattr(moderation.modchecks, "hierarchy_error", lambda *_: None)
    guild = _Guild()
    ctx = _Context(guild)
    cog = moderation.Moderation(_Bot(fake_pool, None))

    await moderation.Moderation.tempban.callback(
        cog, ctx, _member(), _duration(), reason="reason"
    )

    assert guild.bans == []
    assert "Scheduling is unavailable" in ctx.sent[0][0][0]


async def test_tempban_rolls_back_when_timer_insert_fails(fake_pool, monkeypatch):
    monkeypatch.setattr(moderation.modchecks, "hierarchy_error", lambda *_: None)
    guild = _Guild()
    reminder = _Reminder(RuntimeError("database unavailable"))
    ctx = _Context(guild)
    cog = moderation.Moderation(_Bot(fake_pool, reminder))

    await moderation.Moderation.tempban.callback(
        cog, ctx, _member(), _duration(), reason="reason"
    )

    assert guild.bans == [(20, "reason")]
    assert guild.unbans and guild.unbans[0][0] == 20
    assert "rolled it back" in ctx.sent[-1][0][0]


async def test_tempban_persists_timer_before_reporting_success(
    fake_pool, monkeypatch
):
    monkeypatch.setattr(moderation.modchecks, "hierarchy_error", lambda *_: None)
    fake_pool.fetchrow_return = {"case_number": 4}
    guild = _Guild()
    reminder = _Reminder()
    ctx = _Context(guild)
    cog = moderation.Moderation(_Bot(fake_pool, reminder))
    duration = _duration()

    await moderation.Moderation.tempban.callback(
        cog, ctx, _member(), duration, reason="reason"
    )

    assert guild.bans == [(20, "reason")]
    assert guild.unbans == []
    assert reminder.calls == [
        (
            duration.dt,
            "tempban",
            {"guild_id": 10, "user_id": 20},
        )
    ]
    assert "Banned" in ctx.sent[-1][0][0]
