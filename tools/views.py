"""Reusable discord.ui.View base classes.

This module hosts the shared View building blocks that were previously
copy-pasted across the cogs. Keeping a single canonical implementation means
the author gating and timeout cleanup behave identically everywhere and only
have to be fixed in one place.

THE LOCALE RULE. A component click, a modal submit and a dynamic-item click all
run in a task discord.py creates from the gateway coroutine, whose context has
``i18n.current_locale`` at its DEFAULT ("en") - ``Yasuho.get_context`` only ever
set it for command invocations. Any ``_()`` called from such a callback
therefore answers in English unless something applied the clicker's locale
first. The only hook discord.py runs before a callback is a check, so the apply
lives in a check and NOWHERE else:

* :class:`LocaleView` / :class:`LocaleLayoutView` - ``interaction_check``, which
  discord.py calls after the item checks and before the callback.
* :class:`LocaleModal` - ``interaction_check``, called before ``on_submit``.
* :class:`LocaleDynamicItem` - the ITEM's ``interaction_check``; on a dynamic
  item discord.py never consults the enclosing view's check, so a dynamic item
  must carry its own.

One shape has no base here because none exists in the tree: a check on an
ordinary Button / Select / Container. Those run BEFORE the view's
(``ui/view.py:591`` is ``item._run_checks`` and THEN
``self.interaction_check``), so a spotless root does not help them - an item
gate that renders text must call ``i18n.apply_interaction_locale`` itself.
``tests/test_view_locale_hygiene.py`` scans for them too.

Every dispatch root in cogs/ and tools/ derives from one of those four (usually
through :class:`AuthorView` / :class:`AuthorLayoutView`), including the
display-only cards: a card with no dispatchable component never has its check
called, so the base costs it nothing and nobody has to re-decide "is this view
interactive?" when they add the first button.

A subclass that overrides ``interaction_check`` for a gate of its own SHOULD
chain through ``super()`` - that is the house shape, and the only one that stays
correct when a base changes. Be precise about what the guard actually enforces,
though: ``tests/test_view_locale_hygiene.py`` is BEHAVIOURAL, not a grep for
``super()``. It fails the build when a dispatch root's check leaves the locale
on English, whatever the reason; a check that hand-rolls
``i18n.apply_interaction_locale`` itself and never calls ``super()`` passes it
(seven cog-level checks in the tree do exactly that today). So a missing
``super()`` hop is NOT mechanically caught - only a missing locale is. If you
drop the hop you also drop whatever gate the base was applying, and nothing
here will tell you.

THE RENDER RULE, the companion rule, lives on :class:`PinnedRenderLocale` below:
the locale a check installs is the CLICKER's, which is right for what only the
clicker reads and wrong for the body of a shared message.

THE ERROR RULE. discord.py's default ``View.on_error`` / ``LayoutView.on_error``
/ ``Modal.on_error`` only log "Ignoring exception in view/modal ..." - the user
is left on Discord's own "This interaction failed", with no id to report and no
way for anyone to find the traceback it came from. Every base below (and
:class:`LocaleDynamicItem`, which has no ``on_error`` to override at all - see
its docstring) routes a callback crash through
:func:`tools.interactions.report_component_error`: one ``secrets.token_hex(4)``
id logged at ERROR with the traceback, then the SAME id told to the user in an
ephemeral reply that respects whatever ``response.is_done()`` already is. This
is the component-dispatch twin of ``cogs/system/errors.py``'s command-side
``_safe_send`` ladder, and deliberately reuses its id scheme.
"""

from __future__ import annotations

import typing

import discord

from tools import i18n, interactions
from tools.i18n import N_, _

# Deny wordings used as AuthorView.deny_message across the cogs. They are stored
# on the view at construction time (outside the clicker's task) and translated
# at send time in interaction_check, so the literals are registered here with N_
# to be extractable. Add any new deny wording here so it gets translated.
_DENY_STRINGS = [
    N_("This menu isn't for you."),
    N_("This panel isn't for you."),
    N_("This prompt isn't for you."),
    N_("This profile editor isn't for you."),
    N_("This isn't your game, start your own with the command!"),
]


def _item_repr(item) -> str:
    """Short id for a failing component: label/custom_id, else just the class.

    Used only to build the ``where`` string for
    :func:`tools.interactions.report_component_error`'s log line - never shown
    to the user, so there is no translation concern here. ``label`` and
    ``custom_id`` are the two attributes that actually distinguish one button
    from another in a log; most of the house's items have both, a few
    (selects) only the second.
    """
    if item is None:
        return "?"
    label = getattr(item, "label", None)
    custom_id = getattr(item, "custom_id", None)
    bits = [str(bit) for bit in (label, custom_id) if bit]
    return " ".join(bits) if bits else type(item).__name__


class _ReportsComponentErrors:
    """Shared ``on_error`` body for :class:`LocaleView` and :class:`LocaleLayoutView`.

    Both bases they front for - ``discord.ui.View`` and ``discord.ui.LayoutView``
    - are siblings that inherit this exact ``on_error(self, interaction, error,
    item)`` signature from the private ``BaseView`` (discord.py 2.7.1,
    ``ui/view.py``), so one body correctly serves both: a plain mixin ahead of
    either in the MRO overrides the framework default without duplicating
    anything. See THE ERROR RULE at the top of this module.
    """

    async def on_error(self, interaction, error, item):
        where = f"{type(self).__name__} item={_item_repr(item)}"
        await interactions.report_component_error(interaction, error, where=where)


class LocaleView(_ReportsComponentErrors, discord.ui.View):
    """A plain View whose callbacks run in the clicker's locale.

    The single reason to exist: ``interaction_check`` resolves and installs the
    interaction's locale, so every ``_()`` in the item callbacks below it
    renders in the clicker's language instead of the gateway task's English
    default. It adds no gate - it always returns ``True`` - so swapping
    ``discord.ui.View`` for this base never changes who may click.

    Subclasses with a gate of their own override ``interaction_check`` and chain
    through ``super()``::

        async def interaction_check(self, interaction):
            if not await super().interaction_check(interaction):
                return False
            return await my_gate(interaction)

    A callback or ``interaction_check`` that raises is caught by discord.py and
    handed to ``on_error`` (see :class:`_ReportsComponentErrors`): the user gets
    one ephemeral reply with an error id instead of Discord's bare "This
    interaction failed", and the id is in the log next to the traceback.
    """

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True


class AuthorView(LocaleView):
    """A View that only its originating author may interact with.

    Subclasses add their own components (buttons, selects, modals) exactly as
    they would on a plain :class:`discord.ui.View`. This base only supplies two
    behaviours:

    * ``interaction_check`` rejects anyone other than ``author_id`` with an
      ephemeral ``deny_message``.
    * ``on_timeout`` disables every child and edits the bound ``message`` so the
      components stop responding once the View expires.

    Both ``timeout`` and ``deny_message`` are overridable per instance. Assign
    the sent message to ``self.message`` (e.g. ``view.message = await ctx.send(...)``)
    so the timeout cleanup has something to edit.

    Subclasses MAY extend either hook and should call ``super()`` to keep the
    base behaviour, for example::

        async def interaction_check(self, interaction):
            if not await super().interaction_check(interaction):
                return False
            ...  # extra checks
            return True

        async def on_timeout(self):
            ...  # extra cleanup
            await super().on_timeout()
    """

    def __init__(self, author_id, *, timeout=180, deny_message="This menu isn't for you."):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None
        self._deny_message = deny_message

    async def interaction_check(self, interaction):
        # LocaleView resolves the clicker's locale first, so this check AND the
        # callback below it localize.
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.author_id:
            # Translate in the clicker's locale (the stored wording is a
            # registered N_ literal, see _DENY_STRINGS).
            await interaction.response.send_message(
                _(self._deny_message), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# Component types a LayoutView disables on timeout (buttons + every select
# flavour). None of ChannelSelect/RoleSelect/UserSelect/MentionableSelect are
# subclasses of ui.Select (they share a private BaseSelect instead), so each is
# listed explicitly - otherwise a RoleSelect etc. would stay clickable forever.
_DISABLEABLE = (
    discord.ui.Button,
    discord.ui.Select,
    discord.ui.ChannelSelect,
    discord.ui.RoleSelect,
    discord.ui.UserSelect,
    discord.ui.MentionableSelect,
)


class LocaleLayoutView(_ReportsComponentErrors, discord.ui.LayoutView):
    """The Components V2 twin of :class:`LocaleView`.

    ``LayoutView`` is a sibling of ``View`` in discord.py (both inherit the
    private ``BaseView``), not a subclass, so the locale apply cannot simply be
    inherited from :class:`LocaleView` and is reimplemented here against the one
    shared ``i18n.apply_interaction_locale``. Adds no gate: it always returns
    ``True``, so re-basing a layout onto it never changes who may click.

    ``on_error`` is inherited from :class:`_ReportsComponentErrors` - the
    signature is identical to :class:`LocaleView`'s (both come from ``BaseView``)
    so the same mixin body serves this sibling too.
    """

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True


class AuthorLayoutView(LocaleLayoutView):
    """A Components V2 LayoutView gated to its originating author.

    LayoutView cannot subclass :class:`AuthorView` (that is a plain
    ``discord.ui.View``), so the author gate AuthorView normally supplies is
    reimplemented here on top of :class:`LocaleLayoutView`:
    :meth:`interaction_check` chains to the base for the clicker's locale then
    rejects anyone but ``author_id`` (using the same registered deny wording,
    see ``_DENY_STRINGS``), and :meth:`on_timeout` disables every control and
    edits the bound ``message`` in place. Subclasses assemble their own
    :class:`~discord.ui.Container` and set ``self.message`` so the timeout
    cleanup has something to edit.
    """

    def __init__(self, author_id, *, timeout=180, deny_message="This panel isn't for you."):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message = None
        self._deny_message = deny_message

    async def interaction_check(self, interaction):
        # LocaleLayoutView resolves the clicker's locale first, so this check
        # AND the callback below it localize.
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.author_id:
            # Translate in the clicker's locale (the stored wording is a
            # registered N_ literal, see _DENY_STRINGS). Overridable per view so
            # a migrated surface can keep its original deny wording.
            await interaction.response.send_message(
                _(self._deny_message), ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        """Disable every button/select in the layout (walks nested ActionRows)."""

        for child in self.walk_children():
            if isinstance(child, _DISABLEABLE):
                child.disabled = True

    async def on_timeout(self):
        """Grey out the controls in place, WITHOUT ever notifying anyone.

        A Components V2 message carries its text inside the view, so this edit
        resends every TextDisplay - and several layouts legitimately hold raw
        ``<@id>`` / ``<@&id>`` tokens (the hall-of-fame podium, the seasons
        panel's champion role, the reminders card...). discord.py folds the
        CLIENT default (core.Yasuho: users=True) into any edit that does not
        say otherwise (verified: Message.edit passes
        previous_allowed_mentions=state.allowed_mentions), so an unsuppressed
        timeout edit would re-parse those tokens and ping their targets three
        minutes after the fact, for a message nobody touched.
        Suppressing every mention here is safe for ALL consumers: a timeout
        edit only DISABLES controls, it never adds content, so no surface has a
        legitimate reason to (re)notify at that moment - the ones that do mean
        to ping say so on their own send/edit (see cogs/community/leveling/seasons.py's
        announce, which passes its own AllowedMentions).
        """
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(
                    view=self, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                pass


class LocaleModal(discord.ui.Modal):
    """A Modal whose submit callback runs in the interaction's resolved locale.

    Modal submit callbacks run in their own task, where ``Yasuho.get_context``
    never set the i18n locale; resolving it in ``interaction_check`` makes the
    modal's ``_()`` calls localize for the submitter. Subclass this instead of
    ``discord.ui.Modal`` for any modal with user-facing (translatable) text.

    Subclasses that need their own ``interaction_check`` should call
    ``super().interaction_check(interaction)`` to keep the locale resolution.

    A raising ``on_submit`` (or ``interaction_check``) is routed through
    :func:`tools.interactions.report_component_error` the same way as the View
    bases above - see THE ERROR RULE at the top of this module. ``Modal.on_error``
    carries no ``item`` argument (discord.py 2.7.1, ``ui/modal.py``), so the
    description falls back to the modal's own ``custom_id``.
    """

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return True

    async def on_error(self, interaction, error):
        where = f"{type(self).__name__} modal custom_id={getattr(self, 'custom_id', None)}"
        await interactions.report_component_error(interaction, error, where=where)


# A template that can never match a custom_id: the abstract base below has to
# pass one (``DynamicItem.__init_subclass__`` requires it) but must never claim
# a click of its own. ``(?!)`` is a negative lookahead on the empty string, so
# it fails at position 0 for every input, the empty string included.
_NEVER_MATCHES = r"(?!)"

_ItemT = typing.TypeVar("_ItemT", bound=discord.ui.Item)


def _wrap_dynamic_item_callback(callback):
    """Wrap a dynamic item's ``callback`` so a crash is logged and reported.

    See :meth:`LocaleDynamicItem.__init_subclass__` for WHY this exists instead
    of an ``on_error`` override: the library gives dynamic items no such hook.
    The wrapper never re-raises - ``ViewStore.schedule_dynamic_item_call``'s own
    try/except is still there behind it, but by construction has nothing left
    to catch.
    """

    async def _reporting_callback(self, interaction):
        try:
            return await callback(self, interaction)
        except Exception as error:
            where = (
                f"{type(self).__name__} dynamic_item "
                f"custom_id={getattr(self, 'custom_id', None)}"
            )
            await interactions.report_component_error(interaction, error, where=where)

    _reporting_callback.__name__ = getattr(callback, "__name__", "callback")
    _reporting_callback.__qualname__ = getattr(
        callback, "__qualname__", _reporting_callback.__name__
    )
    _reporting_callback.__doc__ = callback.__doc__
    _reporting_callback.__wrapped__ = callback
    return _reporting_callback


class LocaleDynamicItem(discord.ui.DynamicItem[_ItemT], template=_NEVER_MATCHES):
    """A DynamicItem whose callback runs in the clicker's resolved locale.

    Dynamic items are the one dispatch path where the enclosing view's
    ``interaction_check`` is NEVER consulted: ``ViewStore.schedule_dynamic_item_call``
    awaits ``item.interaction_check(interaction)`` and then ``item.callback(...)``
    (verified in the installed discord.py 2.7.1, ``ui/view.py``). So the apply
    has to live on the item, and ``Item.interaction_check`` - which defaults to
    returning ``True`` - is the only hook available before the callback.
    Subclass it with the real template::

        class SeenButton(LocaleDynamicItem[discord.ui.Button], template=r"..."):
            ...

    Adds no gate. ``DynamicItem.interaction_check`` is NOT a do-nothing default
    like ``Item``'s: it delegates to the WRAPPED item's own check
    (``return await self.item.interaction_check(interaction)``), so this
    override applies the locale and then hands the decision straight back to
    ``super()``. Replacing that delegation with a bare ``return True`` would
    quietly drop a gate a wrapped item carried - none does today, which is
    exactly why it would have gone unnoticed.
    """

    async def interaction_check(self, interaction):
        await i18n.apply_interaction_locale(interaction)
        return await super().interaction_check(interaction)

    def __init_subclass__(cls, **kwargs) -> None:
        """Wrap a subclass's own ``callback`` so a crash is reported. See THE ERROR RULE.

        discord.py NEVER calls ``on_error`` for a dynamic item - confirmed two
        ways in the installed 2.7.1 source: ``Item.interaction_check``'s own
        docstring says so outright ("For :class:`~discord.ui.DynamicItem` this
        does not call the ``on_error`` handler", ``ui/item.py``), and
        ``ViewStore.schedule_dynamic_item_call`` (``ui/view.py``) calls
        ``await item.callback(interaction)`` inside ITS OWN try/except that only
        ``_log.exception``s - there is no hook anywhere on that path for a
        subclass to override. So instead of an ``on_error`` that would never
        run, the subclass's ``callback`` itself is wrapped once, here, at
        class-definition time, with the identical try/except/report body every
        other base gets through ``on_error``.

        Only wraps a ``callback`` the subclass defines ITSELF (``cls.__dict__``,
        not one it inherited): a further subclass that adds no new ``callback``
        already got one wrapped at its parent's definition, so re-wrapping would
        be pointless double indirection, not a second report (the wrapper never
        re-raises, so there is nothing left for an outer wrap to catch).
        """
        super().__init_subclass__(**kwargs)
        own_callback = cls.__dict__.get("callback")
        if own_callback is not None:
            cls.callback = _wrap_dynamic_item_callback(own_callback)


class PinnedRenderLocale:
    """Mixin: the BODY of this view's message always renders in ONE language.

    THE RENDER RULE. The locale a check installs (see THE LOCALE RULE at the top
    of this module) is the CLICKER's. That is exactly right for what only the
    clicker reads - an ephemeral refusal, an ephemeral confirmation - and exactly
    wrong for the body of a message several people and a background task all
    re-render in place. A live now-playing panel is edited by whoever presses
    Pause AND by the 60s progress tick AND by the track-change repost; a live
    lyrics card is edited by its poller every few seconds. Let each of those
    render in "whatever locale this particular caller happens to be in" and one
    public message visibly alternates languages: ``### Now Playing`` becomes
    ``### Lecture en cours`` when a French member clicks Pause, then flips back
    on the next tick, because a poller carries no locale at all and falls to the
    English default.

    The fix is not "give the pollers a locale too" - that only narrows the
    flip-flop to clicker-vs-guild. A message gets ONE language, decided once:
    this mixin pins whatever locale is current at the FIRST build (i.e. the view's
    construction) and forces every later build back into it, whoever triggered
    it. A caller that wants a specific language for a surface it posts from a
    context with no locale of its own - the controller's background poster - just
    constructs the view inside ``i18n.locale(...)``; see
    ``Music._send_controller``, which resolves the GUILD locale so a public panel
    speaks the server's language rather than the English default.

    Mixing it in: put it first in the bases, rename the layout builder from
    ``_build`` to ``_compose``, and keep calling ``self._build(...)`` everywhere.
    ``_build`` below is then the only door into a render, so a call site added
    later cannot forget the pin::

        class NowPlaying(PinnedRenderLocale, LocaleLayoutView):
            def __init__(self, ...):
                super().__init__(timeout=None)
                ...
                self._build()          # pins here

            def _compose(self) -> None:
                ...                    # runs in the pinned locale, always

    Only for a message whose body OUTLIVES the render that produced it and can
    be re-rendered by somebody else. An author-gated card (``AuthorView`` /
    ``AuthorLayoutView``) already has exactly one possible clicker and needs
    none of this; neither does an ephemeral card, which nobody else can see.
    """

    #: The pinned language, or None until the first build takes the pin.
    _render_locale: typing.Optional[str] = None

    def _compose(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        """Assemble the layout. Implemented by the subclass; never called directly."""
        raise NotImplementedError

    def _build(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        """Render the body in this message's pinned language.

        The first call takes the pin from the current context, so a view built
        inside ``i18n.locale(loc)`` renders in ``loc`` forever after.
        """
        if self._render_locale is None:
            self._render_locale = i18n.current_locale.get()
        with i18n.locale(self._render_locale):
            return self._compose(*args, **kwargs)

    def _repin_render_locale(self, loc: typing.Optional[str]) -> None:
        """Move the pin to ``loc``, for an EVENT surface at a message boundary.

        The pin above is deliberately taken once and never re-taken, because the
        whole point is that no later caller gets to impose its own language. That
        is final for a COMMAND-created surface: it keeps the language of whoever
        ran the command, for as long as the message lives.

        An EVENT-created surface is different. Its language is not a person's, it
        is the GUILD's (see ``Music._send_controller``), so an admin running
        ``/language`` changes what it should be saying - and a panel that is only
        ever re-rendered in place would otherwise keep the old language until a
        repost that may never come. Such a surface calls this at a natural
        boundary, and ONLY there: the controller re-pins when the track changes,
        which is the moment its whole body is redrawn anyway.

        NOT on a click and NOT on a background tick. Re-pinning from the current
        context on every render is precisely the flip-flop this mixin exists to
        prevent, so the caller passes a language it resolved from the GUILD, never
        ``i18n.current_locale.get()``. A falsy ``loc`` (nothing could be resolved)
        leaves the pin alone: keeping the language the message already speaks
        always beats falling back to the English default.
        """
        if loc:
            self._render_locale = loc
