"""Guards for the /preferences panel: component budget, timeout, select payload.

Everything here builds the real :class:`~cogs.community.usersettings.SettingsView`
(no network, no DB): the panel is assembled from module-level lists, so its shape
is fully checkable offline - and that is exactly where a component-budget overflow
or a still-clickable control after the timeout has to be caught, since production
would only show them as an opaque Discord 400 or a silently failing click.
"""

import types

import discord

from cogs.community import usersettings as us


def _view(**kwargs):
    bot = types.SimpleNamespace(db_pool=object())
    author = types.SimpleNamespace(id=42)
    return us.SettingsView(bot, author, kwargs.pop("states", {}), kwargs.pop("choices", {}))


class _Interaction:
    """Just enough interaction for the handled paths (responses are stubbed out)."""

    def __init__(self):
        self.edited = []
        self.response = types.SimpleNamespace(
            edit_message=self._edit_message, is_done=lambda: False
        )

    async def _edit_message(self, **kwargs):
        self.edited.append(kwargs)


# ---------------------------------------------------------------------------
# component budget
# ---------------------------------------------------------------------------
def test_the_component_formula_matches_what_the_panel_really_emits():
    # The bound below is only worth anything if the formula counts the same things
    # Discord does, so pin it against the rendered view (nested children included).
    view = _view()
    assert len(list(view.walk_children())) == us._component_count(
        len(us.PREFS[: us.MAX_PREFS]), len(us.CHOICE_PREFS)
    )


def test_max_prefs_is_the_largest_bound_that_still_fits():
    # 5 fixed + 4 per toggle + 4 per select <= 40. The old value (10) claimed 49
    # components with today's single select, i.e. a guaranteed 400.
    assert us._component_count(us.MAX_PREFS, len(us.CHOICE_PREFS)) <= us.COMPONENT_CAP
    assert us._component_count(us.MAX_PREFS + 1, len(us.CHOICE_PREFS)) > us.COMPONENT_CAP
    assert us.MAX_PREFS == 7
    assert us._component_count(10, 1) == 49  # what the stale bound promised


def test_the_shipped_panel_fits_with_room_to_spare():
    assert len(us.PREFS) <= us.MAX_PREFS  # nothing is silently dropped today
    assert len(list(_view().walk_children())) <= us.COMPONENT_CAP


def test_building_an_oversized_panel_fails_fast(monkeypatch):
    # Over the cap must raise HERE, with the arithmetic in the message, rather than
    # ship a payload Discord answers with an opaque 400 and no panel at all.
    monkeypatch.setattr(us, "PREFS", list(us.PREFS) * 3)  # 18 toggles
    monkeypatch.setattr(us, "MAX_PREFS", 18)
    over = us._component_count(18, len(us.CHOICE_PREFS))
    try:
        _view()
    except RuntimeError as exc:
        assert str(over) in str(exc) and str(us.COMPONENT_CAP) in str(exc)
    else:
        raise AssertionError("an oversized panel must not build")


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------
async def test_timeout_disables_the_select_as_well_as_the_buttons():
    # A select left enabled outlives the view: the click dispatches nowhere and the
    # member gets a silent failure with the panel still looking alive.
    view = _view()
    selects = [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]
    buttons = [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert selects and buttons  # the panel really has both kinds

    await view.on_timeout()

    assert all(child.disabled for child in selects + buttons)


# ---------------------------------------------------------------------------
# select payload
# ---------------------------------------------------------------------------
async def test_an_empty_select_payload_is_handled_not_raised(monkeypatch):
    # Reading values[0] outside the handled path raised an IndexError that answered
    # nothing (Discord's "interaction failed" with no log line and no write).
    failures = []

    async def _notify(interaction, message):
        failures.append(message)

    async def _set_user(*args, **kwargs):
        raise AssertionError("an empty payload must write nothing")

    monkeypatch.setattr(us, "notify_failure", _notify)
    monkeypatch.setattr(us.settings, "set_user", _set_user)

    view = _view()
    pref = us.CHOICE_PREFS[0]
    interaction = _Interaction()

    await view.choose(interaction, pref, [])

    assert len(failures) == 1  # answered, so no "interaction failed"
    assert interaction.edited == []  # and nothing re-rendered
    assert pref.key not in view.choices


async def test_a_normal_pick_is_persisted_and_rerendered(monkeypatch):
    written = []

    async def _set_user(pool, user_id, key, value):
        written.append((user_id, key, value))

    monkeypatch.setattr(us.settings, "set_user", _set_user)

    view = _view()
    pref = us.CHOICE_PREFS[0]
    interaction = _Interaction()

    await view.choose(interaction, pref, ["fr", "ignored"])

    assert written == [(42, pref.key, "fr")]
    assert view.choices[pref.key] == "fr"
    assert len(interaction.edited) == 1


async def test_the_select_hands_over_its_raw_payload():
    # The panel does the indexing, so the select must NOT index for it.
    seen = []

    class _Panel:
        async def choose(self, interaction, pref, values):
            seen.append(values)

    select = us.ChoiceSelect(_Panel(), us.CHOICE_PREFS[0], us.CHOICE_PREFS[0].default)
    select._values = []  # what discord.py fills from the interaction payload
    await select.callback(_Interaction())
    select._values = ["fr"]
    await select.callback(_Interaction())

    assert seen == [[], ["fr"]]
