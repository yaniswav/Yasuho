"""Structural guard: every role-granting call site in ``cogs/`` and ``tools/``
either guards with ``tools.role_audit.refuse_dangerous_role`` or is explicitly
allowlisted as self-service (a role a MEMBER chooses for themselves, or a role
an admin picks ad hoc for a command - never one of the six roles Yasuho hands
out on her own initiative from a stored setting, see tools/role_audit.py's
module docstring).

Two call shapes grant a role: ``x.add_roles(role)`` and ``x.edit(roles=[...])``
(``discord.Member.edit`` REPLACES the member's whole role set, so a `roles=`
keyword there is a grant too, not just an `add_roles` call). Both are scanned,
and both ``cogs/`` and ``tools/`` are walked, so a grant routed through a
tools/-style helper does not escape the detector the way it did before.

This is a TEXTUAL check, not a dataflow one: it asks "does the function that
makes one of these calls also mention ``refuse_dangerous_role`` anywhere in
its own body" - good enough to catch a brand-new apply site of one of the six
surfaces that nobody wired the guard into, while staying cheap and not trying
to prove the check actually runs on the right branch (the per-site tests in
tests/cogs/ do that). A function is identified as ``root/rel/path.py:qualname``
so a failure names exactly where to look.

Counter-tests: a synthetic function that calls ``add_roles`` with no guard and
is not allowlisted IS flagged, same for ``edit(roles=...)``, and removing the
guard from a real site (proved by hand for the mute command - see the lot
report) makes this same shape of test fail. Keeps this detector from being a
guard that only ever prints silence.
"""

import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_ROOTS = {
    "cogs": PROJECT_ROOT / "cogs",
    "tools": PROJECT_ROOT / "tools",
}

# (relative/path.py, qualname) pairs allowed to call add_roles/edit(roles=...)
# with no refuse_dangerous_role guard in the same function - all self-service
# (a member's own click) or an admin's ad hoc, no stored six-surface role.
# Paths are relative to their SCAN_ROOTS entry ("cogs/" or "tools/" prefix).
_ALLOWLIST = {
    ("cogs/config/reactionroles.py", "ReactionRoles.on_raw_reaction_add"),
    ("cogs/config/rolemenus.py", "RoleMenuSelect.callback"),
    ("cogs/config/buttonroles.py", "ButtonRoleButton.callback"),
    ("cogs/moderation/moderation.py", "Moderation.addrole"),
}


def _is_role_grant_call(node):
    """Whether ``node`` (an ``ast.Call``) grants a role: ``add_roles(...)`` on
    any attribute, or ``edit(...)`` passed a ``roles=`` keyword (``Member.edit``
    replaces the whole role set, so that is a grant too)."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "add_roles":
        return True
    if func.attr == "edit":
        return any(kw.arg == "roles" for kw in node.keywords)
    return False


def _qualnames_calling_add_roles(path):
    """Yield ``(qualname, guarded)`` for every function in ``path`` that makes
    a role-granting call (``add_roles`` or ``edit(roles=...)``) somewhere in
    its own body (nested functions included, since a guard written just above
    a nested call still shows up in the outer source text)."""
    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(path))

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.found = []

        def _function(self, node):
            self.stack.append(node.name)
            qualname = ".".join(self.stack)
            grants_role = any(
                isinstance(n, ast.Call) and _is_role_grant_call(n)
                for n in ast.walk(node)
            )
            if grants_role:
                segment = ast.get_source_segment(source_text, node) or ""
                self.found.append((qualname, "refuse_dangerous_role" in segment))
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self._function(node)

        def visit_AsyncFunctionDef(self, node):
            self._function(node)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.found


def test_every_add_roles_call_site_is_guarded_or_allowlisted():
    unguarded = []
    for root_name, root in SCAN_ROOTS.items():
        for path in sorted(root.rglob("*.py")):
            rel = "{0}/{1}".format(
                root_name, str(path.relative_to(root)).replace("\\", "/")
            )
            for qualname, guarded in _qualnames_calling_add_roles(path):
                if guarded:
                    continue
                if (rel, qualname) in _ALLOWLIST:
                    continue
                unguarded.append(f"{rel}:{qualname}")
    assert not unguarded, (
        "role-granting call site(s) (add_roles or edit(roles=...)) with no "
        "refuse_dangerous_role guard and not allowlisted (new six-surface "
        "apply site, or a self-service site that needs adding to "
        "_ALLOWLIST): " + ", ".join(unguarded)
    )


# ---------------------------------------------------------------------------
# Counter-tests: the detector must actually flag something.
# ---------------------------------------------------------------------------
def test_guard_flags_a_synthetic_unguarded_add_roles_site(tmp_path):
    synthetic = tmp_path / "synthetic.py"
    synthetic.write_text(
        "class Thing:\n"
        "    async def grant(self, member, role):\n"
        "        await member.add_roles(role)\n",
        encoding="utf-8",
    )
    found = _qualnames_calling_add_roles(synthetic)
    assert found == [("Thing.grant", False)]


def test_guard_flags_a_synthetic_unguarded_edit_roles_site(tmp_path):
    """A grant routed through ``member.edit(roles=[...])`` instead of
    ``add_roles`` - the shape that used to escape the detector entirely."""
    synthetic = tmp_path / "synthetic_tools_style.py"
    synthetic.write_text(
        "class Helper:\n"
        "    async def apply(self, member, role):\n"
        "        await member.edit(roles=[role])\n",
        encoding="utf-8",
    )
    found = _qualnames_calling_add_roles(synthetic)
    assert found == [("Helper.apply", False)]


def test_guard_does_not_flag_a_guarded_site(tmp_path):
    synthetic = tmp_path / "synthetic_guarded.py"
    synthetic.write_text(
        "from tools import role_audit\n"
        "class Thing:\n"
        "    async def grant(self, member, role, guild_id):\n"
        "        if role_audit.refuse_dangerous_role(\n"
        "            role, surface='x', guild_id=guild_id\n"
        "        ):\n"
        "            return\n"
        "        await member.add_roles(role)\n",
        encoding="utf-8",
    )
    found = _qualnames_calling_add_roles(synthetic)
    assert found == [("Thing.grant", True)]


def test_guard_ignores_an_unrelated_edit_call(tmp_path):
    """``message.edit(view=self)`` is not a role grant - no `roles=` keyword,
    so it must never show up as a finding at all."""
    synthetic = tmp_path / "synthetic_edit.py"
    synthetic.write_text(
        "class Thing:\n"
        "    async def refresh(self, message):\n"
        "        await message.edit(view=self)\n",
        encoding="utf-8",
    )
    found = _qualnames_calling_add_roles(synthetic)
    assert found == []
