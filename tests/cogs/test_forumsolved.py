"""V5e-B: forum "solved" - permissions, tag rules, archived-thread edit order.

Typography rule: ASCII '-' and '...' only.
"""

import asyncio
import types

import discord

from tools import forumsolved


def tag(tid, name, moderated=False):
    return types.SimpleNamespace(id=tid, name=name, moderated=moderated)


# -- pure rules ---------------------------------------------------------------
def test_pick_tag_prefers_the_configured_one_then_autodetects_case_insensitively():
    tags = [tag(1, "Help"), tag(2, "SOLVED"), tag(3, "Done")]
    assert forumsolved.pick_tag(tags, 3).id == 3
    assert forumsolved.pick_tag(tags, None).id == 2
    assert forumsolved.pick_tag(tags, 99).id == 2  # configured tag was deleted
    assert forumsolved.pick_tag([tag(4, " Resolved ")], None).id == 4
    assert forumsolved.pick_tag([tag(1, "Help")], None) is None


def test_plan_edit_applies_the_tag_prefix_and_close():
    kwargs, skipped = forumsolved.plan_edit("Help me", [tag(1, "a")], tag(2, "Solved"), True, True, False)
    assert [t.id for t in kwargs["applied_tags"]] == [1, 2]
    assert kwargs["name"] == "[Solved] Help me"
    assert kwargs["archived"] is True and kwargs["locked"] is True and not skipped


def test_plan_edit_respects_discord_limits():
    full = [tag(i, "t%d" % i) for i in range(10, 15)]
    kwargs, skipped = forumsolved.plan_edit("x", full, tag(2, "Solved"), False, False, True)
    assert "applied_tags" not in kwargs and skipped  # 5 tags max
    kwargs, skipped = forumsolved.plan_edit("x", [], tag(2, "Solved", moderated=True), False, False, False)
    assert "applied_tags" not in kwargs and skipped  # a moderated tag needs Manage Threads
    kwargs, _skipped = forumsolved.plan_edit("x", [], tag(2, "Solved", moderated=True), False, False, True)
    assert "applied_tags" in kwargs
    kwargs, _skipped = forumsolved.plan_edit("n" * 100, [], None, True, False, False)
    assert len(kwargs["name"]) == 100 and kwargs["name"].startswith("[Solved] ")
    kwargs, _skipped = forumsolved.plan_edit("[solved] done", [], None, True, False, False)
    assert "name" not in kwargs  # idempotent


# -- the command ---------------------------------------------------------------
class FakeForum(discord.ForumChannel):
    def __init__(self, tags):
        self._tags = tags
        self.id = 50

    @property
    def available_tags(self):
        return self._tags


class FakeThread(discord.Thread):
    def __init__(self, forum, owner_id=7, archived=False, name="Question", applied=(), bot_manage=True, manage=(), locked=False, slow_rename=False):
        self._locked = locked
        self._slow_rename = slow_rename
        self._forum = forum
        self.owner_id = owner_id
        self._archived = archived
        self._name = name
        self._applied = list(applied)
        self._bot_manage = bot_manage
        self._manage = set(manage)
        self.calls = []
        self.order = None

    @property
    def parent(self):
        return self._forum

    @property
    def locked(self):
        return self._locked

    @property
    def archived(self):
        return self._archived

    @property
    def name(self):
        return self._name

    @property
    def applied_tags(self):
        return self._applied

    def permissions_for(self, member, /):
        if member == "me":
            return types.SimpleNamespace(manage_threads=self._bot_manage)
        return types.SimpleNamespace(manage_threads=member.id in self._manage)

    async def edit(self, **kwargs):
        if self._slow_rename and "name" in kwargs:
            await asyncio.sleep(3600)
        self.calls.append(kwargs)
        self.order.append(("edit", kwargs))


class FakeCtx:
    def __init__(self, thread, author_id, order):
        self.channel = thread
        self.guild = types.SimpleNamespace(id=1, me="me")
        self.author = types.SimpleNamespace(id=author_id, mention="<@%d>" % author_id)
        self.order = order
        thread.order = order

    async def defer(self):
        self.order.append(("defer", None))

    async def send(self, text=None, **kwargs):
        self.order.append(("send", text))


def build(monkeypatch, config=None):
    async def get_guild(pool, gid, key, default=None):
        return config

    monkeypatch.setattr(forumsolved.settings, "get_guild", get_guild)
    return types.SimpleNamespace(db_pool=None)


async def run(cog, ctx):
    await forumsolved.close_post(cog, ctx)


async def test_a_stranger_cannot_close_a_post_but_the_author_and_a_moderator_can(monkeypatch):
    cog = build(monkeypatch)
    thread = FakeThread(FakeForum([tag(2, "Solved")]), manage={9})
    stranger = FakeCtx(thread, 8, [])
    await run(cog, stranger)
    assert thread.calls == [] and stranger.order[0][0] == "send"
    for uid in (7, 9):
        thread = FakeThread(FakeForum([tag(2, "Solved")]), manage={9})
        ctx = FakeCtx(thread, uid, [])
        await run(cog, ctx)
        assert len(thread.calls) == 1 and thread.calls[0]["archived"] is True


async def test_the_reply_precedes_the_archive_and_an_archived_post_is_unarchived_first(monkeypatch):
    cog = build(monkeypatch)
    thread = FakeThread(FakeForum([tag(2, "Solved")]), archived=True)
    ctx = FakeCtx(thread, 7, [])
    await run(cog, ctx)
    kinds = [k for k, _v in ctx.order]
    assert kinds == ["defer", "send", "edit", "edit"]
    assert thread.calls[0] == {"archived": False, "reason": "Forum post marked solved"}
    assert thread.calls[1]["archived"] is True and "applied_tags" in thread.calls[1]


async def test_it_only_works_in_a_forum_post_and_needs_the_bot_permission(monkeypatch):
    cog = build(monkeypatch)
    ctx = FakeCtx(types.SimpleNamespace(), 7, [])
    await run(cog, ctx)
    assert ctx.order[0][0] == "send"
    thread = FakeThread(FakeForum([]), bot_manage=False)
    ctx = FakeCtx(thread, 7, [])
    await run(cog, ctx)
    assert thread.calls == []


async def test_a_moderator_lock_is_kept_even_when_the_server_does_not_lock(monkeypatch):
    cog = build(monkeypatch, {"lock": False})
    thread = FakeThread(FakeForum([tag(2, "Solved")]), locked=True)
    await run(cog, FakeCtx(thread, 7, []))
    assert thread.calls[-1]["locked"] is True


async def test_the_rename_is_its_own_edit_before_the_archive_and_never_stalls_the_close(monkeypatch):
    monkeypatch.setattr(forumsolved, "RENAME_TIMEOUT", 0.05)
    cog = build(monkeypatch, {"prefix": True})
    thread = FakeThread(FakeForum([tag(2, "Solved")]))
    await run(cog, FakeCtx(thread, 7, []))
    assert thread.calls[0]["name"] == "[Solved] Question" and "archived" not in thread.calls[0]
    assert thread.calls[1]["archived"] is True and "name" not in thread.calls[1]

    slow = FakeThread(FakeForum([tag(2, "Solved")]), slow_rename=True)
    await run(cog, FakeCtx(slow, 7, []))
    assert len(slow.calls) == 1 and slow.calls[0]["archived"] is True  # rename skipped, close done


def test_the_ticket_group_no_longer_blocks_solved_on_the_prefix_path():
    from cogs.config.tickets.panel import Tickets

    def names(command):
        return {getattr(c, "__qualname__", "") for c in command.checks}

    # discord.py runs a group's checks for its subcommands on the PREFIX path, so
    # the group itself must carry no permission check ...
    assert not any("has_permissions" in n for n in names(Tickets.ticket))
    # ... and every configuring subcommand keeps its own.
    for name in ("setup", "status", "config", "disable", "forum"):
        sub = Tickets.ticket.get_command(name)
        assert any("has_permissions" in n for n in names(sub)), name
    assert not any("has_permissions" in n for n in names(Tickets.ticket.get_command("solved")))
