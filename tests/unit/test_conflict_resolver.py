"""All five resolution paths, all three dialog modes."""

from __future__ import annotations

import pytest

from davpunk.conflict import resolver
from davpunk.conflict.resolver import (
    ConflictError,
    MergeState,
    Mode,
    Resolution,
    Side,
    diff,
    load_conflict,
    merge_from_selection,
    mode_of,
    newer_side,
    resolution_for,
    resolve_conflict,
)
from davpunk.core import cache
from davpunk.core.cache import TaskConflictError
from davpunk.core.ical_parser import new_resource
from davpunk.models.task import Alarm, AlarmRelated, Status, SyncState, Task


def ics(uid="c1", **kwargs) -> str:
    return new_resource(Task(uid=uid, **kwargs))


@pytest.fixture
def conflicted(conn, synced_task):
    """A task in ``conflict`` with a conflict row, in whichever mode is asked."""

    def _make(local=..., remote=..., remote_etag='W/"server"'):
        task_id = synced_task("c1", etag='W/"base"', summary="Original")
        local_ics = ics(summary="Local edit") if local is ... else local
        remote_ics = ics(summary="Server edit") if remote is ... else remote

        conn.execute("UPDATE tasks SET raw_ics = ? WHERE id = ?", (local_ics, task_id))
        with cache.tx(conn):
            cache.upsert_conflict(task_id, local_ics, remote_ics, remote_etag, conn)
            cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
        conflict_id = conn.execute(
            "SELECT id FROM conflict_queue WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        return task_id, conflict_id

    return _make


# --------------------------------------------------------------------- modes


def test_both_sides_present_is_mode_a():
    assert mode_of("local", "remote") is Mode.BOTH_CHANGED


def test_a_null_local_is_mode_a_prime():
    """The local intent was a delete (or a move's source delete)."""
    assert mode_of(None, "remote") is Mode.LOCAL_DELETE


def test_a_null_remote_is_mode_b():
    assert mode_of("local", None) is Mode.SERVER_DELETED


def test_both_null_is_refused_loudly():
    with pytest.raises(ConflictError):
        mode_of(None, None)


@pytest.mark.parametrize(
    ("mode", "offered"),
    [
        (Mode.BOTH_CHANGED, {Resolution.MERGE, Resolution.RESTORE_SERVER}),
        (Mode.LOCAL_DELETE, {Resolution.DELETE_ANYWAY, Resolution.RESTORE_SERVER}),
        (Mode.SERVER_DELETED, {Resolution.RECREATE, Resolution.ACCEPT_DELETION}),
    ],
)
def test_each_mode_offers_its_own_buttons(mode, offered):
    assert set(resolver.ALLOWED[mode]) == offered


def test_a_resolution_from_the_wrong_mode_is_refused(conn, conflicted):
    _, conflict_id = conflicted(local=None)  # mode A′
    with pytest.raises(ConflictError, match="not offered"):
        resolve_conflict(conflict_id, Resolution.RECREATE, None, conn)


# ---------------------------------------------------------------------- view


def test_the_view_carries_both_sides_and_the_mode(conn, conflicted):
    _, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)

    assert view.mode is Mode.BOTH_CHANGED
    assert view.local.summary == "Local edit"
    assert view.remote.summary == "Server edit"
    assert view.remote_etag == 'W/"server"'
    assert Resolution.MERGE in view.resolutions


def test_the_diff_lists_only_changed_fields_as_changed(conn, conflicted):
    _, conflict_id = conflicted(
        local=ics(summary="Same", description="Mine"),
        remote=ics(summary="Same", description="Theirs"),
    )
    view = load_conflict(conflict_id, conn)
    assert {d.field for d in view.changed} == {"description"}


def test_description_is_atomic_to_protect_inline_checklists(conn, conflicted):
    _, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)
    assert next(d for d in view.diffs if d.field == "description").atomic
    assert not next(d for d in view.diffs if d.field == "summary").atomic


def test_loading_a_resolved_conflict_is_refused(conn, conflicted):
    _, conflict_id = conflicted()
    conn.execute("UPDATE conflict_queue SET resolved = 1")
    with pytest.raises(ConflictError):
        load_conflict(conflict_id, conn)


def test_diff_tolerates_a_missing_side():
    assert all(d.remote is None for d in diff(Task(uid="a", summary="s"), None))


def test_a_due_value_and_its_tzid_are_one_row(conn, conflicted):
    """Half a DUE is a different instant, so the two never travel apart."""
    _, conflict_id = conflicted(
        local=ics(due_value="20260731T170000", due_tzid="Europe/Berlin"),
        remote=ics(due_value="20260731T170000", due_tzid="America/New_York"),
    )
    view = load_conflict(conflict_id, conn)

    due = view.diff_for("due")
    assert due.differs
    assert due.values(Side.REMOTE) == {
        "due_value": "20260731T170000",
        "due_tzid": "America/New_York",
    }


# -------------------------------------------------------------- merge state


def test_the_centre_starts_from_the_side_last_modified(conn, conflicted):
    _, conflict_id = conflicted(
        local=ics(summary="Mine", last_modified=1000),
        remote=ics(summary="Theirs", last_modified=2000),
    )
    view = load_conflict(conflict_id, conn)

    assert newer_side(view) is Side.REMOTE
    assert MergeState(view).apply().summary == "Theirs"


def test_without_timestamps_the_local_side_wins_the_tie(conn, conflicted):
    """The edit the user can still remember making, and the one that would
    otherwise vanish without a trace."""
    _, conflict_id = conflicted(local=ics(summary="Mine"), remote=ics(summary="Theirs"))
    view = load_conflict(conflict_id, conn)

    assert newer_side(view) is Side.LOCAL
    assert MergeState(view).apply().summary == "Mine"


def test_sequence_breaks_a_last_modified_tie(conn, conflicted):
    _, conflict_id = conflicted(
        local=ics(summary="Mine", last_modified=1000, sequence=1),
        remote=ics(summary="Theirs", last_modified=1000, sequence=4),
    )
    assert newer_side(load_conflict(conflict_id, conn)) is Side.REMOTE


def test_an_arrow_moves_one_group_across(conn, conflicted):
    _, conflict_id = conflicted(
        local=ics(summary="Mine", location="Here", last_modified=2000),
        remote=ics(summary="Theirs", location="There", last_modified=1000),
    )
    state = MergeState(load_conflict(conflict_id, conn))
    state.take("summary", Side.REMOTE)

    merged = state.apply()
    assert (merged.summary, merged.location) == ("Theirs", "Here")


def test_taking_a_due_takes_its_tzid_with_it(conn, conflicted):
    """The bug a field-by-field merge invites: the server's time, my zone."""
    _, conflict_id = conflicted(
        local=ics(due_value="20260731T170000", due_tzid="Europe/Berlin"),
        remote=ics(due_value="20260801T090000", due_tzid="America/New_York"),
    )
    state = MergeState(load_conflict(conflict_id, conn))
    state.take("due", Side.REMOTE)

    merged = state.apply()
    assert (merged.due_value, merged.due_tzid) == ("20260801T090000", "America/New_York")


def test_the_servers_alarms_can_be_taken(conn, conflicted):
    """They used to be dropped wholesale: the merge was always local-based."""
    alarm = Alarm(related=AlarmRelated.END, trigger_offset=-3600)
    # A relative alarm needs its anchor, or the parser drops it.
    task_id, conflict_id = conflicted(
        local=ics(due_value="20260731"),
        remote=ics(due_value="20260731", alarms=[alarm]),
    )

    state = MergeState(load_conflict(conflict_id, conn))
    state.take("alarms", Side.REMOTE)
    resolution, merged = resolution_for(state)
    resolve_conflict(conflict_id, resolution, merged, conn)

    rows = conn.execute(
        "SELECT trigger_offset FROM valarms WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert [row["trigger_offset"] for row in rows] == [-3600]


def test_a_hand_typed_centre_value_reaches_the_database(conn, conflicted):
    task_id, conflict_id = conflicted(local=ics(summary="Mine"), remote=ics(summary="Theirs"))
    state = MergeState(load_conflict(conflict_id, conn))
    state.edit("summary", {"summary": "Neither"})

    assert state.origin("summary") is None  # "edited"
    resolution, merged = resolution_for(state)
    resolve_conflict(conflict_id, resolution, merged, conn)
    assert cache.get_task_row(task_id, conn)["summary"] == "Neither"


def test_typing_one_sides_value_is_recorded_as_taking_that_side(conn, conflicted):
    """Provenance has to match what the user sees, however they got there."""
    _, conflict_id = conflicted(local=ics(summary="Mine"), remote=ics(summary="Theirs"))
    state = MergeState(load_conflict(conflict_id, conn))
    state.edit("summary", {"summary": "Theirs"})

    assert state.origin("summary") is Side.REMOTE


def test_take_all_moves_every_group(conn, conflicted):
    _, conflict_id = conflicted(
        local=ics(summary="Mine", location="Here"),
        remote=ics(summary="Theirs", location="There"),
    )
    state = MergeState(load_conflict(conflict_id, conn))
    state.take_all(Side.REMOTE)

    merged = state.apply()
    assert (merged.summary, merged.location) == ("Theirs", "There")


# -------------------------------------------------------------- resolution_for


def test_a_result_equal_to_the_server_is_a_restore_not_a_put(conn, conflicted):
    """The server already holds those bytes; PUTing them back is churn."""
    _, conflict_id = conflicted()
    state = MergeState(load_conflict(conflict_id, conn))
    state.take_all(Side.REMOTE)

    resolution, merged = resolution_for(state)
    assert (resolution, merged) == (Resolution.RESTORE_SERVER, None)

    resolve_conflict(conflict_id, resolution, merged, conn)
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


def test_any_other_result_is_a_merge(conn, conflicted):
    _, conflict_id = conflicted()
    state = MergeState(load_conflict(conflict_id, conn))
    state.take_all(Side.LOCAL)

    resolution, merged = resolution_for(state)
    assert resolution is Resolution.MERGE
    assert merged.summary == "Local edit"


# ------------------------------------------------------------------- merge


def test_take_all_local_is_a_merge_with_nothing_taken_from_the_server(conn, conflicted):
    task_id, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)

    merged = merge_from_selection(view, take_remote=set())
    assert merged.summary == "Local edit"

    resolve_conflict(conflict_id, Resolution.MERGE, merged, conn)
    row = cache.get_task_row(task_id, conn)
    assert row["summary"] == "Local edit"
    assert row["sync_state"] == SyncState.DIRTY.value


def test_a_per_field_merge_takes_each_side_as_chosen(conn, conflicted):
    task_id, conflict_id = conflicted(
        local=ics(summary="Local title", status=Status.IN_PROCESS, percent_complete=60),
        remote=ics(summary="Server title", status=Status.NEEDS_ACTION, percent_complete=10),
    )
    view = load_conflict(conflict_id, conn)

    merged = merge_from_selection(view, take_remote={"summary"})
    resolve_conflict(conflict_id, Resolution.MERGE, merged, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["summary"] == "Server title"
    assert row["status"] == Status.IN_PROCESS.value
    assert row["percent_complete"] == 60


def test_a_merge_queues_an_update_guarded_by_the_remote_etag(conn, conflicted):
    """The server is at that version, so If-Match guards another race."""
    task_id, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)

    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "update"
    assert pending["base_etag"] == 'W/"server"'


def test_a_merge_resets_the_backoff(conn, conflicted):
    """A user-driven resolution is an explicit "try again now"."""
    task_id, conflict_id = conflicted()
    with cache.tx(conn):
        cache._queue(conn, task_id, "update", 'W/"base"')
    conn.execute(
        "UPDATE pending_changes SET attempts = 5, blocked = 1, next_attempt_at = 999999999"
    )

    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)

    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert (pending["attempts"], pending["blocked"], pending["next_attempt_at"]) == (0, 0, 0)


def test_a_merge_is_canonicalized(conn, conflicted):
    """"""
    task_id, conflict_id = conflicted(
        local=ics(status=Status.COMPLETED, percent_complete=40),
        remote=ics(status=Status.NEEDS_ACTION),
    )
    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)

    row = cache.get_task_row(task_id, conn)
    assert row["status"] == Status.COMPLETED.value
    assert row["percent_complete"] == 100
    assert row["completed"] is not None


def test_merge_without_a_merged_task_is_refused(conn, conflicted):
    _, conflict_id = conflicted()
    with pytest.raises(ConflictError, match="needs a merged task"):
        resolve_conflict(conflict_id, Resolution.MERGE, None, conn)


# ---------------------------------------------------------- restore_server


def test_restore_server_takes_the_server_version_and_sends_no_put(conn, conflicted):
    """Re-PUTing adds churn and risks a conflict loop if the server moved
    again since detection."""
    task_id, conflict_id = conflicted()
    resolve_conflict(conflict_id, Resolution.RESTORE_SERVER, None, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["summary"] == "Server edit"
    assert row["sync_state"] == SyncState.CLEAN.value
    assert row["etag"] == 'W/"server"'
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


def test_restore_server_stores_the_server_bytes_verbatim(conn, conflicted):
    remote_ics = ics(summary="Server edit")
    task_id, conflict_id = conflicted(remote=remote_ics)
    resolve_conflict(conflict_id, Resolution.RESTORE_SERVER, None, conn)
    assert cache.get_task_row(task_id, conn)["raw_ics"] == remote_ics


def test_restore_server_also_resolves_a_mode_a_prime_conflict(conn, conflicted):
    """ "Keep server version" on a delete-vs-change conflict."""
    task_id, conflict_id = conflicted(local=None)
    resolve_conflict(conflict_id, Resolution.RESTORE_SERVER, None, conn)

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.CLEAN.value
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 0


# ----------------------------------------------------------- delete_anyway


def test_delete_anyway_queues_an_unconditional_delete(conn, conflicted):
    task_id, conflict_id = conflicted(local=None)
    resolve_conflict(conflict_id, Resolution.DELETE_ANYWAY, None, conn)

    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value
    pending = conn.execute("SELECT * FROM pending_changes WHERE task_id = ?", (task_id,)).fetchone()
    assert pending["change_type"] == "delete"
    assert pending["base_etag"] is None  # unconditional


# ---------------------------------------------------------------- recreate


def test_recreate_pushes_it_back_as_a_create(conn, conflicted):
    """The href is kept; If-None-Match:* will succeed since it is gone."""
    task_id, conflict_id = conflicted(remote=None)
    before_href = cache.get_task_row(task_id, conn)["href"]

    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.RECREATE, view.local, conn)

    row = cache.get_task_row(task_id, conn)
    assert row["sync_state"] == SyncState.NEW.value
    assert row["etag"] is None
    assert row["href"] == before_href
    assert (
        conn.execute(
            "SELECT change_type FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == "create"
    )


# ---------------------------------------------------------- accept_deletion


def test_accept_deletion_tombstones_and_removes_the_row(conn, conflicted):
    task_id, conflict_id = conflicted(remote=None)
    resolve_conflict(conflict_id, Resolution.ACCEPT_DELETION, None, conn)

    assert cache.get_task_row(task_id, conn) is None
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1
    # The conflict row cascaded away with the task.
    assert conn.execute("SELECT COUNT(*) FROM conflict_queue").fetchone()[0] == 0


def test_accept_deletion_blocks_a_stale_client_replaying_it(conn, conflicted, calendar_id):
    _, conflict_id = conflicted(remote=None)
    resolve_conflict(conflict_id, Resolution.ACCEPT_DELETION, None, conn)
    assert cache.find_tombstone(calendar_id, "c1", conn) is not None


# ------------------------------------------------------------------ guards


def test_a_conflicted_task_rejects_edits_until_resolved(conn, conflicted):
    """"""
    task_id, _ = conflicted()
    with pytest.raises(TaskConflictError):
        cache.update_task_optimistic(task_id, {"summary": "nope"}, conn)


def test_deleting_a_conflicted_task_resolves_it_implicitly(conn, conflicted):
    """"""
    task_id, _ = conflicted()
    cache.delete_task_local(task_id, conn)

    assert (
        conn.execute(
            "SELECT resolved FROM conflict_queue WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        == 1
    )
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value
    assert (
        conn.execute(
            "SELECT base_etag FROM pending_changes WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        is None
    )


def test_resolution_marks_the_row_resolved_and_frees_the_task(conn, conflicted):
    task_id, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)

    assert conn.execute("SELECT COUNT(*) FROM conflict_queue WHERE resolved = 0").fetchone()[0] == 0
    cache.update_task_optimistic(task_id, {"summary": "now editable"}, conn)


def test_resolving_twice_is_refused(conn, conflicted):
    _, conflict_id = conflicted()
    view = load_conflict(conflict_id, conn)
    resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)
    with pytest.raises(ConflictError):
        resolve_conflict(conflict_id, Resolution.MERGE, merge_from_selection(view), conn)


# ------------------------------------------------------------------- prune


def test_resolved_rows_are_pruned_after_ninety_days(conn, conflicted, frozen_now):
    _, conflict_id = conflicted()
    resolve_conflict(conflict_id, Resolution.RESTORE_SERVER, None, conn)

    assert resolver.prune_resolved(conn) == 0
    frozen_now.advance(cache.RESOLVED_CONFLICT_TTL + 1)
    assert resolver.prune_resolved(conn) == 1


def test_an_open_conflict_is_never_pruned(conn, conflicted, frozen_now):
    conflicted()
    frozen_now.advance(cache.RESOLVED_CONFLICT_TTL * 10)
    assert resolver.prune_resolved(conn) == 0
