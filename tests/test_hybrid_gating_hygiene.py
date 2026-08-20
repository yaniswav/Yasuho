"""Structural guard: every privileged hybrid-group subcommand carries its OWN check.

Why this guard has to exist
---------------------------
A check on a ``commands.hybrid_group`` callback does NOT protect that group's
subcommands - on either invocation path. Two independent mechanics in
discord.py 2.7.1 conspire:

1. Prefix path. ``HybridGroup.__init__`` hard-sets
   ``self.invoke_without_command = True`` (documented: "Hybrid groups will
   always have Group.invoke_without_command set to True"). In ``Group.invoke``
   that makes ``early_invoke = not self.invoke_without_command`` False, so
   ``await self.prepare(ctx)`` - the only thing that runs the group's
   ``can_run`` - is skipped, and dispatch goes straight to
   ``ctx.invoked_subcommand.invoke(ctx)``. The group's checks therefore run
   ONLY for a bare ``?group`` with no subcommand.

2. App path. ``HybridAppCommand._check_can_run`` evaluates, in order: the bot's
   global checks, ``parent.interaction_check`` (the app_commands.Group's, which
   defaults to True - it is NOT the ext.commands check), the binding's
   ``interaction_check`` / ``cog_check``, ``self.checks`` (app checks on the
   subcommand) and ``self.wrapped.checks`` (ext checks on the SUBCOMMAND). The
   parent's ext checks are never consulted.

So a permission requirement is only real if it sits on the subcommand itself,
or on a cog-wide mechanism (``cog_check``), which both paths do honour.

Note ``app_commands.default_permissions`` is deliberately NOT accepted as a
gate: it is a client-side default that guild administrators can override in
Server Settings > Integrations, and ``commands.has_permissions`` does not set it
anyway. It hides a command; it does not refuse one.

The guard scans the source with the AST (no imports, no DB, no network - same
discipline as tests/test_cog_hygiene.py and tests/test_command_tree_hygiene.py),
walks hybrid groups to arbitrary nesting depth, and requires every subcommand to
be either gated or listed in :data:`EXEMPT` with a written reason. A new
subcommand added without a check fails the build until someone justifies it.
"""

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COGS = _REPO_ROOT / "cogs"

# Decorators that constitute a real, both-paths permission gate on a subcommand.
#
# Bare ``commands.check`` / ``commands.check_any`` are deliberately NOT here.
# They are checks in the discord.py sense, but they say nothing about what the
# predicate actually asserts: ``@commands.check(lambda ctx: True)`` would have
# satisfied this guard and shipped an ungated config write. No live subcommand
# relies on either today, so excluding them costs nothing; a future one that
# genuinely needs a custom predicate lands in EXEMPT with a written reason,
# which is the outcome this guard exists to force.
_GATING_CHECKS = frozenset(
    {
        "has_permissions",
        "has_guild_permissions",
        "is_owner",
        "has_role",
        "has_any_role",
    }
)

# Whole groups with NO privileged surface at all: every subcommand is meant for
# ordinary members, so a per-command list would be noise. Only groups where the
# authorization model is deliberately something other than a permission
# decorator belong here - a group that mixes public and privileged subcommands
# must use the per-command EXEMPT below instead, so the privileged ones stay
# individually visible.
EXEMPT_GROUPS = {
    "music": (
        "playback surface for every listener. Control actions are authorized "
        "IN-BODY by _require_player(control=True): the invoker must share the "
        "bot's voice channel and be the session DJ or hold Manage Server, the "
        "same gate the controller buttons use. A permission decorator here "
        "would lock ordinary listeners out of their own music."
    ),
    "playlist": "self-scoped: the caller's own saved playlists",
    "info": "read-only: public member/guild/bot information",
    "lookup": "read-only: external lookups (wiki, weather, osu, ...)",
    "poll": "any member may open a poll by design; writes no config",
}

# Subcommands that are intentionally reachable by any member. Each entry states
# WHY it is safe ungated. Read-only or self-scoped only: nothing here may write
# another user's data or the guild's configuration.
EXEMPT = {
    # -- self-scoped: the caller's own privacy surface (acts on ctx.author) ---
    "mydata export": "self-scoped: exports the caller's own data only",
    "mydata deleteprofile": "self-scoped: erases the caller's own profile",
    "mydata deleteavatars": "self-scoped: erases the caller's own avatars",
    # -- self-scoped: the caller's own AniList account link ------------------
    "anilist login": "self-scoped: links the caller's own AniList account",
    "anilist code": "self-scoped: completes the caller's own login",
    "anilist logout": "self-scoped: unlinks the caller's own account",
    "anilist update": "self-scoped: edits the caller's own AniList entries",
    "anilist status": "self-scoped: reports the caller's own link status",
    "anilist score": "self-scoped: scores on the caller's own AniList list",
    "anilist profile": "read-only: public AniList profile lookup",
    "anilist list": "read-only: public AniList list lookup",
    # -- the anilist group extended from sibling modules ---------------------
    # Declared as @AccountMixin.anilist.command(...) in lookup.py / airing.py /
    # chapters.py / schedule.py. These were invisible to the guard until
    # _owner_name reduced the dotted owner to its bare name - see that helper.
    "anilist trending": "read-only: public AniList trending browse",
    "anilist popular": "read-only: public AniList popularity browse",
    "anilist seasonal": "read-only: public AniList seasonal browse",
    "anilist character": "read-only: public AniList character lookup",
    "anilist studio": "read-only: public AniList studio lookup",
    "anilist airing": (
        "self-scoped opt-in: toggles episode DMs for the CALLER only "
        "(anilist_airing_optins is keyed on ctx.author.id)"
    ),
    "anilist chapters": (
        "self-scoped opt-in: toggles chapter DMs for the CALLER only "
        "(anilist_chapter_optins is keyed on ctx.author.id)"
    ),
    "anilist schedule": (
        "read-only: browses upcoming episodes from the caller's own list or "
        "from the titles this channel already tracks publicly; writes nothing"
    ),
    "anilistfeed me": (
        "self-scoped opt-in: joins/leaves the feed with the caller's own "
        "account, and is refused in-body unless the guild enabled member "
        "self-add"
    ),
    # -- self-scoped: the caller's own social profile ------------------------
    "connections list": "self-scoped: the caller's own linked accounts",
    "connections link": "self-scoped: links the caller's own account",
    "connections unlink": "self-scoped: unlinks the caller's own account",
    "connections visibility": "self-scoped: the caller's own visibility",
    "profile view": "read-only: respects each field's visibility in-body",
    "profile set": "self-scoped: edits the caller's own profile",
    "profile visibility": "self-scoped: the caller's own field visibility",
    "profile presence": "self-scoped: the caller's own presence opt-in",
    "profile edit": "self-scoped: edits the caller's own profile",
    "profile panel": "self-scoped: the caller's own profile panel",
    "profile clear": "self-scoped: clears the caller's own profile",
    # -- self-scoped: the caller's own rank card cosmetics -------------------
    "rankcard view": "read-only: renders a rank card",
    "rankcard set-background": "self-scoped: the caller's own card",
    "rankcard set-accent": "self-scoped: the caller's own card",
    "rankcard clear": "self-scoped: the caller's own card",
    # -- user-scoped music --------------------------------------------------
    "serverplaylist save": "any member may create a shared playlist by design",
    "serverplaylist play": "read-only: plays an existing shared playlist",
    "serverplaylist delete": (
        "shared state, but gated IN-BODY by can_manage(author, creator_id, "
        "manage_guild): creator or moderator only"
    ),
    "serverplaylist rename": (
        "shared state, but gated IN-BODY by can_manage(author, creator_id, "
        "manage_guild): creator or moderator only"
    ),
    # -- public read-only ----------------------------------------------------
    "starboard top": (
        "read-only public leaderboard of messages the starboard already posts "
        "publicly; writes nothing"
    ),
}


def _dotted(node):
    """Return the dotted name of an attribute/name chain, else None."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _deco_dotted(deco):
    return _dotted(deco.func if isinstance(deco, ast.Call) else deco)


def _owner_name(owner):
    """Reduce a decorator owner to the bare group-function name.

    ``groups`` is keyed by the FUNCTION name that defines a hybrid group
    (``anilist``, ``modlog``, ...), but a subcommand does not have to be
    declared in the same class: a group can be extended from a sibling module
    through the defining mixin, e.g.::

        @AccountMixin.anilist.command(name="trending")

    whose owner reads ``"AccountMixin.anilist"``. Comparing the full dotted
    string against ``groups`` silently dropped every such subcommand - eight
    live ``anilist`` ones, spread over lookup.py / airing.py / chapters.py /
    schedule.py - so the guard's promise ("a new subcommand without a check
    fails the build") quietly did not hold for that group. Taking the last
    segment restores them. ``test_no_group_function_name_is_ambiguous`` pins
    the assumption this makes: no two files may define a hybrid group under
    the same function name, or the bare key would be ambiguous.
    """
    return owner.rsplit(".", 1)[-1]


def _kwarg_name(deco):
    if not isinstance(deco, ast.Call):
        return None
    for kw in deco.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _functions(tree):
    """Yield (class_node, func_node) for every method in every class."""
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for node in cls.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield cls, node


def _collect():
    """Walk cogs/ and return ``(subcommands, group_sites)``.

    ``subcommands`` maps a qualified command name ("modlog set") to a dict with
    its file, line, check decorators and whether its cog defines ``cog_check``.
    ``group_sites`` maps a group FUNCTION name to the list of "file:line" that
    define it, so :func:`test_no_group_function_name_is_ambiguous` can prove the
    bare-name keying is unambiguous.
    """
    parsed = []
    for path in sorted(_COGS.rglob("*.py")):
        parsed.append((path, ast.parse(path.read_text(encoding="utf-8"))))

    # Pass 1: every function that DEFINES a group, to a fixpoint so nested
    # groups (@levelconfig.group -> @noxp.command) are discovered too.
    # group func name -> (registered name, parent func name or None)
    groups = {}
    group_sites = {}
    for path, tree in parsed:
        for _cls, fn in _functions(tree):
            for deco in fn.decorator_list:
                if _deco_dotted(deco) == "commands.hybrid_group":
                    groups[fn.name] = (_kwarg_name(deco) or fn.name, None)
                    group_sites.setdefault(fn.name, []).append(
                        f"{path.relative_to(_REPO_ROOT)}:{fn.lineno}"
                    )

    changed = True
    while changed:
        changed = False
        for _path, tree in parsed:
            for _cls, fn in _functions(tree):
                for deco in fn.decorator_list:
                    dotted = _deco_dotted(deco)
                    if not dotted or "." not in dotted:
                        continue
                    owner, method = dotted.rsplit(".", 1)
                    owner = _owner_name(owner)
                    if method == "group" and owner in groups and fn.name not in groups:
                        groups[fn.name] = (_kwarg_name(deco) or fn.name, owner)
                        group_sites.setdefault(fn.name, []).append(
                            f"{_path.relative_to(_REPO_ROOT)}:{fn.lineno}"
                        )
                        changed = True

    def qualified(func_name, own_name):
        chain = [own_name]
        parent = groups.get(func_name, (None, None))[1] if func_name in groups else None
        while parent is not None:
            registered, grandparent = groups[parent]
            chain.append(registered)
            parent = grandparent
        return " ".join(reversed(chain))

    subcommands = {}
    for path, tree in parsed:
        for cls, fn in _functions(tree):
            has_cog_check = any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "cog_check"
                for n in cls.body
            )
            for deco in fn.decorator_list:
                dotted = _deco_dotted(deco)
                if not dotted or "." not in dotted:
                    continue
                owner, method = dotted.rsplit(".", 1)
                owner = _owner_name(owner)
                if method not in {"command", "group"} or owner not in groups:
                    continue
                own = _kwarg_name(deco) or fn.name
                # A nested group's own registered name comes from `groups`.
                if fn.name in groups:
                    own = groups[fn.name][0]
                checks = {
                    _deco_dotted(d).rsplit(".", 1)[-1]
                    for d in fn.decorator_list
                    if _deco_dotted(d) and "." in _deco_dotted(d)
                }
                parent_chain = qualified(fn.name, own) if fn.name in groups else None
                if parent_chain is None:
                    chain = [own]
                    node = owner
                    while node is not None:
                        registered, grandparent = groups[node]
                        chain.append(registered)
                        node = grandparent
                    parent_chain = " ".join(reversed(chain))
                subcommands[parent_chain] = {
                    "file": str(path.relative_to(_REPO_ROOT)),
                    "line": fn.lineno,
                    "gated": bool(checks & _GATING_CHECKS),
                    "cog_check": has_cog_check,
                }
    return subcommands, group_sites


_SUBCOMMANDS, _GROUP_SITES = _collect()


def test_sweep_actually_found_the_command_tree():
    """Sanity: the AST walk must see the tree, or every other assert is vacuous."""
    assert len(_SUBCOMMANDS) > 80, len(_SUBCOMMANDS)
    # A nested (depth-2) subcommand proves the fixpoint walk works.
    assert "levelconfig noxp add" in _SUBCOMMANDS
    assert "modlog set" in _SUBCOMMANDS


def test_subcommands_declared_from_a_sibling_module_are_seen():
    """Regression pin for the guard's own blind spot.

    A subcommand attached through the DEFINING class rather than the bare group
    function - ``@AccountMixin.anilist.command(name="trending")`` in another
    file - used to be dropped silently, because the owner was matched as the
    full dotted string. The sweep then covered 146 of the 158 live subcommands
    and said nothing about the missing twelve, so ``anilist`` was effectively
    outside the guard. If this list shrinks, :func:`_owner_name` regressed and
    the guard is lying again.
    """
    for name in (
        "anilist trending",
        "anilist popular",
        "anilist seasonal",
        "anilist character",
        "anilist studio",
        "anilist airing",
        "anilist chapters",
        "anilist schedule",
    ):
        assert name in _SUBCOMMANDS, (
            f"{name} is live but invisible to the guard - a subcommand declared "
            "from a sibling module is being dropped again"
        )


def test_no_group_function_name_is_ambiguous():
    """``groups`` is keyed by the bare function name, so that name must be unique.

    :func:`_owner_name` deliberately throws away the class part of a decorator
    owner. That is only sound while no two cogs define a hybrid group under the
    same method name: if they did, the second definition would overwrite the
    first and every subcommand of one of them would be filed under the other's
    qualified name - a silent mis-attribution, not a failure.
    """
    duplicated = {n: sites for n, sites in _GROUP_SITES.items() if len(sites) > 1}
    assert not duplicated, (
        "Two hybrid groups share a function name, so the guard cannot tell "
        "their subcommands apart. Rename one of the methods:\n  "
        + "\n  ".join(f"{n}: {', '.join(sites)}" for n, sites in sorted(duplicated.items()))
    )


def test_every_hybrid_subcommand_is_gated_or_explicitly_exempt():
    """The build fails if a new subcommand ships without its own check.

    A check on the parent group does NOT count - discord.py never runs it for a
    subcommand on either path (see this module's docstring). Add the check to the
    subcommand, or add the command to EXEMPT with a reason.
    """
    ungated = sorted(
        f"{name}  ({info['file']}:{info['line']})"
        for name, info in _SUBCOMMANDS.items()
        if not info["gated"]
        and not info["cog_check"]
        and name not in EXEMPT
        and name.split(" ")[0] not in EXEMPT_GROUPS
    )
    assert not ungated, (
        "Hybrid-group subcommand(s) with no check of their own. A check on the "
        "parent hybrid_group does NOT protect a subcommand on either the prefix "
        "or the slash path. Add the check to the subcommand itself, or list it "
        "in EXEMPT (tests/test_hybrid_gating_hygiene.py) with a reason:\n  "
        + "\n  ".join(ungated)
    )


def test_exempt_list_has_no_stale_entries():
    """An exemption must name a command that still exists and is still ungated."""
    stale = sorted(n for n in EXEMPT if n not in _SUBCOMMANDS)
    assert not stale, f"EXEMPT names commands that no longer exist: {stale}"

    now_gated = sorted(
        n
        for n in EXEMPT
        if _SUBCOMMANDS[n]["gated"] or _SUBCOMMANDS[n]["cog_check"]
    )
    assert not now_gated, (
        "These are now gated - drop them from EXEMPT so the list keeps meaning "
        f"something: {now_gated}"
    )


def test_every_exemption_carries_a_reason():
    empty = sorted(n for n, why in EXEMPT.items() if not why.strip())
    assert not empty, f"EXEMPT entries without a justification: {empty}"

    empty_groups = sorted(n for n, why in EXEMPT_GROUPS.items() if not why.strip())
    assert not empty_groups, (
        f"EXEMPT_GROUPS entries without a justification: {empty_groups}"
    )


def test_exempt_groups_still_exist_and_stay_wholly_ungated():
    """A group-wide exemption must not quietly cover a privileged subcommand.

    If any subcommand of an exempt group grows its own permission check, that
    group is no longer uniformly member-facing and must move to the per-command
    EXEMPT list, so its privileged members stay individually visible.
    """
    roots = {name.split(" ")[0] for name in _SUBCOMMANDS}
    missing = sorted(g for g in EXEMPT_GROUPS if g not in roots)
    assert not missing, f"EXEMPT_GROUPS names groups that no longer exist: {missing}"

    for group in sorted(EXEMPT_GROUPS):
        gated = sorted(
            name
            for name, info in _SUBCOMMANDS.items()
            if name.split(" ")[0] == group and (info["gated"] or info["cog_check"])
        )
        assert not gated, (
            f"'{group}' is exempt as a whole group, but these subcommands are "
            f"now gated: {gated}. That means the group mixes public and "
            "privileged commands - drop it from EXEMPT_GROUPS and list the "
            "genuinely public ones in EXEMPT instead."
        )


def test_the_two_p0_groups_are_gated_per_subcommand():
    """Regression pin for the modlog / automod holes (and autoroom list)."""
    for name in (
        "modlog set",
        "modlog disable",
        "modlog status",
        "automod links",
        "automod invites",
        "automod spam",
        "automod panel",
        "autoroom list",
    ):
        assert name in _SUBCOMMANDS, name
        assert _SUBCOMMANDS[name]["gated"], (
            f"{name} lost its own permission check - it is reachable by any "
            "member on BOTH the prefix and the slash path without one"
        )
        assert name not in EXEMPT, f"{name} must never be exempt"
