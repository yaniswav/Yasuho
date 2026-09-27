"""Where the slash-tree auto-sync runs inside ``core.Yasuho.setup_hook``.

THE MAIN RISK, and why a policy test is not enough
--------------------------------------------------
``tools/tree_sync.py`` refuses to sync an incomplete tree, and
``tests/tools/test_tree_sync.py`` pins that refusal. But the refusal can only
fire if ``setup_hook`` actually TELLS it which extensions failed, and the hash
is only right if the payload is computed at a point where the tree is whole.
Both are wiring, not policy: a ``sync_if_changed(self)`` that forgot to pass the
failures, or a call moved above the extension loop, would leave every test in
that other file green while production got a bulk overwrite computed from half a
tree - which DELETES the missing cog's commands at Discord.

So these tests drive the REAL ``setup_hook`` and record the ORDER of the three
events that matter, plus the argument the sync is handed:

* every ``load_extension`` call,
* ``set_translator`` - the translator is part of the payload
  (``CommandTree.sync`` runs it to build ``description_localizations``), so a
  hash taken before it is blind to a translation-only change, which is exactly
  what broke ``/config`` on 2026-09-13,
* the auto-sync itself.

Only the boundaries are faked: the database, the HTTP session, the startup
backup and the config read that would reach for Lavalink. The loop, its
try/except, the translator install and the call site are the real ones, and the
last test runs the REAL sync policy behind a recording tree so the end-to-end
claim is asserted rather than inferred from two green halves.
"""

from __future__ import annotations

import logging
import os
import types

import core

from tools import tree_sync

# ---------------------------------------------------------------------------
# Boundaries.
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class StartupPool:
    """Answers the cold-start reads with nothing and records the writes."""

    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append(query)
        return "SELECT 0"

    async def fetch(self, query, *args):
        return []

    async def fetchrow(self, query, *args):
        return None

    async def fetchval(self, query, *args):
        return None

    def acquire(self):
        return _Ctx(self)

    def transaction(self):
        return _Ctx(self)


class _NoSession:
    """Stands in for the aiohttp session startup opens; nothing here uses it."""

    closed = True

    async def close(self):
        return None


def _lavalink_free_config():
    """The real config with its [Lavalink] section hidden.

    ``setup_hook`` connects to Lavalink when the section exists, and it does on
    this box. Hiding just that one section keeps every other import-time read
    honest while the test stays offline.
    """
    real_get = core.config_loader.get

    def get(section, key, *args, **kwargs):
        if section == "Lavalink":
            raise KeyError(section)
        return real_get(section, key, *args, **kwargs)

    return types.SimpleNamespace(get=get)


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


async def _run_setup_hook(monkeypatch, *, extensions, broken=(), on_sync=None):
    """Drive the real ``setup_hook``; return ``(journal, bot)``.

    Journal entries are ``("load", name)`` / ``("translator", None)`` /
    ``("sync", sorted_failed_extensions)``, in the order they happened.
    ``on_sync`` (if given) is awaited with the bot and the failure list, so a
    test can run the REAL policy from the REAL call site.
    """
    monkeypatch.setattr(core, "config_loader", _lavalink_free_config())
    monkeypatch.setattr(
        core, "fixups", types.SimpleNamespace(run_fixups=_async_return([]))
    )
    monkeypatch.setattr(
        core,
        "aiohttp",
        types.SimpleNamespace(ClientSession=lambda *a, **k: _NoSession()),
    )
    monkeypatch.setattr(core, "discover_extensions", lambda: list(extensions))

    bot = core.Yasuho(db_pool=StartupPool())
    bot._schedule_startup_backup = lambda: None

    journal = []

    async def fake_load_extension(name, *args, **kwargs):
        journal.append(("load", name))
        if name in broken:
            raise RuntimeError(f"{name} is broken")

    real_set_translator = bot.tree.set_translator

    async def recording_set_translator(translator):
        journal.append(("translator", None))
        return await real_set_translator(translator)

    async def fake_sync_if_changed(sync_bot, *, failed_extensions=()):
        failed = sorted(failed_extensions)
        journal.append(("sync", failed))
        assert sync_bot is bot
        if on_sync is not None:
            return await on_sync(sync_bot, failed)
        return tree_sync.SYNC

    bot.load_extension = fake_load_extension
    bot.tree.set_translator = recording_set_translator
    monkeypatch.setattr(core.tree_sync, "sync_if_changed", fake_sync_if_changed)

    await bot.setup_hook()
    return journal, bot


def _kinds(journal):
    return [kind for kind, _ in journal]


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_the_sync_runs_after_every_extension_and_after_the_translator(
    monkeypatch,
):
    """POSITIVE CONTROL for the silences below: the wiring does fire, once.

    The ORDER is the assertion. A sync computed before the last
    ``load_extension`` would be a payload missing commands; one computed before
    ``set_translator`` would be a payload missing every localisation.
    """
    journal, _ = await _run_setup_hook(
        monkeypatch, extensions=["cogs.a", "cogs.b", "cogs.c"]
    )

    kinds = _kinds(journal)
    assert kinds.count("sync") == 1
    assert kinds.count("translator") == 1
    last_load = max(i for i, kind in enumerate(kinds) if kind == "load")
    assert last_load < kinds.index("translator") < kinds.index("sync")
    assert [name for kind, name in journal if kind == "load"] == [
        "cogs.a",
        "cogs.b",
        "cogs.c",
    ]


async def test_a_clean_boot_reports_no_failed_extensions(monkeypatch):
    journal, _ = await _run_setup_hook(monkeypatch, extensions=["cogs.a", "cogs.b"])
    assert [failed for kind, failed in journal if kind == "sync"] == [[]]


async def test_a_cog_that_fails_to_load_is_named_to_the_sync(monkeypatch):
    """The refusal in tree_sync can only fire on what setup_hook hands it."""
    journal, _ = await _run_setup_hook(
        monkeypatch,
        extensions=["cogs.a", "cogs.broken", "cogs.c"],
        broken={"cogs.broken"},
    )
    assert [failed for kind, failed in journal if kind == "sync"] == [["cogs.broken"]]


async def test_a_failing_extension_still_lets_startup_finish(monkeypatch):
    """The try/except stays: one broken cog must not take the bot down."""
    journal, _ = await _run_setup_hook(
        monkeypatch,
        extensions=["cogs.a", "cogs.broken", "cogs.c"],
        broken={"cogs.broken"},
    )
    assert [name for kind, name in journal if kind == "load"] == [
        "cogs.a",
        "cogs.broken",
        "cogs.c",
    ]
    assert _kinds(journal).count("sync") == 1


async def test_the_translator_is_installed_before_the_payload_is_computed(
    monkeypatch,
):
    """Not just the order of calls: the tree really carries a translator by then."""
    seen = {}

    async def on_sync(bot, failed):
        seen["translator"] = bot.tree.translator
        return tree_sync.SYNC

    await _run_setup_hook(monkeypatch, extensions=["cogs.a"], on_sync=on_sync)
    assert seen["translator"] is not None
    assert type(seen["translator"]).__name__ == "YasuhoTranslator"


async def test_the_real_policy_refuses_the_partial_tree_setup_hook_reports(
    monkeypatch,
):
    """Both halves joined: setup_hook's list, run through the REAL decision.

    ``sync_if_changed`` is captured BEFORE it is monkeypatched, so the policy
    that runs here is the shipped one rather than the test's own stand-in, and
    the verdict is a COUNT of syncs on a recording tree.
    """
    from tests.tools.test_tree_sync import FakeBot, FakeTree, HashStore, _entry

    real_sync = tree_sync.sync_if_changed
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    probe = FakeBot(tree, store)

    async def on_sync(bot, failed):
        return await real_sync(probe, failed_extensions=failed)

    journal, _ = await _run_setup_hook(
        monkeypatch,
        extensions=["cogs.a", "cogs.broken"],
        broken={"cogs.broken"},
        on_sync=on_sync,
    )

    assert [failed for kind, failed in journal if kind == "sync"] == [["cogs.broken"]]
    assert len(tree.syncs) == 0, "a partial tree reached a real sync call"
    assert store.writes == 0


async def test_the_same_seam_DOES_sync_when_every_cog_loaded(monkeypatch):
    """The other side of the control: same path, nothing broken, one sync."""
    from tests.tools.test_tree_sync import FakeBot, FakeTree, HashStore, _entry

    real_sync = tree_sync.sync_if_changed
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    probe = FakeBot(tree, store)

    async def on_sync(bot, failed):
        return await real_sync(probe, failed_extensions=failed)

    await _run_setup_hook(
        monkeypatch, extensions=["cogs.a", "cogs.b"], on_sync=on_sync
    )

    assert len(tree.syncs) == 1
    assert store.writes == 1


# ---------------------------------------------------------------------------
# Discovery is upstream of the refusal, so a cog that HIDES defeats it.
#
# The refusal above can only fire on names the load loop was given, and the
# load loop is given exactly what ``discover_extensions()`` found. A cog file
# that cannot be READ used to answer "no setup here" and vanish: absent from
# the list, absent from `failed_extensions`, and therefore a tree the sync
# believes is COMPLETE - which bulk-overwrites Discord and deletes every
# command that cog owns. The two ways of being wrong are not symmetrical, so an
# IO fault now degrades towards the loud one.
# ---------------------------------------------------------------------------
def _fake_repo(tmp_path):
    """A miniature repo root: <root>/cogs/system/, the shape core.py walks."""
    cogs = tmp_path / "cogs"
    cogs.mkdir()
    (cogs / "__init__.py").write_text("", encoding="utf-8")
    category = cogs / "system"
    category.mkdir()
    (category / "__init__.py").write_text("", encoding="utf-8")
    return category


def test_an_unreadable_cog_file_cannot_hide_from_the_load_loop(
    tmp_path, monkeypatch, caplog
):
    """A file the walk can see but not open is offered anyway, and logged.

    The unreadable file is a DANGLING SYMLINK rather than a chmod: os.walk
    still lists it among the files, ``open()`` still raises ``OSError``, and
    unlike a permission bit that is true for root as well.
    """

    category = _fake_repo(tmp_path)
    (category / "real.py").write_text("def setup(bot):\n    pass\n", encoding="utf-8")
    (category / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    os.symlink(str(tmp_path / "does-not-exist"), str(category / "unreadable.py"))

    monkeypatch.setattr(core, "__file__", str(tmp_path / "core.py"))
    with caplog.at_level(logging.WARNING, logger="core"):
        found = core.discover_extensions()

    assert "cogs.system.unreadable" in found, "an unreadable cog vanished"
    # NEGATIVE CONTROL: a file that IS readable and has no setup still goes.
    assert "cogs.system.helpers" not in found
    assert "cogs.system.real" in found

    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "unreadable.py" in rec.getMessage()
    ]
    assert len(warnings) == 1, warnings


def test_an_unreadable_package_init_cannot_hide_a_whole_category(
    tmp_path, monkeypatch
):
    """Same fault one level up: the folder is claimed rather than skipped past.

    ``__init__.py`` is what decides "this package IS one extension" versus
    "descend into it". Unreadable, it must not silently become "descend", which
    would work right up until the folder really was a package and its commands
    were the ones deleted.
    """

    cogs = tmp_path / "cogs"
    cogs.mkdir()
    (cogs / "__init__.py").write_text("", encoding="utf-8")
    package = cogs / "anilist"
    package.mkdir()
    os.symlink(str(tmp_path / "does-not-exist"), str(package / "__init__.py"))
    (package / "feed.py").write_text("def setup(bot):\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(core, "__file__", str(tmp_path / "core.py"))
    found = core.discover_extensions()

    # Claimed as one extension, which will fail to load and stop the sync.
    assert found == ["cogs.anilist"]


async def test_the_unreadable_cog_is_what_makes_the_real_policy_refuse(monkeypatch):
    """Join the two halves: a name that will not load means no sync at all.

    ``discover_extensions`` hands the hidden name over (above); ``setup_hook``
    cannot load it, so it lands in ``failed_extensions``; the REAL policy then
    refuses on a recording tree. That is the chain the silent deletion needed
    broken at every link.
    """

    from tests.tools.test_tree_sync import FakeBot, FakeTree, HashStore, _entry

    real_sync = tree_sync.sync_if_changed
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    probe = FakeBot(tree, store)

    async def on_sync(bot, failed):
        return await real_sync(probe, failed_extensions=failed)

    journal, _ = await _run_setup_hook(
        monkeypatch,
        extensions=["cogs.system.real", "cogs.system.unreadable"],
        broken={"cogs.system.unreadable"},
        on_sync=on_sync,
    )

    assert [failed for kind, failed in journal if kind == "sync"] == [
        ["cogs.system.unreadable"]
    ]
    assert len(tree.syncs) == 0
    assert store.writes == 0


def test_a_readable_file_answers_on_its_contents_and_not_on_the_fallback(tmp_path):
    """The fallback must not swallow the ordinary case: it is IO faults only."""

    plain = tmp_path / "plain.py"
    plain.write_text("VALUE = 1\n", encoding="utf-8")
    entry = tmp_path / "entry.py"
    entry.write_text("async def " + "setup(bot):\n    pass\n", encoding="utf-8")

    assert core._module_has_setup(str(plain)) is False
    assert core._module_has_setup(str(entry)) is True
    # A DIRECTORY is the other OSError shape (IsADirectoryError): unreadable,
    # so it answers with the fallback rather than with False.
    assert core._module_has_setup(str(tmp_path)) is True
