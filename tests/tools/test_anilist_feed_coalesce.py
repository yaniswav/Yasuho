"""Unit tests for tools.anilist_feed_coalesce (pure coalescing engine).

decide_delivery is the whole contract: given an incoming normalised activity, the
live coalescing record for its (channel, user, media) slot (or None), and "now",
return POST_NEW (fresh card, maybe recorded) or EDIT(message_id) (silent in-place
edit). These tests pin the happy fold, every reason a fresh card is forced
(no record, session gap, age cap, status change, backwards/unparseable progress,
non-list activity), the record flag, and the prune predicate.
"""

from datetime import datetime, timedelta, timezone

from tools import anilist_feed_coalesce as afc

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def _list_activity(*, progress="54", status="CURRENT", kind="ListActivity"):
    return {
        "id": 999,
        "kind": kind,
        "user_id": 7,
        "status": status,
        "progress": progress,
        "media": {"id": 42},
    }


def _record(
    *,
    message_id=555,
    status="CURRENT",
    last_progress="50",
    created_age=timedelta(minutes=10),
    updated_age=timedelta(minutes=2),
):
    return afc.CoalesceRecord(
        message_id=message_id,
        status=status,
        last_progress=last_progress,
        created_at=NOW - created_age,
        updated_at=NOW - updated_age,
    )


# ---------------------------------------------------------------------------
# progress_value
# ---------------------------------------------------------------------------


def test_progress_value_single_and_range_take_the_max():
    assert afc.progress_value("54") == 54
    assert afc.progress_value("50 - 54") == 54
    assert afc.progress_value("1 - 12") == 12


def test_progress_value_int_passthrough_and_nonnumeric_none():
    assert afc.progress_value(54) == 54
    assert afc.progress_value(None) is None
    assert afc.progress_value("") is None
    assert afc.progress_value("volume") is None


def test_progress_value_bool_is_not_a_number():
    assert afc.progress_value(True) is None


# ---------------------------------------------------------------------------
# is_coalescible
# ---------------------------------------------------------------------------


def test_only_list_activity_with_progress_is_coalescible():
    assert afc.is_coalescible(_list_activity()) is True
    assert afc.is_coalescible(_list_activity(progress=None)) is False
    assert afc.is_coalescible({"kind": "TextActivity", "text": "hi"}) is False


# ---------------------------------------------------------------------------
# decide_delivery - fresh post cases
# ---------------------------------------------------------------------------


def test_first_post_no_record_is_post_new_and_recorded():
    d = afc.decide_delivery(_list_activity(), None, NOW)
    assert d.action == afc.POST_NEW
    assert d.message_id is None
    assert d.record is True


def test_text_activity_is_post_new_never_recorded():
    d = afc.decide_delivery({"kind": "TextActivity", "text": "hi"}, None, NOW)
    assert d.action == afc.POST_NEW
    assert d.record is False


def test_list_activity_without_progress_is_post_new_never_recorded():
    d = afc.decide_delivery(_list_activity(progress=None), None, NOW)
    assert d.action == afc.POST_NEW
    assert d.record is False


def test_status_change_forces_fresh_card_but_records():
    rec = _record(status="CURRENT")
    d = afc.decide_delivery(_list_activity(status="COMPLETED"), rec, NOW)
    assert d.action == afc.POST_NEW
    assert d.message_id is None
    assert d.record is True


def test_progress_backwards_forces_fresh_card():
    rec = _record(last_progress="54")
    d = afc.decide_delivery(_list_activity(progress="50"), rec, NOW)
    assert d.action == afc.POST_NEW
    assert d.record is True


def test_unparseable_stored_progress_forces_fresh_card():
    rec = _record(last_progress="volume")
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.POST_NEW


def test_past_session_gap_forces_fresh_card():
    rec = _record(updated_age=timedelta(seconds=afc.SESSION_GAP + 1))
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.POST_NEW
    assert d.record is True


def test_past_age_cap_forces_fresh_card_even_within_session():
    rec = _record(
        created_age=timedelta(seconds=afc.AGE_CAP + 1),
        updated_age=timedelta(seconds=30),  # session still live
    )
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.POST_NEW
    assert d.record is True


# ---------------------------------------------------------------------------
# decide_delivery - the EDIT (fold) case
# ---------------------------------------------------------------------------


def test_progress_increment_within_windows_edits_in_place():
    rec = _record(message_id=777, last_progress="50")
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.EDIT
    assert d.message_id == 777
    assert d.record is True


def test_equal_progress_still_edits():
    """A re-save at the same progress folds (>=), it does not spawn a card."""
    rec = _record(last_progress="54")
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.EDIT


def test_edit_holds_right_up_to_the_window_boundaries():
    rec = _record(
        created_age=timedelta(seconds=afc.AGE_CAP),
        updated_age=timedelta(seconds=afc.SESSION_GAP),
    )
    d = afc.decide_delivery(_list_activity(progress="54"), rec, NOW)
    assert d.action == afc.EDIT


def test_range_progress_advances_over_prior_single():
    rec = _record(last_progress="50")
    d = afc.decide_delivery(_list_activity(progress="50 - 54"), rec, NOW)
    assert d.action == afc.EDIT


# ---------------------------------------------------------------------------
# is_prunable
# ---------------------------------------------------------------------------


def test_prunable_only_past_age_cap_plus_grace():
    fresh = _record(updated_age=timedelta(seconds=afc.AGE_CAP + afc.PRUNE_GRACE - 1))
    dead = _record(updated_age=timedelta(seconds=afc.AGE_CAP + afc.PRUNE_GRACE + 1))
    assert afc.is_prunable(fresh, NOW) is False
    assert afc.is_prunable(dead, NOW) is True


def test_prunable_missing_updated_at_is_dead():
    rec = afc.CoalesceRecord(
        message_id=1, status="CURRENT", last_progress="1",
        created_at=NOW, updated_at=None,
    )
    assert afc.is_prunable(rec, NOW) is True


# ---------------------------------------------------------------------------
# custom window overrides
# ---------------------------------------------------------------------------


def test_session_gap_override_is_honoured():
    rec = _record(updated_age=timedelta(seconds=120))
    assert afc.decide_delivery(_list_activity(), rec, NOW).action == afc.EDIT
    tight = afc.decide_delivery(_list_activity(), rec, NOW, session_gap=60)
    assert tight.action == afc.POST_NEW
