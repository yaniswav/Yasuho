"""Structural guard: no dispatch root may answer a click in the wrong language.

THE DEFECT THIS EXISTS FOR (measured, not theorised). ``i18n.current_locale`` is
a ContextVar whose default is ENGLISH. ``Yasuho.get_context`` sets it per
command, but a component click, a modal submit and a dynamic-item click each run
in a task discord.py creates with ``asyncio.create_task`` from the gateway
coroutine - and ``create_task`` copies the context it is created in, where the
locale is still the "en" default. So every ``_()`` a callback evaluates answers
in English unless something installed the clicker's locale FIRST. Before the fix
this guard ships with, ten dispatch roots did not, nine of them the whole of
``cogs/music/``: a French member clicking Pause was told "You must be in my
voice channel to use these controls." while the identical refusal from a slash
command came back in French.

The only hook discord.py runs before a callback is a check, so the apply lives in
a check and the rule is a property of the CLASS, not of any one callback:

======================  =================================================
dispatch                what discord.py awaits (verified, discord.py 2.7.1)
======================  =================================================
component click         ``item._run_checks`` then ``view.interaction_check``
                        then ``item.callback``            (ui/view.py:591)
modal submit            ``modal.interaction_check`` then ``on_submit``
                                                          (ui/modal.py:212)
dynamic-item click      ``item.interaction_check`` then ``item.callback`` -
                        the enclosing view's check is NEVER consulted
                                                          (ui/view.py:1031)
======================  =================================================

WHAT IS CHECKED. :func:`applies_interaction_locale` is behavioural, not a grep:
it runs the class's real ``interaction_check`` in a FRESH task whose context
starts at the English default - exactly the condition a click arrives in - and
asks whether the ContextVar came out on the clicker's locale. It therefore
accepts any correct implementation (a ``Locale*`` base, a ``super()`` chain that
reaches one, a hand-rolled ``apply_interaction_locale``) and rejects the one
thing that matters: a root where the locale is still English when the callback
starts. Say the consequence plainly, because the inverse is easy to assume and
false: a missing ``super()`` hop is NOT what this guard detects. It caught the
``Paginator`` and ``MusicController`` defects because those checks dropped the
hop AND had nothing else applying the locale
(:func:`test_the_real_paginator_is_caught_when_its_super_hop_is_removed` and its
``MusicController`` twin prove exactly that, by putting the pre-fix body back on
the real class). Seven cog-level checks in the tree today hand-roll
``i18n.apply_interaction_locale`` and never call ``super()``; they pass here, and
should - but if one of them were sitting on a base that also carried a GATE,
this guard would not notice the gate had been dropped.

EXEMPTIONS ARE WRITTEN DOWN, NEVER SILENT. :data:`LOCALE_EXEMPT` maps
``module.Class`` to a prose reason. An entry with a blank reason does NOT
silence anything (:func:`unexempted_offenders` ignores it), and an entry that no
longer names an offender fails :func:`test_no_stale_exemptions`, so a fixed or
deleted class cannot leave a dead pass behind. The registry is empty today: every
dispatch root in the tree reaches ``LocaleView`` / ``LocaleLayoutView`` /
``LocaleModal`` / ``LocaleDynamicItem``, display-only cards included, because a
card without a dispatchable component never has its check called and the base
therefore costs it nothing.

KNOWN BLIND SPOTS, stated rather than left to be discovered. All three are empty
today; each was checked, not assumed, and each would need its own coverage the
day it stops being empty.

1. CLASSES BUILT AT CALL TIME. The scan walks ``__subclasses__()`` after
   importing every module, so it sees classes created at import time. A dispatch
   root defined INSIDE a function body only exists once that function has run,
   and is invisible here. There are none in the tree (checked: every
   ``class X(<dispatch base>)`` in cogs/ and tools/ is at module level), and the
   house style puts views at module level, but a factory that builds a View
   class per call would escape.

2. (CLOSED, kept here because it is the non-obvious one.) A check can live on an
   ITEM instead of on the root, and it runs FIRST: ``ui/view.py:591`` is
   ``await item._run_checks(interaction) and await self.interaction_check(...)``,
   and ``Item._run_checks`` recurses into its parent's - so a Button's, a
   Select's or a Container's own ``interaction_check`` evaluates in the
   still-English context, and a spotless root does not save it. Scanning the
   four dispatch roots alone would not see it, so section (5) walks the in-tree
   ``ui.Item`` subclasses too and runs the same detector over the ones carrying
   a check of their own. Empty today (the one item-side override is
   ``LocaleDynamicItem``, which is a scanned root) - which is exactly why that
   empty list is not left to speak for itself: the walk size is asserted beside
   it, the filter is aimed at constructed classes both ways, and
   :func:`test_the_item_pipeline_reports_a_planted_offender` pushes a class that
   MUST be named through the real filter, the real detector and the real
   exemption sieve.

3. ONE PATH PER CHECK. The probe's ``_AnyAttribute`` is falsy
   (``__bool__`` -> False), so a check that can ``return False`` early behind a
   truthy instance attribute is never driven down that branch: a class that
   applied the locale only on its happy path would be cleared here. The single
   branching check in the tree is ``tools.paginator.Paginator``'s
   (``author_id is not None``), and only that branch is probed - its other
   branch is covered behaviourally instead, by
   ``test_a_public_paginator_still_lets_anyone_page`` in
   ``tests/test_view_locale_behaviour.py``. Falsy is the deliberate direction:
   it can turn a true "applies" into a false "does not", never the reverse, so
   the blind spot costs a missed catch and never a false alarm.

NOTHING IS TOUCHED BUT MEMORY: the probe interaction is a stand-in whose
``db_pool.fetchval`` answers None, so ``i18n.resolve_locale`` runs for real all
the way down to ``interaction.locale``. No network, no database, no Discord, no
Lavalink.
"""

import asyncio
import gc
import importlib
import inspect
import pathlib
import types

import discord
from discord import ui

from tools import i18n

# ---------------------------------------------------------------------------
# Registry of legitimate exemptions
# ---------------------------------------------------------------------------

#: ``"module.Class" -> why this dispatch root may leave the locale unapplied``.
#:
#: A reason is MANDATORY: an entry whose reason is empty or blank is ignored by
#: :func:`unexempted_offenders`, so it cannot silence anything. An entry that is
#: no longer an offender (fixed, renamed, deleted) fails
#: :func:`test_no_stale_exemptions`. Both rules exist so this dict can never
#: become the place a real regression goes to hide.
#:
#: It is empty on purpose. The three shapes that once looked exempt - a
#: display-only card with no callbacks, a LayoutView that only hosts
#: DynamicItems, an item that applies the locale inside its own callback - are
#: all better served by deriving from a ``Locale*`` base: free when the check is
#: never called, and impossible to forget when a button is added later.
LOCALE_EXEMPT: dict[str, str] = {}


# ---------------------------------------------------------------------------
# The probe: a click-shaped interaction, and an instance to run the check on
# ---------------------------------------------------------------------------

# A locale that is NOT the default and DOES have a compiled catalogue, so
# "the ContextVar moved" and "the ContextVar is on the clicker's language" are
# the same statement. Pinned by test_the_probe_locale_is_a_real_foreign_locale.
PROBE_LOCALE = "fr"

# Ids nothing else in the suite uses, so the settings cache entries this probe
# seats can never be mistaken for another test's state.
PROBE_USER_ID = 909_090_909_090_909_090
PROBE_GUILD_ID = 808_080_808_080_808_080

# The template LocaleDynamicItem itself carries: needed again here because
# subclassing a DynamicItem requires one, and a probe subclass must never be
# able to claim a real custom_id either.
_UNREACHABLE_TEMPLATE = r"(?!)"


class _AnyAttribute:
    """Answers any attribute, call, truth test or iteration without raising.

    A real ``interaction_check`` reaches for view state the probe has no way to
    build (``self.player``, ``self.cog``, ``self._owner``...). Answering those
    with a permissive stand-in lets the check run FURTHER than a bare instance
    would, which matters in one direction only: it can turn a false "does not
    apply" into a true "applies", never the reverse.
    """

    def __getattr__(self, name):
        return _AnyAttribute()

    def __call__(self, *args, **kwargs):
        return _AnyAttribute()

    def __bool__(self):
        return False

    def __iter__(self):
        return iter(())

    def __str__(self):
        return "probe"


class _Pool:
    """A db_pool that answers "no row" - the only boundary the probe fakes."""

    async def fetchval(self, *args, **kwargs):
        return None


class _Response:
    """Swallows whatever a refusing check tries to send."""

    def __init__(self):
        self.sent = []

    def is_done(self):
        return False

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.sent.append((None, kwargs))


def make_probe_interaction(locale=PROBE_LOCALE, user_id=PROBE_USER_ID):
    """A click-shaped interaction whose resolved locale is ``locale``.

    Only the database is faked: ``i18n.resolve_locale`` really runs, finds no
    per-user and no per-guild preference, and falls through to
    ``interaction.locale`` - the real third link of the real chain.
    """
    client = types.SimpleNamespace(db_pool=_Pool())
    user = types.SimpleNamespace(id=user_id, name="probe")
    return types.SimpleNamespace(
        client=client,
        user=user,
        guild_id=PROBE_GUILD_ID,
        locale=locale,
        data={},
        response=_Response(),
    )


def _probe_instance(cls, user_id=PROBE_USER_ID):
    """An instance of ``cls`` that answers every attribute, built without ``__init__``.

    A real subclass (not a duck), because a zero-argument ``super()`` inside an
    overridden ``interaction_check`` requires the first argument to be an
    instance of the defining class. ``__module__`` is forced to this test module
    so the throwaway never shows up in another guard's scan of
    ``View.__subclasses__()``.
    """
    namespace = {
        "__getattr__": lambda self, name: _AnyAttribute(),
        "__module__": __name__,
    }
    try:
        probe_cls = type(f"_Probe_{cls.__name__}", (cls,), namespace)
    except TypeError:
        # DynamicItem.__init_subclass__ demands a template.
        probe_cls = type(
            f"_Probe_{cls.__name__}", (cls,), namespace, template=_UNREACHABLE_TEMPLATE
        )
    obj = object.__new__(probe_cls)
    # The handful of attributes the shared gates read before deciding. Set
    # through object.__setattr__ so a class with a custom __setattr__ cannot
    # intercept them.
    object.__setattr__(obj, "author_id", user_id)
    object.__setattr__(obj, "_deny_message", "This menu isn't for you.")
    object.__setattr__(obj, "item", discord.ui.Button(label="probe"))
    return obj


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


async def applies_interaction_locale(cls, *, interaction=None):
    """True when ``cls.interaction_check`` leaves the locale on the clicker's.

    Runs the check inside ``asyncio.create_task``, from a context pinned to
    ``i18n.DEFAULT_LOCALE``, which is exactly how discord.py dispatches a click:
    a fresh task carrying a copy of the gateway task's context. Whatever the
    check raises afterwards is irrelevant - the question is only whether the
    ContextVar was moved before a callback could read it - so exceptions are
    swallowed and the ContextVar is read from inside the task, the only place a
    ``ContextVar.set`` made in it is visible.
    """
    interaction = make_probe_interaction() if interaction is None else interaction
    check = cls.interaction_check
    probe = _probe_instance(cls, interaction.user.id)

    async def body():
        i18n.current_locale.set(i18n.DEFAULT_LOCALE)  # the gateway-task baseline
        try:
            await check(probe, interaction)
        except Exception:
            pass
        return i18n.current_locale.get()

    return (await asyncio.create_task(body())) == interaction.locale


# ---------------------------------------------------------------------------
# Offender / exemption bookkeeping - pure, aimable at constructed input
# ---------------------------------------------------------------------------


def unexempted_offenders(results, exemptions):
    """Names in ``results`` that do not apply the locale and are not excused.

    ``results`` maps ``module.Class`` to the detector's verdict. An exemption
    only counts when it carries a non-blank reason, so adding a key with an
    empty string silences nothing.
    """
    return sorted(
        name
        for name, applied in results.items()
        if not applied and not str(exemptions.get(name, "")).strip()
    )


def reasonless_exemptions(exemptions):
    """Exemption keys whose reason is missing or blank."""
    return sorted(name for name, reason in exemptions.items() if not str(reason).strip())


def items_with_own_check(classes):
    """The classes that DEFINE an ``interaction_check``, not the ones that inherit one.

    Pure and aimable at constructed input, because the scan that uses it
    succeeds by producing an empty list and an empty list is also what a filter
    that matches nobody produces. Looking anywhere but ``cls.__dict__`` would
    sweep in every Button in the tree (they all inherit ``Item``'s permissive
    default), which is not an override and has nothing to apply.
    """
    return [cls for cls in classes if "interaction_check" in cls.__dict__]


def stale_exemptions(results, exemptions):
    """Exemptions that no longer name an offender, including vanished classes.

    A class that was fixed, renamed or deleted must lose its exemption in the
    same change, otherwise the registry slowly turns into a list of blanket
    passes for classes nobody has looked at in a year.
    """
    return sorted(name for name in exemptions if results.get(name, True))


# ---------------------------------------------------------------------------
# (1) The detector is not vacuous: the cases it MUST report and MUST clear
# ---------------------------------------------------------------------------


def test_the_hooks_this_guard_polices_are_still_the_hooks():
    """Pin the discord.py contract the whole design rests on.

    If a library upgrade moved the pre-callback hook, or made ``DynamicItem``
    consult its view after all, this guard would keep checking a method nobody
    calls - green and blind. Fail here instead, and say what to revisit. This
    is the sibling of ``test_view_hygiene.test_refresh_is_a_real_framework_internal``.
    """
    for base in (ui.View, ui.LayoutView, ui.Modal, ui.Item, ui.DynamicItem):
        assert inspect.iscoroutinefunction(base.interaction_check), base.__name__

    # A bare check must be permissive, or "did nothing" and "refused" would be
    # indistinguishable to the probe.
    assert asyncio.run(ui.View.interaction_check(object(), object())) is True
    assert asyncio.run(ui.Item.interaction_check(object(), object())) is True

    # DynamicItem's check is NOT the permissive Item default: it forwards to
    # the wrapped item. LocaleDynamicItem must therefore keep a super() hop, so
    # pin the delegation the hop preserves - if discord.py ever stops
    # forwarding, that hop becomes a silent no-op and the base needs revisiting.
    from tools.views import LocaleDynamicItem

    class _Refusing(ui.Button):
        async def interaction_check(self, interaction, /):
            return False

    wrapper = object.__new__(LocaleDynamicItem)
    object.__setattr__(wrapper, "item", _Refusing(label="no"))
    interaction = make_probe_interaction()
    assert asyncio.run(ui.DynamicItem.interaction_check(wrapper, interaction)) is False
    assert asyncio.run(LocaleDynamicItem.interaction_check(wrapper, interaction)) is False

    object.__setattr__(wrapper, "item", ui.Button(label="yes"))
    assert asyncio.run(LocaleDynamicItem.interaction_check(wrapper, interaction)) is True


def test_the_probe_locale_is_a_real_foreign_locale():
    """Anti-vacuity pin for the whole file.

    If PROBE_LOCALE stopped resolving to a shipped catalogue,
    ``i18n.normalize`` would return None, every class would fall back to "en"
    and the scan would report all 169 roots as offenders. Loud, but for the
    wrong reason - so say the real reason here.
    """
    assert PROBE_LOCALE != i18n.DEFAULT_LOCALE
    assert PROBE_LOCALE in i18n.LOCALES
    assert i18n.normalize(PROBE_LOCALE) == PROBE_LOCALE


async def test_apply_interaction_locale_really_moves_the_context_var():
    """The second anti-vacuity pin: the thing being detected must be detectable.

    If a future i18n refactor stopped setting the ContextVar, every
    ``applies_interaction_locale`` call would answer False and the guard would
    look like a flood of regressions. This test names that cause directly.
    """

    async def body():
        i18n.current_locale.set(i18n.DEFAULT_LOCALE)
        await i18n.apply_interaction_locale(make_probe_interaction())
        return i18n.current_locale.get()

    assert await asyncio.create_task(body()) == PROBE_LOCALE


async def test_a_bare_view_subclass_is_reported():
    """A class that inherits discord.py's do-nothing check must be flagged."""

    class Bare(ui.View):
        pass

    try:
        assert await applies_interaction_locale(Bare) is False
    finally:
        del Bare
        gc.collect()


async def test_a_bare_dynamic_item_is_reported():
    """Same for the dynamic path, where the view's check is never consulted."""

    class BareDynamic(ui.DynamicItem[ui.Button], template=r"probe:(?P<x>\d+)"):
        pass

    try:
        assert await applies_interaction_locale(BareDynamic) is False
    finally:
        del BareDynamic
        gc.collect()


async def test_an_override_without_super_is_reported():
    """THE defect: a gate of one's own that drops the locale on the floor."""

    from tools.views import LocaleView

    class Forgetful(LocaleView):
        async def interaction_check(self, interaction):
            return True

    try:
        assert await applies_interaction_locale(Forgetful) is False
    finally:
        del Forgetful
        gc.collect()


async def test_an_override_with_super_is_cleared():
    """The fix shape: chain through super(), then gate."""

    from tools.views import LocaleView

    class Careful(LocaleView):
        async def interaction_check(self, interaction):
            if not await super().interaction_check(interaction):
                return False
            return True

    try:
        assert await applies_interaction_locale(Careful) is True
    finally:
        del Careful
        gc.collect()


async def test_a_hand_rolled_apply_is_cleared():
    """The detector is behavioural: it accepts a correct non-base implementation."""

    class HandRolled(ui.View):
        async def interaction_check(self, interaction):
            await i18n.apply_interaction_locale(interaction)
            return True

    try:
        assert await applies_interaction_locale(HandRolled) is True
    finally:
        del HandRolled
        gc.collect()


async def test_each_shared_base_is_cleared():
    """All four bases must satisfy the detector, on their own MRO."""

    from tools.views import (
        LocaleDynamicItem,
        LocaleLayoutView,
        LocaleModal,
        LocaleView,
    )

    class OnView(LocaleView):
        pass

    class OnLayout(LocaleLayoutView):
        pass

    class OnModal(LocaleModal, title="probe"):
        pass

    class OnDynamic(LocaleDynamicItem[ui.Button], template=r"probe:(?P<x>\d+)"):
        pass

    try:
        for cls in (OnView, OnLayout, OnModal, OnDynamic):
            assert await applies_interaction_locale(cls) is True, cls.__name__
    finally:
        del OnView, OnLayout, OnModal, OnDynamic
        gc.collect()


# ---------------------------------------------------------------------------
# (2) Calibration on the real classes: remove the fix, see the guard fire
# ---------------------------------------------------------------------------
#
# The two tests below are the ones that prove this guard would have caught the
# shipped bug. They do not reason about a synthetic lookalike: they put the
# PRE-FIX body back on the real class and check the detector reports it.


async def test_the_real_paginator_is_caught_when_its_super_hop_is_removed(monkeypatch):
    """tools.paginator.Paginator, exactly as it was before the fix."""

    from tools.i18n import _
    from tools.paginator import Paginator

    assert await applies_interaction_locale(Paginator) is True

    async def pre_fix_interaction_check(self, interaction):
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                _("This menu isn't for you."), ephemeral=True
            )
            return False
        return True

    monkeypatch.setattr(Paginator, "interaction_check", pre_fix_interaction_check)
    assert await applies_interaction_locale(Paginator) is False


async def test_the_real_music_controller_is_caught_without_its_super_hop(monkeypatch):
    """cogs.music.views.MusicController, exactly as it was before the fix."""

    # music first: views.py imports from it at module level (documented cycle).
    from cogs.music import music, views  # noqa: F401

    assert await applies_interaction_locale(views.MusicController) is True

    async def pre_fix_interaction_check(self, interaction):
        return await views._ensure_in_voice(self.player, interaction)

    monkeypatch.setattr(
        views.MusicController, "interaction_check", pre_fix_interaction_check
    )
    assert await applies_interaction_locale(views.MusicController) is False


# ---------------------------------------------------------------------------
# (3) The exemption registry behaves, on constructed input
# ---------------------------------------------------------------------------


def test_an_offender_with_no_exemption_is_reported():
    assert unexempted_offenders({"m.A": False, "m.B": True}, {}) == ["m.A"]


def test_an_offender_with_a_written_reason_is_cleared():
    assert unexempted_offenders({"m.A": False}, {"m.A": "display-only, no callbacks"}) == []


def test_a_blank_reason_silences_nothing():
    """The whole point of "expressible with a reason, not silently skipped"."""
    assert unexempted_offenders({"m.A": False}, {"m.A": ""}) == ["m.A"]
    assert unexempted_offenders({"m.A": False}, {"m.A": "   \n "}) == ["m.A"]


def test_reasonless_exemptions_are_named():
    assert reasonless_exemptions({"m.A": "", "m.B": "a reason", "m.C": " "}) == ["m.A", "m.C"]


def test_a_fixed_or_vanished_class_cannot_keep_its_exemption():
    assert stale_exemptions({"m.A": True}, {"m.A": "reason"}) == ["m.A"]
    assert stale_exemptions({}, {"m.Gone": "reason"}) == ["m.Gone"]
    assert stale_exemptions({"m.A": False}, {"m.A": "reason"}) == []


# ---------------------------------------------------------------------------
# (4) Integration: the whole tree
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TARGET_PACKAGES = ("cogs", "tools")

# Deliberately not imported from tests/test_view_hygiene.py: a guard that breaks
# when another guard's helpers are edited is a guard with a second way to go
# quiet. The twenty lines below are the price of independence.


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


#: The four dispatch roots, by the base discord.py dispatches them through.
_ROOT_BASES = {
    "View": ui.View,
    "LayoutView": ui.LayoutView,
    "Modal": ui.Modal,
    "DynamicItem": ui.DynamicItem,
}

_scan_cache = None


async def _scan():
    """{module.Class: applied}, plus the per-kind tally. Computed once."""
    global _scan_cache
    if _scan_cache is not None:
        return _scan_cache

    skipped = []
    for modname in _iter_target_modules():
        try:
            importlib.import_module(modname)
        except ImportError as exc:  # optional dep absent -> skip, as elsewhere
            skipped.append((modname, str(exc)))

    kinds = {}
    for kind, base in _ROOT_BASES.items():
        for cls in _all_subclasses(base):
            if cls.__module__.split(".")[0] in _TARGET_PACKAGES:
                kinds.setdefault(cls, kind)

    results = {}
    for cls, _kind in kinds.items():
        results[f"{cls.__module__}.{cls.__qualname__}"] = await applies_interaction_locale(cls)

    tally = {}
    for cls, kind in kinds.items():
        tally[kind] = tally.get(kind, 0) + 1

    gc.collect()  # drop the throwaway probe subclasses from the class registry
    _scan_cache = (results, tally, skipped)
    return _scan_cache


async def test_the_scan_actually_covered_the_tree():
    """A collector that silently found nothing would pass every assertion below.

    The count is the counter this rule demands of any guard whose success is an
    empty list: 169 roots today, so a floor of 150 catches a collapse (a broken
    import, a renamed base, a filter that stopped matching) without tripping on
    ordinary growth or a deleted view.
    """
    results, tally, skipped = await _scan()

    assert len(results) >= 150, (len(results), skipped)
    assert set(tally) == set(_ROOT_BASES), (tally, "a whole dispatch kind vanished")
    assert all(count > 0 for count in tally.values()), tally

    # Landmarks: one per dispatch kind, and the two classes this guard was
    # written for. If the scan stops reaching these it is not scanning.
    for name in (
        "tools.paginator.Paginator",                      # the View that started it
        "cogs.music.views.MusicController",               # the LayoutView ditto
        "cogs.music.views.AddSongModal",                  # a Modal
        "cogs.anilist.airing.AiringSeenButton",           # a DynamicItem
        "cogs.config.tickets.lifecycle.TicketCloseButton",
    ):
        assert name in results, (name, "not reached by the scan")


async def test_every_dispatch_root_applies_the_interaction_locale():
    """THE guard: no View/LayoutView/Modal/DynamicItem answers in the wrong language.

    Adding a root that overrides ``interaction_check`` without chaining to
    ``super()``, or one that subclasses ``discord.ui.View`` directly instead of
    ``tools.views.LocaleView``, fails here with the offending ``module.Class``
    spelled out.
    """
    results, _tally, _skipped = await _scan()

    offenders = unexempted_offenders(results, LOCALE_EXEMPT)

    assert not offenders, (
        "these dispatch roots leave i18n.current_locale on the English default, "
        "so every _() in their callbacks answers in the wrong language. Derive "
        "from tools.views.LocaleView / LocaleLayoutView / LocaleModal / "
        "LocaleDynamicItem, or chain an existing interaction_check through "
        "super(), or add a WRITTEN reason to LOCALE_EXEMPT:\n  "
        + "\n  ".join(offenders)
    )


async def test_every_exemption_carries_a_written_reason():
    """A key with a blank reason is not an exemption; say so out loud."""
    assert reasonless_exemptions(LOCALE_EXEMPT) == [], (
        "LOCALE_EXEMPT entries must explain themselves in prose; these do not"
    )


# ---------------------------------------------------------------------------
# (5) The item side: a check that runs BEFORE the root's
# ---------------------------------------------------------------------------
#
# ``ui/view.py:591`` is ``await item._run_checks(interaction) and await
# self.interaction_check(interaction)``, and ``Item._run_checks`` recurses into
# its parent's. So a check living on a Button / Select / Container runs first,
# in the still-English context, and a root that is perfectly clean does not save
# it. Scanning only the four dispatch roots would leave that hole open, so the
# items that carry a check of their own are scanned too, with the same detector.


_item_scan_cache = None


async def _scan_item_checks():
    """{module.Class: applied} for in-tree ITEMS with a check of their own.

    Second element is how many item classes were WALKED, not how many carried a
    check: the offender list is legitimately empty today, so the walk size is the
    only thing that can tell "nobody overrides it" from "the walk found nothing".
    DynamicItems are skipped - they are dispatch roots and :func:`_scan` already
    has them.
    """
    global _item_scan_cache
    if _item_scan_cache is not None:
        return _item_scan_cache

    await _scan()  # guarantees every target module is imported

    walked = _walked_items()
    results = {}
    for cls in items_with_own_check(walked):
        results[f"{cls.__module__}.{cls.__qualname__}"] = (
            await applies_interaction_locale(cls)
        )

    gc.collect()
    _item_scan_cache = (results, len(walked))
    return _item_scan_cache


def _walked_items():
    """The in-tree items the scan considers, recomputed (not cached) on purpose."""
    return [
        cls
        for cls in _all_subclasses(ui.Item)
        if cls.__module__.split(".")[0] in _TARGET_PACKAGES
        and not issubclass(cls, ui.DynamicItem)
    ]


def test_the_item_filter_keeps_an_own_check_and_drops_an_inherited_one():
    """Aim the filter itself at constructed input, both ways."""

    class Gate(ui.Button):
        async def interaction_check(self, interaction, /):
            return True

    class Inherits(Gate):
        pass

    class Plain(ui.Button):
        pass

    try:
        assert items_with_own_check([Plain, Inherits, Gate]) == [Gate]
        assert items_with_own_check([]) == []
    finally:
        del Gate, Inherits, Plain
        gc.collect()


async def test_the_item_pipeline_reports_a_planted_offender():
    """The negative control ``test_no_item_level_check_answers_in_english`` needs.

    That guard passes by saying nothing, and a collector that finds nobody says
    nothing too - the offender list is legitimately empty today, so the empty
    list proves nothing on its own. Plant a class that MUST come out the far end
    and push it through the same three stages the guard uses (the filter, the
    detector, the exemption sieve), alongside the real in-tree items.
    """

    class Planted(ui.Button):
        async def interaction_check(self, interaction, /):
            return False

    try:
        candidates = items_with_own_check(_walked_items() + [Planted])
        assert Planted in candidates, "the filter dropped a class that overrides the check"

        results = {}
        for cls in candidates:
            results[f"{cls.__module__}.{cls.__qualname__}"] = (
                await applies_interaction_locale(cls)
            )
        offenders = unexempted_offenders(results, LOCALE_EXEMPT)

        assert offenders == [f"{__name__}.{Planted.__qualname__}"], offenders
    finally:
        del Planted
        gc.collect()


async def test_an_item_check_without_the_locale_is_reported():
    """Aim the detector at the shape this guard exists for: a speaking Button gate."""

    class Gatekeeper(ui.Button):
        async def interaction_check(self, interaction, /):
            return False

    try:
        assert await applies_interaction_locale(Gatekeeper) is False
    finally:
        del Gatekeeper
        gc.collect()


async def test_an_item_check_that_applies_the_locale_is_cleared():
    """...and the fix shape it must NOT report."""

    class Polite(ui.Button):
        async def interaction_check(self, interaction, /):
            await i18n.apply_interaction_locale(interaction)
            return False

    try:
        assert await applies_interaction_locale(Polite) is True
    finally:
        del Polite
        gc.collect()


async def test_the_item_walk_actually_covered_the_tree():
    """The counter for a guard whose success is an empty list.

    163 Item subclasses live in cogs/ + tools/ today (155 once the eight
    DynamicItem roots are handed to :func:`_scan`), so a floor of 100 catches a
    collapsed walk without tripping on ordinary churn.
    """
    _results, walked = await _scan_item_checks()
    assert walked >= 100, walked


async def test_no_item_level_check_answers_in_english():
    """An item gate that renders text must install the locale itself.

    Empty today: the only in-tree override on the item side is
    ``tools.views.LocaleDynamicItem``, which :func:`_scan` owns. The moment
    somebody adds a Button or Select with a refusal of its own, this is where it
    gets told that the view's base will NOT have run yet.
    """
    results, _walked = await _scan_item_checks()

    offenders = unexempted_offenders(results, LOCALE_EXEMPT)

    assert not offenders, (
        "these ITEM checks run before the view's and leave i18n.current_locale "
        "on the English default, so any _() they evaluate answers in the wrong "
        "language. Call tools.i18n.apply_interaction_locale at the top of the "
        "check, or add a WRITTEN reason to LOCALE_EXEMPT:\n  "
        + "\n  ".join(offenders)
    )


async def test_no_stale_exemptions():
    """An exemption whose class is fixed, renamed or gone must go with it."""
    results, _tally, _skipped = await _scan()
    item_results, _walked = await _scan_item_checks()

    stale = stale_exemptions({**results, **item_results}, LOCALE_EXEMPT)
    assert not stale, (
        "these LOCALE_EXEMPT entries no longer name an offender (the class now "
        "applies the locale, or no longer exists). Delete them - a dead "
        "exemption is a silent pass waiting for the name to be reused:\n  "
        + "\n  ".join(stale)
    )
