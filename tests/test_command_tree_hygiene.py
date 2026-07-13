"""Regression guard: no two commands share a name or alias in one namespace.

discord.py registers prefix commands (and the prefix side of hybrid commands)
into a single namespace per parent: the bot's root for top-level commands, and
one namespace per group object for that group's subcommands. Within a namespace,
a command's primary ``name`` and every entry in its ``aliases`` must be unique -
a clash makes ``add_command`` raise ``CommandRegistrationError`` at load and the
whole cog fails to attach. Slash has no aliases, so this is purely the text side,
but a colliding alias silently shadows another command for every prefix user.

This scans the source with the AST (no imports, no network, no DB - the same
discipline as tests/test_cog_hygiene.py) and asserts the whole command tree is
collision-free. It pins the hand-audited alias set (Lot UX3) and catches any
future alias that would step on an existing command bot-wide.

Namespace keying: the root is one global bucket; every group's subcommand bucket
is keyed by the group's *registered name*. That correctly folds the AniList
account/airing/chapters mixin (subcommands of the one ``anilist`` group defined
across several files) into a single bucket. It assumes no two distinct group
objects share a registered name - true here (root groups cannot collide, and no
two subgroups share a name), and the guard itself would flag it if that changed.
"""

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COGS = _REPO_ROOT / "cogs"

_COMMAND_METHODS = frozenset(
    {"command", "group", "hybrid_command", "hybrid_group"}
)
_GROUP_METHODS = frozenset({"group", "hybrid_group"})
_ROOT_RECEIVERS = frozenset({"commands", "app_commands", None})


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


def _decorator_command(call):
    """If ``call`` is a command-defining decorator, return
    (receiver, method, name_or_None, aliases_list); else None."""
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
        return None
    method = call.func.attr
    if method not in _COMMAND_METHODS:
        return None
    receiver = _dotted(call.func.value)
    name = None
    aliases = []
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            name = kw.value.value
        elif kw.arg == "aliases" and isinstance(kw.value, ast.List):
            aliases = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
    if name is None and call.args and isinstance(call.args[0], ast.Constant):
        if isinstance(call.args[0].value, str):
            name = call.args[0].value
    return receiver, method, name, aliases


def collect_command_defs(tree):
    """Yield dicts describing every command/group defined in an AST module."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            parsed = _decorator_command(dec)
            if parsed is None:
                continue
            receiver, method, name, aliases = parsed
            out.append(
                {
                    "receiver": receiver,
                    "method": method,
                    "name": name or node.name,
                    "aliases": aliases,
                    "func": node.name,
                    "line": node.lineno,
                }
            )
    return out


def find_tree_collisions(defs):
    """Given command defs (with a ``file`` key), return
    {namespace: {token: [refs]}} for every token used more than once."""
    func_to_group_name = {
        d["func"]: d["name"] for d in defs if d["method"] in _GROUP_METHODS
    }

    def namespace_of(receiver):
        if receiver in _ROOT_RECEIVERS:
            return "ROOT"
        var = receiver.split(".")[-1]
        return f"GROUP:{func_to_group_name.get(var, var)}"

    seen = {}  # namespace -> token -> [ref, ...]
    for d in defs:
        ns = namespace_of(d["receiver"])
        bucket = seen.setdefault(ns, {})
        ref = f"{d['file']}:{d['line']} {d['func']}"
        for token in (d["name"], *d["aliases"]):
            bucket.setdefault(token, []).append(ref)

    collisions = {}
    for ns, tokens in seen.items():
        clashing = {tok: refs for tok, refs in tokens.items() if len(refs) > 1}
        if clashing:
            collisions[ns] = clashing
    return collisions


def _all_defs():
    defs = []
    for path in sorted(_COGS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for d in collect_command_defs(tree):
            d["file"] = rel
            defs.append(d)
    return defs


def test_scan_is_not_vacuous():
    """Sanity: the AST scan actually finds the command surface."""
    defs = _all_defs()
    names = {d["name"] for d in defs}
    # A handful of stable anchors across several cogs.
    for anchor in {"play", "rank", "ban", "anilist", "remind", "translate"}:
        assert anchor in names, anchor
    assert len(defs) > 150, len(defs)


def test_synthetic_collision_is_flagged():
    """Prove the guard is not vacuous: a clash is detected."""
    src = (
        "class C:\n"
        "    @commands.hybrid_command(name='remind', aliases=['reminder'])\n"
        "    async def a(self): ...\n"
        "    @commands.command(name='reminder')\n"
        "    async def b(self): ...\n"
    )
    defs = collect_command_defs(ast.parse(src))
    for d in defs:
        d["file"] = "synthetic"
    collisions = find_tree_collisions(defs)
    assert "reminder" in collisions.get("ROOT", {})


def test_no_command_name_or_alias_collisions():
    """THE guard: the real command tree has no duplicate name/alias per namespace."""
    collisions = find_tree_collisions(_all_defs())
    assert not collisions, (
        "Command name/alias collision(s) - one command shadows another for prefix "
        "users (or blocks cog load). Rename/deconflict:\n"
        + "\n".join(
            f"  [{ns}] {tok!r}:\n    " + "\n    ".join(refs)
            for ns, toks in sorted(collisions.items())
            for tok, refs in sorted(toks.items())
        )
    )
