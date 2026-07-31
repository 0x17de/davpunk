"""The Qt-free view logic: grouping, kanban, trees, reordering, checklists."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from davpunk.config import DavPunkConfig, KanbanColumn
from davpunk.models.task import ORDER_STEP, Status, Task
from davpunk.ui import viewmodel as vm
from davpunk.ui.keymap import Keymap

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 31, 14, 0, tzinfo=BERLIN)

COLUMNS = DavPunkConfig().kanban.columns


def task(uid="t", **kwargs) -> Task:
    kwargs.setdefault("calendar_id", "cal")
    return Task(uid=uid, **kwargs)


# ------------------------------------------------------------------ grouping


def test_a_task_due_today_is_in_today_not_overdue():
    """The T69 regression, at the view layer.

    In Berlin the UTC day starts at 02:00 local, so a naive comparison against
    the coarse sort key would have shown this under Overdue since 02:00.
    """
    assert vm.bucket_of(task(due_value="20260731"), NOW, BERLIN) is vm.Bucket.TODAY


def test_a_task_due_yesterday_is_overdue():
    assert vm.bucket_of(task(due_value="20260730"), NOW, BERLIN) is vm.Bucket.OVERDUE


def test_a_task_due_tomorrow_is_upcoming():
    assert vm.bucket_of(task(due_value="20260801"), NOW, BERLIN) is vm.Bucket.UPCOMING


def test_a_task_with_no_due_date_has_its_own_bucket():
    assert vm.bucket_of(task(), NOW, BERLIN) is vm.Bucket.NO_DUE_DATE


@pytest.mark.parametrize("status", [Status.COMPLETED, Status.CANCELLED])
def test_a_finished_task_is_never_overdue(status):
    assert vm.bucket_of(task(due_value="20200101", status=status), NOW, BERLIN) is (
        vm.Bucket.COMPLETED
    )


def test_a_timed_due_earlier_today_is_already_overdue():
    due = task(due_value="20260731T090000", due_tzid="Europe/Berlin")
    assert vm.bucket_of(due, NOW, BERLIN) is vm.Bucket.OVERDUE


def test_a_timed_due_later_today_is_still_today():
    due = task(due_value="20260731T170000", due_tzid="Europe/Berlin")
    assert vm.bucket_of(due, NOW, BERLIN) is vm.Bucket.TODAY


def test_grouping_hides_completed_unless_asked():
    tasks = [task("a", due_value="20260731"), task("b", status=Status.COMPLETED)]
    assert vm.Bucket.COMPLETED not in vm.group_tasks(tasks, NOW, BERLIN)
    assert vm.Bucket.COMPLETED in vm.group_tasks(tasks, NOW, BERLIN, show_completed=True)


def test_grouping_drops_empty_buckets():
    grouped = vm.group_tasks([task("a", due_value="20260731")], NOW, BERLIN)
    assert list(grouped) == [vm.Bucket.TODAY]


def test_groups_are_sorted_by_order_then_uid():
    tasks = [
        task("c", due_value="20260731"),
        task("a", due_value="20260731", davpunk_order=2000),
        task("b", due_value="20260731", davpunk_order=1000),
    ]
    grouped = vm.group_tasks(tasks, NOW, BERLIN)
    assert [t.uid for t in grouped[vm.Bucket.TODAY]] == ["b", "a", "c"]


# ---------------------------------------------------------- midnight rebucket


def test_the_rebucket_timer_targets_the_next_local_midnight():
    """An app left open overnight otherwise shows yesterday's buckets."""
    seconds = vm.seconds_to_midnight(datetime(2026, 7, 31, 23, 30, tzinfo=BERLIN), BERLIN)
    assert seconds == pytest.approx(1800, abs=1)


def test_the_timer_is_never_zero():
    at_midnight = datetime(2026, 8, 1, 0, 0, tzinfo=BERLIN)
    assert vm.seconds_to_midnight(at_midnight, BERLIN) > 0


def test_a_task_moves_bucket_when_midnight_passes():
    due_today = task(due_value="20260731")
    assert vm.bucket_of(due_today, NOW, BERLIN) is vm.Bucket.TODAY

    just_after = datetime(2026, 8, 1, 0, 0, 1, tzinfo=BERLIN)
    assert vm.bucket_of(due_today, just_after, BERLIN) is vm.Bucket.OVERDUE


# ------------------------------------------------------------- due rendering


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        ("20260731", "today"),
        ("20260801", "tomorrow"),
        ("20260730", "yesterday"),
        ("20260803", "in 3d"),
        ("20260728", "3d ago"),
        ("20261231", "2026-12-31"),
    ],
)
def test_relative_due_text(due, expected):
    assert vm.relative_due(task(due_value=due), NOW, BERLIN) == expected


def test_a_foreign_timezone_is_named_alongside():
    """Display renders in the local zone; a different original zone is shown."""
    tokyo = task(due_value="20260731T090000", due_tzid="Asia/Tokyo")
    assert "Asia/Tokyo" in vm.relative_due(tokyo, NOW, BERLIN)


def test_the_local_timezone_is_not_named():
    local = task(due_value="20260731T170000", due_tzid="Europe/Berlin")
    assert vm.relative_due(local, NOW, BERLIN) == "today"


def test_no_due_date_renders_empty():
    assert vm.relative_due(task(), NOW, BERLIN) == ""


# -------------------------------------------------------------------- kanban


def test_the_override_wins_when_it_names_a_configured_column():
    """"""
    t = task(status=Status.NEEDS_ACTION, kanban_col="done")
    assert vm.column_of(t, COLUMNS).id == "done"


def test_status_decides_without_an_override():
    assert vm.column_of(task(status=Status.IN_PROCESS), COLUMNS).id == "inprogress"


def test_an_orphaned_override_falls_through_to_status():
    """It is preserved in the ICS and rewritten on the next drag."""
    t = task(status=Status.IN_PROCESS, kanban_col="a-column-that-was-removed")
    assert vm.column_of(t, COLUMNS).id == "inprogress"


def test_a_task_with_neither_lands_in_the_first_column():
    assert vm.column_of(task(), COLUMNS).id == "todo"


def test_two_columns_may_share_a_status_and_the_override_disambiguates():
    columns = [
        KanbanColumn(id="todo", label="To Do", status="NEEDS-ACTION"),
        KanbanColumn(id="triage", label="Triage", status="NEEDS-ACTION"),
    ]
    assert vm.column_of(task(status=Status.NEEDS_ACTION), columns).id == "todo"
    assert vm.column_of(task(status=Status.NEEDS_ACTION, kanban_col="triage"), columns).id == (
        "triage"
    )


def test_a_drag_writes_both_status_and_the_override():
    """One update_task_optimistic call, both fields."""
    fields = vm.drop_fields(COLUMNS[1])
    assert fields == {"status": Status.IN_PROCESS, "kanban_col": "inprogress"}


def test_the_board_has_a_lane_per_column():
    board = vm.kanban_board([task("a", status=Status.COMPLETED)], COLUMNS)
    assert set(board) == {c.id for c in COLUMNS}
    assert [t.uid for t in board["done"]] == ["a"]


# --------------------------------------------------------------------- trees


def test_children_nest_under_their_parent():
    tree = vm.build_tree([task("p"), task("c", parent_uid="p")])
    assert len(tree) == 1
    assert [n.task.uid for n in tree[0].children] == ["c"]


def test_a_parent_in_another_calendar_renders_at_root_with_a_marker():
    """uid is deliberately non-unique across calendars."""
    child = task("c", parent_uid="p")
    child.calendar_id = "other-calendar"
    tree = vm.build_tree([task("p"), child])

    roots = {n.task.uid: n for n in tree}
    assert set(roots) == {"p", "c"}
    assert roots["c"].linked_parent_elsewhere is True
    assert roots["p"].linked_parent_elsewhere is False


def test_a_missing_parent_renders_at_root():
    tree = vm.build_tree([task("c", parent_uid="not-here")])
    assert tree[0].linked_parent_elsewhere is True


def test_a_cycle_is_broken_rather_than_recursing_forever():
    a = task("a", parent_uid="b")
    b = task("b", parent_uid="a")
    tree = vm.build_tree([a, b])
    assert len(vm.flatten(tree)) <= 4  # terminates


def test_depth_is_recorded_for_indentation():
    tasks = [task("a"), task("b", parent_uid="a"), task("c", parent_uid="b")]
    depths = {n.task.uid: n.depth for n in vm.flatten(vm.build_tree(tasks))}
    assert depths == {"a": 0, "b": 1, "c": 2}


def test_a_very_deep_chain_stops_at_the_cap():
    tasks = [task("t0")]
    for n in range(1, 60):
        tasks.append(task(f"t{n}", parent_uid=f"t{n - 1}"))
    flat = vm.flatten(vm.build_tree(tasks))
    assert len(flat) <= vm.DEPTH_CAP + 1


def test_siblings_are_ordered_within_a_parent():
    tasks = [
        task("p"),
        task("y", parent_uid="p", davpunk_order=2000),
        task("x", parent_uid="p", davpunk_order=1000),
    ]
    assert [n.task.uid for n in vm.build_tree(tasks)[0].children] == ["x", "y"]


# ------------------------------------------------------------------ ordering


def test_an_insertion_takes_the_midpoint():
    siblings = [
        task("a", davpunk_order=1000),
        task("b", davpunk_order=2000),
        task("c", davpunk_order=3000),
    ]
    result = vm.reorder_siblings(siblings, "c", 1)
    assert result.assignments == {"c": 1500}
    assert result.rebalanced is False


def test_moving_to_the_front_stays_above_everything():
    siblings = [task("a", davpunk_order=1000), task("b", davpunk_order=2000)]
    result = vm.reorder_siblings(siblings, "b", 0)
    assert result.assignments["b"] < 1000


def test_moving_to_the_end_stays_below_everything():
    siblings = [task("a", davpunk_order=1000), task("b", davpunk_order=2000)]
    result = vm.reorder_siblings(siblings, "a", 1)
    assert result.assignments["a"] > 2000


def test_a_closed_gap_renumbers_only_the_run_that_closed():
    """Renumbering every sibling would push each of them to the server for a
    change the user did not make."""
    siblings = [
        task("a", davpunk_order=1000),
        task("b", davpunk_order=1001),
        task("c", davpunk_order=1002),
        task("far", davpunk_order=9000),
    ]
    result = vm.reorder_siblings(siblings, "c", 1)

    assert result.rebalanced is True
    assert "far" not in result.assignments  # untouched, so never marked dirty


def test_a_rebalance_still_produces_the_requested_order():
    siblings = [
        task("a", davpunk_order=1000),
        task("b", davpunk_order=1001),
        task("c", davpunk_order=1002),
    ]
    result = vm.reorder_siblings(siblings, "c", 0)

    for t in siblings:
        if t.uid in result.assignments:
            t.davpunk_order = result.assignments[t.uid]
    assert [t.uid for t in sorted(siblings, key=vm.sort_key)] == ["c", "a", "b"]


def test_reordering_an_unknown_task_is_refused():
    with pytest.raises(ValueError, match="not among these siblings"):
        vm.reorder_siblings([task("a")], "nope", 0)


def test_an_index_beyond_the_end_is_clamped():
    siblings = [task("a", davpunk_order=1000), task("b", davpunk_order=2000)]
    assert vm.reorder_siblings(siblings, "a", 99).assignments["a"] > 2000


def test_an_appended_task_gets_the_next_step():
    assert vm.initial_order([task("a", davpunk_order=2000)]) == 3000
    assert vm.initial_order([]) == ORDER_STEP


def test_unordered_siblings_can_still_be_reordered():
    result = vm.reorder_siblings([task("a"), task("b")], "b", 0)
    assert result.assignments


# ---------------------------------------------------------------- checklists


def test_toggling_an_unchecked_item():
    text = "- [ ] first\n- [ ] second"
    assert vm.toggle_checklist_item(text, 0) == "- [x] first\n- [ ] second"


def test_toggling_a_checked_item_back():
    assert vm.toggle_checklist_item("- [x] done", 0) == "- [ ] done"


def test_indentation_is_preserved():
    assert vm.toggle_checklist_item("    - [ ] nested", 0) == "    - [x] nested"


def test_a_non_checklist_line_is_untouched():
    assert vm.toggle_checklist_item("just prose", 0) == "just prose"


def test_an_out_of_range_index_is_a_no_op():
    assert vm.toggle_checklist_item("- [ ] x", 5) == "- [ ] x"


def test_checklist_progress_is_a_hint_only():
    """It is never written to PERCENT-COMPLETE: an inline checklist is the
    user's own structure, and inferring progress would overwrite a value they
    set explicitly."""
    text = "- [x] a\n- [ ] b\n- [x] c\nprose"
    assert vm.checklist_progress(text) == (2, 3)
    assert vm.checklist_progress(None) == (0, 0)
    assert vm.checklist_progress("no checklist here") == (0, 0)


# ------------------------------------------------------------------ loading


def test_load_tasks_reads_categories(conn, make_task):
    from davpunk.core import cache

    t = make_task("t1")
    t.categories = ["work"]
    cache.create_task_local(t, conn)

    loaded = vm.load_tasks(conn)
    assert len(loaded) == 1
    assert loaded[0].categories == ["work"]


def test_load_tasks_hides_tasks_pending_deletion(conn, synced_task):
    from davpunk.core import cache

    task_id = synced_task("gone")
    cache.delete_task_local(task_id, conn)
    assert vm.load_tasks(conn) == []


def test_load_tasks_can_filter_calendars(conn, calendar_id, other_calendar_id, make_task):
    from davpunk.core import cache

    cache.create_task_local(make_task("here"), conn)
    assert len(vm.load_tasks(conn, calendar_ids=[calendar_id])) == 1
    assert vm.load_tasks(conn, calendar_ids=[other_calendar_id]) == []


def test_the_refresh_fingerprint_changes_when_data_changes(conn, make_task):
    from davpunk.core import cache

    before = vm.refresh_fingerprint(conn)
    cache.create_task_local(make_task("t1"), conn)
    assert vm.refresh_fingerprint(conn) != before


def test_the_refresh_fingerprint_is_cheap_and_stable(conn):
    assert vm.refresh_fingerprint(conn) == vm.refresh_fingerprint(conn)


# ------------------------------------------------------------------- keymap


def test_the_default_keymap_is_complete():
    keymap = Keymap()
    for action in ("new_task", "delete_task", "move_task", "sync_now", "help_overlay"):
        assert keymap[action]


def test_overrides_are_applied():
    keymap = Keymap(DavPunkConfig(keys={"sync_now": "Ctrl+S"}))
    assert keymap["sync_now"] == "Ctrl+S"


def test_chords_are_separated_from_plain_shortcuts():
    """gg and dd are two-keystroke sequences Qt cannot bind as one shortcut."""
    keymap = Keymap()
    assert keymap.chords()["g,g"] == "top"
    assert keymap.chords()["d,d"] == "delete_task"
    assert "g,g" not in keymap.qt_shortcuts()
    assert keymap.qt_shortcuts()["n"] == "new_task"


def test_the_overlay_lists_every_action():
    text = Keymap().overlay_text()
    assert "New task" in text
    assert "Move to another list" in text
    assert "Take all server" in text
    assert "Kanban" in text


def test_actions_are_grouped_by_context():
    grouped = Keymap().by_context()
    assert {"global", "kanban", "conflict"} <= set(grouped)
    assert any(a.name == "take_local" for a in grouped["conflict"])


def test_a_binding_resolves_back_to_its_action():
    keymap = Keymap()
    assert keymap.action_for("n") == "new_task"
    assert keymap.action_for("l", context="conflict") == "take_local"
    assert keymap.action_for("l", context="kanban") == "card_next_column"


def test_every_action_is_reachable_without_a_mouse():
    """Hard requirement."""
    keymap = Keymap()
    assert all(action.binding for action in keymap.actions())


def test_a_comma_key_is_not_mistaken_for_a_chord():
    """Ctrl+, is the conventional Preferences accelerator. Treating any binding
    containing a comma as a two-keystroke sequence silently unbinds it."""
    from davpunk.ui.keymap import is_chord

    assert is_chord("g,g") is True
    assert is_chord("d,d") is True
    assert is_chord("Ctrl+,") is False
    assert is_chord("Ctrl+R") is False
    assert is_chord(",") is False
