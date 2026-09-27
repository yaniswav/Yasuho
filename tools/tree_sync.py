"""Hash-gated self-sync of the global slash command tree.

THE INCIDENTS THIS EXISTS TO PREVENT
------------------------------------
The bot never synced its own tree. Registering application commands with
Discord was a MANUAL step (``y!sync``) the owner had to remember after every
deploy that changed a command definition or its localisations, and twice in
five weeks nobody did:

* 2026-08-26 - ``/trending`` had been folded into ``/anilist trending``. Discord
  still advertised the old name, so invoking it reached a bot that no longer had
  that command: ``CommandNotFound``.
* 2026-09-13 - a commit translated command descriptions
  (``locales/command_strings.py``). Descriptions are part of the payload Discord
  stores, so ``/config`` came back as ``CommandSignatureMismatch`` ("The
  signature for command config is different from the one provided by Discord"),
  twice.

Both are the same failure: Discord's copy of the tree drifted from the running
bot's, with nothing watching.

WHY A HASH AND NOT "SYNC EVERY BOOT"
------------------------------------
Discord rate-limits command registration, and an unconditional sync on every
restart spends that budget on nothing - the tree is identical the vast majority
of the time. So the bot syncs only when the payload actually CHANGED. What is
hashed is therefore load-bearing: it must be the EXACT bytes ``sync`` would
POST, or the change that mattered is the one the hash cannot see. The
2026-09-13 case is the proof - a hash of command NAMES, or of names plus
English descriptions, would have missed a pure localisation change entirely.

THE PAYLOAD IS DISCORD.PY'S OWN
-------------------------------
:func:`build_payload` reuses the path ``CommandTree.sync`` takes, verbatim
(discord.py 2.7.1, ``discord/app_commands/tree.py`` lines 1106-1112)::

    commands = self._get_all_commands(guild=guild)
    translator = self.translator
    if translator:
        payload = [await command.get_translated_payload(self, translator) for command in commands]
    else:
        payload = [command.to_dict(self) for command in commands]

The translator branch is not optional decoration here: Yasuho installs
``tools.translator.YasuhoTranslator`` in ``core.setup_hook``, and it is what
turns the catalogs into the ``description_localizations`` blocks Discord stores.
On the live tree that branch is the difference between a 52 KB payload and a
223 KB one - i.e. three quarters of what Discord is told. The payload is
computed AFTER ``set_translator`` for exactly that reason.

CANONICALISATION
----------------
Two boots of unchanged code must produce byte-identical text, and only a real
change may move it:

* dict keys are sorted at every depth (``sort_keys=True``), so nothing depends
  on the order discord.py happened to build a dict in;
* the TOP-LEVEL list is sorted by ``(type, name)``, because the order commands
  are registered in is an artifact of extension load order and of ``?reload``,
  not a difference Discord is told about (``bulk_upsert`` is a set operation);
* nested ``options`` lists are deliberately NOT sorted. Their order IS semantic -
  it is the order parameters are shown and required-first is a real constraint -
  so a reordering there is a genuine change and must move the hash.

There are no sets anywhere in the payload (every value is str/int/float/bool/
None/list/dict; verified against the live 78-command tree), so no unordered
container can leak in through the back door. ``allow_nan=False`` makes a stray
NaN an error rather than the non-standard ``NaN`` token.

SCOPING
-------
The row is keyed by APPLICATION ID. A development bot and the production bot
pointed at the same database have different application ids, so neither can
convince the other that its own tree is already synced.
"""

import asyncio
import hashlib
import json
import logging

log = logging.getLogger(__name__)

# Ceiling on the one HTTP call this module makes. setup_hook runs BEFORE the
# gateway connection, so a sync that hangs would hold the bot offline; at the
# timeout we log and carry on. The bulk overwrite is idempotent, and the hash is
# only stored on a clean return, so an abandoned attempt simply retries next
# boot.
SYNC_TIMEOUT = 60.0

# Decision codes returned by :func:`decide` / :func:`sync_if_changed`. Strings
# rather than an enum so a log line and a test assertion read the same.
SYNC = "sync"
SKIP_UNCHANGED = "skip-unchanged"
SKIP_PARTIAL_TREE = "skip-partial-tree"
SKIP_EMPTY_TREE = "skip-empty-tree"
SKIP_NO_APPLICATION_ID = "skip-no-application-id"
FAILED = "failed"


async def build_payload(tree):
    """Return the exact list of command dicts ``tree.sync()`` would POST.

    Mirrors ``CommandTree.sync`` (discord.py 2.7.1 tree.py:1106-1112) for the
    GLOBAL scope, translator step included. See the module docstring.
    """
    commands = tree._get_all_commands(guild=None)
    translator = tree.translator
    if translator:
        return [
            await command.get_translated_payload(tree, translator)
            for command in commands
        ]
    return [command.to_dict(tree) for command in commands]


def _entry_text(entry):
    """One command as canonical JSON text: keys sorted at every depth."""
    return json.dumps(
        entry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonicalise(payload):
    """Serialise a payload to text that depends only on its MEANING.

    Stable across dict-key order and across the order the top-level commands
    were registered in; NOT stable across a change to any value, nor across a
    reordering of a nested ``options`` list (see the module docstring for why
    that asymmetry is deliberate).
    """
    entries = sorted(
        (str(entry.get("type")), str(entry.get("name")), _entry_text(entry))
        for entry in payload
    )
    return "[" + ",".join(text for _, _, text in entries) + "]"


def payload_hash(payload):
    """SHA-256 (hex) of the canonical text of ``payload``."""
    return hashlib.sha256(canonicalise(payload).encode("utf-8")).hexdigest()


def decide(*, new_hash, stored_hash, command_count, failed_extensions):
    """Should the tree be synced? Pure; returns one of the decision codes.

    THE ORDER OF THESE CHECKS IS THE SAFETY PROPERTY. A global sync is a bulk
    OVERWRITE: whatever is not in the payload is DELETED at Discord. So the
    tree being COMPLETE is checked before anything else, and before the hash is
    even consulted.

    ``failed_extensions`` non-empty means at least one cog did not attach -
    ``core.setup_hook`` logs such a failure and carries on rather than raising,
    which is right for staying up and fatal for syncing: every command that cog
    owns is missing from the payload, and pushing it would erase them in
    production. Refusing costs nothing (the previous registration stays live and
    correct) and the owner still has ``y!sync``, so refusal is the answer.

    A zero-command tree is the same failure with no surviving evidence, and is
    refused for the same reason.
    """
    if failed_extensions:
        return SKIP_PARTIAL_TREE
    if command_count <= 0:
        return SKIP_EMPTY_TREE
    if stored_hash is not None and stored_hash == new_hash:
        return SKIP_UNCHANGED
    return SYNC


async def load_hash(pool, application_id):
    """The hash of the last SUCCESSFUL global sync for this application id."""
    row = await pool.fetchrow(
        "SELECT payload_hash FROM app_command_sync WHERE application_id = $1;",
        application_id,
    )
    return row["payload_hash"] if row else None


async def store_hash(pool, application_id, digest, command_count):
    """Record a successful global sync. Called ONLY after ``sync()`` returned."""
    await pool.execute(
        """
        INSERT INTO app_command_sync
            (application_id, payload_hash, command_count, synced_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (application_id) DO UPDATE SET
            payload_hash  = EXCLUDED.payload_hash,
            command_count = EXCLUDED.command_count,
            synced_at     = now();
        """,
        application_id,
        digest,
        command_count,
    )


async def sync_if_changed(bot, *, failed_extensions=()):
    """Sync the global tree if and only if its payload changed. Never raises.

    Returns the decision code, which is also what the tests assert on.

    FAILURE IS A WARNING AND NOTHING ELSE. Startup must not depend on Discord
    answering: every error - no application id, a payload that will not
    serialise, a database that will not answer, an HTTP failure, the timeout -
    is logged and swallowed. The stored hash is left ALONE on every one of those
    paths, so the next boot sees the same difference and tries again.
    """
    try:
        # Materialised once: the list is read by decide AND by the refusal log,
        # and a caller handing in a generator would otherwise leave the second
        # reader looking at an empty one - i.e. reporting no failures.
        failed = sorted(failed_extensions)

        application_id = getattr(bot, "application_id", None)
        if application_id is None:
            log.warning(
                "Slash tree auto-sync skipped: no application id yet."
            )
            return SKIP_NO_APPLICATION_ID

        payload = await build_payload(bot.tree)
        digest = payload_hash(payload)
        stored = await load_hash(bot.db_pool, application_id)
        decision = decide(
            new_hash=digest,
            stored_hash=stored,
            command_count=len(payload),
            failed_extensions=failed,
        )

        if decision == SKIP_PARTIAL_TREE:
            log.warning(
                "Slash tree auto-sync REFUSED: %d extension(s) failed to load "
                "(%s). Syncing a partial tree would delete the commands they "
                "own. Fix the load failure and restart, or sync by hand.",
                len(failed),
                ", ".join(failed),
            )
            return decision
        if decision == SKIP_EMPTY_TREE:
            log.warning(
                "Slash tree auto-sync REFUSED: the tree holds no commands."
            )
            return decision
        if decision == SKIP_UNCHANGED:
            log.info(
                "Slash tree unchanged (%d commands, %s); no sync needed.",
                len(payload),
                digest[:12],
            )
            return decision

        synced = await asyncio.wait_for(bot.tree.sync(), timeout=SYNC_TIMEOUT)
        # Only now - a stored hash means "Discord has accepted exactly this".
        await store_hash(bot.db_pool, application_id, digest, len(synced))
        log.info(
            "Slash tree changed (%s -> %s); synced %d commands.",
            (stored or "none")[:12],
            digest[:12],
            len(synced),
        )
        return SYNC
    except Exception:
        log.warning(
            "Slash tree auto-sync failed; the previous hash is kept so the "
            "next boot retries.",
            exc_info=True,
        )
        return FAILED


async def record_global_sync(bot, *, synced_count=None):
    """Record the hash after a MANUAL GLOBAL sync (``y!sync`` with no spec).

    Without this, a hand sync followed by a reboot would make the next boot see
    a hash it has never stored and sync again for nothing.

    Deliberately NOT gated on a complete tree, unlike :func:`sync_if_changed`:
    the owner asked for that sync and Discord has already been overwritten, so
    the hash must describe what was actually pushed - partial or not - or the
    record would be a lie. The GUILD-scoped variants (``y!sync ~ / * / ^``) do
    not call this at all: they never touch the global registration.

    Returns True when the hash was written. Never raises - the reply to the
    owner must not turn into a traceback because a bookkeeping row would not
    write; the only cost of a miss is one redundant sync next boot.
    """
    try:
        application_id = getattr(bot, "application_id", None)
        if application_id is None:
            log.warning("Manual global sync not recorded: no application id.")
            return False
        payload = await build_payload(bot.tree)
        await store_hash(
            bot.db_pool,
            application_id,
            payload_hash(payload),
            len(payload) if synced_count is None else synced_count,
        )
        return True
    except Exception:
        log.warning(
            "Could not record the manual global sync; the next boot will "
            "sync once more.",
            exc_info=True,
        )
        return False
