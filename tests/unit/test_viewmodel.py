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


def test_no_status_and_needs_action_are_different_columns():
    """A task nobody has looked at yet and one explicitly marked NEEDS-ACTION
    are a pool to pick from and work that has been picked. Collapsing them
    loses the distinction the board exists to show."""
    assert vm.column_of(task(), COLUMNS).id == "todo"
    assert vm.column_of(task(status=Status.NEEDS_ACTION), COLUMNS).id == "needsaction"


def test_a_status_with_no_column_of_its_own_is_off_the_board():
    """Dropping the Done column is how you stop looking at finished work, and
    it only works if the cards go with it. Piling them into the first column
    instead would make the board worse, not smaller."""
    columns = [c for c in COLUMNS if c.id not in {"done", "cancelled"}]

    assert vm.column_of(task(status=Status.COMPLETED), columns) is None
    assert vm.column_of(task(status=Status.CANCELLED), columns) is None
    assert vm.column_of(task(status=Status.IN_PROCESS), columns).id == "inprogress"


def test_a_hidden_status_is_left_out_of_the_board_entirely():
    columns = [c for c in COLUMNS if c.id != "done"]
    tasks = [task("a"), task("b", status=Status.COMPLETED)]

    board = vm.kanban_board(tasks, columns)

    assert [t.uid for t in board["todo"]] == ["a"]
    assert sum(len(items) for items in board.values()) == 1


def test_an_override_still_places_a_card_whose_status_has_no_column():
    """The override is an explicit choice, and it outranks the status rule."""
    columns = [c for c in COLUMNS if c.id != "done"]
    parked = task(status=Status.COMPLETED, kanban_col="inprogress")

    assert vm.column_of(parked, columns).id == "inprogress"


def test_two_columns_may_share_a_status_and_the_override_disambiguates():
    columns = [
        KanbanColumn(id="todo", label="To Do", status="NEEDS-ACTION"),
        KanbanColumn(id="triage", label="Triage", status="NEEDS-ACTION"),
    ]
    assert vm.column_of(task(status=Status.NEEDS_ACTION), columns).id == "todo"
    assert vm.column_of(task(status=Status.NEEDS_ACTION, kanban_col="triage"), columns).id == (
        "triage"
    )


def _column(column_id: str) -> KanbanColumn:
    return next(c for c in COLUMNS if c.id == column_id)


def test_a_drag_writes_both_status_and_the_override():
    """One update_task_optimistic call, both fields."""
    fields = vm.drop_fields(_column("inprogress"))
    assert fields == {"status": Status.IN_PROCESS, "kanban_col": "inprogress"}


def test_a_drag_into_the_no_status_column_clears_the_status():
    """Dragging back to the pool has to undo having picked the task up."""
    fields = vm.drop_fields(_column("todo"))
    assert fields == {"status": None, "kanban_col": "todo"}


def test_the_board_has_a_lane_per_column():
    board = vm.kanban_board([task("a", status=Status.COMPLETED)], COLUMNS)
    assert set(board) == {c.id for c in COLUMNS}
    assert [t.uid for t in board["done"]] == ["a"]


# ------------------------------------------------------------------ filtering


def test_an_empty_filter_shows_everything():
    """Empty means "no restriction", not "show nothing" — a fresh board is full."""
    tasks = [task("a"), task("b")]
    assert vm.apply_filter(tasks, vm.TaskFilter()) == tasks
    assert not vm.TaskFilter().is_active


def test_filtering_by_one_calendar():
    tasks = [task("a", calendar_id="one"), task("b", calendar_id="two")]
    kept = vm.apply_filter(tasks, vm.TaskFilter(calendar_ids=frozenset({"one"})))
    assert [t.uid for t in kept] == ["a"]


def test_filtering_by_several_calendars_keeps_all_of_them():
    """The multi-select case: three lists means the union, not an intersection."""
    tasks = [task(c, calendar_id=c) for c in ("one", "two", "three")]
    kept = vm.apply_filter(tasks, vm.TaskFilter(calendar_ids=frozenset({"one", "three"})))
    assert {t.uid for t in kept} == {"one", "three"}


def test_tags_default_to_any_of_them():
    tasks = [task("a", categories=["home"]), task("b", categories=["work"]), task("c")]
    kept = vm.apply_filter(tasks, vm.TaskFilter(tags=frozenset({"home", "work"})))
    assert {t.uid for t in kept} == {"a", "b"}


def test_match_all_tags_requires_every_one():
    """ "shopping and urgent" is a different question from "shopping or urgent"."""
    both = task("both", categories=["home", "urgent"])
    one = task("one", categories=["home"])
    flt = vm.TaskFilter(tags=frozenset({"home", "urgent"}), match_all_tags=True)
    assert [t.uid for t in vm.apply_filter([both, one], flt)] == ["both"]


def test_a_task_with_extra_tags_still_matches_match_all():
    task_ = task("a", categories=["home", "urgent", "later"])
    flt = vm.TaskFilter(tags=frozenset({"home", "urgent"}), match_all_tags=True)
    assert vm.apply_filter([task_], flt) == [task_]


def test_text_matches_summary_and_description_case_insensitively():
    tasks = [
        task("a", summary="Buy a Bagger"),
        task("b", description="the bagger is red"),
        task("c", summary="unrelated"),
    ]
    assert {t.uid for t in vm.apply_filter(tasks, vm.TaskFilter(text="BAGGER"))} == {"a", "b"}


def test_whitespace_only_text_is_not_a_filter():
    assert not vm.TaskFilter(text="   ").is_active
    assert vm.apply_filter([task("a")], vm.TaskFilter(text="   ")) == [task("a")]


def test_the_axes_combine():
    tasks = [
        task("a", calendar_id="one", categories=["home"], summary="milk"),
        task("b", calendar_id="one", categories=["work"], summary="milk"),
        task("c", calendar_id="two", categories=["home"], summary="milk"),
        task("d", calendar_id="one", categories=["home"], summary="bread"),
    ]
    flt = vm.TaskFilter(calendar_ids=frozenset({"one"}), tags=frozenset({"home"}), text="milk")
    assert [t.uid for t in vm.apply_filter(tasks, flt)] == ["a"]


def test_available_tags_are_sorted_deduplicated_and_only_those_in_use():
    tasks = [task("a", categories=["work", "home"]), task("b", categories=["home"])]
    assert vm.available_tags(tasks) == ["home", "work"]


def test_the_board_filters_before_it_groups():
    tasks = [
        task("a", calendar_id="one", status=Status.COMPLETED),
        task("b", calendar_id="two", status=Status.COMPLETED),
    ]
    board = vm.kanban_board(tasks, COLUMNS, vm.TaskFilter(calendar_ids=frozenset({"one"})))
    assert [t.uid for t in board["done"]] == ["a"]


def test_describe_names_a_single_list_but_counts_several():
    names = {"one": "Shopping", "two": "Work"}
    assert vm.TaskFilter(calendar_ids=frozenset({"one"})).describe(names) == "Shopping"
    assert vm.TaskFilter(calendar_ids=frozenset({"one", "two"})).describe(names) == "2 lists"
    assert vm.TaskFilter().describe(names) == "No filter"


# --------------------------------------------------------------------- trees


def test_a_child_whose_parent_is_merely_filtered_out_is_not_marked_elsewhere():
    """The marker means "another calendar", not "another bucket".

    Every view slices the task list — one bucket, one kanban column, one
    filter — and a child left on the other side of that cut still has its
    parent right there in the same calendar.
    """
    parent = task("p")
    child = task("c", parent_uid="p")
    [node] = vm.build_tree([child], universe=[parent, child])
    assert not node.linked_parent_elsewhere


def test_a_child_whose_parent_really_is_absent_is_still_marked():
    [node] = vm.build_tree([task("c", parent_uid="gone")], universe=[task("c", parent_uid="gone")])
    assert node.linked_parent_elsewhere


def test_without_a_universe_the_slice_is_the_universe():
    """The old single-argument behaviour, for callers that pass everything."""
    [node] = vm.build_tree([task("c", parent_uid="p")])
    assert node.linked_parent_elsewhere


def test_subtree_size_counts_every_descendant_not_just_children():
    tasks = [task("p"), task("c", parent_uid="p"), task("g", parent_uid="c")]
    [root] = vm.build_tree(tasks)
    assert vm.subtree_size(root) == 2


# ---------------------------------------------------------------- folding


def test_an_unseen_node_takes_the_callers_default():
    folds = vm.FoldState()
    assert folds.is_open("x", default=True)
    assert not folds.is_open("x", default=False)


def test_remembering_overrides_the_default_in_both_directions():
    folds = vm.FoldState()
    folds.remember("x", True)
    assert folds.is_open("x", default=False)
    folds.remember("x", False)
    assert not folds.is_open("x", default=True)


def test_set_all_applies_to_every_key():
    folds = vm.FoldState()
    folds.set_all(["a", "b"], True)
    assert all(folds.is_open(k, default=False) for k in ("a", "b"))


# ------------------------------------------------------------ reparenting


def test_indent_makes_a_task_a_child_of_the_sibling_above_it():
    tasks = [task("a", davpunk_order=1000), task("b", davpunk_order=2000)]
    assert vm.indent_fields(tasks[1], tasks)["parent_uid"] == "a"


def test_the_first_task_in_a_group_has_nothing_to_indent_under():
    tasks = [task("a", davpunk_order=1000), task("b", davpunk_order=2000)]
    assert vm.indent_fields(tasks[0], tasks) is None


def test_an_indented_task_lands_at_the_end_of_its_new_siblings():
    """Its old order value belongs to the group it left."""
    tasks = [
        task("a", davpunk_order=1000),
        task("existing", parent_uid="a", davpunk_order=5000),
        task("b", davpunk_order=2000),
    ]
    assert vm.indent_fields(tasks[2], tasks)["davpunk_order"] == 5000 + ORDER_STEP


def test_outdent_promotes_a_task_to_sit_beside_its_parent():
    tasks = [task("root"), task("p", parent_uid="root"), task("c", parent_uid="p")]
    assert vm.outdent_fields(tasks[2], tasks) == {
        "parent_uid": "root",
        "davpunk_order": ORDER_STEP,
    }


def test_a_root_has_nowhere_to_outdent_to():
    assert vm.outdent_fields(task("a"), [task("a")]) is None


def test_a_task_cannot_be_reparented_under_its_own_descendant():
    """Cycles are refused here rather than broken by the tree walker later."""
    tasks = [task("p"), task("c", parent_uid="p"), task("g", parent_uid="c")]
    assert vm.reparent_fields(tasks[0], "g", tasks) is None


def test_a_task_cannot_be_reparented_under_itself():
    assert vm.reparent_fields(task("a"), "a", [task("a")]) is None


def test_reparenting_where_it_already_is_is_not_a_change():
    tasks = [task("p"), task("c", parent_uid="p")]
    assert vm.reparent_fields(tasks[1], "p", tasks) is None


def test_descendants_survive_a_cycle_in_the_data():
    tasks = [task("a", parent_uid="b"), task("b", parent_uid="a")]
    assert vm.descendants(tasks[0], tasks) == {"a", "b"}


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


# ------------------------------------------------------------------- drops

ON = vm.DropPosition.ON
ABOVE = vm.DropPosition.ABOVE
BELOW = vm.DropPosition.BELOW


def group(*uids, parent_uid=None, step=ORDER_STEP):
    """A sibling group with sparse, evenly spaced orders."""
    return [
        task(uid, parent_uid=parent_uid, davpunk_order=(i + 1) * step) for i, uid in enumerate(uids)
    ]


def test_dropping_onto_a_task_nests_it_there():
    tasks = group("a", "b")
    plan = vm.plan_list_drop(tasks[1], tasks[0], ON, tasks)

    assert plan.parent_uid == "a"
    assert plan.orders == {"b": ORDER_STEP}


def test_dropping_below_the_last_sibling_lands_last():
    tasks = group("a", "b", "c")
    plan = vm.plan_list_drop(tasks[0], tasks[2], BELOW, tasks)

    assert plan.parent_uid is None
    assert plan.orders["a"] > tasks[2].davpunk_order


def test_dropping_above_a_sibling_takes_the_midpoint():
    tasks = group("a", "b", "c")
    plan = vm.plan_list_drop(tasks[2], tasks[1], ABOVE, tasks)

    assert plan.orders["c"] == (1000 + 2000) // 2
    assert plan.rebalanced is False


def test_dropping_beside_a_task_in_another_group_reparents_and_orders():
    """One plan, not a reparent followed by a reorder: an interrupted drop must
    not leave a task nested where its order says it does not belong."""
    tasks = [*group("p", "q"), *group("x", parent_uid="p", step=500)]
    plan = vm.plan_list_drop(tasks[1], tasks[2], BELOW, tasks)

    assert plan.parent_uid == "p"
    assert plan.orders["q"] > 500


def test_a_drop_that_would_not_move_anything_is_refused():
    tasks = group("a", "b", "c")

    assert vm.plan_list_drop(tasks[1], tasks[0], BELOW, tasks) is None
    assert vm.plan_list_drop(tasks[1], tasks[2], ABOVE, tasks) is None


def test_a_drop_onto_itself_is_refused():
    tasks = group("a")
    assert vm.plan_list_drop(tasks[0], tasks[0], ON, tasks) is None


def test_a_drop_onto_a_own_descendant_is_refused():
    """A cycle refused here is a cycle the tree walker never has to break."""
    tasks = [task("p"), task("c", parent_uid="p"), task("g", parent_uid="c")]

    assert vm.plan_list_drop(tasks[0], tasks[2], ON, tasks) is None
    assert vm.plan_list_drop(tasks[0], tasks[2], BELOW, tasks) is None


def test_a_drop_into_another_calendar_is_refused():
    """RELATED-TO resolves within one calendar, so the link would never render."""
    here = task("a")
    there = task("b")
    there.calendar_id = "other-calendar"

    assert vm.plan_list_drop(here, there, ON, [here, there]) is None
    assert vm.plan_list_drop(here, there, ABOVE, [here, there]) is None


def test_a_closed_gap_renumbers_the_closed_run_and_says_so():
    tasks = [
        task("a", davpunk_order=1000),
        task("b", davpunk_order=1001),
        task("c", davpunk_order=5000),
    ]
    plan = vm.plan_list_drop(tasks[2], tasks[1], ABOVE, tasks)

    assert plan.rebalanced is True
    assert len(plan.orders) > 1
    assert len(set(plan.orders.values())) == len(plan.orders)


def test_a_drop_into_an_empty_sibling_group_starts_at_the_step():
    tasks = group("a", "b")
    plan = vm.plan_list_drop(tasks[1], tasks[0], ON, tasks)

    assert plan.orders["b"] == ORDER_STEP
    assert plan.touched == 1


# ------------------------------------------------------------------- paste


def test_the_clipboard_holds_ids_and_a_new_cut_replaces_the_old():
    """Ids, not snapshots: the row can change — or be deleted — between the cut
    and the paste, so the paste re-reads it."""
    clipboard = vm.TaskClipboard()
    assert clipboard.is_empty

    first = task("a")
    first.id = "id-a"
    clipboard.cut([first])
    assert clipboard.task_ids == ["id-a"]
    assert not clipboard.is_empty

    second = task("b")
    second.id = "id-b"
    clipboard.cut([second])
    assert clipboard.task_ids == ["id-b"]

    clipboard.clear()
    assert clipboard.is_empty


def test_a_task_with_no_id_is_not_cuttable():
    clipboard = vm.TaskClipboard()
    clipboard.cut([task("unsaved")])
    assert clipboard.is_empty


def test_pasting_onto_a_task_nests_the_cut_task_under_it():
    tasks = group("a", "b")
    [plan] = vm.plan_paste([tasks[1]], tasks[0], tasks)

    assert plan.parent_uid == "a"
    assert plan.calendar_id == "cal"
    assert plan.needs_move is False
    assert plan.fields == {"parent_uid": "a", "davpunk_order": ORDER_STEP}


def test_pasting_with_no_target_makes_a_root_task():
    tasks = [task("p", davpunk_order=1000), task("c", parent_uid="p", davpunk_order=500)]
    [plan] = vm.plan_paste([tasks[1]], None, tasks)

    assert plan.parent_uid is None
    assert plan.calendar_id == "cal"
    assert plan.davpunk_order > 1000  # after the roots already there


def test_pasting_a_parent_and_its_child_moves_only_the_parent():
    """The ancestor's move already carries its descendants."""
    tasks = [task("t"), task("p"), task("c", parent_uid="p")]
    plans = vm.plan_paste([tasks[1], tasks[2]], tasks[0], tasks)

    assert [plan.task.uid for plan in plans] == ["p"]


def test_pasting_into_your_own_cut_set_is_refused():
    tasks = [task("p"), task("c", parent_uid="p"), task("g", parent_uid="c")]

    assert vm.plan_paste([tasks[0]], tasks[2], tasks) == []
    assert vm.plan_paste([tasks[0]], tasks[0], tasks) == []


def test_pasting_where_it_already_is_is_not_a_change():
    tasks = [task("p"), task("c", parent_uid="p")]
    assert vm.plan_paste([tasks[1]], tasks[0], tasks) == []


def test_several_pasted_tasks_keep_their_order_and_do_not_collide():
    tasks = [task("t"), *group("a", "b", "c")]
    plans = vm.plan_paste(tasks[1:], tasks[0], tasks)

    assert [plan.task.uid for plan in plans] == ["a", "b", "c"]
    orders = [plan.davpunk_order for plan in plans]
    assert orders == sorted(orders)
    assert len(set(orders)) == 3


def test_an_existing_child_of_the_target_is_not_overwritten():
    tasks = [task("t"), task("kid", parent_uid="t", davpunk_order=9000), task("a")]
    [plan] = vm.plan_paste([tasks[2]], tasks[0], tasks)

    assert plan.davpunk_order > 9000


def test_a_cross_calendar_paste_is_planned_as_a_move():
    """It is reported rather than dropped: the caller runs move_task_local
    first, then the reparent."""
    here = task("a")
    there = task("t")
    there.calendar_id = "other-calendar"
    [plan] = vm.plan_paste([here], there, [here, there])

    assert plan.needs_move is True
    assert plan.calendar_id == "other-calendar"
    assert plan.parent_uid == "t"


def test_pasting_nothing_plans_nothing():
    assert vm.plan_paste([], task("t"), [task("t")]) == []


def test_topmost_drops_what_an_ancestor_already_carries():
    """Acting on a parent and its child both moves the child twice — once
    inside the subtree, once on its own, which is what un-nests it."""
    tasks = [task("p"), task("c", parent_uid="p"), task("g", parent_uid="c"), task("loose")]

    assert [t.uid for t in vm.topmost(tasks, tasks)] == ["p", "loose"]


def test_topmost_keeps_unrelated_tasks_and_the_order_given():
    tasks = [task("a"), task("b"), task("c")]
    assert [t.uid for t in vm.topmost(tasks, tasks)] == ["a", "b", "c"]


def test_topmost_is_scoped_to_one_calendar():
    parent = task("p")
    elsewhere = task("c", parent_uid="p")
    elsewhere.calendar_id = "other-calendar"
    chosen = [parent, elsewhere]

    assert [t.uid for t in vm.topmost(chosen, chosen)] == ["p", "c"]


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
