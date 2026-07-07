"""Unit tests for tools.modchecks.hierarchy_error (pure, fakes only)."""

from tools import modchecks


class _Role:
    def __init__(self, position):
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


class _Member:
    def __init__(self, uid, top_role_pos):
        self.id = uid
        self.top_role = _Role(top_role_pos)


class _Guild:
    def __init__(self, owner_id, bot_top_pos, members):
        self.owner_id = owner_id
        self.me = _Member(999, bot_top_pos)
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)


class _Ctx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild


def _ctx(author, guild):
    return _Ctx(author, guild)


def test_cannot_action_self():
    author = _Member(1, 10)
    guild = _Guild(owner_id=100, bot_top_pos=20, members=[author])
    assert modchecks.hierarchy_error(_ctx(author, guild), author) is not None


def test_cannot_action_owner():
    author = _Member(1, 10)
    owner = _Member(100, 5)
    guild = _Guild(owner_id=100, bot_top_pos=20, members=[author, owner])
    assert modchecks.hierarchy_error(_ctx(author, guild), owner) is not None


def test_cannot_action_equal_or_higher_role():
    author = _Member(1, 5)
    target = _Member(2, 10)  # outranks the author
    guild = _Guild(owner_id=100, bot_top_pos=20, members=[author, target])
    assert modchecks.hierarchy_error(_ctx(author, guild), target) is not None


def test_bot_not_high_enough():
    author = _Member(1, 10)
    target = _Member(2, 5)
    guild = _Guild(owner_id=100, bot_top_pos=3, members=[author, target])
    assert modchecks.hierarchy_error(_ctx(author, guild), target) is not None


def test_allowed_when_author_outranks_and_bot_high_enough():
    author = _Member(1, 10)
    target = _Member(2, 5)
    guild = _Guild(owner_id=100, bot_top_pos=20, members=[author, target])
    assert modchecks.hierarchy_error(_ctx(author, guild), target) is None


def test_owner_may_action_a_higher_role_target():
    author = _Member(100, 1)  # low role but owns the guild
    target = _Member(2, 10)
    guild = _Guild(owner_id=100, bot_top_pos=20, members=[author, target])
    assert modchecks.hierarchy_error(_ctx(author, guild), target) is None


def test_target_not_in_guild_only_self_check_applies():
    author = _Member(1, 10)
    target = _Member(2, 5)  # not registered in the guild
    guild = _Guild(owner_id=100, bot_top_pos=1, members=[author])
    # no hierarchy to compare -> allowed (a hackban by id, say)
    assert modchecks.hierarchy_error(_ctx(author, guild), target) is None
