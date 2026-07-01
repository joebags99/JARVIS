"""Tests for the overlay's proactive-notification bookkeeping (app/overlay.py).

Overlay.__init__ needs pywebview (a native dependency this test suite
otherwise avoids — see requirements-dev.txt), so these construct it via
__new__ and set only the attributes record_notification()/_get_notifications()
touch, the same bypass pattern used for WakeWordListener/Speaker elsewhere in
this suite.
"""

from __future__ import annotations

from app.overlay import Overlay, _MAX_NOTIFICATIONS


def _bare_overlay() -> Overlay:
    overlay = Overlay.__new__(Overlay)
    overlay._notifications = []
    overlay._unread_notifications = 0
    overlay._eval_calls: list[tuple[str, tuple]] = []
    overlay._eval = lambda fn, *args: overlay._eval_calls.append((fn, args))
    return overlay


def test_record_notification_stores_and_pushes_to_the_page():
    overlay = _bare_overlay()
    overlay.record_notification("Still open?", "Old Planning: Follow up with Sam")

    assert len(overlay._notifications) == 1
    entry = overlay._notifications[0]
    assert entry["title"] == "Still open?"
    assert entry["message"] == "Old Planning: Follow up with Sam"
    assert entry["at"]  # timestamped

    fn, args = overlay._eval_calls[-1]
    assert fn == "pushNotification"
    assert args == (entry, 1)


def test_record_notification_keeps_most_recent_first():
    overlay = _bare_overlay()
    overlay.record_notification("A", "1")
    overlay.record_notification("B", "2")
    assert [n["title"] for n in overlay._notifications] == ["B", "A"]


def test_get_notifications_returns_items_and_resets_unread():
    overlay = _bare_overlay()
    overlay.record_notification("A", "1")
    overlay.record_notification("B", "2")
    assert overlay._unread_notifications == 2

    result = overlay._get_notifications()
    assert result["unread"] == 0
    assert [n["title"] for n in result["items"]] == ["B", "A"]
    assert overlay._unread_notifications == 0  # opening the panel marks all read


def test_record_notification_trims_to_max_history():
    overlay = _bare_overlay()
    for i in range(_MAX_NOTIFICATIONS + 10):
        overlay.record_notification(f"T{i}", "msg")
    assert len(overlay._notifications) == _MAX_NOTIFICATIONS
    # Most recent survives the trim; oldest are dropped.
    assert overlay._notifications[0]["title"] == f"T{_MAX_NOTIFICATIONS + 9}"
