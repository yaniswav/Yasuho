"""A crashing component/modal/dynamic-item must tell the user, with an id.

THE DEFECT THIS EXISTS FOR. ``grep -rn "async def on_error" cogs/ tools/`` found
nothing: every ``discord.ui.View``, ``LayoutView`` and ``Modal`` subclass in the
tree relied on discord.py's own default ``on_error``, which only logs "Ignoring
exception in view/modal ..." (``discord.py`` 2.7.1, ``ui/view.py`` /
``ui/modal.py``) and never answers the interaction. A crashing button, select or
modal submit therefore left the clicker on Discord's own "This interaction
failed", with no id to report and nothing in the log pointing back to them - the
exact failure mode ``cogs/system/errors.py``'s ``_safe_send`` ladder already
solved on the COMMAND side (see that module's docstring for the prod incident
behind it).

THE FIX, under test here. :class:`tools.views.LocaleView`,
:class:`LocaleLayoutView` and :class:`LocaleModal` now route a crash through
:func:`tools.interactions.report_component_error`: one body, shared (a mixin for
the two View-family bases, which share the exact ``on_error(self, interaction,
error, item)`` signature off ``BaseView``; a direct override on
:class:`LocaleModal`, whose ``on_error(self, interaction, error)`` carries no
``item``). It logs one ERROR line with an 8-hex id, tells the user ephemerally
with the SAME id, and never raises - even if the notify itself fails.

:class:`LocaleDynamicItem` is different: discord.py NEVER calls ``on_error`` for
a dynamic item (``ui/item.py``'s ``Item.interaction_check`` docstring says so
outright, and ``ViewStore.schedule_dynamic_item_call`` in ``ui/view.py`` proves
it two ways - ``item.callback(interaction)`` inside its OWN try/except that only
``_log.exception``s, AND ``allow = await item.interaction_check(interaction)``
inside a SEPARATE bare try/except that does not even log, just silently denies
- with no hook anywhere on either path). There is no ``on_error`` to add, so
``LocaleDynamicItem.__init_subclass__`` wraps BOTH of the subclass's own hooks
instead, at class-definition time, with the identical try/except/report body
(one shared helper, :func:`tools.views._wrap_dynamic_item_hook`): ``callback``
never re-raises, and a raising ``interaction_check`` reports then returns
``False`` (deny) instead of propagating - the same fail-closed result the
library's own silent swallow already produced, just no longer invisible.

STRUCTURE OF THIS FILE.

1. Pin the discord.py facts the fix is built on (signatures, and the "no
   on_error for DynamicItem" contract, for both the callback AND the
   interaction_check try/except) - so a discord.py upgrade that changes either
   fails loudly here instead of silently.
2. Behavioural: for each of the four bases (the fourth split into its two
   wrapped hooks), a raising callback/on_submit/interaction_check produces
   exactly one ERROR log record and exactly one ephemeral reply, carrying the
   SAME id - parametrized over the already-responded fork too
   (``response.is_done()`` True routes through ``followup.send`` instead of
   ``response.send_message``, but it is still exactly one reply).
3. ``report_component_error`` never raises, even when the notify itself blows up.
4. A structural guard over the whole codebase (every ``cogs/``/``tools/`` class
   deriving from a Locale* base that defines its OWN ``on_error`` must call the
   shared helper, or be named in a documented exemption dict - empty today),
   with synthetic negative controls proving the detector actually flags a
   bypass. A parallel pair of guards for dynamic items checks every
   subclass's OWN ``callback`` and OWN ``interaction_check`` each really got
   wrapped - and that a subclass with no ``interaction_check`` of its own
   is left alone.
5. THE MANDATORY negative controls: copy ``tools/views.py`` aside (never
   ``git checkout``/``stash``), strip ONE fix at a time - ``LocaleView``'s
   on_error mixin, then separately the ``interaction_check`` wrap line in
   ``LocaleDynamicItem.__init_subclass__`` - load the mutated source under a
   throwaway module name, show the matching assertion above now FAILS, then
   copy the original back.

Nothing here touches the network, a database, Discord or Lavalink.
"""

from __future__ import annotations

import gc
import importlib
import importlib.util
import inspect
import logging
import pathlib
import re
import shutil

import discord
import pytest

import tools.views as views
from tools import interactions

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# (1) Pin the discord.py contract this fix is built on
# ---------------------------------------------------------------------------


def test_view_and_layoutview_on_error_signature_carries_an_item():
    """``BaseView.on_error`` (shared by View and LayoutView) takes ``item``.

    This is WHY one mixin (:class:`tools.views._ReportsComponentErrors`) can
    serve both :class:`tools.views.LocaleView` and
    :class:`tools.views.LocaleLayoutView` without duplicating a body: if
    discord.py ever gave the two different signatures, the mixin would break
    for one of them without this test noticing first.
    """
    view_params = list(inspect.signature(discord.ui.View.on_error).parameters)
    layout_params = list(inspect.signature(discord.ui.LayoutView.on_error).parameters)
    assert view_params == ["self", "interaction", "error", "item"]
    assert layout_params == view_params
    assert discord.ui.View.on_error is discord.ui.LayoutView.on_error, (
        "View and LayoutView no longer share BaseView.on_error - the shared "
        "mixin's assumption needs revisiting"
    )


def test_modal_on_error_signature_carries_no_item():
    """``Modal.on_error`` has no ``item`` - why ``LocaleModal`` builds its own ``where``."""
    params = list(inspect.signature(discord.ui.Modal.on_error).parameters)
    assert params == ["self", "interaction", "error"]


def test_dynamic_item_dispatch_never_calls_an_on_error_hook():
    """Pin the exact discord.py fact ``LocaleDynamicItem`` is built around.

    Two independent proofs, so a library refactor that quietly adds a hook (or
    removes this comment) is caught either way:

    * the documented contract, on ``Item.interaction_check``;
    * the actual dispatch code, ``ViewStore.schedule_dynamic_item_call`` - it
      calls the item's ``callback`` inside its OWN try/except that never
      reaches for ``on_error`` anywhere.
    """
    doc = discord.ui.Item.interaction_check.__doc__ or ""
    assert "DynamicItem" in doc and "does not call the" in doc and "on_error" in doc

    src = inspect.getsource(discord.ui.view.ViewStore.schedule_dynamic_item_call)
    assert "item.callback(interaction)" in src
    assert "on_error" not in src


def test_locale_dynamic_item_has_no_on_error_of_its_own():
    """Confirms there is really nothing to override - the wrap is the only lever."""
    assert "on_error" not in vars(views.LocaleDynamicItem)
    assert not hasattr(discord.ui.DynamicItem, "on_error")


# ---------------------------------------------------------------------------
# (2) + (3): behavioural - one report, one reply, the same id, never a raise
# ---------------------------------------------------------------------------

_ERROR_ID_RE = re.compile(r"error_id=([0-9a-f]{8})")
_MESSAGE_ID_RE = re.compile(r"`([0-9a-f]{8})`")


def _extract_log_id(record: logging.LogRecord) -> str:
    match = _ERROR_ID_RE.search(record.getMessage())
    assert match, f"no error_id in log record: {record.getMessage()!r}"
    return match.group(1)


def _extract_message_id(content: str) -> str:
    match = _MESSAGE_ID_RE.search(content)
    assert match, f"no error_id in the user-facing message: {content!r}"
    return match.group(1)


def _view_on_error_trigger():
    """A LocaleView whose on_error is invoked for a button that "raised"."""

    class CrashView(views.LocaleView):
        pass

    view = CrashView()
    button = discord.ui.Button(label="Go", custom_id="go")
    return lambda interaction: view.on_error(interaction, ValueError("view boom"), button)


def _layout_view_on_error_trigger():
    """The Components V2 twin of the above, same signature off BaseView."""

    class CrashLayoutView(views.LocaleLayoutView):
        pass

    view = CrashLayoutView()
    button = discord.ui.Button(label="Go", custom_id="go")
    return lambda interaction: view.on_error(
        interaction, ValueError("layout boom"), button
    )


def _modal_on_error_trigger():
    """A LocaleModal's on_error, as discord.py calls it when on_submit raises."""

    class CrashModal(views.LocaleModal, title="crash"):
        pass

    modal = CrashModal()
    return lambda interaction: modal.on_error(interaction, RuntimeError("modal boom"))


def _dynamic_item_callback_trigger():
    """A LocaleDynamicItem subclass whose own ``callback`` raises.

    Calls ``item.callback(interaction)`` directly - exactly what
    ``ViewStore.schedule_dynamic_item_call`` does (see section 1) - so this
    exercises the real wrapper installed by ``__init_subclass__``, not a stand-in.
    """

    class CrashButton(
        views.LocaleDynamicItem[discord.ui.Button], template=r"zz_test_crash:(?P<n>\d+)"
    ):
        @classmethod
        async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
            return cls(item)

        async def callback(self, interaction):
            raise KeyError("dynamic boom")

    item = CrashButton(discord.ui.Button(custom_id="zz_test_crash:1", label="go"))
    return lambda interaction: item.callback(interaction)


def _dynamic_item_interaction_check_trigger():
    """A LocaleDynamicItem subclass whose own ``interaction_check`` raises.

    Calls ``item.interaction_check(interaction)`` directly - exactly what
    ``ViewStore.schedule_dynamic_item_call`` does (see section 1, the
    ``try: allow = await item.interaction_check(interaction) except Exception:
    allow = False`` branch, which swallows silently and logs nothing) - so
    this exercises the real wrapper installed by ``__init_subclass__``, not a
    stand-in.
    """

    class CrashCheckButton(
        views.LocaleDynamicItem[discord.ui.Button],
        template=r"zz_test_crash_check:(?P<n>\d+)",
    ):
        @classmethod
        async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
            return cls(item)

        async def interaction_check(self, interaction):
            raise KeyError("check boom")

        async def callback(self, interaction):  # pragma: no cover
            pass

    item = CrashCheckButton(
        discord.ui.Button(custom_id="zz_test_crash_check:1", label="go")
    )
    return lambda interaction: item.interaction_check(interaction)


_TRIGGER_BUILDERS = {
    "LocaleView": _view_on_error_trigger,
    "LocaleLayoutView": _layout_view_on_error_trigger,
    "LocaleModal": _modal_on_error_trigger,
    "LocaleDynamicItem.callback": _dynamic_item_callback_trigger,
    "LocaleDynamicItem.interaction_check": _dynamic_item_interaction_check_trigger,
}


async def _assert_reports_exactly_once(trigger, make_interaction, caplog, *, done):
    """Run ``trigger`` and assert the one-report/one-reply/same-id contract."""

    interaction = make_interaction(done=done)
    with caplog.at_level(logging.ERROR, logger=interactions.log.name):
        await trigger(interaction)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected exactly one ERROR record, got {errors!r}"
    log_id = _extract_log_id(errors[0])

    total_replies = len(interaction.sent) + len(interaction.followups)
    assert total_replies == 1, (
        f"expected exactly one reply, got sent={interaction.sent!r} "
        f"followups={interaction.followups!r}"
    )

    if done:
        # Already acknowledged -> the reply MUST go through the followup
        # webhook, never a second response.send_message (which discord.py
        # would reject with InteractionResponded anyway).
        assert interaction.sent == []
        (args, kwargs) = interaction.followups[0]
    else:
        assert interaction.followups == []
        (args, kwargs) = interaction.sent[0]

    assert kwargs.get("ephemeral") is True
    assert _extract_message_id(args[0]) == log_id
    return log_id


@pytest.mark.parametrize("base_name", sorted(_TRIGGER_BUILDERS))
@pytest.mark.parametrize("done", [False, True], ids=["fresh", "already-responded"])
async def test_each_base_reports_exactly_once(base_name, done, make_interaction, caplog):
    trigger = _TRIGGER_BUILDERS[base_name]()
    await _assert_reports_exactly_once(trigger, make_interaction, caplog, done=done)


class _FailingFollowup:
    """A followup transport that always blows up - never discord.HTTPException.

    The task is specifically to prove ``report_component_error`` survives MORE
    than the HTTPException ``tools.interactions.reply`` already catches
    internally (an expired token, a race): a bare ``RuntimeError`` is exactly
    the "even that fails" case the helper's own docstring calls out.
    """

    async def send(self, *args, **kwargs):
        raise RuntimeError("webhook token expired")


async def test_on_error_never_raises_even_when_the_notify_itself_fails(
    make_interaction, caplog
):
    interaction = make_interaction(done=True)  # done -> routes to followup
    interaction.followup = _FailingFollowup()

    class CrashView(views.LocaleView):
        pass

    view = CrashView()
    button = discord.ui.Button(label="Go", custom_id="go")

    with caplog.at_level(logging.WARNING, logger=interactions.log.name):
        # Must not raise. If it does, pytest fails this test with the
        # propagated RuntimeError - that IS the assertion.
        await view.on_error(interaction, ValueError("boom"), button)

    assert interaction.sent == []
    assert interaction.followups == []  # the one attempt raised, nothing recorded
    warnings_ = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("could not notify" in r.getMessage() for r in warnings_), (
        f"expected a WARNING naming the failed notify, got {warnings_!r}"
    )


async def test_notify_failure_itself_is_still_reached_on_a_fresh_interaction(
    make_interaction,
):
    """Sanity: the happy path really calls response.send_message, not a stub."""
    interaction = make_interaction(done=False)

    class CrashModal(views.LocaleModal, title="crash"):
        pass

    await CrashModal().on_error(interaction, RuntimeError("x"))
    assert len(interaction.sent) == 1
    assert interaction.response.is_done()


# ---------------------------------------------------------------------------
# (4) Structural guard: every on_error override must call the shared helper
# ---------------------------------------------------------------------------

#: ``"module.Class" -> why this override may skip the shared helper``.
#: A reason is MANDATORY (see :func:`on_error_offenders`); empty today, exactly
#: like the sibling registry in ``tests/test_view_locale_hygiene.py`` - every
#: on_error override in the tree is the canonical one on the bases themselves.
ON_ERROR_EXEMPT: dict[str, str] = {}


def own_on_error_overrides(classes):
    """The classes that DEFINE their own ``on_error`` (not an inherited one)."""
    return [cls for cls in classes if "on_error" in cls.__dict__]


def calls_shared_helper(cls) -> bool:
    """True when ``cls``'s own ``on_error`` body calls ``report_component_error``.

    A source-text check, not a behavioural one: good enough here because the
    thing under test IS "did the author wire this override to the shared
    helper", which is a property of the code, not of any one input.
    """
    try:
        src = inspect.getsource(cls.__dict__["on_error"])
    except (OSError, TypeError, KeyError):
        return False
    return "report_component_error" in src


def on_error_offenders(classes, exemptions):
    """``module.Class`` names that override on_error, skip the helper, and are unexcused."""
    out = []
    for cls in own_on_error_overrides(classes):
        if calls_shared_helper(cls):
            continue
        name = f"{cls.__module__}.{cls.__qualname__}"
        if str(exemptions.get(name, "")).strip():
            continue
        out.append(name)
    return sorted(out)


def stale_on_error_exemptions(classes, exemptions):
    """Exemption keys that no longer name a real, unexcused offender."""
    live = {
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in own_on_error_overrides(classes)
        if not calls_shared_helper(cls)
    }
    return sorted(name for name in exemptions if name not in live)


def test_synthetic_bypass_override_is_flagged():
    """The detector is not vacuous: a bypass IS caught."""

    class Bypass(views.LocaleView):
        async def on_error(self, interaction, error, item):  # pragma: no cover
            pass

    try:
        assert own_on_error_overrides([Bypass]) == [Bypass]
        assert not calls_shared_helper(Bypass)
        name = f"{Bypass.__module__}.{Bypass.__qualname__}"
        assert on_error_offenders([Bypass], ON_ERROR_EXEMPT) == [name]
    finally:
        del Bypass
        gc.collect()


def test_synthetic_compliant_override_is_cleared():
    """A subclass that DOES call the helper is not flagged, even with its own body."""

    class Compliant(views.LocaleLayoutView):
        async def on_error(self, interaction, error, item):
            await interactions.report_component_error(
                interaction, error, where="synthetic"
            )

    try:
        assert on_error_offenders([Compliant], ON_ERROR_EXEMPT) == []
    finally:
        del Compliant
        gc.collect()


def test_exemption_needs_a_real_reason_to_silence_anything():
    """A blank-reason exemption silences nothing; a real one does."""

    class Weird(views.LocaleModal, title="weird"):
        async def on_error(self, interaction, error):  # pragma: no cover
            pass

    name = f"{Weird.__module__}.{Weird.__qualname__}"
    try:
        assert on_error_offenders([Weird], {}) == [name]
        assert on_error_offenders([Weird], {name: "   "}) == [name]
        assert on_error_offenders([Weird], {name: "documented residual, see report"}) == []
    finally:
        del Weird
        gc.collect()


def test_stale_exemption_is_reported():
    """An exemption for a class that no longer offends must be removed."""

    class FixedNow(views.LocaleView):
        async def on_error(self, interaction, error, item):
            await interactions.report_component_error(
                interaction, error, where="synthetic"
            )

    name = f"{FixedNow.__module__}.{FixedNow.__qualname__}"
    try:
        assert stale_on_error_exemptions([FixedNow], {name: "old reason"}) == [name]
        assert stale_on_error_exemptions([FixedNow], {}) == []
    finally:
        del FixedNow
        gc.collect()


def _iter_target_modules():
    """Dotted module names for every ``.py`` under ``cogs/`` and ``tools/``."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    for pkg in ("cogs", "tools"):
        pkg_dir = repo_root / pkg
        if not pkg_dir.is_dir():
            continue
        for path in sorted(pkg_dir.rglob("*.py")):
            parts = list(path.relative_to(repo_root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                yield ".".join(parts)


def _all_subclasses(base):
    """Every transitive subclass of ``base`` currently loaded in the process."""
    seen = set()
    stack = list(base.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen


def _collect_locale_subclasses():
    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:
            skipped.append((modname, str(exc)))

    classes = set()
    for base in (views.LocaleView, views.LocaleLayoutView, views.LocaleModal):
        classes |= _all_subclasses(base)
    target = {cls for cls in classes if cls.__module__.startswith(("cogs", "tools"))}
    return target, skipped


def test_no_bare_on_error_overrides_in_codebase():
    """THE guard: no View/LayoutView/Modal subclass bypasses the shared helper.

    This is also the answer to "check whether any existing subclass already
    overrides on_error": with ``ON_ERROR_EXEMPT`` empty, this failing would mean
    one does and skips the fix.
    """
    target, skipped = _collect_locale_subclasses()
    assert target, (
        "no LocaleView/LocaleLayoutView/LocaleModal subclasses were discovered "
        "under cogs/ or tools/ - this scan would be vacuous. Skipped imports: "
        + repr(skipped)
    )

    offenders = on_error_offenders(target, ON_ERROR_EXEMPT)
    assert offenders == [], (
        "on_error overrides that bypass tools.interactions.report_component_error "
        "(a crash there answers nothing to the user - see this module's docstring):\n  "
        + "\n  ".join(offenders)
    )

    stale = stale_on_error_exemptions(target, ON_ERROR_EXEMPT)
    assert stale == [], f"stale ON_ERROR_EXEMPT entries, remove them: {stale}"


def test_every_dynamic_item_subclass_in_codebase_has_a_wrapped_callback():
    """Parallel guard for the dynamic-item path (no on_error exists to check).

    Every ``LocaleDynamicItem`` subclass that defines its own ``callback`` must
    have gone through ``__init_subclass__``'s wrap - the only lever this
    dispatch path has (see section 1). A subclass that defines no callback of
    its own inherits an already-wrapped one and is correctly skipped.
    """
    # Modules are already imported by test_no_bare_on_error_overrides_in_codebase
    # in the same session; import again defensively in case this test runs alone.
    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:
            skipped.append((modname, str(exc)))

    target = {
        cls
        for cls in _all_subclasses(views.LocaleDynamicItem)
        if cls.__module__.startswith(("cogs", "tools"))
    }
    assert target, (
        "no LocaleDynamicItem subclasses were discovered under cogs/ or tools/ - "
        "this scan would be vacuous. Skipped imports: " + repr(skipped)
    )

    unwrapped = sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in target
        if "callback" in cls.__dict__
        and not hasattr(cls.__dict__["callback"], "__wrapped__")
    )
    assert unwrapped == [], (
        "dynamic items whose callback bypasses the error-reporting wrap:\n  "
        + "\n  ".join(unwrapped)
    )


def test_synthetic_dynamic_item_callback_is_wrapped():
    """The wrap guard is not vacuous either: a fresh subclass really gets wrapped."""

    class Probe(
        views.LocaleDynamicItem[discord.ui.Button], template=r"zz_test_probe:(?P<n>\d+)"
    ):
        @classmethod
        async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
            return cls(item)

        async def callback(self, interaction):  # pragma: no cover
            pass

    try:
        assert hasattr(Probe.__dict__["callback"], "__wrapped__")
    finally:
        del Probe
        gc.collect()


def test_every_dynamic_item_subclass_in_codebase_has_a_wrapped_interaction_check():
    """Parallel guard, symmetric with the callback one above: every
    ``LocaleDynamicItem`` subclass that defines its OWN ``interaction_check``
    must have gone through ``__init_subclass__``'s wrap. A subclass that
    defines no ``interaction_check`` of its own correctly inherits
    ``LocaleDynamicItem.interaction_check`` itself - never wrapped, since it
    cannot raise anything a subclass introduced - and is skipped."""

    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:
            skipped.append((modname, str(exc)))

    target = {
        cls
        for cls in _all_subclasses(views.LocaleDynamicItem)
        if cls.__module__.startswith(("cogs", "tools"))
    }
    assert target, (
        "no LocaleDynamicItem subclasses were discovered under cogs/ or tools/ - "
        "this scan would be vacuous. Skipped imports: " + repr(skipped)
    )

    unwrapped = sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in target
        if "interaction_check" in cls.__dict__
        and not hasattr(cls.__dict__["interaction_check"], "__wrapped__")
    )
    assert unwrapped == [], (
        "dynamic items whose own interaction_check bypasses the "
        "error-reporting wrap:\n  " + "\n  ".join(unwrapped)
    )


async def test_synthetic_dynamic_item_interaction_check_is_wrapped_and_denies_on_raise(
    make_interaction, caplog
):
    """The wrap guard is not vacuous: a fresh subclass's raising
    ``interaction_check`` really gets wrapped, reports exactly once, and
    returns ``False`` (deny) instead of propagating - the behaviour
    ``ViewStore.schedule_dynamic_item_call`` would otherwise swallow with no
    log and no reply at all (see section 1)."""

    class CheckProbe(
        views.LocaleDynamicItem[discord.ui.Button],
        template=r"zz_test_check_probe:(?P<n>\d+)",
    ):
        @classmethod
        async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
            return cls(item)

        async def interaction_check(self, interaction):
            raise RuntimeError("check boom")

        async def callback(self, interaction):  # pragma: no cover
            pass

    try:
        assert hasattr(CheckProbe.__dict__["interaction_check"], "__wrapped__")

        item = CheckProbe(
            discord.ui.Button(custom_id="zz_test_check_probe:1", label="go")
        )
        interaction = make_interaction(done=False)
        with caplog.at_level(logging.ERROR, logger=interactions.log.name):
            result = await item.interaction_check(interaction)

        assert result is False  # deny, never a raise reaching the caller
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, f"expected exactly one ERROR record, got {errors!r}"
        log_id = _extract_log_id(errors[0])
        assert len(interaction.sent) == 1
        args, kwargs = interaction.sent[0]
        assert kwargs.get("ephemeral") is True
        assert _extract_message_id(args[0]) == log_id
    finally:
        del CheckProbe
        gc.collect()


def test_dynamic_item_subclass_with_no_own_interaction_check_is_untouched():
    """A subclass that adds a ``callback`` but no ``interaction_check`` of its
    own must NOT gain one in its own ``__dict__`` - it keeps inheriting
    ``LocaleDynamicItem.interaction_check`` unwrapped, exactly as before this
    fix."""

    class NoCheckProbe(
        views.LocaleDynamicItem[discord.ui.Button],
        template=r"zz_test_nocheck_probe:(?P<n>\d+)",
    ):
        @classmethod
        async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
            return cls(item)

        async def callback(self, interaction):  # pragma: no cover
            pass

    try:
        assert "interaction_check" not in NoCheckProbe.__dict__
        assert (
            NoCheckProbe.interaction_check
            is views.LocaleDynamicItem.__dict__["interaction_check"]
        )
    finally:
        del NoCheckProbe
        gc.collect()


# ---------------------------------------------------------------------------
# (5) MANDATORY negative control: remove the fix on disk, prove the test fails
# ---------------------------------------------------------------------------


def _load_module_from_file(name: str, path: pathlib.Path):
    """Exec ``path`` as a FRESH, independently-named module. Never touches ``sys.modules[real name]``.

    Deliberately NOT ``importlib.reload`` on the real ``tools.views``: reload
    rebinds ``tools.views.LocaleView`` etc. to brand new class objects, but every
    cog already imported in this process keeps its OLD reference (``from
    tools.views import AuthorView`` copies the name at import time). Other
    suites make identity-based ``issubclass`` checks against
    ``tools.views.LocaleView`` (``tests/test_view_authorization_census.py``'s
    ``locale_only_bases``) - reloading would sever those for the rest of the
    SESSION, breaking tests that never touch this file, in a way the file
    restore below cannot undo (the restore fixes the SOURCE; it does not undo a
    reload already done). Loading the mutated source under an alias name
    instead gives this test the broken classes to assert against while the real
    ``tools.views`` singleton every other test relies on is never re-executed.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_negative_control_stripping_locale_view_on_error_breaks_the_report(
    make_interaction, tmp_path
):
    """Copy ``tools/views.py`` aside, strip the fix, prove the report breaks.

    Never ``git checkout``/``stash`` - a plain file copy (``shutil.copy``) is the
    entire mechanism, in both directions: back up the original, mutate the REAL
    file on disk, load that mutated source under a throwaway module name (see
    :func:`_load_module_from_file` for why not a reload of the real module),
    exercise it, then copy the backup back over the real file. The real
    ``tools.views`` module object is never reloaded, so nothing else in this
    session's imports is affected - only the two statements inside the ``try``
    that touch the file on disk are a mutation at all, and the ``finally``
    undoes exactly that.
    """
    views_path = pathlib.Path(views.__file__)
    backup_path = tmp_path / "views.py.orig"
    shutil.copy(views_path, backup_path)

    marker = "class LocaleView(_ReportsComponentErrors, discord.ui.View):"
    original = views_path.read_text()
    try:
        assert marker in original, (
            "the LocaleView class line changed shape - update this negative "
            "control's marker to match"
        )
        broken = original.replace(marker, "class LocaleView(discord.ui.View):", 1)
        assert broken != original
        views_path.write_text(broken)

        broken_module = _load_module_from_file(
            "_negative_control_tools_views", views_path
        )

        # Sanity: the mixin really is gone from the loaded LocaleView's MRO.
        assert broken_module._ReportsComponentErrors not in broken_module.LocaleView.__mro__

        class CrashView(broken_module.LocaleView):
            pass

        view = CrashView()
        interaction = make_interaction(done=False)
        button = discord.ui.Button(label="Go", custom_id="go")

        # discord.py's own default on_error only logs - it never replies. The
        # real test (test_each_base_reports_exactly_once) asserts exactly one
        # reply; with the fix stripped there must be ZERO, which is the exact
        # failure this whole chantier exists to fix.
        await view.on_error(interaction, ValueError("boom"), button)

        assert interaction.sent == [] and interaction.followups == [], (
            "the broken LocaleView still sent something - the on_error removal "
            "did not take effect, so this negative control proves nothing"
        )

        del CrashView, view, broken_module
        gc.collect()
    finally:
        shutil.copy(backup_path, views_path)
        assert views_path.read_text() == original, (
            "failed to restore tools/views.py to its original content"
        )
        # The REAL tools.views singleton was never touched above, so no
        # reload is needed here - but confirm that assumption rather than
        # trust it silently.
        assert views._ReportsComponentErrors in views.LocaleView.__mro__
        assert "report_component_error" in inspect.getsource(
            views._ReportsComponentErrors.on_error
        )


async def test_negative_control_removing_the_interaction_check_wrap_breaks_the_report(
    make_interaction, tmp_path
):
    """Same mechanism as the negative control above, aimed at Fix 2: strip the
    ``interaction_check`` wrap line from ``__init_subclass__`` and prove a
    raising subclass ``interaction_check`` goes back to silently propagating
    (exactly what ``ViewStore.schedule_dynamic_item_call``'s own bare
    ``except Exception: allow = False`` would then swallow with no log and no
    reply at all)."""

    views_path = pathlib.Path(views.__file__)
    backup_path = tmp_path / "views.py.orig"
    shutil.copy(views_path, backup_path)

    marker = (
        "        own_check = cls.__dict__.get(\"interaction_check\")\n"
        "        if own_check is not None:\n"
        "            cls.interaction_check = _wrap_dynamic_item_interaction_check(own_check)\n"
    )
    original = views_path.read_text()
    try:
        assert marker in original, (
            "the interaction_check wrap lines changed shape - update this "
            "negative control's marker to match"
        )
        broken = original.replace(marker, "", 1)
        assert broken != original
        views_path.write_text(broken)

        broken_module = _load_module_from_file(
            "_negative_control_tools_views_check", views_path
        )

        class CrashCheckButton(
            broken_module.LocaleDynamicItem[discord.ui.Button],
            template=r"zz_test_negctrl_check:(?P<n>\d+)",
        ):
            @classmethod
            async def from_custom_id(cls, interaction, item, match):  # pragma: no cover
                return cls(item)

            async def interaction_check(self, interaction):
                raise RuntimeError("check boom")

            async def callback(self, interaction):  # pragma: no cover
                pass

        # Sanity: the wrap really did not run on the broken module.
        assert not hasattr(
            CrashCheckButton.__dict__["interaction_check"], "__wrapped__"
        )

        item = CrashCheckButton(
            discord.ui.Button(custom_id="zz_test_negctrl_check:1", label="go")
        )
        interaction = make_interaction(done=False)

        # With the wrap stripped, the raise propagates straight out - the
        # real test (test_synthetic_dynamic_item_interaction_check_is_wrapped_
        # and_denies_on_raise) asserts a clean ``False`` return with one
        # report and one reply; with the fix removed there must be a bare
        # raise instead, which IS the regression this fix closes.
        raised = False
        try:
            await item.interaction_check(interaction)
        except RuntimeError:
            raised = True

        assert raised, (
            "the broken LocaleDynamicItem still denied cleanly instead of "
            "raising - the wrap removal did not take effect, so this "
            "negative control proves nothing"
        )
        assert interaction.sent == [] and interaction.followups == []

        del CrashCheckButton, item, broken_module
        gc.collect()
    finally:
        shutil.copy(backup_path, views_path)
        assert views_path.read_text() == original, (
            "failed to restore tools/views.py to its original content"
        )
        assert "_wrap_dynamic_item_interaction_check" in inspect.getsource(
            views.LocaleDynamicItem.__init_subclass__
        )
