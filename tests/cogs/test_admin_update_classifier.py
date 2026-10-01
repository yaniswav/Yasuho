"""``?update``'s changed-file classifier (lot S2-1 review fix).

``Admin.update`` (no argument) pulls, then splits the changed files between
"changed cogs" (hot-reloadable) and "restart needed" (core/tools .py changes
that reload_extension cannot reach). Before this fix the loop skipped every
non-``.py`` file outright (``if not f.endswith(".py"): continue``), so a
change to ``requirements.txt`` / ``requirements.lock`` (sonolink 1.2.1 ->
1.4.0, say) or ``run.sh`` never showed "restart needed" - an owner could
``?update`` and ``?reload`` cog code that imports a package version the
running process never actually installed.

Pure fakes: ``_git`` is monkeypatched directly (no real git/subprocess), and
the test only inspects the ``Restart needed`` embed field the command builds.
"""

from __future__ import annotations

import pytest

from cogs.system import admin as admin_cog


class _TypingCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.author = type("FakeAuthor", (), {"id": 1})()

    def typing(self):
        return _TypingCtx()

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return object()


def _cog(changed_files, extensions=()):
    """An Admin cog whose ``_git`` is scripted: one pull that moves HEAD, with
    ``changed_files`` as the (fake) diff between before/after."""
    cog = admin_cog.Admin.__new__(admin_cog.Admin)
    cog.bot = type(
        "FakeBot", (), {"extensions": {e: object() for e in extensions}}
    )()

    calls = []

    async def fake_git(*args):
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            # First call (before) returns "old", second (after) returns "new".
            return "old" if calls.count(("rev-parse", "HEAD")) == 1 else "new"
        if args == ("pull", "--ff-only"):
            return "Updating old..new\nFast-forward"
        if args[:2] == ("diff", "--name-only"):
            return "\n".join(changed_files)
        if args[:2] == ("log", "--oneline"):
            return "new1234 some commit"
        raise AssertionError(f"unexpected _git call: {args}")

    cog._git = fake_git
    return cog


def _restart_field(embed):
    for field in embed.fields:
        if "Restart needed" in field.name:
            return field.value
    return None


@pytest.mark.asyncio
async def test_requirements_txt_change_is_flagged_restart_needed():
    cog = _cog(["requirements.txt"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    (args, kwargs) = ctx.sent[0]
    embed = kwargs["embed"]
    value = _restart_field(embed)
    assert value is not None
    assert "requirements.txt" in value


@pytest.mark.asyncio
async def test_requirements_lock_change_is_flagged_restart_needed():
    cog = _cog(["requirements.lock"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    value = _restart_field(ctx.sent[0][1]["embed"])
    assert value is not None
    assert "requirements.lock" in value


@pytest.mark.asyncio
async def test_run_sh_change_is_flagged_restart_needed():
    cog = _cog(["run.sh"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    value = _restart_field(ctx.sent[0][1]["embed"])
    assert value is not None
    assert "run.sh" in value


@pytest.mark.asyncio
async def test_a_changed_cog_file_is_not_flagged_restart_needed():
    """Counter-test: an ordinary cog .py change is hot-reloadable, not a
    restart-needed line - this guard must not over-flag."""
    cog = _cog(["cogs/music/music.py"], extensions=["cogs.music.music"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    embed = ctx.sent[0][1]["embed"]
    assert _restart_field(embed) is None
    changed_cogs_field = next(
        f for f in embed.fields if "Changed cogs" in f.name
    )
    assert "cogs.music.music" in changed_cogs_field.value


@pytest.mark.asyncio
async def test_a_core_py_change_is_still_flagged_restart_needed():
    """Unchanged pre-existing behaviour: a core/tools .py file outside any
    known extension is restart-needed, same bucket as the new non-.py files."""
    cog = _cog(["tools/i18n.py"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    value = _restart_field(ctx.sent[0][1]["embed"])
    assert value is not None
    assert "tools/i18n.py" in value


@pytest.mark.asyncio
async def test_an_unrelated_non_py_file_is_not_flagged():
    """Counter-test: a non-.py file that is NOT one of the three tracked
    names (e.g. a locale catalogue) must not be swept into restart-needed -
    only the dependency/launcher set is."""
    cog = _cog(["locales/fr/LC_MESSAGES/bot.po"])
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    embed = ctx.sent[0][1]["embed"]
    assert _restart_field(embed) is None


@pytest.mark.asyncio
async def test_requirements_and_a_cog_change_both_land_in_their_own_bucket():
    cog = _cog(
        ["requirements.txt", "cogs/music/music.py"],
        extensions=["cogs.music.music"],
    )
    ctx = FakeCtx()

    await admin_cog.Admin.update.callback(cog, ctx)

    embed = ctx.sent[0][1]["embed"]
    restart_value = _restart_field(embed)
    assert restart_value is not None and "requirements.txt" in restart_value
    changed_cogs_field = next(
        f for f in embed.fields if "Changed cogs" in f.name
    )
    assert "cogs.music.music" in changed_cogs_field.value


# ---------------------------------------------------------------------------
# The classifier constant itself
# ---------------------------------------------------------------------------


def test_restart_required_non_py_files_is_exactly_the_three_tracked_names():
    assert admin_cog.RESTART_REQUIRED_NON_PY_FILES == {
        "requirements.txt",
        "requirements.lock",
        "run.sh",
    }
