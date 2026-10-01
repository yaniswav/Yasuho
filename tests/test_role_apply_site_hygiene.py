"""Structural guard: every ``add_roles`` call site in ``cogs/`` either guards
with ``tools.role_audit.refuse_dangerous_role`` or is explicitly allowlisted as
self-service (a role a MEMBER chooses for themselves, or a role an admin picks
ad hoc for a command - never one of the six roles Yasuho hands out on her own
initiative from a stored setting, see tools/role_audit.py's module docstring).

This is a TEXTUAL check, not a dataflow one: it asks "does the function that
calls ``add_roles`` also mention ``refuse_dangerous_role`` anywhere in its own
body" - good enough to catch a brand-new apply site of one of the six surfaces
that nobody wired the guard into, while staying cheap and not trying to prove
the check actually runs on the right branch (the per-site tests in
tests/cogs/ do that). A function is identified as ``module:qualname`` so a
failure names exactly where to look.

Counter-test: a synthetic function that calls ``add_roles`` with no guard and
is not allowlisted IS flagged, and removing the guard from a real site (proved
by hand for the mute command - see the lot report) makes this same shape of
test fail. Keeps this detector from being a guard that only ever prints
silence.
"""

import ast
import pathlib

COGS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "cogs"

# (relative/path.py, qualname) pairs allowed to call add_roles with no
# refuse_dangerous_role guard in the same function - all self-service (a
# member's own click) or an admin's ad hoc, no stored six-surface role.
_ALLOWLIST = {
    ("config/reactionroles.py", "ReactionRoles.on_raw_reaction_add"),
    ("config/rolemenus.py", "RoleMenuSelect.callback"),
    ("config/buttonroles.py", "ButtonRoleButton.callback"),
    ("moderation/moderation.py", "Moderation.addrole"),
}


def _qualnames_calling_add_roles(path):
    """Yield ``(qualname, guarded)`` for every function in ``path`` that calls
    ``add_roles`` somewhere in its own body (nested functions included, since
    a guard written just above a nested call still shows up in the outer
    source text)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.found = []

        def _function(self, node):
            self.stack.append(node.name)
            qualname = ".".join(self.stack)
            calls_add_roles = any(
                isinstance(n, ast.Attribute) and n.attr == "add_roles"
                for n in ast.walk(node)
            )
            if calls_add_roles:
                source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                self.found.append((qualname, "refuse_dangerous_role" in source))
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
    for path in sorted(COGS_ROOT.rglob("*.py")):
        rel = str(path.relative_to(COGS_ROOT)).replace("\\", "/")
        for qualname, guarded in _qualnames_calling_add_roles(path):
            if guarded:
                continue
            if (rel, qualname) in _ALLOWLIST:
                continue
            unguarded.append(f"{rel}:{qualname}")
    assert not unguarded, (
        "add_roles call site(s) with no refuse_dangerous_role guard and not "
        "allowlisted (new six-surface apply site, or a self-service site "
        "that needs adding to _ALLOWLIST): " + ", ".join(unguarded)
    )


# ---------------------------------------------------------------------------
# Counter-test: the detector must actually flag something.
# ---------------------------------------------------------------------------
def test_guard_flags_a_synthetic_unguarded_site(tmp_path):
    synthetic = tmp_path / "synthetic.py"
    synthetic.write_text(
        "class Thing:\n"
        "    async def grant(self, member, role):\n"
        "        await member.add_roles(role)\n",
        encoding="utf-8",
    )
    found = _qualnames_calling_add_roles(synthetic)
    assert found == [("Thing.grant", False)]
