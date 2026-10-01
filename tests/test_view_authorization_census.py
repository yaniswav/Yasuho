"""Structural guard: no dispatch root may act for someone it never checked.

THE DEFECT THIS EXISTS FOR (fixed in 5397b1b). The voice-room panel's root view
(``cogs/config/rooms_panels.py``) checked the room owner on every click, but the
ephemeral sub-pickers it opened (one Select wrapped in a short-lived
``_RoomSubView``) and the rename modal they led to all derived from
``LocaleView`` / ``LocaleModal`` with no gate of their own. A click only ever
reaches a Select's callback through the ENCLOSING view's ``interaction_check``
for a plain item (``ui/view.py:591``), so the root's gate protected the first
click - but a sub-picker, once opened, is its own root and the root's gate was
never consulted again. A former room owner (kicked, or who transferred the room)
could still act through a picker left open for up to the modal's 15-minute
token. Nothing in the test suite caught it: ``tests/test_view_locale_hygiene.py``
only answers "does this root install the clicker's locale", never "may this
clicker act at all" - ``tools/views.py`` says as much (see its module docstring,
lines ~41-46): a missing access gate is not mechanically caught today, only a
missing locale is.

THE FIX SHAPE, now in the tree, is why the census below finds no repeat: the
root stayed ungated (an ephemeral sub-picker legitimately needs no author lock
of its own - it is reached only through an already-gated flow), but every
actionable callback downstream re-reads entitlement FRESH from the live request,
never from a value captured when the picker was opened
(``cogs/config/rooms_panels.py:98`` calls ``self._owner._still_owner
(interaction.user.id)`` inline for the slot select; the member-action select
delegates to ``_handle_member_action``, which does the same at ``:614``; the
rename modal's ``on_submit`` does the same at ``:179``). That is exactly the
CALLBACK_CHECKS / re-checked-GATED_FLOW_MODAL shape this file's categories are
built to recognise and tell apart from the bug shape (a stored id trusted
without re-reading it).

THE CENSUS. :func:`ungated_dispatch_roots` is the detector: given a set of real
classes, it reports every one whose own MRO installs no access gate - neither a
derivation from :class:`~tools.views.AuthorView` / ``AuthorLayoutView`` (whose
``interaction_check`` IS the gate) nor ANY repo-defined ancestor's own
``interaction_check`` that is a REAL gate. Two things never count as a gate:
the four locale-only bases below (which only resolve the clicker's locale and
gate nothing - see ``tools/views.py``'s module docstring), and - since this
audit - a repo class's OWN ``interaction_check`` written in that exact
locale-only SHAPE inline rather than inherited from one of those four bases
(:func:`_is_non_restrictive_check`; this closed a real blind spot, see below).
It runs on the real, imported classes, so inheritance, aliasing, mixins and
generics resolve exactly as Python itself resolves them for dispatch - more
reliable than a textual AST walk would be for the question of WHO may act;
the AST walk :func:`_is_non_restrictive_check` does is deliberately narrow -
answering only "does this one override's body add a restriction", never
"is this class gated" - so that reliability argument still holds. Library
bases (``discord.ui.View`` and friends) are deliberately excluded from the
ancestor walk: they always carry their own permissive ``interaction_check``
default, which must never count as a gate or every root would be "gated" by
discord.py itself and the detector would report nothing, always, including on
the real bug.

THE BLIND SPOT THIS AUDIT CLOSED. The detector originally treated ANY
repo-defined ``interaction_check`` override as a gate, full stop - so
``cogs/config/rooms_config.py``'s ``_HubManageView`` (whose own check is
exactly ``await i18n.apply_interaction_locale(interaction); return True``,
the same locale-only shape as the four bases, just written inline instead of
inherited) read as "gated" and never reached the audit below at all; worse,
the two modals it opens (``EditHubModal``, ``_RenameChannelsModal``) were
mis-cited as "opened only from AutoroomPanel" even though both call sites
(rooms_config.py:317 and :339) are inside ``_HubManageView``, not
``AutoroomPanel`` itself. :func:`_is_non_restrictive_check` fixes the
detector (see its own docstring for the exact recognised shapes, proven on
synthetic classes below); ``_HubManageView`` is now classified
GATED_FLOW_EPHEMERAL (see that category and its own entry), and both modal
entries now cite ``_HubManageView`` at the correct lines. The audit also
re-checked every OTHER "opened only from X (file:line)" citation in
:data:`CLASSIFIED` against the real source by grepping each cited line:
``feed_delivery.py:642`` (one line past the real ``send_modal`` call for
``_ReplyModal``) and ``rooms_panels.py:21``/``:179`` (the rename modal's
re-check is actually at ``:179``, and the member-action select's own
citation pointed at that SAME line, which is really inside the rename modal,
not ``_MemberActionSelect`` - its real re-check is in ``_handle_member_action``
at ``:614``) were wrong and are fixed; every other citation checked out
against the real file.

THE AUDIT. Every class the detector reports is looked up in :data:`CLASSIFIED`
(module.Class -> (CATEGORY, reason)) or, for anything that could not be
honestly justified, in :data:`PENDING` (reason prefixed ``"FINDING:"`` -
adjudicated by the orchestrator, not silenced here). Categories:

* PUBLIC - any clicker is fine; the reason cites why, and what bounds the
  effect (acts only on the clicker, or re-checks per action at a cited line).
* CALLBACK_CHECKS - every actionable callback re-reads entitlement itself; the
  reason cites each check's file:line.
* GATED_FLOW_MODAL - a modal only ever opened from an already-gated view or
  command. The reason says who can open it and whether the entitlement it acts
  on could move to someone ELSE before submit (a FINDING, unless re-checked) or
  can only be revoked from the opener (an accepted residual - the room bug's own
  shape, now closed by the re-check above).
* GATED_FLOW_EPHEMERAL - an ephemeral view only its gated opener can ever see
  (Discord delivers an ephemeral response only to the interaction's own user,
  so nobody else can click it regardless of this view's own check). The
  reason names what entitlement let the opener see it and whether that
  entitlement could move to someone ELSE while this view is still open (a
  FINDING for the orchestrator, unless re-checked downstream) or can only be
  revoked (an accepted residual, same as GATED_FLOW_MODAL's).
* DISPLAY_ONLY - no actionable component (text/media only, or only link
  buttons); a persistent-item child does not count, because dispatch never
  consults the enclosing root's check for one (the dynamic item is scanned, and
  classified, on its own).

Audited 2026-09-30, re-audited 2026-10-01 for the ``_is_non_restrictive_check``
blind spot above, against this worktree: 92 ungated roots (88 real classes plus
the four locale-only bases themselves, which install no gate by design and are
classified :data:`DISPLAY_ONLY` or ``GATED_FLOW_MODAL`` below for the same
reason their own real subclasses are - they carry no action of their own).
Zero findings: every one of the shapes the task brief names as typical
(a privileged action reachable by anyone who can see a public message; an
action on movable ownership with no action-time re-check; a modal reachable
from an ungated view performing a privileged write; a dynamic item trusting a
custom_id-encoded identity) was checked against the real callback bodies and
none were found, ``_HubManageView`` included (see THE BLIND SPOT above: its
entitlement cannot move to someone else, only be revoked). The two closest
calls are noted in-line where they occur (``AddSongModal``, ``_VibeSearchModal``):
a submit that does not re-check same-voice / same-author, accepted because the
entitlement there can only LAPSE (leaving voice, a timeout elsewhere), never
move to a different person, and the action is a cooperative, reversible room
action every other eligible clicker could equally take.
"""

import ast
import gc
import importlib
import inspect
import pathlib
import textwrap

import discord
from discord import ui

# ---------------------------------------------------------------------------
# (1) The detector - pure, aimable at constructed input
# ---------------------------------------------------------------------------


def _is_locale_apply_call(call) -> bool:
    """True for a call shaped like ``i18n.apply_interaction_locale(...)``."""
    return isinstance(call, ast.Call) and (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "apply_interaction_locale"
    )


def _is_super_check_call(call) -> bool:
    """True for a call shaped like ``super().interaction_check(...)``."""
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "interaction_check"
        and isinstance(call.func.value, ast.Call)
        and isinstance(call.func.value.func, ast.Name)
        and call.func.value.func.id == "super"
    )


def _is_non_restrictive_check(func) -> bool:
    """AST-only: does ``func``'s OWN body add any restriction of its own?

    Mirrors ``tools/views.py``'s locale-only bases (``LocaleView.interaction_check``
    is exactly ``await i18n.apply_interaction_locale(interaction); return True``)
    so a repo class that writes the SAME shape inline - rather than inheriting
    it, e.g. ``cogs/config/rooms_config.py``'s ``_HubManageView`` - is told
    apart from a class that writes a real gate. A non-restrictive body, after
    stripping a leading docstring, is:

    * zero or more bare statements, each EITHER
      ``await i18n.apply_interaction_locale(...)`` OR
      ``await super().interaction_check(...)`` (locale install / passthrough,
      the return value discarded), followed by
    * a tail statement that is EITHER ``return True`` OR
      ``return await super().interaction_check(...)`` (a pure tail
      delegation - whatever super decides is not a restriction THIS level
      adds; :func:`ungated_dispatch_roots` visits super separately in the
      same MRO walk, so a real gate further up is still found there).

    Anything else - a stored boolean, an ``if``, comparing
    ``interaction.user.id``, extra logic after the super call, a bare
    ``return`` with no value, ``return False`` - is a REAL gate: this is
    deliberately a body-shape check, not a dataflow one, and errs toward
    "real gate" for anything it does not recognise outright.
    """
    try:
        src = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        return False

    fn_def = tree.body[0] if tree.body else None
    if not isinstance(fn_def, (ast.AsyncFunctionDef, ast.FunctionDef)):
        return False

    body = fn_def.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring, it is not a restriction
    if not body:
        return False

    *lead, tail = body
    for stmt in lead:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Await):
            return False
        call = stmt.value.value
        if not (_is_locale_apply_call(call) or _is_super_check_call(call)):
            return False

    if not isinstance(tail, ast.Return):
        return False
    value = tail.value
    if isinstance(value, ast.Constant) and value.value is True:
        return True
    if isinstance(value, ast.Await) and _is_super_check_call(value.value):
        return True
    return False


def ungated_dispatch_roots(classes, *, locale_only_bases, repo_packages):
    """The subset of ``classes`` whose own MRO installs no access gate.

    A class counts as GATED when some ancestor in its MRO - other than one of
    ``locale_only_bases`` (whose ``interaction_check`` only resolves the
    clicker's locale and gates nothing, see ``tools/views.py``) - is itself
    defined in one of ``repo_packages`` (so a lirbary default never counts,
    see below), defines ``interaction_check`` in its OWN ``__dict__`` (an
    override, not an inherited default), AND that override is not itself
    :func:`_is_non_restrictive_check` - the same "installs the locale, gates
    nothing" shape the four ``locale_only_bases`` have, just written inline on
    a repo class instead of inherited from one (see that function's docstring
    for the exact shapes recognised, and why a non-restrictive override does
    not stop the MRO walk: whatever it delegates to is still checked).
    Everything else is ungated.

    ``repo_packages`` bounds what "defines a gate" can even mean: every one of
    the four discord.py dispatch roots (``View``, ``LayoutView``, ``Modal``,
    ``DynamicItem``) carries ITS OWN permissive ``interaction_check`` default
    in its ``__dict__`` (``DynamicItem``'s delegates to the wrapped item's,
    which is the same permissive default again) - without this filter every
    class would walk straight into that default, see it as "defined in
    __dict__", and report itself gated. The real repo bug this guard exists
    for would then be invisible by construction.
    """
    locale_only = set(locale_only_bases)
    ungated = set()
    for cls in classes:
        gated = False
        for ancestor in cls.__mro__:
            if ancestor in locale_only:
                continue
            if ancestor.__module__.split(".")[0] not in repo_packages:
                continue
            if "interaction_check" in ancestor.__dict__:
                if _is_non_restrictive_check(ancestor.__dict__["interaction_check"]):
                    continue  # locale-only passthrough written inline; not a gate
                gated = True
                break
        if not gated:
            ungated.add(cls)
    return ungated


# ---------------------------------------------------------------------------
# (2) Negative control: the detector is not vacuous, in either direction
# ---------------------------------------------------------------------------


def test_negative_control_reports_the_right_two_and_clears_the_third():
    """(a) and (c) must be reported; (b) must not be - proven on synthetic classes.

    ``_FakeLocaleBase`` stands in for a locale-only base (its check always
    returns True and gates nothing); ``_FakeGatedBase`` stands in for a real
    gate (``AuthorView``-shaped). ``repo_packages=(__name__,)`` treats this
    test module as "the repo" for the control, exactly mirroring how the real
    scan below treats ``cogs``/``tools`` as the repo.
    """

    class _FakeLocaleBase(discord.ui.View):
        async def interaction_check(self, interaction):
            return True  # installs a locale in the real base; gates nothing

    class _CaseA_UngatedLocaleSubclass(_FakeLocaleBase):
        """(a) An ungated LocaleView-shaped subclass: must be reported."""

    class _FakeGatedBase(discord.ui.View):
        async def interaction_check(self, interaction):
            return False  # a real, repo-defined gate

    class _CaseB_SubclassOfAGatedBase(_FakeGatedBase):
        """(b) Inherits a real gate from a repo base: must NOT be reported."""

    class _CaseC_OnlyInheritsTheLocaleCheck(_CaseA_UngatedLocaleSubclass):
        """(c) Two levels down from the locale-only base, still no gate of its
        own anywhere in between: must be reported."""

    try:
        result = ungated_dispatch_roots(
            [
                _CaseA_UngatedLocaleSubclass,
                _CaseB_SubclassOfAGatedBase,
                _CaseC_OnlyInheritsTheLocaleCheck,
            ],
            locale_only_bases={_FakeLocaleBase},
            repo_packages=(__name__.split(".")[0],),
        )
        assert _CaseA_UngatedLocaleSubclass in result
        assert _CaseC_OnlyInheritsTheLocaleCheck in result
        assert _CaseB_SubclassOfAGatedBase not in result
        assert result == {
            _CaseA_UngatedLocaleSubclass,
            _CaseC_OnlyInheritsTheLocaleCheck,
        }
    finally:
        del (
            _FakeLocaleBase,
            _CaseA_UngatedLocaleSubclass,
            _FakeGatedBase,
            _CaseB_SubclassOfAGatedBase,
            _CaseC_OnlyInheritsTheLocaleCheck,
        )
        gc.collect()


def test_negative_control_recognises_a_non_restrictive_own_override():
    """The EditHubModal/_HubManageView defect this fix closes, isolated: a
    class's OWN ``interaction_check`` (not inherited from any
    ``locale_only_bases`` - this test passes an empty set, so the only thing
    that can keep these out of the result is :func:`_is_non_restrictive_check`
    itself) that only installs the locale and returns ``True`` is NOT a gate
    and must be reported ungated; one with a real conditional ``False`` path
    is a gate and must not be.

    Uses the real ``tools.i18n.apply_interaction_locale`` so the call shape
    matches production exactly, rather than a same-named stand-in that would
    only prove the AST check matches ITS OWN fixture.
    """
    from tools import i18n

    class _CaseD_OwnLocaleOnlyCheck(discord.ui.View):
        """Own check, no locale_only_bases involved: must be reported."""

        async def interaction_check(self, interaction):
            await i18n.apply_interaction_locale(interaction)
            return True

    class _CaseE_OwnRealGate(discord.ui.View):
        """Own check with a real conditional False path: must not be."""

        async def interaction_check(self, interaction):
            if interaction.user.id != 999:
                return False
            return True

    try:
        result = ungated_dispatch_roots(
            [_CaseD_OwnLocaleOnlyCheck, _CaseE_OwnRealGate],
            locale_only_bases=set(),
            repo_packages=(__name__.split(".")[0],),
        )
        assert result == {_CaseD_OwnLocaleOnlyCheck}
    finally:
        del _CaseD_OwnLocaleOnlyCheck, _CaseE_OwnRealGate
        gc.collect()


def test_negative_control_tail_delegation_defers_to_the_super_it_chains_to():
    """``return await super().interaction_check(interaction)`` with no logic
    of its own is non-restrictive AT THAT LEVEL - but the MRO walk still
    visits whatever it delegates to, so a real gate further up is still
    found. Chaining onto a non-restrictive super stays ungated; chaining onto
    a real gate does not."""

    class _PermissiveSuper(discord.ui.View):
        async def interaction_check(self, interaction):
            return True

    class _CaseF_TailDelegatesToPermissiveSuper(_PermissiveSuper):
        async def interaction_check(self, interaction):
            return await super().interaction_check(interaction)

    class _StrictSuper(discord.ui.View):
        async def interaction_check(self, interaction):
            if interaction.user.id != 999:
                return False
            return True

    class _CaseG_TailDelegatesToARealGate(_StrictSuper):
        async def interaction_check(self, interaction):
            return await super().interaction_check(interaction)

    try:
        result = ungated_dispatch_roots(
            [_CaseF_TailDelegatesToPermissiveSuper, _CaseG_TailDelegatesToARealGate],
            locale_only_bases=set(),
            repo_packages=(__name__.split(".")[0],),
        )
        assert result == {_CaseF_TailDelegatesToPermissiveSuper}
    finally:
        del (
            _PermissiveSuper,
            _CaseF_TailDelegatesToPermissiveSuper,
            _StrictSuper,
            _CaseG_TailDelegatesToARealGate,
        )
        gc.collect()


def test_a_bare_view_with_no_bases_at_all_is_reported():
    """Anti-vacuity floor: a class with nothing in its MRO but the library
    default must come out ungated, so an empty result elsewhere is never just
    the filter matching nobody."""

    class _Bare(discord.ui.View):
        pass

    try:
        assert ungated_dispatch_roots(
            [_Bare], locale_only_bases=set(), repo_packages=(__name__.split(".")[0],)
        ) == {_Bare}
    finally:
        del _Bare
        gc.collect()


# ---------------------------------------------------------------------------
# (3) The real scan: every discord.ui dispatch root in cogs/ and tools/
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGET_PACKAGES = ("cogs", "tools")

# Deliberately not imported from tests/test_view_locale_hygiene.py: a guard
# that breaks when another guard's helpers are edited is a guard with a second
# way to go quiet. The ~20 lines below are the price of independence.


def _iter_target_modules():
    for pkg in _TARGET_PACKAGES:
        pkg_dir = _REPO_ROOT / pkg
        if not pkg_dir.is_dir():
            continue
        for path in sorted(pkg_dir.rglob("*.py")):
            parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                yield ".".join(parts)


def _all_subclasses(base):
    seen = set()
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


#: The four dispatch roots discord.py itself checks before a callback runs.
_ROOT_BASES = {
    "View": ui.View,
    "LayoutView": ui.LayoutView,
    "Modal": ui.Modal,
    "DynamicItem": ui.DynamicItem,
}

_scan_cache = None


def _scan():
    """(ungated_names -> cls, total_roots_walked, skipped). Computed once."""
    global _scan_cache
    if _scan_cache is not None:
        return _scan_cache

    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:  # optional dep absent, as elsewhere in the suite
            skipped.append((modname, str(exc)))

    all_roots = {}
    for base in _ROOT_BASES.values():
        for cls in _all_subclasses(base):
            if cls.__module__.split(".")[0] in _TARGET_PACKAGES:
                all_roots.setdefault(cls, None)

    from tools.views import (
        AuthorLayoutView,  # noqa: F401 (documents what already gates; not used directly)
        AuthorView,  # noqa: F401
        LocaleDynamicItem,
        LocaleLayoutView,
        LocaleModal,
        LocaleView,
    )

    locale_only = {LocaleView, LocaleLayoutView, LocaleModal, LocaleDynamicItem}
    ungated = ungated_dispatch_roots(
        all_roots.keys(), locale_only_bases=locale_only, repo_packages=_TARGET_PACKAGES
    )

    results = {f"{cls.__module__}.{cls.__qualname__}": cls for cls in ungated}
    gc.collect()
    _scan_cache = (results, len(all_roots), skipped)
    return _scan_cache


def test_the_scan_actually_covered_the_tree():
    """A collector that silently found nothing would pass every assertion below.

    169 dispatch roots live in cogs/ + tools/ as of this audit (the same
    universe tests/test_view_locale_hygiene.py walks), so a floor of 150 catches
    a collapsed import or a renamed base without tripping on ordinary growth.
    The brief's own floor (50) is implied by the stronger one asserted here.
    """
    results, total_roots, skipped = _scan()
    assert total_roots >= 150, (total_roots, skipped)
    assert total_roots > 50, total_roots
    # Landmarks: the exact bug class (now fixed, so classified not findings)
    # and one class per dispatch kind, so a filter that stopped matching a
    # whole kind cannot pass silently.
    for name in (
        "cogs.config.rooms_panels._RoomSubView",  # the View the bug lived in
        "cogs.config.rooms_panels._RoomRenameModal",  # the Modal the bug lived in
        "cogs.anilist.feed_render.ActivityCard",  # a LayoutView
        "cogs.config.tickets.lifecycle.TicketClaimButton",  # a DynamicItem
    ):
        assert name in results, (name, "not reached by the scan")


# ---------------------------------------------------------------------------
# (4) The audit: every ungated class, classified
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {
    "PUBLIC",
    "CALLBACK_CHECKS",
    "GATED_FLOW_MODAL",
    "GATED_FLOW_EPHEMERAL",
    "DISPLAY_ONLY",
}

#: ``"module.Class" -> (CATEGORY, "why")``. Every class the scan reports must be
#: a key here or in :data:`PENDING`; see the module docstring for what each
#: category means and the file header's audit note for the two accepted
#: residuals. File:line citations below point at this worktree.
CLASSIFIED: dict[str, tuple[str, str]] = {
    # ---- AniList ----------------------------------------------------------
    "cogs.anilist.account.AniListProfileView": (
        "DISPLAY_ONLY",
        "Only Containers/TextDisplay/MediaGallery are added in _build; no "
        "button or select exists on the class, so its check is never called.",
    ),
    "cogs.anilist.airing.AiringCard": (
        "DISPLAY_ONLY",
        "Its own _build adds only a link Button plus a child AiringSeenButton "
        "(a DynamicItem, dispatched without ever consulting this view's check "
        "- see AiringSeenButton's own entry); the card itself has no "
        "actionable component.",
    ),
    "cogs.anilist.airing.AiringSeenButton": (
        "PUBLIC",
        "callback (airing.py:417-418) calls _run_seen, which resolves the "
        "CLICKER's own AniList token (airing.py:296, interaction.user) and "
        "only ever advances that same user's progress; media_id/episode "
        "riding in the custom_id are public content ids, not another user's "
        "identity.",
    ),
    "cogs.anilist.chapters.ChapterCard": (
        "DISPLAY_ONLY",
        "Same shape as AiringCard: _build adds only a link Button plus a "
        "child ChapterReadButton (a DynamicItem, see its own entry); no "
        "actionable component belongs to this class.",
    ),
    "cogs.anilist.chapters.ChapterReadButton": (
        "PUBLIC",
        "callback (chapters.py:636-637) calls _run_read, which resolves the "
        "CLICKER's own token (chapters.py:504, interaction.user) and only "
        "advances that user's own progress; media_id/chapter in the custom_id "
        "are content ids, not a trusted identity.",
    ),
    "cogs.anilist.edit_forms.EditEntryModal": (
        "GATED_FLOW_MODAL",
        "Opened from _ConfigureEntryView.configure (ephemeral, gated by "
        "visibility - see that entry) and from the AniListFeedPanel/hub "
        "flows, all author-gated. on_submit resolves the SUBMITTER's own "
        "AniList token via self.cog._get_token(interaction.user.id) "
        "(edit_forms.py, never a stored id), so there is no entitlement to "
        "go stale: whoever submits only ever edits their own list entry.",
    ),
    "cogs.anilist.feed_delivery._ConfigureEntryView": (
        "PUBLIC",
        "Sent only as an ephemeral followup to the user who just added the "
        "entry (feed_delivery.py:613-618, ephemeral=True) - no one else can "
        "ever see or click it. Its one button opens EditEntryModal, which is "
        "itself keyed to the clicker via interaction.user.id at submit time.",
    ),
    "cogs.anilist.feed_delivery._ReplyModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _run_reply (feed_delivery.py:641, after a debounce "
        "and a token pre-check); on_submit re-resolves the SUBMITTER's own "
        "token fresh (_resolve_token(interaction)) and posts the reply as "
        "that same user, so there is no other user's entitlement involved.",
    ),
    "cogs.anilist.feed_render.ActivityCard": (
        "DISPLAY_ONLY",
        "_add_action_row adds only a link Button plus the persistent "
        "FeedLikeButton/FeedReplyButton/FeedAddButton DynamicItems (each "
        "classified on its own); the LayoutView itself has no actionable "
        "component of its own.",
    ),
    "cogs.anilist.feed_render.ActivityDigest": (
        "DISPLAY_ONLY",
        "Docstring states it directly: 'Purely presentational (no "
        "interactive components)'; _build adds only TextDisplay/Separator.",
    ),
    "cogs.anilist.feed_views.AddFollowModal": (
        "GATED_FLOW_MODAL",
        "Opened only by _AddFollowButton.callback (feed_views.py:1104), a "
        "child Button of AniListFeedPanel whose own interaction_check "
        "(feed_views.py:1196-1204) gates to the panel's author_id and runs "
        "BEFORE a plain Button's callback; the residual (author's admin "
        "permission revoked between click and submit) is accepted, not a "
        "transfer of entitlement.",
    ),
    "cogs.anilist.feed_views.FeedAddButton": (
        "PUBLIC",
        "callback delegates to _run_add (feed_delivery.py), which mirrors "
        "_run_like exactly: resolves the CLICKER's own token and adds the "
        "media to THEIR OWN planning list; media_id is a public content id.",
    ),
    "cogs.anilist.feed_views.FeedLikeButton": (
        "PUBLIC",
        "callback -> _run_like (feed_delivery.py:387-449): resolves the "
        "clicker's own token and toggles THEIR OWN like on the activity; "
        "activity_id is a public content id, not a trusted user identity.",
    ),
    "cogs.anilist.feed_views.FeedReplyButton": (
        "PUBLIC",
        "callback -> _run_reply (feed_delivery.py:624-642): same-user token "
        "resolution before opening _ReplyModal (see that entry); acts only "
        "on the clicker's own account.",
    ),
    "cogs.anilist.feed_views._FeedListView": (
        "DISPLAY_ONLY",
        "Docstring: 'Non-interactive, so no gating or timeout.' __init__ adds "
        "only TextDisplay/Separator inside one Container.",
    ),
    "cogs.anilist.feed_views._FeedNoticeView": (
        "DISPLAY_ONLY",
        "Docstring: 'Carries no components, so it needs no author gating.' "
        "__init__ adds only a TextDisplay.",
    ),
    "cogs.anilist.feed_views._TrackTitleModal": (
        "GATED_FLOW_MODAL",
        "Opened only from the track button inside _SubsManagerView "
        "(feed_views.py:473), which is an AuthorLayoutView; on_submit edits "
        "the manager's own message via self.manager.run_search, no other "
        "user's data is touched.",
    ),
    "cogs.anilist.hub.HubSearchModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _HubSearchButton (hub.py:97), a child of AniListHub "
        "whose own interaction_check (hub.py:279) gates to its author; the "
        "action is a read-only AniList search that posts a new public "
        "message, so even a stale open-vs-submit gap touches no one's data.",
    ),
    "cogs.anilist.login.LoginModal": (
        "GATED_FLOW_MODAL",
        "Opened only from the /anilist login command for that same "
        "interaction's user; self.author_id is always interaction.user.id of "
        "whoever ran the command, and Discord guarantees only that user can "
        "ever submit a modal shown to them - there is no other identity in "
        "play to go stale.",
    ),
    "cogs.anilist.lookup.CharacterCard": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay/Thumbnail plus one link Button; no "
        "actionable component.",
    ),
    "cogs.anilist.lookup.StudioCard": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay plus one link Button; no actionable "
        "component.",
    ),
    # ---- Community ---------------------------------------------------------
    "cogs.community.leveling.level_admin._ResetAllModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _ResetAllView's danger button (level_admin.py), an "
        "AuthorView; the residual (admin permission revoked in the 15-minute "
        "window) is accepted, matching the task brief's own carve-out.",
    ),
    "cogs.community.leveling.level_config_ui.LevelConfigOverviewView": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay/Separator; no button or select exists.",
    ),
    "cogs.community.leveling.level_config_ui.MultiplierListView": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay/Separator; no actionable component.",
    ),
    "cogs.community.leveling.level_config_ui.NoXpListView": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay/Separator; no actionable component.",
    ),
    "cogs.community.leveling.level_config_ui._RankAccentModal": (
        "GATED_FLOW_MODAL",
        "Opened only from RankCardPanel (level_config_ui.py:390), an "
        "AuthorLayoutView; same accepted residual as the other admin-panel "
        "modals (permission revocation, not an ownership transfer).",
    ),
    "cogs.community.leveling.level_rewards.LevelRewardsListView": (
        "DISPLAY_ONLY",
        "_build adds only TextDisplay; no actionable component.",
    ),
    "cogs.community.profile.views.ProfileCard": (
        "DISPLAY_ONLY",
        "Docstring: 'not author-gated: there is nothing to interact WITH (no "
        "button, no select)'; _build adds only display components.",
    ),
    "cogs.community.profile.views.ProfileEditModal": (
        "GATED_FLOW_MODAL",
        "Opened from AuthorView/AuthorLayoutView buttons AND directly from "
        "the /profile edit command; on_submit always writes through "
        "self.cog.apply_field(interaction.user.id, ...) - the SUBMITTER's own "
        "field, never a stored id - so no entitlement can go stale.",
    ),
    "cogs.community.reminders.RemindModal": (
        "GATED_FLOW_MODAL",
        "Opened directly from the /remind command (self.author_id = "
        "ctx.author.id) and from an AuthorView button (self.author_id = that "
        "same author); the reminder is always created FOR self.author_id, "
        "which Discord guarantees equals the submitter.",
    ),
    "cogs.community.votes.VoteStatusView": (
        "DISPLAY_ONLY",
        "Docstring: 'Its one control is a link button, which Discord opens "
        "client-side without ever sending the bot an interaction.'",
    ),
    # ---- Config -------------------------------------------------------------
    "cogs.config.announcements.ScheduleModal": (
        "GATED_FLOW_MODAL",
        "Opened only from AnnouncePanel (announcements.py:206), an "
        "AuthorView; accepted revocation-only residual.",
    ),
    "cogs.config.buttonroles.AddButtonModal": (
        "GATED_FLOW_MODAL",
        "Opened only from BuilderView (buttonroles.py:437,619), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    "cogs.config.buttonroles.AttachModal": (
        "GATED_FLOW_MODAL",
        "Opened only from BuilderView (buttonroles.py:398); same residual as "
        "AddButtonModal.",
    ),
    "cogs.config.buttonroles.ButtonRoleView": (
        "PUBLIC",
        "The canonical self-role panel: ButtonRoleButton.callback "
        "(buttonroles.py:103-131) acts only on member = interaction.user, "
        "adding/removing the role from the CLICKER and nobody else.",
    ),
    "cogs.config.buttonroles._DoneView": (
        "DISPLAY_ONLY",
        "Docstring: 'a single heading-over-body Container with no "
        "components, so it needs no author gating.'",
    ),
    "cogs.config.customcommands.AddEmbedNameModal": (
        "GATED_FLOW_MODAL",
        "Opened only from CustomCommandsPanel (customcommands.py:456,519), an "
        "AuthorView; accepted revocation-only residual.",
    ),
    "cogs.config.customcommands.AddTextModal": (
        "GATED_FLOW_MODAL",
        "Opened only from CustomCommandsPanel (customcommands.py:440); same "
        "residual as AddEmbedNameModal.",
    ),
    "cogs.config.customcommands.EditTextModal": (
        "GATED_FLOW_MODAL",
        "Opened only from CustomCommandsPanel (customcommands.py:494); same "
        "residual as AddEmbedNameModal.",
    ),
    "cogs.config.reactionroles.AddReactionRoleModal": (
        "GATED_FLOW_MODAL",
        "Opened only from AddReactionRoleView (an AuthorView, "
        "reactionroles.py:128,144) or the manage_roles-gated /reactionrole "
        "command (reactionroles.py:234-237); on_submit ALSO re-checks the "
        "submitter's own role hierarchy fresh via "
        "modchecks.self_assignable_role_error(interaction.user, ...) "
        "(reactionroles.py:52-57), so even a stale permission is re-verified "
        "at submit, not just at open.",
    ),
    "cogs.config.rolemenus.HeaderModal": (
        "GATED_FLOW_MODAL",
        "Opened only from RoleMenuBuilder (rolemenus.py:510,543), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    "cogs.config.rolemenus.RoleMenuView": (
        "PUBLIC",
        "The public self-role dropdown; RoleMenuSelect.callback "
        "(rolemenus.py:106-121) acts only on member = interaction.user, "
        "granting/removing roles for the CLICKER only (deferred before the "
        "per-role loop, but still scoped to that same member throughout).",
    ),
    "cogs.config.rolemenus.RoleOptionModal": (
        "GATED_FLOW_MODAL",
        "Opened only from RoleMenuBuilder (rolemenus.py:370); same residual "
        "as HeaderModal.",
    ),
    "cogs.config.rooms_config.AddHubModal": (
        "GATED_FLOW_MODAL",
        "Opened only from AutoroomPanel (rooms_config.py:495), which defines "
        "its own interaction_check (rooms_config.py:449) restricted to its "
        "author; accepted revocation-only residual.",
    ),
    "cogs.config.rooms_config.EditHubModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _HubManageView._on_settings (rooms_config.py:317) "
        "- see that class's own GATED_FLOW_EPHEMERAL entry for why it needs "
        "no gate: it is itself only ever shown, ephemeral, to AutoroomPanel's "
        "already-gated author. Same accepted revocation-only residual as "
        "AddHubModal.",
    ),
    "cogs.config.rooms_config._RenameChannelsModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _HubManageView._on_rename (rooms_config.py:339) - "
        "same reach and residual as EditHubModal above.",
    ),
    "cogs.config.rooms_config._HubManageView": (
        "GATED_FLOW_EPHEMERAL",
        "Sent only as an ephemeral followup to AutoroomPanel's author "
        "(rooms_config.py:514, ephemeral=True, from _on_edit which "
        "AutoroomPanel's own interaction_check already gated - "
        "rooms_config.py:449-458) - no one else can ever see or click it, so "
        "its own interaction_check only installs the locale (rooms_config.py:"
        "311-313). The entitlement is 'being this AutoroomPanel's author', "
        "which cannot move to someone ELSE the way the room-panel bug's "
        "ownership could (it is not a claimable/transferable resource, just "
        "whoever ran /autorooms); it can only be REVOKED (manage_guild taken "
        "away between opening this view and submitting EditHubModal/"
        "_RenameChannelsModal) - the same accepted residual every other "
        "admin-panel modal in this file carries, not a transfer, so not a "
        "FINDING.",
    ),
    "cogs.config.rooms_panels._RoomRenameModal": (
        "GATED_FLOW_MODAL",
        "THE FIXED BUG CLASS. Opened from an ephemeral _RoomSubView wrapping "
        "a rename-launching control with no gate of its own; on_submit "
        "re-reads entitlement FRESH at rooms_panels.py:179 "
        "(self._owner._still_owner(interaction.user.id)) rather than trusting "
        "a value captured at open time - exactly closing the gap the "
        "original bug left open (the entitlement, room ownership, CAN move "
        "to someone else via claim/transfer, and now is re-checked for it).",
    ),
    "cogs.config.rooms_panels._RoomSubView": (
        "CALLBACK_CHECKS",
        "Generic one-item ephemeral wrapper with no gate of its own; both "
        "items it is ever built with re-check live ownership themselves: "
        "_SlotSelect.callback re-checks inline at rooms_panels.py:98, and "
        "_MemberActionSelect.callback (rooms_panels.py:146) delegates to "
        "self._owner._handle_member_action (rooms_panels.py:148), which "
        "itself re-checks FRESH at rooms_panels.py:614 - each calling "
        "self._owner._still_owner(interaction.user.id) before acting. "
        "Confirmed exhaustively: rooms_panels.py:436,512,532,555 are the only "
        "four call sites that construct it, and all four wrap one of those "
        "two items.",
    ),
    "cogs.config.starboard.StarboardSetModal": (
        "GATED_FLOW_MODAL",
        "Opened only from StarboardSetView (an AuthorView, starboard.py:224) "
        "or directly from the manage_guild-gated /starboard set command "
        "(starboard.py:298-314); accepted revocation-only residual.",
    ),
    "cogs.config.tickets.lifecycle.TicketClaimButton": (
        "CALLBACK_CHECKS",
        "callback -> _run_claim, which re-checks is_support(member, pool) "
        "FRESH at lifecycle.py:362 (live DB read via settings, not a cached "
        "or custom_id-encoded role); thread_id in the custom_id is a content "
        "id, never trusted as an identity.",
    ),
    "cogs.config.tickets.lifecycle.TicketCloseButton": (
        "CALLBACK_CHECKS",
        "callback -> _run_close_click, which re-checks "
        "member.id == row['opener_id'] or is_support(member, pool) FRESH at "
        "lifecycle.py:477 against a live DB read of the ticket row, never a "
        "stale or custom_id-encoded value.",
    ),
    "cogs.config.tickets.lifecycle.TicketControlsView": (
        "DISPLAY_ONLY",
        "Its only children are TicketClaimButton / TicketCloseButton, both "
        "persistent DynamicItems (classified on their own); dispatch never "
        "consults this view's own check for a dynamic item's click, so it "
        "has no actionable component of its own.",
    ),
    "cogs.config.tickets.open.TicketPanelView": (
        "PUBLIC",
        "Hosts only the public 'Open a ticket' button (open.py:155-170, a "
        "plain non-dynamic Button, so this view's check DOES gate it - "
        "deliberately permissive); the button's own callback acts on "
        "member = interaction.user, creating a ticket for the CLICKER.",
    ),
    "cogs.config.tickets.open.TicketSubjectModal": (
        "GATED_FLOW_MODAL",
        "Opened only from the public TicketOpenButton (see TicketPanelView); "
        "on_submit creates a NEW ticket for whoever submits - every member "
        "is equally entitled to open one, so there is no prior entitlement "
        "to go stale.",
    ),
    "cogs.config.tickets.panel.TicketStatusView": (
        "DISPLAY_ONLY",
        "Docstring: 'Read-only, no controls.' _build adds only TextDisplay.",
    ),
    "cogs.config.tickets.panel._PanelMessageModal": (
        "GATED_FLOW_MODAL",
        "Opened only from TicketConfigPanel (panel.py:403,419), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    "cogs.config.twitch.MessageModal": (
        "GATED_FLOW_MODAL",
        "Opened only from TwitchPanel (twitch.py:246,361), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    "cogs.config.verification.VerifyStatusView": (
        "DISPLAY_ONLY",
        "Docstring: 'read-only, no controls'; _build adds only TextDisplay.",
    ),
    "cogs.config.verification.VerifyView": (
        "PUBLIC",
        "Hosts the public Verify button; VerifyButton.callback "
        "(verification.py:38-56) acts only on member = interaction.user, "
        "granting the role to the CLICKER.",
    ),
    "cogs.config.welcome.AddGifModal": (
        "GATED_FLOW_MODAL",
        "Opened only from ManageGifsView (welcome.py:274,282), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    "cogs.config.welcome.WelcomeStatusView": (
        "DISPLAY_ONLY",
        "Docstring: 'read-only, no controls'; _build adds only TextDisplay.",
    ),
    # ---- Moderation ---------------------------------------------------------
    "cogs.moderation.moderation.NewUsersView": (
        "DISPLAY_ONLY",
        "_build adds only Section/TextDisplay/Thumbnail per member; no "
        "button or select.",
    ),
    "cogs.moderation.moderation.ReasonEditModal": (
        "GATED_FLOW_MODAL",
        "Opened only from the manage_messages-gated 'reason' command "
        "(moderation.py:1340,1363, @commands.has_permissions); accepted "
        "revocation-only residual.",
    ),
    "cogs.moderation.modlog.ModLogStatusView": (
        "DISPLAY_ONLY",
        "Docstring: 'read-only, no controls'; _build adds only TextDisplay.",
    ),
    "cogs.moderation.warn_config._AddRuleModal": (
        "GATED_FLOW_MODAL",
        "Opened only from WarnConfigPanel (warn_config.py:220,343), an "
        "AuthorLayoutView; accepted revocation-only residual.",
    ),
    # ---- Music --------------------------------------------------------------
    "cogs.music.lyrics.StaticLyricsCard": (
        "PUBLIC",
        "Docstring: 'Ephemeral, so only the invoker can see or click it.' "
        "Pagination (_prev/_next) changes nothing but the local page index; "
        "the one privileged action (_follow, lyrics.py:114-120) re-checks "
        "_in_players_voice(self.player, interaction.user) before doing "
        "anything.",
    ),
    "cogs.music.lyrics.SyncedLyricsCard": (
        "PUBLIC",
        "Docstring: 'any listener in the voice channel may stop it.' Every "
        "actionable button delegates to stop_from_interaction "
        "(lyrics.py:1146-1155) or shift_offset_from_interaction "
        "(lyrics.py:1157-1177), both of which re-check "
        "_in_players_voice(self.player, interaction.user) FIRST, on every "
        "click, not just at open.",
    ),
    "cogs.music.playlists_shared._PlaylistListCard": (
        "DISPLAY_ONLY",
        "Docstring: 'No interactive items, so no author gate'; __init__ adds "
        "only TextDisplay.",
    ),
    "cogs.music.views.AddSongModal": (
        "GATED_FLOW_MODAL",
        "Opened only from MusicController._add_song (views.py:1086) and "
        "QueueView._add (views.py:1699), both reached only through those "
        "views' own same-voice-gated interaction_check. on_submit does not "
        "re-check same-voice at submit; CLOSE CALL, accepted because the "
        "action is a cooperative, reversible write to a SHARED room queue "
        "(not another user's private data) and the gating condition can only "
        "LAPSE (leaving voice), never move to a different, non-eligible "
        "person - the shape the brief calls an accepted residual, not a "
        "transfer. track.extras.requester is set from interaction.user.id, "
        "so no identity is spoofed either.",
    ),
    "cogs.music.views.EffectsView": (
        "PUBLIC",
        "Docstring: 'needs no author gate (only the clicker sees it)... The "
        "select re-checks same-voice before applying.' Confirmed at "
        "views.py:1230-1231: _EffectsSelect.callback calls "
        "_ensure_in_voice then _ensure_can_control before doing anything.",
    ),
    "cogs.music.views._FavouriteActions": (
        "PUBLIC",
        "Docstring: 'the panel is ephemeral on an author-gated card, so only "
        "the owner of the list ever sees it' (sent ephemeral=True at "
        "views.py:2463-2468, from a FavouritesCard whose own author_id gated "
        "the click that opened it). Both actions operate on "
        "self._owner.author_id (the card's fixed owner, never "
        "interaction.user, and favourites are not a resource whose ownership "
        "can move to someone else).",
    ),
    "cogs.music.views._VibeSearchModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _VibeSearchButton on VibeCard (views.py:2649), an "
        "AuthorLayoutView; self.author_id is captured at open time and not "
        "re-checked at submit, but Discord guarantees the submitter IS the "
        "same user who was shown the modal (the author who passed the "
        "card's gate), and nothing here is a claimable/transferable "
        "resource - the same accepted-residual shape as AddSongModal.",
    ),
    "cogs.music.voteskip.SkipVoteView": (
        "PUBLIC",
        "Docstring: 'Multi-user by nature (any listener in the channel may "
        "vote)'; _VoteButton.callback (voteskip.py:219-227) re-checks "
        "_in_players_voice(vote.player, interaction.user) on every click "
        "before recording a vote.",
    ),
    # ---- System / Utility ----------------------------------------------------
    "cogs.system.arg_completion._CompletionModal": (
        "GATED_FLOW_MODAL",
        "Opened only from _CompletionView (arg_completion.py:252,607), an "
        "AuthorView; accepted revocation-only residual.",
    ),
    "cogs.system.help._HelpCard": (
        "DISPLAY_ONLY",
        "Docstring: 'Purely presentational (no interactive components)'.",
    ),
    "cogs.system.onboarding.OnboardingCardView": (
        "DISPLAY_ONLY",
        "Docstring: 'read-only, no controls'; _build adds only TextDisplay.",
    ),
    "cogs.utility.info.UserInfoView": (
        "DISPLAY_ONLY",
        "__init__ adds only a Section/TextDisplay/Thumbnail and an optional "
        "MediaGallery; no button or select.",
    ),
    "cogs.utility.meta.WeatherView": (
        "DISPLAY_ONLY",
        "Docstring: 'display-only, so it carries no interactive components'.",
    ),
    "cogs.utility.utility.QuickPollModal": (
        "GATED_FLOW_MODAL",
        "Opened only from QuickPollLauncher (an AuthorView, utility.py:88-102) "
        "or directly from the /quickpoll command (utility.py:349); the "
        "action is sending a new public native poll, no privileged write to "
        "anyone's data.",
    ),
    # ---- tools/embed_creator.py: one generic modal family, five gated hosts --
    "tools.embed_creator.AddFieldModal": (
        "GATED_FLOW_MODAL",
        "Reached only through _EditSelect (embed_creator.py:703-722), a "
        "Select embedded in a 'host' panel. Every in-tree host is "
        "AuthorView/AuthorLayoutView-gated (CustomEmbedPanel, "
        "CustomCommandsPanel, BuilderView, TwitchPanel, AnnouncePanel, "
        "WelcomePanel - verified via grep for every make_edit_select/"
        "_EditSelect call site in cogs/); accepted revocation-only residual.",
    ),
    "tools.embed_creator.AssetModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach (embed_creator.py:712,717) and same gated "
        "hosts as AddFieldModal.",
    ),
    "tools.embed_creator.AuthorModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach and same gated hosts as AddFieldModal.",
    ),
    "tools.embed_creator.ColourModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach and same gated hosts as AddFieldModal.",
    ),
    "tools.embed_creator.DescriptionModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach and same gated hosts as AddFieldModal.",
    ),
    "tools.embed_creator.FooterModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach and same gated hosts as AddFieldModal.",
    ),
    "tools.embed_creator.TitleModal": (
        "GATED_FLOW_MODAL",
        "Same _EditSelect reach and same gated hosts as AddFieldModal.",
    ),
    "tools.embed_creator._EmbedModal": (
        "GATED_FLOW_MODAL",
        "The shared base of the seven modals above; never instantiated "
        "directly (it carries no text input of its own to submit), so it "
        "has no action beyond the ones its real subclasses are each "
        "classified for above.",
    ),
    # ---- tools/views.py: the four locale-only bases themselves ------------
    # These are dispatch roots in their own right (each subclasses one of the
    # four discord.py bases) and install no gate by design - that IS the
    # point of the module, see its docstring. None is ever sent with an
    # actionable component of its own; every real consumer either adds one
    # and is classified above, or is itself a plain display card.
    "tools.views.LocaleView": (
        "DISPLAY_ONLY",
        "Base class; its own interaction_check only resolves the locale "
        "(tools/views.py) and it carries no component of its own to act on.",
    ),
    "tools.views.LocaleLayoutView": (
        "DISPLAY_ONLY",
        "Base class; same shape as LocaleView for the LayoutView family.",
    ),
    "tools.views.LocaleModal": (
        "GATED_FLOW_MODAL",
        "Base class; its own on_submit is the inert discord.py default (no "
        "body - see discord.ui.Modal.on_submit), so instantiating it bare "
        "and submitting does nothing. Every real submit path belongs to an "
        "audited subclass above.",
    ),
    "tools.views.LocaleDynamicItem": (
        "DISPLAY_ONLY",
        "Base class; its own module docstring states the point directly: "
        "its template is '(?!)', which can never match a real custom_id, so "
        "this class can never be dispatched to by a real interaction. Every "
        "real subclass supplies its own template and is audited above.",
    ),
}

#: Classes the audit could NOT honestly justify. Empty today (see the module
#: docstring's audit note) - kept as a dict, not a list, so the orchestrator
#: can adjudicate each one by key exactly like CLASSIFIED. Every value here
#: MUST start with "FINDING:" (see test_pending_entries_are_marked_as_findings):
#: that prefix is what stops this dict from quietly becoming a second,
#: unlabelled CLASSIFIED.
PENDING: dict[str, tuple[str, str]] = {}


def test_every_classified_category_is_a_valid_one():
    for key, (category, _reason) in CLASSIFIED.items():
        assert category in _VALID_CATEGORIES, (key, category)
    for key, (category, _reason) in PENDING.items():
        assert category in _VALID_CATEGORIES, (key, category)


def test_every_reason_is_a_non_empty_sentence():
    for key, (_category, reason) in {**CLASSIFIED, **PENDING}.items():
        assert isinstance(reason, str) and reason.strip(), (key, "blank reason")
        # "a sentence": more than one bare word, so a lazy one-word placeholder
        # like "fine" or "ok" cannot pass as a justification.
        assert len(reason.strip().split()) >= 4, (key, reason)


def test_pending_entries_are_marked_as_findings():
    for key, (_category, reason) in PENDING.items():
        assert reason.startswith("FINDING:"), (
            key,
            "PENDING entries must be prefixed 'FINDING:' so this dict can "
            "never silently double as an unlabelled CLASSIFIED",
        )


def test_no_classified_or_pending_key_is_a_duplicate():
    overlap = sorted(set(CLASSIFIED) & set(PENDING))
    assert not overlap, ("classified in both dicts", overlap)


# ---------------------------------------------------------------------------
# (5) Integration: the real scan, judged against the real audit
# ---------------------------------------------------------------------------


def test_every_ungated_class_the_scan_finds_is_classified():
    """The guard: a new ungated dispatch root must be looked at, not ignored.

    Adding a View/LayoutView/Modal/DynamicItem in cogs/ or tools/ with no
    access gate of its own, and no entry here or in PENDING, fails this with
    the class spelled out - exactly the shape the room-panel bug would have
    hit had this guard existed before 5397b1b.
    """
    results, _total, _skipped = _scan()
    judged = set(CLASSIFIED) | set(PENDING)
    missing = sorted(set(results) - judged)
    assert not missing, (
        "these ungated dispatch roots are not in CLASSIFIED or PENDING - audit "
        "them (see the module docstring for the four categories) before "
        "declaring this safe:\n  " + "\n  ".join(missing)
    )


def test_no_classified_or_pending_entry_is_stale():
    """A class that was fixed (now gated), renamed, or deleted must lose its
    entry in the same change - otherwise this dict slowly turns into a list
    of blanket passes for classes nobody has looked at since."""
    results, _total, _skipped = _scan()
    stale = sorted(k for k in {**CLASSIFIED, **PENDING} if k not in results)
    assert not stale, (
        "these entries no longer name an ungated class (it is now gated, "
        "renamed, or gone) - delete the entry, a dead one is a silent pass "
        "waiting for the name to be reused:\n  " + "\n  ".join(stale)
    )
