"""Alarm delivery and the ledger that deduplicates it."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from davpunk.core import cache
from davpunk.models.task import Alarm, AlarmRelated, Status
from davpunk.notifications import alarm_scan

DUE = "20260731T170000Z"
DUE_EPOCH = int(datetime(2026, 7, 31, 17, 0, tzinfo=UTC).timestamp())


class Recorder:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, summary, body="", **_kwargs):
        self.sent.append((summary, body))
        return True


@pytest.fixture
def notifier():
    return Recorder()


@pytest.fixture
def alarmed(conn, make_task):
    """A task with one relative alarm, one hour before DUE."""

    def _make(offset=-3600, due=DUE, status=Status.NEEDS_ACTION, uid="a1"):
        task = make_task(uid, due_value=due, status=status)
        task.alarms = [Alarm(related=AlarmRelated.END, trigger_offset=offset)]
        return cache.create_task_local(task, conn)

    return _make


def fires(conn) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM alarm_fires"))


# ------------------------------------------------------------------- timing


def test_an_alarm_before_its_time_does_not_fire(conn, alarmed, notifier):
    alarmed()
    result = alarm_scan.scan(conn, now=DUE_EPOCH - 7200, notifier=notifier)
    assert result.notified == 0
    assert notifier.sent == []


def test_an_alarm_fires_once_its_trigger_has_passed(conn, alarmed, notifier):
    alarmed()
    result = alarm_scan.scan(conn, now=DUE_EPOCH - 3599, notifier=notifier)
    assert result.notified == 1
    assert len(notifier.sent) == 1


def test_five_scans_produce_exactly_one_notification(conn, alarmed, notifier):
    """The whole point of the ledger."""
    alarmed()
    for _ in range(5):
        alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)
    assert len(notifier.sent) == 1
    assert len(fires(conn)) == 1


def test_moving_the_anchor_lets_it_fire_once_more(conn, alarmed, notifier):
    """A rescheduled task legitimately alarms again: the ledger is keyed on the
    *computed* trigger time, not on the alarm row."""
    task_id = alarmed()
    alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)
    assert len(notifier.sent) == 1

    cache.update_task_optimistic(task_id, {"due_value": "20260801T170000Z"}, conn)
    later = int(datetime(2026, 8, 1, 16, 30, tzinfo=UTC).timestamp())
    alarm_scan.scan(conn, now=later, notifier=notifier)

    assert len(notifier.sent) == 2
    assert len(fires(conn)) == 2


def test_a_pull_that_rewrites_the_alarm_row_does_not_reset_the_ledger(conn, alarmed, notifier):
    """Alarm rows are dropped and reinserted on every server pull.  A flag
    stored on ``valarms`` would reset itself and fire forever."""
    task_id = alarmed()
    alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)

    with cache.tx(conn):
        cache._replace_alarms(
            task_id, [Alarm(related=AlarmRelated.END, trigger_offset=-3600)], conn
        )
    alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)

    assert len(notifier.sent) == 1


# ------------------------------------------------------------- suppression


@pytest.mark.parametrize("status", [Status.COMPLETED, Status.CANCELLED])
def test_a_finished_task_is_recorded_as_fired_but_not_shown(conn, alarmed, notifier, status):
    alarmed(status=status)
    result = alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)

    assert result.notified == 0
    assert result.suppressed == 1
    assert notifier.sent == []
    assert len(fires(conn)) == 1  # recorded, so it never resurfaces


def test_a_three_day_downtime_does_not_produce_a_flood(conn, make_task, notifier):
    """Recorded, deliberately not shown: the user does not want 40 popups when
    they open the laptop after a long weekend."""
    for n in range(40):
        task = make_task(f"old{n}", due_value=DUE)
        task.alarms = [Alarm(related=AlarmRelated.END, trigger_offset=-3600)]
        cache.create_task_local(task, conn)

    result = alarm_scan.scan(conn, now=DUE_EPOCH + 3 * 86400, notifier=notifier)

    assert result.notified == 0
    assert result.suppressed == 40
    assert len(fires(conn)) == 40


def test_the_stale_window_is_twenty_four_hours(conn, alarmed, notifier):
    alarmed()
    just_inside = DUE_EPOCH - 3600 + alarm_scan.STALE_AFTER - 60
    assert alarm_scan.scan(conn, now=just_inside, notifier=notifier).notified == 1


# -------------------------------------------------------------------- races


def test_the_primary_key_insert_is_the_race_guard(conn, alarmed, notifier):
    """Both the daemon and the UI may scan at once; whichever inserts first
    sends, and the other skips."""
    task_id = alarmed()
    trigger_at = DUE_EPOCH - 3600

    assert alarm_scan._record(task_id, trigger_at, DUE_EPOCH, conn) is True
    assert alarm_scan._record(task_id, trigger_at, DUE_EPOCH, conn) is False

    result = alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier)
    assert result.notified == 0
    assert result.already_fired == 1


# ------------------------------------------------------------------ anchors


def test_an_alarm_with_no_anchor_never_becomes_due(conn, make_task, notifier):
    task = make_task("no-due")
    task_id = cache.create_task_local(task, conn)
    with cache.tx(conn):
        conn.execute(
            "INSERT INTO valarms (task_id, related, trigger_offset) VALUES (?, 'END', -3600)",
            (task_id,),
        )
    assert alarm_scan.due_alarms(conn, now=DUE_EPOCH + 86400) == []


def test_a_start_anchored_alarm_uses_dtstart(conn, make_task, notifier):
    task = make_task("s1", dtstart_value="20260731T090000Z")
    task.alarms = [Alarm(related=AlarmRelated.START, trigger_offset=-900)]
    cache.create_task_local(task, conn)

    start = int(datetime(2026, 7, 31, 9, 0, tzinfo=UTC).timestamp())
    assert alarm_scan.scan(conn, now=start - 1000, notifier=notifier).notified == 0
    assert alarm_scan.scan(conn, now=start - 800, notifier=notifier).notified == 1


def test_a_task_pending_deletion_does_not_alarm(conn, alarmed, notifier, synced_task):
    task_id = alarmed()
    conn.execute("UPDATE tasks SET sync_state = 'pending_delete' WHERE id = ?", (task_id,))
    assert alarm_scan.scan(conn, now=DUE_EPOCH - 1800, notifier=notifier).notified == 0


# ------------------------------------------------------------------ expiry


def test_old_ledger_rows_are_pruned(conn, alarmed, notifier, frozen_now):
    alarmed()
    fired_at = DUE_EPOCH - 1800
    alarm_scan.scan(conn, now=fired_at, notifier=notifier)
    assert len(fires(conn)) == 1

    frozen_now.set(fired_at + cache.ALARM_FIRE_TTL - 1)
    cache.expire(conn)
    assert len(fires(conn)) == 1  # not yet

    frozen_now.set(fired_at + cache.ALARM_FIRE_TTL + 1)
    cache.expire(conn)
    assert fires(conn) == []
