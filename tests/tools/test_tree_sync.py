"""The hash-gated slash-tree self-sync (``tools/tree_sync.py``).

What these tests are defending
------------------------------
The bot used to never sync its own command tree; ``y!sync`` was a manual step
after every deploy, and twice in five weeks it was forgotten:

* ``/trending`` had become ``/anilist trending``. Discord kept advertising the
  old name -> ``CommandNotFound``.
* a commit translated command descriptions -> ``/config`` came back as
  ``CommandSignatureMismatch``, twice.

So the two properties that matter pull against each other, and both are pinned
here:

1. **Nothing may hide from the hash.** A description-only change must flip it,
   and so must a LOCALISATION-only change - the second incident happened with
   every English string untouched. Several tests therefore run against a REAL
   ``discord.app_commands`` tree with a REAL translator installed, not against
   hand-written dicts, because the thing being asserted is that we hash what
   discord.py would actually POST.
2. **Noise may not move it.** The order commands were registered in, and the
   order discord.py happened to build a dict in, are not differences Discord is
   told about; if they moved the hash the bot would sync on every boot and
   spend a rate-limited budget on nothing.

And the safety property that outranks both: a global sync is a bulk OVERWRITE,
so an INCOMPLETE tree must never be synced. That test's success is a SILENCE
(no sync happened), so it is paired with a positive control on the same
recording seam - :func:`test_a_changed_tree_is_synced_exactly_once` - which
proves the seam can see a sync at all, and both assert a COUNT rather than a
bare "not called".

Everything is offline: the tree, the HTTP sync and the table are in-memory
stand-ins.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tools import tree_sync

# ---------------------------------------------------------------------------
# In-memory boundaries.
# ---------------------------------------------------------------------------


class FakeCommand:
    """Stand-in for an app command: only the two payload hooks matter.

    ``to_dict`` and ``get_translated_payload`` are the exact pair
    ``CommandTree.sync`` picks between (tree.py:1109-1112), so a fake that
    implements both is enough for :func:`tools.tree_sync.build_payload` to run
    unmodified against it. Each call returns a deep copy, like the real ones do
    by construction, so a caller mutating the payload cannot corrupt the source.
    """

    def __init__(self, data, translated=None):
        self._data = data
        self._translated = translated

    def to_dict(self, tree):
        return copy.deepcopy(self._data)

    async def get_translated_payload(self, tree, translator):
        source = self._data if self._translated is None else self._translated
        return copy.deepcopy(source)


class FakeTree:
    """Records every ``sync()`` call; can be told to fail or to hang."""

    def __init__(self, entries, *, translator=None, translated=None):
        translated = translated or {}
        self._commands = [
            FakeCommand(entry, translated.get(entry.get("name")))
            for entry in entries
        ]
        self.translator = translator
        self.syncs = []
        self.sync_error = None
        self.sync_sleep = 0.0

    def _get_all_commands(self, *, guild=None):
        return list(self._commands)

    async def sync(self, *, guild=None):
        self.syncs.append(guild)
        if self.sync_sleep:
            await asyncio.sleep(self.sync_sleep)
        if self.sync_error is not None:
            raise self.sync_error
        return list(self._commands)


class HashStore:
    """In-memory stand-in for the ``app_command_sync`` table.

    Stores real rows rather than replaying a canned answer, so a test can pin
    WHAT was written and not merely that something was. ``writes`` is the count
    the silence-guards assert on.
    """

    def __init__(self):
        self.rows = {}
        self.writes = 0
        self.write_error = None
        self.read_error = None

    async def fetchrow(self, query, *args):
        assert "app_command_sync" in query, query
        if self.read_error is not None:
            raise self.read_error
        row = self.rows.get(args[0])
        return None if row is None else {"payload_hash": row[0]}

    async def execute(self, query, *args):
        assert "app_command_sync" in query, query
        if self.write_error is not None:
            raise self.write_error
        self.writes += 1
        self.rows[args[0]] = (args[1], args[2])
        return "INSERT 0 1"


class FakeBot:
    def __init__(self, tree, pool, application_id=4242):
        self.tree = tree
        self.db_pool = pool
        self.application_id = application_id


def _entry(name, description, *, options=None, type_=1):
    """A minimal command payload in the shape discord.py's ``to_dict`` returns."""
    return {
        "name": name,
        "description": description,
        "type": type_,
        "options": list(options or []),
        "nsfw": False,
        "dm_permission": True,
        "default_member_permissions": None,
    }


# ---------------------------------------------------------------------------
# Canonicalisation: what must move the hash, and what must not.
# ---------------------------------------------------------------------------


def test_canonical_text_is_valid_json_holding_every_command():
    """Guard the guard: the canonical form must not silently drop entries."""
    payload = [_entry("config", "Configure me."), _entry("ban", "Ban someone.")]
    parsed = json.loads(tree_sync.canonicalise(payload))
    assert sorted(entry["name"] for entry in parsed) == ["ban", "config"]


def test_a_description_change_flips_the_hash():
    """THE /config INCIDENT, at its smallest: only the description moved."""
    before = tree_sync.payload_hash([_entry("config", "Configure the server.")])
    after = tree_sync.payload_hash([_entry("config", "Configure this server.")])
    assert before != after


def test_reordering_the_commands_does_not_flip_the_hash():
    """Registration order is an artifact of extension load order, not a change."""
    a, b = _entry("alpha", "First."), _entry("beta", "Second.")
    assert tree_sync.payload_hash([a, b]) == tree_sync.payload_hash([b, a])


def test_reordering_dict_keys_does_not_flip_the_hash():
    """Key insertion order is a Python detail; Discord never sees it."""
    straight = {"name": "ping", "description": "Pong.", "type": 1, "options": []}
    shuffled = {}
    for key in ("options", "type", "description", "name"):
        shuffled[key] = straight[key]
    assert list(straight) != list(shuffled)
    assert tree_sync.payload_hash([straight]) == tree_sync.payload_hash([shuffled])


def test_nested_key_order_deep_in_the_payload_does_not_flip_the_hash():
    """sort_keys has to reach all the way down, not just the top dict."""
    deep_a = _entry(
        "config", "Configure.", options=[{"name": "key", "type": 3, "required": True}]
    )
    deep_b = _entry(
        "config", "Configure.", options=[{"required": True, "type": 3, "name": "key"}]
    )
    assert tree_sync.payload_hash([deep_a]) == tree_sync.payload_hash([deep_b])


def test_reordering_parameters_DOES_flip_the_hash():
    """The deliberate asymmetry: option order is semantic, so it is a change.

    Parameter order decides what Discord shows first and constrains which
    options may be required, so unlike top-level registration order it is a real
    difference. Pinned so the choice stays a decision rather than an accident.
    """
    one = {"name": "a", "type": 3}
    two = {"name": "b", "type": 3}
    assert tree_sync.payload_hash(
        [_entry("config", "Configure.", options=[one, two])]
    ) != tree_sync.payload_hash(
        [_entry("config", "Configure.", options=[two, one])]
    )


def test_a_value_that_will_not_serialise_raises_rather_than_hashing_around_it():
    """A set has no order, so it must never reach a hash - it must explode."""
    with pytest.raises(TypeError):
        tree_sync.payload_hash([{"name": "x", "type": 1, "weird": {1, 2}}])


# ---------------------------------------------------------------------------
# The payload really is discord.py's own (real tree, real translator).
# ---------------------------------------------------------------------------


class FixedTranslator(app_commands.Translator):
    """Translates descriptions to a fixed string, names never (Discord's rule)."""

    _NAME_LOCATIONS = {
        app_commands.TranslationContextLocation.command_name,
        app_commands.TranslationContextLocation.group_name,
        app_commands.TranslationContextLocation.parameter_name,
    }

    def __init__(self, mapping):
        super().__init__()
        self._mapping = mapping

    async def translate(self, string, locale, context):
        if context.location in self._NAME_LOCATIONS:
            return None
        return self._mapping.get(string.message)


def _make_bot():
    return commands.Bot(
        command_prefix="?", intents=discord.Intents.none(), help_command=None
    )


async def _real_tree_hash(specs, *, translator=None):
    """Hash a REAL ``app_commands`` tree built from ``(name, description)`` pairs."""
    bot = _make_bot()
    for name, description in specs:

        async def callback(interaction):  # pragma: no cover - never invoked
            return None

        bot.tree.add_command(
            app_commands.Command(
                name=name, description=description, callback=callback
            )
        )
    if translator is not None:
        await bot.tree.set_translator(translator)
    return tree_sync.payload_hash(await tree_sync.build_payload(bot.tree))


async def test_real_tree_description_change_flips_the_hash():
    before = await _real_tree_hash([("config", "Configure the server.")])
    after = await _real_tree_hash([("config", "Configure this server.")])
    assert before != after


async def test_real_tree_registration_order_does_not_flip_the_hash():
    forwards = await _real_tree_hash([("alpha", "One."), ("beta", "Two.")])
    backwards = await _real_tree_hash([("beta", "Two."), ("alpha", "One.")])
    assert forwards == backwards


async def test_installing_a_translator_flips_the_hash():
    """The localisations ARE the payload - tree.py:1110 vs 1112.

    A hash taken from ``to_dict`` alone would be identical on both sides of this
    assertion, which is exactly how a translation-only commit reached production
    unsynced and broke /config.
    """
    plain = await _real_tree_hash([("config", "Configure the server.")])
    localised = await _real_tree_hash(
        [("config", "Configure the server.")],
        translator=FixedTranslator({"Configure the server.": "Configure le serveur."}),
    )
    assert plain != localised


async def test_changing_only_a_translation_flips_the_hash():
    """THE 2026-09-13 INCIDENT: every English string identical, payload different."""
    specs = [("config", "Configure the server.")]
    first = await _real_tree_hash(
        specs,
        translator=FixedTranslator({"Configure the server.": "Configure le serveur."}),
    )
    second = await _real_tree_hash(
        specs,
        translator=FixedTranslator({"Configure the server.": "Parametre le serveur."}),
    )
    assert first != second


async def test_the_same_tree_hashes_the_same_twice():
    """Two boots of unchanged code must agree, or the gate is worthless."""
    translator = FixedTranslator({"Configure the server.": "Configure le serveur."})
    specs = [("config", "Configure the server."), ("ping", "Pong.")]
    assert await _real_tree_hash(specs, translator=translator) == await _real_tree_hash(
        specs, translator=FixedTranslator({"Configure the server.": "Configure le serveur."})
    )


async def test_build_payload_takes_the_translator_branch_when_one_is_set():
    """Pin the branch itself, not only its effect on the digest."""
    bot = _make_bot()

    async def callback(interaction):  # pragma: no cover - never invoked
        return None

    bot.tree.add_command(
        app_commands.Command(
            name="config", description="Configure the server.", callback=callback
        )
    )
    await bot.tree.set_translator(
        FixedTranslator({"Configure the server.": "Configure le serveur."})
    )
    payload = await tree_sync.build_payload(bot.tree)
    assert payload[0]["description_localizations"]["fr"] == "Configure le serveur."


# ---------------------------------------------------------------------------
# decide(): the pure policy.
# ---------------------------------------------------------------------------


def test_decide_syncs_when_there_is_no_stored_hash():
    assert (
        tree_sync.decide(
            new_hash="a", stored_hash=None, command_count=3, failed_extensions=[]
        )
        == tree_sync.SYNC
    )


def test_decide_syncs_when_the_hash_moved():
    assert (
        tree_sync.decide(
            new_hash="b", stored_hash="a", command_count=3, failed_extensions=[]
        )
        == tree_sync.SYNC
    )


def test_decide_skips_when_the_hash_matches():
    assert (
        tree_sync.decide(
            new_hash="a", stored_hash="a", command_count=3, failed_extensions=[]
        )
        == tree_sync.SKIP_UNCHANGED
    )


def test_decide_refuses_a_partial_tree_even_though_the_hash_moved():
    """Completeness is checked BEFORE the hash, and outranks it."""
    assert (
        tree_sync.decide(
            new_hash="b",
            stored_hash="a",
            command_count=70,
            failed_extensions=["cogs.moderation.moderation"],
        )
        == tree_sync.SKIP_PARTIAL_TREE
    )


def test_decide_refuses_an_empty_tree():
    assert (
        tree_sync.decide(
            new_hash="b", stored_hash="a", command_count=0, failed_extensions=[]
        )
        == tree_sync.SKIP_EMPTY_TREE
    )


# ---------------------------------------------------------------------------
# sync_if_changed(): the wiring. Positive control first.
# ---------------------------------------------------------------------------


async def test_a_changed_tree_is_synced_exactly_once():
    """POSITIVE CONTROL for every "no sync happened" assertion below.

    If this seam could not observe a sync, the silence the partial-tree guards
    assert would mean nothing.
    """
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    bot = FakeBot(tree, store)

    assert await tree_sync.sync_if_changed(bot) == tree_sync.SYNC
    assert len(tree.syncs) == 1
    assert tree.syncs == [None], "the auto-sync must be GLOBAL, not guild-scoped"
    assert store.writes == 1
    expected = tree_sync.payload_hash(await tree_sync.build_payload(tree))
    assert store.rows[bot.application_id][0] == expected


async def test_an_unchanged_tree_is_not_synced_again():
    """The whole point: a deploy that changed no command costs ZERO syncs."""
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    bot = FakeBot(tree, store)
    await tree_sync.sync_if_changed(bot)
    assert len(tree.syncs) == 1

    assert await tree_sync.sync_if_changed(bot) == tree_sync.SKIP_UNCHANGED
    assert len(tree.syncs) == 1
    assert store.writes == 1


async def test_a_description_only_change_triggers_one_sync():
    """Boot, translate a description, boot again: exactly one more sync."""
    store = HashStore()
    first = FakeBot(FakeTree([_entry("config", "Configure the server.")]), store)
    await tree_sync.sync_if_changed(first)

    second = FakeBot(FakeTree([_entry("config", "Configure ce serveur.")]), store)
    assert await tree_sync.sync_if_changed(second) == tree_sync.SYNC
    assert len(second.tree.syncs) == 1
    assert store.writes == 2


async def test_a_partial_tree_is_never_synced(caplog):
    """SILENCE GUARD, with the count and the untouched row spelled out.

    One cog failing to load means every command it owns is absent from the
    payload. A global sync is a bulk overwrite, so pushing it would DELETE those
    commands in production - the very outage this feature exists to prevent,
    caused by the feature itself.
    """
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    bot = FakeBot(tree, store)
    store.rows[bot.application_id] = ("previous-hash", 78)

    with caplog.at_level(logging.WARNING, logger="tools.tree_sync"):
        decision = await tree_sync.sync_if_changed(
            bot, failed_extensions=["cogs.moderation.moderation"]
        )

    assert decision == tree_sync.SKIP_PARTIAL_TREE
    assert len(tree.syncs) == 0
    assert store.writes == 0
    assert store.rows[bot.application_id][0] == "previous-hash"
    assert any(
        "cogs.moderation.moderation" in record.getMessage()
        and record.levelno == logging.WARNING
        for record in caplog.records
    ), "the refusal has to name the cog, loudly"


async def test_a_partial_tree_is_refused_even_on_a_first_ever_boot():
    """No stored hash is not a licence to publish an incomplete tree."""
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    bot = FakeBot(tree, store)

    decision = await tree_sync.sync_if_changed(bot, failed_extensions=["cogs.x"])
    assert decision == tree_sync.SKIP_PARTIAL_TREE
    assert len(tree.syncs) == 0
    assert store.rows == {}


async def test_a_generator_of_failures_is_still_seen():
    """A one-shot iterable must not read as "no failures" to the second reader."""
    tree = FakeTree([_entry("config", "Configure the server.")])
    bot = FakeBot(tree, HashStore())
    decision = await tree_sync.sync_if_changed(
        bot, failed_extensions=(name for name in ["cogs.x"])
    )
    assert decision == tree_sync.SKIP_PARTIAL_TREE
    assert len(tree.syncs) == 0


async def test_an_empty_tree_is_never_synced():
    tree = FakeTree([])
    store = HashStore()
    bot = FakeBot(tree, store)
    assert await tree_sync.sync_if_changed(bot) == tree_sync.SKIP_EMPTY_TREE
    assert len(tree.syncs) == 0
    assert store.writes == 0


async def test_no_application_id_means_no_sync():
    tree = FakeTree([_entry("config", "Configure the server.")])
    bot = FakeBot(tree, HashStore(), application_id=None)
    assert await tree_sync.sync_if_changed(bot) == tree_sync.SKIP_NO_APPLICATION_ID
    assert len(tree.syncs) == 0


async def test_a_failed_sync_keeps_the_old_hash_so_the_next_boot_retries():
    """The retry property, stated as the row the failure must not touch."""
    store = HashStore()
    first = FakeBot(FakeTree([_entry("config", "Old.")]), store)
    await tree_sync.sync_if_changed(first)
    old_hash = store.rows[first.application_id][0]

    broken = FakeBot(FakeTree([_entry("config", "New.")]), store)
    broken.tree.sync_error = discord.HTTPException(
        type("R", (), {"status": 500, "reason": "boom"})(), "boom"
    )
    assert await tree_sync.sync_if_changed(broken) == tree_sync.FAILED
    assert len(broken.tree.syncs) == 1
    assert store.rows[first.application_id][0] == old_hash

    healthy = FakeBot(FakeTree([_entry("config", "New.")]), store)
    assert await tree_sync.sync_if_changed(healthy) == tree_sync.SYNC
    assert store.rows[healthy.application_id][0] != old_hash


async def test_a_failed_sync_logs_a_warning_and_does_not_raise(caplog):
    tree = FakeTree([_entry("config", "Configure.")])
    tree.sync_error = RuntimeError("discord is down")
    bot = FakeBot(tree, HashStore())
    with caplog.at_level(logging.WARNING, logger="tools.tree_sync"):
        assert await tree_sync.sync_if_changed(bot) == tree_sync.FAILED
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_a_hanging_sync_is_abandoned_rather_than_holding_startup(monkeypatch):
    """setup_hook runs before the gateway connects, so this cannot hang forever."""
    monkeypatch.setattr(tree_sync, "SYNC_TIMEOUT", 0.05)
    tree = FakeTree([_entry("config", "Configure.")])
    tree.sync_sleep = 5.0
    store = HashStore()
    bot = FakeBot(tree, store)
    assert await tree_sync.sync_if_changed(bot) == tree_sync.FAILED
    assert store.writes == 0


async def test_a_database_that_cannot_be_read_does_not_sync_blind():
    """Without the previous hash there is no gate, so there is no sync."""
    tree = FakeTree([_entry("config", "Configure.")])
    store = HashStore()
    store.read_error = RuntimeError("db down")
    bot = FakeBot(tree, store)
    assert await tree_sync.sync_if_changed(bot) == tree_sync.FAILED
    assert len(tree.syncs) == 0


async def test_a_write_failure_after_a_good_sync_only_costs_one_extra_sync():
    tree = FakeTree([_entry("config", "Configure.")])
    store = HashStore()
    store.write_error = RuntimeError("db down")
    bot = FakeBot(tree, store)
    assert await tree_sync.sync_if_changed(bot) == tree_sync.FAILED
    assert len(tree.syncs) == 1
    assert store.rows == {}


async def test_two_applications_sharing_one_database_cannot_fool_each_other():
    """A dev bot's sync must not tell the prod bot its tree is already live."""
    store = HashStore()
    entries = [_entry("config", "Configure the server.")]
    dev = FakeBot(FakeTree(entries), store, application_id=111)
    prod = FakeBot(FakeTree(entries), store, application_id=222)

    assert await tree_sync.sync_if_changed(dev) == tree_sync.SYNC
    assert await tree_sync.sync_if_changed(prod) == tree_sync.SYNC
    assert len(prod.tree.syncs) == 1
    assert set(store.rows) == {111, 222}


# ---------------------------------------------------------------------------
# record_global_sync(): the manual y!sync interplay.
# ---------------------------------------------------------------------------


async def test_a_hand_sync_then_a_reboot_causes_no_second_sync():
    """The interplay, end to end: ``y!sync`` records, the next boot stays quiet."""
    tree = FakeTree([_entry("config", "Configure the server.")])
    store = HashStore()
    bot = FakeBot(tree, store)

    assert await tree_sync.record_global_sync(bot, synced_count=1) is True
    assert store.writes == 1

    reboot = FakeBot(FakeTree([_entry("config", "Configure the server.")]), store)
    assert await tree_sync.sync_if_changed(reboot) == tree_sync.SKIP_UNCHANGED
    assert len(reboot.tree.syncs) == 0


async def test_a_hand_sync_of_a_DIFFERENT_tree_still_lets_the_next_boot_sync():
    """Recording must describe the tree, not merely mark "someone synced"."""
    store = HashStore()
    hand = FakeBot(FakeTree([_entry("config", "Old.")]), store)
    await tree_sync.record_global_sync(hand, synced_count=1)

    reboot = FakeBot(FakeTree([_entry("config", "New.")]), store)
    assert await tree_sync.sync_if_changed(reboot) == tree_sync.SYNC
    assert len(reboot.tree.syncs) == 1


async def test_recording_never_raises_when_the_write_fails():
    store = HashStore()
    store.write_error = RuntimeError("db down")
    bot = FakeBot(FakeTree([_entry("config", "Configure.")]), store)
    assert await tree_sync.record_global_sync(bot, synced_count=1) is False


async def test_recording_needs_an_application_id():
    bot = FakeBot(FakeTree([_entry("config", "Configure.")]), HashStore(), None)
    assert await tree_sync.record_global_sync(bot, synced_count=1) is False
