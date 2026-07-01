"""Construction smoke test for the autoroom control components.

Regression guard for a production crash: the per-room control sub-components set
a reference to their owning view in __init__. It was named ``self.parent``, but
``discord.ui.Item.parent`` is a READ-ONLY property in Components V2, so building
any of them raised "AttributeError: property 'parent' ... has no setter" and
every room-control action failed. import-smoke only imports the module (the class
bodies); it does not instantiate, so it could not catch this. These tests build
the components and would fail again if a reserved discord.ui attribute name
(parent / view) were assigned in an __init__.
"""

from cogs.config import rooms


class _FakeOwner:
    """Stand-in for the RoomControlView the components reference."""


def test_slot_select_constructs():
    rooms._SlotSelect(_FakeOwner())


def test_member_action_select_constructs():
    rooms._MemberActionSelect(_FakeOwner(), [], "kick")


def test_room_sub_view_wraps_a_child():
    rooms._RoomSubView(rooms._SlotSelect(_FakeOwner()))
