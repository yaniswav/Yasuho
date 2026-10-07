"""A hybrid group with no subcommand is registered on Discord as a plain
command, while the local tree still treats it as a group: every slash
invocation then fails with CommandSignatureMismatch (/config, live since at
least 2026-09-07). Every hybrid group in cogs/ must have a subcommand."""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _decorator_name(node):
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr, target.value
    if isinstance(target, ast.Name):
        return target.id, None
    return None, None


def empty_hybrid_groups(source, filename="<src>"):
    tree = ast.parse(source, filename)
    groups = set()
    parents = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            name, owner = _decorator_name(deco)
            if name == "hybrid_group":
                groups.add(node.name)
            if name in ("command", "group") and isinstance(owner, ast.Name):
                parents.add(owner.id)
    return sorted(groups - parents)


def test_detector_flags_an_empty_hybrid_group():
    source = (
        "class C:\n"
        "    @commands.hybrid_group()\n"
        "    async def lonely(self, ctx): pass\n"
        "    @commands.hybrid_group()\n"
        "    async def full(self, ctx): pass\n"
        "    @full.command()\n"
        "    async def child(self, ctx): pass\n"
    )
    assert empty_hybrid_groups(source) == ["lonely"]


def test_no_hybrid_group_without_a_subcommand():
    offenders = []
    scanned = 0
    for path in sorted((ROOT / "cogs").rglob("*.py")):
        scanned += 1
        for name in empty_hybrid_groups(path.read_text(encoding="utf-8"), str(path)):
            offenders.append("{}:{}".format(path.relative_to(ROOT), name))
    assert scanned > 50
    assert offenders == []
