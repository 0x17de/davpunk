"""The Qt layer, driven offscreen.

Most view *logic* is tested in ``test_viewmodel.py``, which needs no display.
What is left here is the wiring: that the window builds, that a keystroke
reaches the right cache helper, that a read-only task disables its editor, and
that the conflict dialog offers the buttons its mode allows.

Skipped when PySide6 cannot initialise — a headless machine without libGL is a
perfectly normal place to run the rest of the suite.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# exc_type is not optional here: since pytest 8.2 importorskip only skips on
# ModuleNotFoundError, and PySide6 failing for want of libGL.so.1 raises a plain
# ImportError — which would abort collection on exactly the headless machine
# this skip exists for.
pytest.importorskip(
    "PySide6.QtWidgets",
    reason="PySide6 cannot be imported here",
    exc_type=ImportError,
)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from davpunk.config import DavPunkConfig, RemoteConfig
from davpunk.conflict.resolver import Mode, Resolution, load_conflict
from davpunk.core import cache
from davpunk.models.task import ReadOnlyReason, Status, SyncState
from davpunk.ui import viewmodel as vm
from davpunk.ui.dialogs import ConflictDialog, MoveDialog, TaskEditor
from davpunk.ui.keymap import Keymap, is_chord

pytestmark = pytest.mark.qt


def ics(uid: str, summary: str) -> str:
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//EN\r\n"
        f"BEGIN:VTODO\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def ui_config():
    return DavPunkConfig(
        remotes=[RemoteConfig(id="work", url="https://cal.example.test/dav/", username="u")]
    )


@pytest.fixture
def window(qapp, conn, ui_config, calendar_id, other_calendar_id, db_path):
    from davpunk.ui.main_window import MainWindow

    cache.reconcile_remotes(ui_config.remotes, conn)
    win = MainWindow(ui_config, conn, db_path)
    yield win
    win.sync.stop()
    win._poll.stop()


# ------------------------------------------------------------------- window


def test_the_window_builds_with_all_three_views(window):
    assert window.windowTitle() == "DavPunk"
    assert [window.view_selector.itemText(i) for i in range(window.view_selector.count())] == [
        "List",
        "Kanban",
        "Search",
    ]


def test_the_default_view_comes_from_config(qapp, conn, calendar_id, db_path):
    from davpunk.ui.main_window import MainWindow

    config = DavPunkConfig(default_view="kanban")
    win = MainWindow(config, conn, db_path)
    try:
        assert win.stack.currentIndex() == 1
        # The selector has to agree with the stack, or the first switch back is
        # a no-op: the combo already reads "List" while Kanban is on screen.
        assert win.view_selector.currentText() == "Kanban"
    finally:
        win.sync.stop()
        win._poll.stop()


# -------------------------------------------------------------- kanban filter


def _kanban(window):
    window.switch_view(1)
    window.refresh()
    return window.kanban_view


def _list(window):
    # switch_view only refreshes on an actual change, and the window already
    # starts on the list.
    window.switch_view(0)
    window.refresh()
    return window.list_view.tree


def _tick(button, values):
    for action in button._checkable_actions():
        action.setChecked(action.data() in values)


def _column(kanban, column_id):
    """Every card in a column, nesting included."""
    from davpunk.ui.views import _walk

    return list(_walk(kanban.lists[column_id]))


def _shown(kanban):
    return sum(len(_column(kanban, c)) for c in kanban.lists)


def test_the_filter_offers_every_calendar_and_only_tags_in_use(
    window, make_task, calendar_id, other_calendar_id
):
    cache.create_task_local(make_task("t1", categories=["home"]), window.conn)
    kanban = _kanban(window)
    assert set(kanban.filter_bar.calendars._entries) == {calendar_id, other_calendar_id}
    assert list(kanban.filter_bar.tags._entries) == ["home"]


def test_filtering_by_several_calendars(window, make_task, calendar_id, other_calendar_id):
    cache.create_task_local(make_task("t1", summary="here"), window.conn)
    cache.create_task_local(
        make_task("t2", summary="there", calendar_id=other_calendar_id), window.conn
    )
    kanban = _kanban(window)
    assert _shown(kanban) == 2

    _tick(kanban.filter_bar.calendars, {calendar_id})
    assert _shown(kanban) == 1
    assert kanban.filter_bar.calendars.text() != "Lists: all"

    _tick(kanban.filter_bar.calendars, {calendar_id, other_calendar_id})
    # Everything ticked is the same as nothing ticked, and must not read as a
    # filter — otherwise "Select all" looks like it hid something.
    assert _shown(kanban) == 2
    assert not kanban.filter.is_active


def test_filtering_by_tag_and_by_text(window, make_task):
    cache.create_task_local(make_task("t1", summary="milk", categories=["shop"]), window.conn)
    cache.create_task_local(make_task("t2", summary="report"), window.conn)
    kanban = _kanban(window)

    _tick(kanban.filter_bar.tags, {"shop"})
    assert _shown(kanban) == 1

    kanban.filter_bar.clear()
    assert _shown(kanban) == 2

    kanban.filter_bar.text.setText("repo")
    assert _shown(kanban) == 1


def test_picking_every_tag_still_hides_the_untagged(window, make_task):
    """Unlike calendars, "all tags" is a real filter: it means "has a tag"."""
    cache.create_task_local(make_task("t1", categories=["a"]), window.conn)
    cache.create_task_local(make_task("t2", categories=["b"]), window.conn)
    cache.create_task_local(make_task("t3"), window.conn)
    kanban = _kanban(window)

    _tick(kanban.filter_bar.tags, {"a", "b"})
    assert _shown(kanban) == 2


def test_a_refresh_does_not_destroy_the_open_menus_actions(window, make_task):
    """Ticking a box refreshes the board, and the refresh repopulates the very
    menu the tick came from.  Rebuilding it there would delete the QAction
    mid-signal — a hard crash, not a redraw."""
    cache.create_task_local(make_task("t1"), window.conn)
    kanban = _kanban(window)
    before = kanban.filter_bar.calendars._checkable_actions()

    kanban.refresh()
    kanban.refresh()

    after = kanban.filter_bar.calendars._checkable_actions()
    assert [a.data() for a in before] == [a.data() for a in after]


# ------------------------------------------------------------- hierarchies


def _find(tree, uid):
    from davpunk.ui.views import TASK_ROLE, _walk

    for item in _walk(tree):
        task = item.data(0, TASK_ROLE)
        if task is not None and task.uid == uid:
            return item
    return None


def test_a_kanban_column_nests_children_under_their_parent(window, make_task):
    """The board was flat, so a parent and its subtasks were peer cards."""
    cache.create_task_local(make_task("p", summary="parent"), window.conn)
    cache.create_task_local(make_task("c", summary="child", parent_uid="p"), window.conn)
    kanban = _kanban(window)

    column = kanban.lists["todo"]
    assert column.topLevelItemCount() == 1
    assert _find(column, "p").childCount() == 1
    assert _find(column, "c").parent() is _find(column, "p")


def test_a_folded_parent_says_how_many_it_is_hiding(window, make_task):
    cache.create_task_local(make_task("p", summary="parent"), window.conn)
    cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    cache.create_task_local(make_task("g", parent_uid="c"), window.conn)
    kanban = _kanban(window)
    assert _find(kanban.lists["todo"], "p").text(0) == "parent  (2)"


def test_a_subtree_starts_folded_and_a_bucket_heading_starts_open(window, make_task):
    cache.create_task_local(make_task("p"), window.conn)
    cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    kanban = _kanban(window)
    assert not _find(kanban.lists["todo"], "p").isExpanded()

    tree = _list(window)
    assert tree.topLevelItem(0).isExpanded()
    assert not _find(tree, "p").isExpanded()


def test_a_fold_survives_a_refresh(window, make_task):
    """A refresh rebuilds every row, and the poll fires every couple of
    seconds — an unremembered fold would re-open on its own."""
    cache.create_task_local(make_task("p"), window.conn)
    cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    kanban = _kanban(window)

    _find(kanban.lists["todo"], "p").setExpanded(True)
    kanban.refresh()
    assert _find(kanban.lists["todo"], "p").isExpanded()

    _find(kanban.lists["todo"], "p").setExpanded(False)
    kanban.refresh()
    assert not _find(kanban.lists["todo"], "p").isExpanded()


def test_fold_all_and_unfold_all(window, make_task):
    cache.create_task_local(make_task("p"), window.conn)
    cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    kanban = _kanban(window)

    kanban.set_all_folded(True)
    assert _find(kanban.lists["todo"], "p").isExpanded()
    kanban.set_all_folded(False)
    assert not _find(kanban.lists["todo"], "p").isExpanded()


def test_indent_makes_the_selected_task_a_subtask(window, make_task):
    """Tab and Shift+Tab were in the keymap and the ? overlay, bound to nothing."""
    cache.create_task_local(make_task("a", davpunk_order=1000), window.conn)
    cache.create_task_local(make_task("b", davpunk_order=2000), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "b"))

    window.reparent_selected(vm.indent_fields)
    assert _find(tree, "b").parent() is _find(tree, "a")

    tree.setCurrentItem(_find(tree, "b"))
    window.reparent_selected(vm.outdent_fields)
    assert _find(tree, "b").parent() is not _find(tree, "a")


def test_indenting_a_read_only_task_does_nothing(window, make_task):
    cache.create_task_local(make_task("a", davpunk_order=1000), window.conn)
    task = make_task("b", davpunk_order=2000)
    task.read_only_reason = ReadOnlyReason.OVERSIZE
    cache.create_task_local(task, window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "b"))

    window.reparent_selected(vm.indent_fields)
    assert _find(tree, "b").parent() is not _find(tree, "a")


def test_dropping_a_card_onto_another_nests_it_and_moves_it_there(window, make_task):
    """A drag used to be accepted by Qt and dropped on the floor: nothing was
    written, and the next refresh snapped the card back."""
    cache.create_task_local(make_task("p"), window.conn)
    cache.create_task_local(make_task("c"), window.conn)
    kanban = _kanban(window)
    parent = _find(kanban.lists["todo"], "p")
    child = _find(kanban.lists["todo"], "c").data(0, _TASK_ROLE())

    kanban._on_drop("inprogress", child, parent.data(0, _TASK_ROLE()))

    moved = _reload(window, child)
    assert moved.parent_uid == "p"
    assert moved.status is Status.IN_PROCESS


def test_dropping_a_card_on_empty_space_only_moves_it(window, make_task):
    cache.create_task_local(make_task("c"), window.conn)
    kanban = _kanban(window)
    card = _find(kanban.lists["todo"], "c").data(0, _TASK_ROLE())

    kanban._on_drop("done", card, None)

    moved = _reload(window, card)
    assert moved.parent_uid is None
    assert moved.status is Status.COMPLETED


def test_a_card_cannot_be_nested_under_one_in_another_calendar(
    window, make_task, other_calendar_id
):
    """RELATED-TO resolves within one calendar, so the link would never render."""
    cache.create_task_local(make_task("p", calendar_id=other_calendar_id), window.conn)
    cache.create_task_local(make_task("c"), window.conn)
    kanban = _kanban(window)
    parent = _find(kanban.lists["todo"], "p").data(0, _TASK_ROLE())
    child = _find(kanban.lists["todo"], "c").data(0, _TASK_ROLE())

    kanban._on_drop("todo", child, parent)

    assert _reload(window, child).parent_uid is None


def _TASK_ROLE():
    from davpunk.ui.views import TASK_ROLE

    return TASK_ROLE


def _reload(window, task):
    return cache.get_task(task.id, window.conn)


def test_switching_views_refreshes(window, make_task):
    cache.create_task_local(make_task("t1", summary="Visible"), window.conn)
    window.switch_view(1)
    qapp_process(window)
    assert _shown(window.kanban_view)


def qapp_process(window) -> None:
    QApplication.instance().processEvents()


# --------------------------------------------------------------- list view


def test_tasks_are_grouped_into_buckets(window, make_task):
    cache.create_task_local(make_task("old", summary="Old", due_value="20200101"), window.conn)
    cache.create_task_local(make_task("none", summary="No due"), window.conn)
    window.refresh()

    tree = window.list_view.tree
    headers = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert any(h.startswith("Overdue") for h in headers)
    assert any(h.startswith("No due date") for h in headers)


def test_subtasks_nest_under_their_parent(window, make_task):
    cache.create_task_local(make_task("p", summary="Parent"), window.conn)
    cache.create_task_local(make_task("c", summary="Child", parent_uid="p"), window.conn)
    window.refresh()

    assert window.list_view.select_uid("c")
    assert window.list_view.selected_task().uid == "c"


def test_a_read_only_task_is_badged(window, synced_task):
    task_id = synced_task("ro")
    window.conn.execute("UPDATE tasks SET read_only_reason = 'multipart' WHERE id = ?", (task_id,))
    window.refresh()
    window.list_view.select_uid("ro")

    item = window.list_view.tree.currentItem()
    assert "multipart" in item.text(0)


def test_a_conflicted_task_is_badged(window, synced_task):
    task_id = synced_task("cf")
    with cache.tx(window.conn):
        cache.upsert_conflict(task_id, ics("cf", "Mine"), ics("cf", "Theirs"), "e", window.conn)
        cache.set_sync_state(task_id, SyncState.CONFLICT, window.conn)
    window.refresh()
    window.list_view.select_uid("cf")

    assert "conflict" in window.list_view.tree.currentItem().text(0)


def test_show_completed_is_a_runtime_toggle(window, make_task):
    """Non-persistent by design: it is a view state, not a preference."""
    cache.create_task_local(
        make_task("done", summary="Finished", status=Status.COMPLETED), window.conn
    )
    window.refresh()
    assert not window.list_view.select_uid("done")

    window.list_view.toggle_show_completed()
    assert window.list_view.select_uid("done")


def test_the_midnight_timer_is_armed(window):
    """Otherwise an app left open overnight shows yesterday's buckets."""
    assert window.list_view._midnight.isActive()
    assert window.list_view._midnight.isSingleShot()


# -------------------------------------------------------------------- actions


def test_toggle_complete_goes_through_the_cache_helper(window, synced_task):
    task_id = synced_task("t")
    window.refresh()
    window.list_view.select_uid("t")
    window.toggle_complete()

    row = cache.get_task_row(task_id, window.conn)
    assert row["status"] == Status.COMPLETED.value
    assert row["percent_complete"] == 100  # canonicalized
    assert row["sync_state"] == SyncState.DIRTY.value


def test_toggling_a_completed_task_reopens_it(window, synced_task):
    task_id = synced_task("t", status=Status.COMPLETED)
    # A completed task is hidden by default, so it has to be visible to select.
    window.list_view.toggle_show_completed()
    assert window.list_view.select_uid("t")
    window.toggle_complete()
    assert cache.get_task_row(task_id, window.conn)["status"] == Status.NEEDS_ACTION.value


def test_editing_a_conflicted_task_opens_the_conflict_instead(window, synced_task, monkeypatch):
    task_id = synced_task("cf")
    with cache.tx(window.conn):
        cache.upsert_conflict(task_id, ics("cf", "Mine"), ics("cf", "Theirs"), "e", window.conn)
        cache.set_sync_state(task_id, SyncState.CONFLICT, window.conn)
    window.refresh()

    opened = {"n": 0}
    monkeypatch.setattr(window, "resolve_conflict_for", lambda t: opened.__setitem__("n", 1))
    window.open_editor(cache.get_task(task_id, window.conn))
    assert opened["n"] == 1


def test_reordering_writes_a_new_order(window, make_task):
    for uid, order in (("a", 1000), ("b", 2000), ("c", 3000)):
        cache.create_task_local(make_task(uid, davpunk_order=order), window.conn)
    window.refresh()
    window.list_view.select_uid("c")
    window.reorder_selected(-1)

    orders = {
        row["uid"]: row["davpunk_order"]
        for row in window.conn.execute("SELECT uid, davpunk_order FROM tasks")
    }
    assert orders["a"] < orders["c"] < orders["b"]


def test_the_blocked_change_count_is_surfaced(window, synced_task):
    task_id = synced_task("t")
    cache.update_task_optimistic(task_id, {"summary": "x"}, window.conn)
    window.conn.execute("UPDATE pending_changes SET blocked = 1")
    window.refresh()

    # isVisible() is False for a child of a window that was never shown;
    # isHidden() is the state the code actually sets.
    assert not window.attention.isHidden()
    assert "1 change" in window.attention.text()


# ------------------------------------------------------------------ selection


def _select(tree, *uids):
    """Select several rows, the way an ExtendedSelection click-drag would."""
    items = [_find(tree, uid) for uid in uids]
    tree.setCurrentItem(items[0])  # clears the selection, so it goes first
    for item in items:
        item.setSelected(True)
    return items


def test_both_trees_allow_a_multiple_selection(window):
    """Cut and delete act on a selection, so one row at a time is not enough."""
    from PySide6.QtWidgets import QAbstractItemView

    extended = QAbstractItemView.SelectionMode.ExtendedSelection
    assert window.list_view.tree.selectionMode() == extended
    assert all(w.selectionMode() == extended for w in window.kanban_view.lists.values())


def test_selected_tasks_returns_the_whole_selection(window, make_task):
    for uid in ("a", "b", "c"):
        cache.create_task_local(make_task(uid), window.conn)
    tree = _list(window)
    _select(tree, "a", "c")

    assert {t.uid for t in window.selected_tasks()} == {"a", "c"}
    assert window.selected_task().uid == "a"  # still the first, for older callers


def test_a_board_action_survives_focus_leaving_the_column(window, make_task):
    """Focus moves to the menu or the filter bar the moment you use them; a
    card the user can still see selected is the one they mean."""
    cache.create_task_local(make_task("t"), window.conn)
    kanban = _kanban(window)
    kanban.select_uid(_find(kanban.lists["todo"], "t").data(0, _TASK_ROLE()))

    assert not any(w.hasFocus() for w in kanban.lists.values())
    assert window.selected_task().uid == "t"
    assert [t.uid for t in window.selected_tasks()] == ["t"]


# ------------------------------------------------------------- list drag-drop

_ON = vm.DropPosition.ON
_ABOVE = vm.DropPosition.ABOVE
_BELOW = vm.DropPosition.BELOW


def _task(tree, uid):
    return _find(tree, uid).data(0, _TASK_ROLE())


def test_the_list_tree_actually_accepts_drops(window):
    """The kanban board once accepted a drag and dropped it on the floor; the
    list view could not even start one, because nothing set a drag-drop mode."""
    from PySide6.QtWidgets import QAbstractItemView

    tree = window.list_view.tree
    assert tree.dragDropMode() == QAbstractItemView.DragDropMode.DragDrop
    assert tree.defaultDropAction() == Qt.DropAction.MoveAction
    assert tree.dragEnabled()


def test_dropping_a_row_onto_another_nests_it(window, make_task):
    cache.create_task_local(make_task("a", davpunk_order=1000), window.conn)
    cache.create_task_local(make_task("b", davpunk_order=2000), window.conn)
    tree = _list(window)

    window.list_view._on_drop(_task(tree, "b"), _task(tree, "a"), _ON)

    assert _find(tree, "b").parent() is _find(tree, "a")


def test_dropping_a_row_between_two_others_reorders_it(window, make_task):
    for uid, order in (("a", 1000), ("b", 2000), ("c", 3000)):
        cache.create_task_local(make_task(uid, davpunk_order=order), window.conn)
    tree = _list(window)

    window.list_view._on_drop(_task(tree, "c"), _task(tree, "a"), _BELOW)

    orders = _orders(window)
    assert orders["a"] < orders["c"] < orders["b"]


def test_dropping_beside_a_row_in_another_group_reparents_and_reorders(window, make_task):
    """One update, not two: an interrupted drop must not leave a task nested
    where its order says it does not belong."""
    cache.create_task_local(make_task("p", davpunk_order=1000), window.conn)
    cache.create_task_local(make_task("kid", parent_uid="p", davpunk_order=500), window.conn)
    cache.create_task_local(make_task("loose", davpunk_order=2000), window.conn)
    tree = _list(window)

    window.list_view._on_drop(_task(tree, "loose"), _task(tree, "kid"), _BELOW)

    moved = _reload(window, _task(tree, "loose"))
    assert moved.parent_uid == "p"
    assert moved.davpunk_order > 500


def test_dropping_on_a_bucket_heading_does_nothing(window, make_task):
    """A bucket is a computed view of DUE, not a settable field — and a heading
    carries no task, so it arrives here as no target at all."""
    cache.create_task_local(make_task("a"), window.conn)
    tree = _list(window)
    before = _orders(window)

    window.list_view._on_drop(_task(tree, "a"), None, _ON)

    assert _orders(window) == before
    assert _reload(window, _task(tree, "a")).parent_uid is None


def test_dropping_a_read_only_row_is_refused(window, make_task):
    cache.create_task_local(make_task("a", davpunk_order=1000), window.conn)
    cache.create_task_local(
        make_task("b", davpunk_order=2000, read_only_reason=ReadOnlyReason.OVERSIZE), window.conn
    )
    tree = _list(window)

    window.list_view._on_drop(_task(tree, "b"), _task(tree, "a"), _ON)

    assert _reload(window, _task(tree, "b")).parent_uid is None


def test_a_row_cannot_be_dropped_onto_one_in_another_calendar(window, make_task, other_calendar_id):
    """RELATED-TO resolves within one calendar, so the link would never render."""
    cache.create_task_local(make_task("there", calendar_id=other_calendar_id), window.conn)
    cache.create_task_local(make_task("here"), window.conn)
    tree = _list(window)

    window.list_view._on_drop(_task(tree, "here"), _task(tree, "there"), _ON)

    assert _reload(window, _task(tree, "here")).parent_uid is None


def test_a_rebalancing_drop_reports_it_once(window, make_task):
    for uid, order in (("a", 1000), ("b", 1001), ("c", 5000)):
        cache.create_task_local(make_task(uid, davpunk_order=order), window.conn)
    tree = _list(window)

    said = []
    window.list_view.toast.connect(said.append)
    window.list_view._on_drop(_task(tree, "c"), _task(tree, "b"), _ABOVE)

    assert said and "reordering" in said[0]


class _FakeDrop:
    """A drop event with a source.

    ``QDropEvent`` takes its source from Qt's live drag manager, and there is
    no way to set one on a synthetic event — so the parts that can be built for
    real are, and the source is stood in for.
    """

    def __init__(self, source, point):
        self._source = source
        self._point = point
        self.accepted = False
        self.ignored = False

    def source(self):
        return self._source

    def position(self):
        return self._point

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


@pytest.mark.parametrize(
    ("indicator", "expected"),
    [
        ("OnItem", vm.DropPosition.ON),
        ("AboveItem", vm.DropPosition.ABOVE),
        ("BelowItem", vm.DropPosition.BELOW),
        ("OnViewport", vm.DropPosition.ON),
    ],
)
def test_a_drop_event_maps_qts_indicator_to_a_drop_position(
    window, make_task, monkeypatch, indicator, expected
):
    """The seam where a Qt API change would silently break every drop."""
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QAbstractItemView

    cache.create_task_local(make_task("a"), window.conn)
    cache.create_task_local(make_task("b"), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "b"))

    monkeypatch.setattr(
        type(tree),
        "dropIndicatorPosition",
        lambda self: getattr(QAbstractItemView.DropIndicatorPosition, indicator),
    )
    monkeypatch.setattr(type(tree), "itemAt", lambda self, point: _find(self, "a"))

    seen = []
    tree.dropped.connect(lambda task, onto, position: seen.append((task.uid, onto.uid, position)))
    event = _FakeDrop(tree, QPointF(0, 0))
    tree.dropEvent(event)

    assert seen == [("b", "a", expected)]
    assert event.accepted


def test_a_drop_with_no_dragged_task_is_ignored(window, make_task):
    """A real QDropEvent: outside a live drag it carries no source at all."""
    from PySide6.QtCore import QMimeData, QPointF
    from PySide6.QtGui import QDropEvent

    cache.create_task_local(make_task("a", davpunk_order=1000), window.conn)
    tree = _list(window)
    before = _orders(window)

    seen = []
    tree.dropped.connect(lambda *args: seen.append(args))
    event = QDropEvent(
        QPointF(0, 0),
        Qt.DropAction.MoveAction,
        QMimeData(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    tree.dropEvent(event)

    assert seen == []
    assert _orders(window) == before


def _orders(window):
    return {
        row["uid"]: row["davpunk_order"]
        for row in window.conn.execute("SELECT uid, davpunk_order FROM tasks")
    }


# ------------------------------------------------------------------ new task


def _accept_editor(monkeypatch, fill=None):
    """Run the editor without showing it, filling it in the way a user would."""

    def _exec(editor):
        if fill is not None:
            fill(editor)
        return TaskEditor.DialogCode.Accepted

    monkeypatch.setattr(TaskEditor, "exec", _exec)


def test_a_new_task_is_created_with_what_the_editor_holds(window, monkeypatch):
    _accept_editor(monkeypatch, lambda e: e.summary.setText("Written down"))
    window.new_task()

    row = window.conn.execute("SELECT summary, sync_state FROM tasks").fetchone()
    assert row["summary"] == "Written down"
    assert row["sync_state"] == SyncState.NEW.value


def test_cancelling_the_editor_creates_nothing(window, monkeypatch):
    monkeypatch.setattr(TaskEditor, "exec", lambda self: TaskEditor.DialogCode.Rejected)
    window.new_task()

    assert window.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_a_task_with_no_summary_still_has_a_name(window, monkeypatch):
    _accept_editor(monkeypatch)
    window.new_task()

    assert window.conn.execute("SELECT summary FROM tasks").fetchone()[0] == "New task"


def test_creating_needs_a_list_to_create_into(qapp, conn, ui_config, db_path, monkeypatch):
    """Before the first sync DavPunk does not know any collections, and a task
    with no calendar_id is one the cache refuses outright."""
    from davpunk.ui.main_window import MainWindow

    cache.reconcile_remotes(ui_config.remotes, conn)
    win = MainWindow(ui_config, conn, db_path)
    try:
        told = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a: told.append(a))
        monkeypatch.setattr(
            TaskEditor, "exec", lambda self: pytest.fail("the editor must not open")
        )
        win.new_task()

        assert told and "No lists yet" in told[0][1]
    finally:
        win.sync.stop()
        win._poll.stop()


def test_a_new_task_starts_in_the_list_the_user_is_looking_at(
    window, make_task, other_calendar_id, monkeypatch
):
    cache.create_task_local(make_task("there", calendar_id=other_calendar_id), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "there"))

    seen = {}
    _accept_editor(monkeypatch, lambda e: seen.update(start=e.calendar.currentData()))
    window.new_task()

    assert seen["start"] == other_calendar_id


def test_the_chosen_list_is_where_the_task_lands(
    window, calendar_id, other_calendar_id, monkeypatch
):
    def pick_the_other(editor):
        editor.summary.setText("Elsewhere")
        editor.calendar.setCurrentIndex(editor.calendar.findData(other_calendar_id))

    _accept_editor(monkeypatch, pick_the_other)
    window.new_task()

    row = window.conn.execute("SELECT calendar_id FROM tasks").fetchone()
    assert row["calendar_id"] == other_calendar_id


def test_a_new_subtask_starts_under_the_selection(window, make_task, monkeypatch):
    cache.create_task_local(make_task("p"), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "p"))

    seen = {}
    _accept_editor(
        monkeypatch,
        lambda e: (e.summary.setText("Child"), seen.update(parent=e.parent_task.currentData())),
    )
    window.new_subtask()

    assert seen["parent"] == "p"
    row = window.conn.execute("SELECT parent_uid FROM tasks WHERE summary = 'Child'").fetchone()
    assert row["parent_uid"] == "p"


def test_a_new_subtask_needs_a_selection(window, monkeypatch):
    monkeypatch.setattr(TaskEditor, "exec", lambda self: pytest.fail("nothing was selected"))
    window.new_subtask()


def test_a_new_task_is_ordered_after_its_own_siblings_only(window, make_task, monkeypatch):
    """An order value is only meaningful inside one sibling group, so a new
    child must not be pushed past an unrelated root task's order."""
    cache.create_task_local(make_task("p", davpunk_order=1000), window.conn)
    cache.create_task_local(make_task("loud", davpunk_order=99000), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "p"))

    _accept_editor(monkeypatch, lambda e: e.summary.setText("Child"))
    window.new_subtask()

    row = window.conn.execute("SELECT davpunk_order FROM tasks WHERE summary = 'Child'").fetchone()
    assert row["davpunk_order"] < 99000


# --------------------------------------------------------- reparent by editor


def test_changing_the_parent_in_the_editor_reparents_the_task(window, make_task, monkeypatch):
    cache.create_task_local(make_task("p"), window.conn)
    task_id = cache.create_task_local(make_task("c"), window.conn)
    window.refresh()

    _accept_editor(
        monkeypatch, lambda e: e.parent_task.setCurrentIndex(e.parent_task.findData("p"))
    )
    window.open_editor(cache.get_task(task_id, window.conn))

    assert cache.get_task_row(task_id, window.conn)["parent_uid"] == "p"


def test_a_parent_change_moves_the_order_with_it(window, make_task, monkeypatch):
    """Through reparent_fields, so the task lands at the end of its new
    siblings: the order it carries belongs to the group it left."""
    cache.create_task_local(make_task("p", davpunk_order=1000), window.conn)
    cache.create_task_local(make_task("kid", parent_uid="p", davpunk_order=7000), window.conn)
    task_id = cache.create_task_local(make_task("c", davpunk_order=2000), window.conn)
    window.refresh()

    _accept_editor(
        monkeypatch, lambda e: e.parent_task.setCurrentIndex(e.parent_task.findData("p"))
    )
    window.open_editor(cache.get_task(task_id, window.conn))

    assert cache.get_task_row(task_id, window.conn)["davpunk_order"] > 7000


def test_clearing_the_parent_in_the_editor_promotes_the_task(window, make_task, monkeypatch):
    cache.create_task_local(make_task("p"), window.conn)
    task_id = cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    window.refresh()

    _accept_editor(monkeypatch, lambda e: e.parent_task.setCurrentIndex(0))
    window.open_editor(cache.get_task(task_id, window.conn))

    assert cache.get_task_row(task_id, window.conn)["parent_uid"] is None


# -------------------------------------------------------------------- delete


def _confirm(monkeypatch, answer=None, subtree=False):
    """Answer the delete confirmation, optionally ticking its checkbox."""
    answer = QMessageBox.StandardButton.Yes if answer is None else answer

    def _exec(box):
        if subtree and box.checkBox() is not None:
            box.checkBox().setChecked(True)
        return answer

    monkeypatch.setattr(QMessageBox, "exec", _exec)


def test_deleting_a_task_queues_it(window, synced_task, monkeypatch):
    task_id = synced_task("t")
    window.refresh()
    window.list_view.select_uid("t")

    _confirm(monkeypatch)
    window.delete_task()

    assert cache.get_task_row(task_id, window.conn)["sync_state"] == SyncState.PENDING_DELETE.value


def test_declining_the_confirmation_deletes_nothing(window, synced_task, monkeypatch):
    task_id = synced_task("t")
    window.refresh()
    window.list_view.select_uid("t")

    _confirm(monkeypatch, answer=QMessageBox.StandardButton.No)
    window.delete_task()

    assert cache.get_task_row(task_id, window.conn)["sync_state"] == SyncState.CLEAN.value


def test_deleting_a_never_synced_task_purges_it(window, make_task, monkeypatch):
    """"""
    task_id = cache.create_task_local(make_task("t"), window.conn)
    window.refresh()
    window.list_view.select_uid("t")

    _confirm(monkeypatch)
    window.delete_task()

    assert cache.get_task_row(task_id, window.conn) is None
    assert window.conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0


def test_children_are_promoted_rather_than_deleted_by_default(window, synced_task, monkeypatch):
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")
    window.refresh()
    window.list_view.select_uid("p")

    _confirm(monkeypatch)
    window.delete_task()

    assert cache.get_task_row(parent, window.conn)["sync_state"] == SyncState.PENDING_DELETE.value
    row = cache.get_task_row(child, window.conn)
    assert row["parent_uid"] is None
    assert row["sync_state"] == SyncState.DIRTY.value


def test_the_subtree_checkbox_takes_the_children_too(window, synced_task, monkeypatch):
    parent = synced_task("p")
    child = synced_task("c", parent_uid="p")
    grandchild = synced_task("g", parent_uid="c")
    window.refresh()
    window.list_view.select_uid("p")

    _confirm(monkeypatch, subtree=True)
    window.delete_task()

    states = {
        row["id"]: row["sync_state"]
        for row in window.conn.execute("SELECT id, sync_state FROM tasks")
    }
    assert set(states.values()) == {SyncState.PENDING_DELETE.value}
    assert set(states) == {parent, child, grandchild}
    # Deepest-first, so no child was ever promoted on its way out.
    assert cache.get_task_row(child, window.conn)["parent_uid"] == "p"


def test_the_subtree_checkbox_is_absent_without_children(window, synced_task, monkeypatch):
    """A question with only one answer is not worth asking."""
    synced_task("lonely")
    window.refresh()
    window.list_view.select_uid("lonely")

    seen = {}

    def _exec(box):
        seen["checkbox"] = box.checkBox()
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", _exec)
    window.delete_task()

    assert seen["checkbox"] is None


def test_deleting_a_multiple_selection_takes_all_of_them(window, synced_task, monkeypatch):
    ids = [synced_task(uid) for uid in ("a", "b", "c")]
    tree = _list(window)
    _select(tree, "a", "c")

    _confirm(monkeypatch)
    window.delete_task()

    states = {
        row["id"]: row["sync_state"]
        for row in window.conn.execute("SELECT id, sync_state FROM tasks")
    }
    assert states[ids[0]] == SyncState.PENDING_DELETE.value
    assert states[ids[2]] == SyncState.PENDING_DELETE.value
    assert states[ids[1]] == SyncState.CLEAN.value


def test_deleting_with_nothing_selected_asks_nothing(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("nothing was selected"))
    window.delete_task()


# ------------------------------------------------------------- cut and paste


def test_cutting_then_pasting_onto_a_task_nests_it(window, make_task):
    cache.create_task_local(make_task("p"), window.conn)
    child = cache.create_task_local(make_task("c"), window.conn)
    tree = _list(window)

    tree.setCurrentItem(_find(tree, "c"))
    window.cut_task()
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "p"))
    window.paste_task()

    assert cache.get_task_row(child, window.conn)["parent_uid"] == "p"
    assert window.clipboard.is_empty  # a cut is a move, not a copy


def test_pasting_with_nothing_selected_promotes_to_the_top_level(window, make_task):
    cache.create_task_local(make_task("p"), window.conn)
    child = cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    tree = _list(window)

    tree.setCurrentItem(_find(tree, "c"))
    window.cut_task()
    _list(window).setCurrentItem(None)
    window.paste_task()

    assert cache.get_task_row(child, window.conn)["parent_uid"] is None


def test_pasting_into_your_own_subtree_is_refused_and_the_cut_survives(
    window, make_task, monkeypatch
):
    parent = cache.create_task_local(make_task("p"), window.conn)
    cache.create_task_local(make_task("c", parent_uid="p"), window.conn)
    tree = _list(window)

    tree.setCurrentItem(_find(tree, "p"))
    window.cut_task()

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warned.append(a))
    tree.setCurrentItem(_find(tree, "c"))
    window.paste_task()

    assert warned
    assert not window.clipboard.is_empty  # the cut is still there to retarget
    assert cache.get_task_row(parent, window.conn)["parent_uid"] is None


def test_pasting_something_that_was_deleted_clears_the_clipboard(window, make_task, monkeypatch):
    task_id = cache.create_task_local(make_task("gone"), window.conn)
    cache.create_task_local(make_task("target"), window.conn)
    tree = _list(window)

    tree.setCurrentItem(_find(tree, "gone"))
    window.cut_task()
    cache.delete_task_local(task_id, window.conn)

    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: told.append(a))
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "target"))
    window.paste_task()

    assert told and "no longer exists" in told[0][2]
    assert window.clipboard.is_empty


def test_cutting_several_tasks_pastes_all_of_them_in_order(window, make_task):
    cache.create_task_local(make_task("p"), window.conn)
    for uid, order in (("a", 1000), ("b", 2000)):
        cache.create_task_local(make_task(uid, davpunk_order=order), window.conn)
    tree = _list(window)

    _select(tree, "a", "b")
    window.cut_task()
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "p"))
    window.paste_task()

    orders = _orders(window)
    parents = {
        row["uid"]: row["parent_uid"]
        for row in window.conn.execute("SELECT uid, parent_uid FROM tasks")
    }
    assert parents["a"] == parents["b"] == "p"
    assert orders["a"] < orders["b"]


def test_pasting_across_lists_asks_and_then_moves(
    window, make_task, calendar_id, other_calendar_id, monkeypatch
):
    """Pasting into another list is a two-stage PUT/DELETE, so it goes through
    the move dialog and its subtree question rather than happening silently."""
    moved = cache.create_task_local(make_task("mover"), window.conn)
    cache.create_task_local(make_task("target", calendar_id=other_calendar_id), window.conn)
    tree = _list(window)

    asked = []

    def _exec(dialog):
        asked.append(dialog.target_calendar_id())
        return MoveDialog.DialogCode.Accepted

    monkeypatch.setattr(MoveDialog, "exec", _exec)

    tree.setCurrentItem(_find(tree, "mover"))
    window.cut_task()
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "target"))
    window.paste_task()

    assert asked == [other_calendar_id]
    row = cache.get_task_row(moved, window.conn)
    assert row["calendar_id"] == other_calendar_id
    assert row["parent_uid"] == "target"


def test_declining_the_move_leaves_the_paste_undone(
    window, make_task, calendar_id, other_calendar_id, monkeypatch
):
    moved = cache.create_task_local(make_task("mover"), window.conn)
    cache.create_task_local(make_task("target", calendar_id=other_calendar_id), window.conn)
    tree = _list(window)

    monkeypatch.setattr(MoveDialog, "exec", lambda self: MoveDialog.DialogCode.Rejected)

    tree.setCurrentItem(_find(tree, "mover"))
    window.cut_task()
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "target"))
    window.paste_task()

    row = cache.get_task_row(moved, window.conn)
    assert row["calendar_id"] == calendar_id
    assert row["parent_uid"] is None
    assert not window.clipboard.is_empty


def test_a_cut_reaches_a_parent_the_filter_is_hiding(window, make_task):
    """The whole reason cut and paste exists: with a filter on, the two tasks
    are never on screen together, so no drag can connect them."""
    parent = make_task("p")
    parent.categories = ["alpha"]
    cache.create_task_local(parent, window.conn)
    child = make_task("c")
    child.categories = ["beta"]
    child_id = cache.create_task_local(child, window.conn)

    kanban = _kanban(window)
    kanban._on_filter_changed(vm.TaskFilter(tags=frozenset({"beta"})))
    assert _find(kanban.lists["todo"], "p") is None  # the parent is not rendered
    kanban.select_uid(_task(kanban.lists["todo"], "c"))
    window.cut_task()

    kanban._on_filter_changed(vm.TaskFilter(tags=frozenset({"alpha"})))
    assert _find(kanban.lists["todo"], "c") is None  # and now the child is not
    kanban.select_uid(_task(kanban.lists["todo"], "p"))
    window.paste_task()

    assert cache.get_task_row(child_id, window.conn)["parent_uid"] == "p"


def test_cutting_nothing_holds_nothing(window):
    window.cut_task()
    assert window.clipboard.is_empty


def test_pasting_an_empty_clipboard_says_so(window, make_task):
    cache.create_task_local(make_task("t"), window.conn)
    tree = _list(window)
    tree.setCurrentItem(_find(tree, "t"))

    window.paste_task()

    assert "Nothing has been cut" in window.statusBar().currentMessage()


# ------------------------------------------------------------- context menu


def test_the_context_menu_offers_the_task_actions(window, make_task):
    cache.create_task_local(make_task("t"), window.conn)
    tree = _list(window)
    task = _task(tree, "t")

    labels = [a.text() for a in window.context_menu_for(task).actions() if not a.isSeparator()]

    assert labels == [entry for entry in window.CONTEXT_ENTRIES if entry is not None]


def test_every_context_entry_shares_the_menu_bars_handler(window, make_task):
    """Naming them by handler key is what stops a right-click entry from
    quietly drifting away from its Edit-menu counterpart."""
    for entry in window.CONTEXT_ENTRIES:
        if entry is not None:
            assert entry in window.menu_handlers


def test_paste_is_disabled_until_something_is_cut(window, make_task):
    cache.create_task_local(make_task("t"), window.conn)
    tree = _list(window)
    task = _task(tree, "t")

    enabled = {a.text(): a.isEnabled() for a in window.context_menu_for(task).actions()}
    assert enabled["Paste"] is False

    tree.setCurrentItem(_find(tree, "t"))
    window.cut_task()
    enabled = {a.text(): a.isEnabled() for a in window.context_menu_for(task).actions()}
    assert enabled["Paste"] is True


def test_the_task_entries_are_disabled_with_nothing_selected(window):
    enabled = {a.text(): a.isEnabled() for a in window.context_menu_for(None).actions()}

    assert enabled["New task"] is True
    assert enabled["Delete task"] is False
    assert enabled["Cut"] is False
    assert enabled["Open task"] is False


def test_every_tree_answers_a_right_click(window):
    trees = [window.list_view.tree, window.search_view.results, *window.kanban_view.lists.values()]
    assert all(t.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu for t in trees)


# ------------------------------------------------------------------- kanban


def test_a_card_lands_in_the_column_its_status_names(window, make_task):
    cache.create_task_local(
        make_task("t", summary="Working", status=Status.IN_PROCESS), window.conn
    )
    window.switch_view(1)
    window.refresh()
    assert len(_column(window.kanban_view, "inprogress")) == 1


def test_an_override_beats_status(window, make_task):
    """"""
    cache.create_task_local(
        make_task("t", status=Status.NEEDS_ACTION, kanban_col="done"), window.conn
    )
    window.switch_view(1)
    window.refresh()
    assert len(_column(window.kanban_view, "done")) == 1


def test_moving_a_card_writes_both_status_and_the_override(window, synced_task):
    task_id = synced_task("t")
    window.switch_view(1)
    window.refresh()
    window.kanban_view.move_to_column(cache.get_task(task_id, window.conn), "inprogress")

    row = cache.get_task_row(task_id, window.conn)
    assert row["status"] == Status.IN_PROCESS.value
    assert row["kanban_col"] == "inprogress"


# ------------------------------------------------------------------- search


def test_search_finds_a_task(window, make_task):
    cache.create_task_local(make_task("t", summary="unmistakable zorblat"), window.conn)
    window.switch_view(2)
    window.search_view.query.setText("zorblat")

    assert window.search_view.results.topLevelItemCount() == 1


def test_invalid_fts_syntax_does_not_raise(window):
    """A user typing into a search box produces invalid FTS5 constantly."""
    window.switch_view(2)
    window.search_view.query.setText('"unterminated')
    assert window.search_view.results.topLevelItemCount() == 0


# ------------------------------------------------------------------- editor


def test_the_editor_reports_only_changed_fields(qapp, make_task):
    editor = TaskEditor(make_task("t", summary="Before"))
    assert editor.changed_fields() == {}

    editor.summary.setText("After")
    assert editor.changed_fields() == {"summary": "After"}


def test_the_editor_parses_tags(qapp, make_task):
    editor = TaskEditor(make_task("t"))
    editor.categories.setText("work, urgent ,  q3")
    assert editor.changed_fields()["categories"] == ["work", "urgent", "q3"]


def test_priority_zero_means_undefined_in_the_editor(qapp, make_task):
    """"""
    editor = TaskEditor(make_task("t", priority=3))
    editor.priority.setValue(0)
    assert editor.changed_fields()["priority"] is None


@pytest.mark.parametrize(
    "reason",
    [ReadOnlyReason.MULTIPART, ReadOnlyReason.OVERSIZE, ReadOnlyReason.CALENDAR_UNAVAILABLE],
)
def test_a_read_only_task_disables_every_field(qapp, make_task, reason):
    editor = TaskEditor(make_task("t", read_only_reason=reason))
    assert not editor.summary.isEnabled()
    assert not editor.status.isEnabled()


def test_a_conflicted_task_disables_the_editor(qapp, make_task):
    """"""
    editor = TaskEditor(make_task("t", sync_state=SyncState.CONFLICT))
    assert not editor.summary.isEnabled()


def test_the_read_only_banner_explains_why(qapp, make_task):
    from davpunk.ui.dialogs import _banner_for

    assert "recurring series" in _banner_for(make_task("t", read_only_reason="multipart"))
    assert "delete or move" in _banner_for(make_task("t", read_only_reason="oversize"))
    assert _banner_for(make_task("t")) == ""


def test_without_the_rows_there_are_no_pickers(qapp, make_task):
    """The two combos can only be honest about choices they were given."""
    editor = TaskEditor(make_task("t"))
    assert editor.calendar is None
    assert editor.parent_task is None
    assert editor.calendar_id() == make_task("t").calendar_id


# ------------------------------------------------------------- editor pickers


def test_the_list_picker_starts_on_the_tasks_own_list(qapp, conn, calendar_id, make_task):
    editor = TaskEditor(make_task("t"), calendars=cache.calendar_rows(conn), creating=True)

    assert editor.calendar.currentData() == calendar_id
    assert editor.calendar_id() == calendar_id


def test_the_list_picker_is_read_only_when_editing(qapp, conn, make_task):
    """Changing the list of a saved task is a PUT to the new collection and a
    DELETE from the old one, so it belongs to the move dialog."""
    editing = TaskEditor(make_task("t"), calendars=cache.calendar_rows(conn))
    creating = TaskEditor(make_task("t"), calendars=cache.calendar_rows(conn), creating=True)

    assert not editing.calendar.isEnabled()
    assert creating.calendar.isEnabled()


def test_an_unavailable_list_is_offered_only_to_the_task_already_in_it(
    qapp, conn, calendar_id, other_calendar_id, make_task
):
    with cache.tx(conn):
        cache.mark_calendar_unavailable(other_calendar_id, conn)

    here = TaskEditor(make_task("t"), calendars=cache.calendar_rows(conn), creating=True)
    there = TaskEditor(
        make_task("t", calendar_id=other_calendar_id),
        calendars=cache.calendar_rows(conn),
        creating=True,
    )

    assert [here.calendar.itemData(i) for i in range(here.calendar.count())] == [calendar_id]
    assert other_calendar_id in [there.calendar.itemData(i) for i in range(there.calendar.count())]


def test_the_parent_picker_offers_no_parent_first(qapp, make_task):
    editor = TaskEditor(make_task("t"), tasks=[make_task("t")])

    assert editor.parent_task.itemText(0) == "(no parent)"
    assert editor.parent_task.itemData(0) is None


def test_the_parent_picker_excludes_the_task_and_its_descendants(qapp, make_task):
    """A cycle refused here is a cycle the tree walker never has to break."""
    tasks = [
        make_task("p"),
        make_task("c", parent_uid="p"),
        make_task("g", parent_uid="c"),
        make_task("other"),
    ]
    editor = TaskEditor(tasks[0], tasks=tasks)

    offered = {editor.parent_task.itemData(i) for i in range(editor.parent_task.count())}
    assert offered == {None, "other"}


def test_the_parent_picker_only_offers_the_selected_list(
    qapp, make_task, calendar_id, other_calendar_id
):
    """RELATED-TO resolves within one calendar only."""
    tasks = [make_task("here"), make_task("there", calendar_id=other_calendar_id)]
    editor = TaskEditor(make_task("t"), tasks=tasks)

    offered = {editor.parent_task.itemData(i) for i in range(editor.parent_task.count())}
    assert offered == {None, "here"}


def test_changing_the_list_repopulates_the_parents(qapp, conn, make_task, other_calendar_id):
    tasks = [make_task("here"), make_task("there", calendar_id=other_calendar_id)]
    editor = TaskEditor(
        make_task("t"), calendars=cache.calendar_rows(conn), tasks=tasks, creating=True
    )
    assert editor.parent_task.findData("here") >= 0

    editor.calendar.setCurrentIndex(editor.calendar.findData(other_calendar_id))

    assert editor.parent_task.findData("here") == -1
    assert editor.parent_task.findData("there") >= 0


def test_the_editor_reports_a_changed_parent(qapp, make_task):
    tasks = [make_task("p"), make_task("t")]
    editor = TaskEditor(tasks[1], tasks=tasks)
    assert editor.changed_fields() == {}

    editor.parent_task.setCurrentIndex(editor.parent_task.findData("p"))
    assert editor.changed_fields() == {"parent_uid": "p"}


def test_choosing_no_parent_clears_it(qapp, make_task):
    tasks = [make_task("p"), make_task("t", parent_uid="p")]
    editor = TaskEditor(tasks[1], tasks=tasks)
    assert editor.parent_task.currentData() == "p"

    editor.parent_task.setCurrentIndex(0)
    assert editor.changed_fields() == {"parent_uid": None}


def test_a_read_only_task_disables_the_pickers_too(qapp, conn, make_task):
    editor = TaskEditor(
        make_task("t", read_only_reason=ReadOnlyReason.OVERSIZE),
        calendars=cache.calendar_rows(conn),
        tasks=[make_task("t")],
        creating=True,
    )

    assert not editor.calendar.isEnabled()
    assert not editor.parent_task.isEnabled()


def test_a_conflicted_task_disables_the_pickers_too(qapp, conn, make_task):
    editor = TaskEditor(
        make_task("t", sync_state=SyncState.CONFLICT),
        calendars=cache.calendar_rows(conn),
        tasks=[make_task("t")],
        creating=True,
    )

    assert not editor.calendar.isEnabled()
    assert not editor.parent_task.isEnabled()


# --------------------------------------------------------------- move dialog


def test_the_move_dialog_excludes_the_current_calendar(qapp, conn, calendar_id, other_calendar_id):
    dialog = MoveDialog(cache.calendar_rows(conn), calendar_id, has_children=False)
    assert dialog.target.count() == 1
    assert dialog.target_calendar_id() == other_calendar_id


def test_the_subtree_prompt_defaults_to_moving_them(qapp, conn, calendar_id):
    """Parent resolution is per-calendar, so leaving children behind orphans
    them."""
    dialog = MoveDialog(cache.calendar_rows(conn), calendar_id, has_children=True)
    assert dialog.wants_subtree() is True


def test_an_unavailable_calendar_is_not_a_move_target(qapp, conn, calendar_id, other_calendar_id):
    with cache.tx(conn):
        cache.mark_calendar_unavailable(other_calendar_id, conn)
    dialog = MoveDialog(cache.calendar_rows(conn), calendar_id, has_children=False)
    assert dialog.target.count() == 0


def test_a_fixed_target_shows_the_destination_without_reopening_it(
    qapp, conn, calendar_id, other_calendar_id
):
    """A paste already named the destination by where it was pasted."""
    dialog = MoveDialog(
        cache.calendar_rows(conn),
        calendar_id,
        has_children=True,
        fixed_target=other_calendar_id,
        heading="Pasting here also moves it:",
    )

    assert dialog.target.count() == 1
    assert dialog.target_calendar_id() == other_calendar_id
    assert not dialog.target.isEnabled()
    assert dialog.wants_subtree() is True


# ----------------------------------------------------------- conflict dialog


def conflict_view(conn, task_id, local, remote, etag='W/"v2"'):
    with cache.tx(conn):
        cache.upsert_conflict(task_id, local, remote, etag, conn)
        cache.set_sync_state(task_id, SyncState.CONFLICT, conn)
    conflict_id = conn.execute("SELECT id FROM conflict_queue WHERE resolved = 0").fetchone()[0]
    return load_conflict(conflict_id, conn)


def test_mode_a_offers_merge_and_take_all_server(qapp, conn, synced_task):
    task_id = synced_task("cf")
    view = conflict_view(conn, task_id, ics("cf", "Mine"), ics("cf", "Theirs"))
    dialog = ConflictDialog(view)

    assert view.mode is Mode.BOTH_CHANGED
    assert Resolution.MERGE in view.resolutions
    assert Resolution.RESTORE_SERVER in view.resolutions
    assert "summary" in dialog._choices


def test_mode_a_prime_offers_delete_anyway(qapp, conn, synced_task):
    task_id = synced_task("cf")
    view = conflict_view(conn, task_id, None, ics("cf", "Theirs"))

    assert view.mode is Mode.LOCAL_DELETE
    assert set(view.resolutions) == {Resolution.DELETE_ANYWAY, Resolution.RESTORE_SERVER}
    ConflictDialog(view)  # builds without a diff table


def test_mode_b_offers_recreate_and_accept(qapp, conn, synced_task):
    task_id = synced_task("cf")
    view = conflict_view(conn, task_id, ics("cf", "Mine"), None, None)

    assert view.mode is Mode.SERVER_DELETED
    assert set(view.resolutions) == {Resolution.RECREATE, Resolution.ACCEPT_DELETION}


def test_take_all_local_and_server_flip_every_row(qapp, conn, synced_task):
    task_id = synced_task("cf")
    view = conflict_view(
        conn,
        task_id,
        ics("cf", "Mine").replace("END:VTODO", "DESCRIPTION:mine\r\nEND:VTODO"),
        ics("cf", "Theirs").replace("END:VTODO", "DESCRIPTION:theirs\r\nEND:VTODO"),
    )
    dialog = ConflictDialog(view)

    dialog.take_all(server=True)
    assert dialog.selected_remote_fields() == {"summary", "description"}
    dialog.take_all(server=False)
    assert dialog.selected_remote_fields() == set()


def test_the_dialog_defaults_to_the_local_side(qapp, conn, synced_task):
    task_id = synced_task("cf")
    view = conflict_view(conn, task_id, ics("cf", "Mine"), ics("cf", "Theirs"))
    assert ConflictDialog(view).selected_remote_fields() == set()


# ------------------------------------------------------------------- keymap


def test_the_overlay_dialog_lists_the_bindings(qapp, ui_config):
    from davpunk.ui.dialogs import KeymapOverlay

    overlay = KeymapOverlay(Keymap(ui_config))
    assert "New task" in overlay.findChildren(type(overlay.children()[1]))[0].toPlainText()


def test_chords_are_not_registered_as_qt_shortcuts(window):
    """gg and dd are sequences; Qt would bind only the first key."""
    from PySide6.QtGui import QShortcut

    bound = {s.key().toString() for s in window.findChildren(QShortcut)}
    assert "g,g" not in bound
    assert "d,d" not in bound


def test_every_bound_action_has_a_handler(window):
    """A binding with no handler is a key that silently does nothing.

    Checked against everything ``_bind_shortcuts`` claims rather than a hand-
    picked few, so a handler added without its binding — or the reverse — is a
    test failure rather than something you find by pressing the key.
    """
    from PySide6.QtGui import QKeySequence, QShortcut

    bound = {s.key().toString().lower() for s in window.findChildren(QShortcut)}
    handlers = {
        "new_task",
        "new_subtask",
        "cut_task",
        "paste_task",
        "open_editor",
        "toggle_complete",
        "move_task",
        "sync_now",
        "help_overlay",
        "search",
        "view_list",
        "view_kanban",
        "view_search",
        "reorder_down",
        "reorder_up",
        "card_prev_column",
        "card_next_column",
        "indent",
        "outdent",
    }
    for action in handlers:
        binding = window.keymap[action]
        if not is_chord(binding):
            assert QKeySequence(binding).toString().lower() in bound, action


def test_cut_and_paste_are_in_the_overlay(window):
    overlay = window.keymap.overlay_text()
    assert "Ctrl+X" in overlay
    assert "Ctrl+V" in overlay
    assert "New subtask of the selection" in overlay


# -------------------------------------------------------------- first run


def test_the_wizard_rejects_http(qapp, davpunk_home):
    """"""
    from davpunk.ui.first_run import RemotePage

    page = RemotePage()
    page.url.setText("http://cal.example.com/dav/")
    page.remote_id.setText("work")
    page.username.setText("u")

    assert not page.isComplete()
    assert "cleartext" in page.problem.text()


def test_the_wizard_accepts_https(qapp, davpunk_home):
    from davpunk.ui.first_run import RemotePage

    page = RemotePage()
    page.url.setText("https://cal.example.com/dav/")
    page.remote_id.setText("work")
    page.username.setText("u")
    assert page.isComplete()


def test_the_wizard_requires_a_remote_id(qapp, davpunk_home):
    from davpunk.ui.first_run import RemotePage

    page = RemotePage()
    page.remote_id.setText("")
    assert not page.isComplete()


def test_writing_a_remote_preserves_existing_comments(davpunk_home, tmp_path):
    """tomlkit, not a re-serialize: a user's comments are theirs."""
    from davpunk.ui.first_run import write_remote

    path = tmp_path / "config.toml"
    path.write_text("# my careful notes\n[davpunk]\ntheme = 'dark'  # inline note\n")

    write_remote(
        path,
        {
            "id": "work",
            "name": "Work",
            "url": "https://cal.example.com/dav/",
            "username": "u",
            "sync_interval": 300,
            "color": "#4A9EFF",
            "gpg_key_id": "0xKEY",
        },
    )

    text = path.read_text()
    assert "# my careful notes" in text
    assert "# inline note" in text
    assert "[[davpunk.remotes]]" in text


def test_the_written_config_is_valid_and_0600(davpunk_home, tmp_path):
    from davpunk.config import load_config
    from davpunk.ui.first_run import write_remote

    path = tmp_path / "config.toml"
    write_remote(
        path,
        {
            "id": "work",
            "name": "Work",
            "url": "https://cal.example.com/dav/",
            "username": "u",
            "sync_interval": 300,
            "color": "#4A9EFF",
            "gpg_key_id": "0xKEY",
        },
    )

    assert path.stat().st_mode & 0o077 == 0
    config = load_config(path)
    assert config.remotes[0].id == "work"
    assert config.remotes[0].gpg_key_id == "0xKEY"


# ------------------------------------------------------------------- menu bar


def test_the_window_has_a_menu_bar(window):
    """Preferences used to be reachable only from the first-run wizard, so a
    setting you got wrong on day one could not be corrected in the app."""
    menus = [a.text().replace("&", "") for a in window.menuBar().actions()]
    assert menus == ["File", "Edit", "View", "Help"]


def test_preferences_is_reachable_from_the_menu(window):
    labels = [a.text().replace("&", "") for a in window.menus["Edit"].actions()]
    assert any("Preferences" in label for label in labels)


def test_every_menu_entry_has_a_handler(window):
    """A menu item that does nothing is worse than no menu item."""
    for name, menu in window.menus.items():
        for action in menu.actions():
            label = action.text().replace("&", "").split("\t")[0]
            if label:
                assert label in window.menu_handlers, f"{name} → {label}"
                assert callable(window.menu_handlers[label])


def test_menu_shortcuts_match_the_keymap(window):
    """The menu and the keymap must not disagree about what a key does."""
    new_task = next(a for a in window.menus["File"].actions() if "New task" in a.text())
    assert new_task.shortcut().toString().lower() == window.keymap["new_task"].lower()


def test_a_chord_is_shown_but_not_bound_as_a_shortcut(window):
    """QKeySequence cannot express "d then d"; keyPressEvent handles those."""
    delete = next(a for a in window.menus["Edit"].actions() if "Delete task" in a.text())
    assert delete.shortcut().isEmpty()
    assert window.keymap["delete_task"] in delete.text()


def test_show_completed_is_a_checkable_view_entry(window, make_task):
    cache.create_task_local(
        make_task("done", summary="Finished", status=Status.COMPLETED), window.conn
    )
    window.refresh()
    assert not window.show_completed_action.isChecked()

    window.show_completed_action.trigger()
    assert window.show_completed_action.isChecked()
    assert window.list_view.select_uid("done")


def test_the_toolbar_also_offers_preferences(window):
    """Discoverability: not everyone goes looking in a menu."""
    assert window.settings_button.text().startswith("Preferences")


# ------------------------------------------------------------------ settings


@pytest.fixture
def written_config(davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text(
        "# a comment the user wrote\n"
        "[davpunk]\n"
        "theme = 'dark'\n\n"
        "[davpunk.keys]\n"
        "sync_now = 'Ctrl+S'\n\n"
        "[[davpunk.remotes]]\n"
        "id = 'work'\nname = 'Work'\n"
        "url = 'https://cal.example.test/dav/'\nusername = 'u'\n"
    )
    path.chmod(0o600)
    return path


def test_the_settings_dialog_lists_the_configured_accounts(qapp, written_config):
    from davpunk.config import load_config
    from davpunk.ui.settings import SettingsDialog

    config = load_config(written_config)
    dialog = SettingsDialog(config, written_config)
    assert dialog.remote_list.count() == 1
    assert "Work" in dialog.remote_list.item(0).text()


def test_the_settings_dialog_shows_the_current_general_values(qapp, written_config):
    from davpunk.config import load_config
    from davpunk.ui.settings import SettingsDialog

    dialog = SettingsDialog(load_config(written_config), written_config)
    assert dialog.theme.currentText() == "dark"
    assert dialog.default_view.currentText() == "list"


def test_saving_preserves_comments_and_untouched_sections(qapp, written_config):
    """A settings dialog that eats your hand-written key bindings is worse than
    no settings dialog."""
    from davpunk.config import load_config
    from davpunk.ui.settings import write_settings

    config = load_config(written_config)
    write_settings(
        written_config,
        general={
            "theme": "light",
            "default_view": "kanban",
            "show_completed": True,
            "max_resource_bytes": 262144,
        },
        remotes=[r.model_dump() for r in config.remotes],
        mcp={
            "enabled": True,
            "capabilities": {"read": True, "write": False, "delete": False, "sync": False},
        },
    )

    text = written_config.read_text()
    assert "# a comment the user wrote" in text
    assert "sync_now" in text  # the [davpunk.keys] table survived

    back = load_config(written_config)
    assert back.theme == "light"
    assert back.default_view == "kanban"
    assert back.mcp.capabilities.read is True
    assert back.keymap()["sync_now"] == "Ctrl+S"
    assert [r.id for r in back.remotes] == ["work"]


def test_saving_keeps_the_config_readable(qapp, written_config):
    """Writing a config that will not load leaves no dialog to explain it."""
    from davpunk.config import load_config
    from davpunk.ui.settings import write_settings

    write_settings(
        written_config,
        general={
            "theme": "dark",
            "default_view": "list",
            "show_completed": False,
            "max_resource_bytes": 262144,
        },
        remotes=[
            {
                "id": "new",
                "name": "New",
                "url": "https://other.example.test/dav/",
                "username": "u",
                "gpg_key_id": "ABCD1234ABCD1234!",
                "sync_interval": 300,
            }
        ],
        mcp={
            "enabled": False,
            "capabilities": {"read": False, "write": False, "delete": False, "sync": False},
        },
    )
    config = load_config(written_config)
    assert config.remotes[0].gpg_key_id == "ABCD1234ABCD1234!"
    assert written_config.stat().st_mode & 0o077 == 0


def test_removing_an_account_rewrites_the_list(qapp, written_config):
    from davpunk.config import load_config
    from davpunk.ui.settings import write_settings

    write_settings(
        written_config,
        general={
            "theme": "dark",
            "default_view": "list",
            "show_completed": False,
            "max_resource_bytes": 262144,
        },
        remotes=[],
        mcp={
            "enabled": False,
            "capabilities": {"read": False, "write": False, "delete": False, "sync": False},
        },
    )
    assert load_config(written_config).remotes == []


def test_the_account_id_cannot_be_changed_when_editing(qapp, davpunk_home):
    """It names the credential and lock files; renaming would orphan both."""
    from davpunk.ui.remote_form import RemoteForm

    adding = RemoteForm()
    editing = RemoteForm({"id": "work", "url": "https://x.test/", "username": "u"}, editing=True)
    assert adding.remote_id.isEnabled()
    assert not editing.remote_id.isEnabled()


# ------------------------------------------------------- field explanations


def test_every_form_field_carries_an_explanation(qapp, davpunk_home):
    """The setup screen should not require you to already know CalDAV.

    The explanations are tooltips behind a ? badge rather than inline text:
    printed inline they wrapped to two or three lines each, which overflowed
    the wizard page and clipped every one of them.
    """
    from PySide6.QtWidgets import QLabel

    from davpunk.ui.remote_form import HELP, RemoteForm

    form = RemoteForm()
    tips = {b.toolTip() for b in form.findChildren(QLabel) if b.text() == "?"}
    for key in ("id", "url", "username", "gpg_key", "sync_interval", "name", "color", "auto_sync"):
        assert HELP[key] in tips, key


def test_every_field_has_a_question_mark_badge(qapp, davpunk_home):
    """The badge is the only thing telling you a tooltip exists."""
    from PySide6.QtWidgets import QLabel

    from davpunk.ui.remote_form import HELP, RemoteForm

    form = RemoteForm()
    badges = [b for b in form.findChildren(QLabel) if b.text() == "?"]
    assert len(badges) == len(HELP) - 1  # every field except the password
    assert all(b.toolTip() for b in badges)


def test_the_inputs_carry_the_help_too(qapp, davpunk_home):
    """Hovering the field itself should work, not only the badge."""
    from davpunk.ui.remote_form import RemoteForm

    form = RemoteForm()
    for widget in (form.remote_id, form.url, form.username, form.gpg_key, form.interval):
        assert widget.toolTip()
        assert widget.whatsThis()  # Shift+F1, i.e. reachable from the keyboard


def test_no_label_is_clipped_or_truncated(qapp, davpunk_home):
    """The reported bug: hints wrapped to three lines and overlapped the next
    field. Checked at a deliberately narrow width."""
    from PySide6.QtWidgets import QLabel

    from davpunk.ui.remote_form import RemoteForm

    form = RemoteForm()
    form.resize(560, 400)
    form.show()
    QApplication.instance().processEvents()

    for label in form.findChildren(QLabel):
        if not label.isVisible() or not label.text():
            continue
        if label.wordWrap():
            needed = label.heightForWidth(label.width())
            assert needed <= label.height() + 1, f"clipped: {label.text()[:40]!r}"
        else:
            assert label.width() >= label.sizeHint().width(), f"truncated: {label.text()!r}"


def test_a_field_label_keeps_its_full_text_when_narrow(qapp, davpunk_home):
    """A container can be squeezed where a bare QFormLayout label cannot, and
    the QLabel inside then silently renders "Encryption ke"."""
    from PySide6.QtWidgets import QLabel

    from davpunk.ui.widgets import help_label

    widget = help_label("Encryption key", "some help")
    label = next(child for child in widget.findChildren(QLabel) if child.text() != "?")
    assert widget.minimumWidth() >= label.sizeHint().width()
    assert label.minimumWidth() >= label.sizeHint().width()


def test_a_stored_none_does_not_blank_a_defaulted_field(qapp, davpunk_home):
    """model_dump() emits explicit None for anything unset, and dict.get only
    falls back on a *missing* key — so editing an account wiped its colour."""
    from davpunk.ui.remote_form import RemoteForm

    form = RemoteForm(
        {
            "id": "work",
            "url": "https://x.test/",
            "username": "u",
            "color": None,
            "sync_interval": None,
        },
        editing=True,
    )
    assert form.color.text() == "#4A9EFF"
    assert form.interval.value() == 300


def test_the_url_help_names_real_servers(qapp):
    from davpunk.ui.remote_form import HELP

    assert "Nextcloud" in HELP["url"]
    assert "Radicale" in HELP["url"]


def test_the_wizard_explains_what_it_will_ask_for(qapp, davpunk_home):
    from PySide6.QtWidgets import QLabel

    from davpunk.ui.first_run import WelcomePage

    page = WelcomePage()
    text = " ".join(w.text() for w in page.findChildren(QLabel))
    assert "GPG key" in text
    assert "?" in text  # points at the affordance
    assert "Preferences" in text  # tells you it is all changeable later


def test_the_key_picker_offers_only_usable_subkeys(qapp, davpunk_home, monkeypatch):
    from davpunk.core import credentials
    from davpunk.ui import remote_form

    listing = """\
sec:u:4096:1:AAAA000000000001:1700951442:0:::::cESCA:::#::23:
uid:u::::1700951442::ABC::Ada <ada@example.com>::::::::::0:
ssb:u:4096:1:BBBB000000000002:1700951442:0:::::e:::D276000124010000::23:
ssb:u:4096:1:CCCC000000000003:1739483354:0:::::e:::#::23:
"""
    monkeypatch.setattr(
        credentials, "list_secret_keys", lambda: credentials.parse_colon_listing(listing)
    )
    monkeypatch.setattr(
        remote_form.credentials,
        "list_secret_keys",
        lambda: credentials.parse_colon_listing(listing),
    )

    form = remote_form.RemoteForm()
    stored = [form.gpg_key.itemData(i) for i in range(form.gpg_key.count())]

    assert stored == ["BBBB000000000002!"]  # pinned, and only the usable one
    assert "CCCC000000000003" in form.skipped_note.text()
    assert "not on this machine" in form.skipped_note.text()


def test_a_configured_key_missing_from_the_keyring_is_kept_selectable(
    qapp, davpunk_home, monkeypatch
):
    """Saving must not silently swap the key out from under an existing setup."""
    from davpunk.ui import remote_form

    monkeypatch.setattr(remote_form.credentials, "list_secret_keys", list)
    form = remote_form.RemoteForm({"gpg_key_id": "DEAD000000000001!"})
    assert form.gpg_key.currentData() == "DEAD000000000001!"


def test_the_preferences_accelerator_is_actually_bound(window):
    """Ctrl+, contains a comma, which the chord check used to swallow."""
    preferences = next(a for a in window.menus["Edit"].actions() if "Preferences" in a.text())
    assert preferences.shortcut().toString() == "Ctrl+,"
    assert "\t" not in preferences.text()


# --------------------------------------------------------- MCP transport tab


@pytest.fixture
def sse_config(davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text(
        "[davpunk]\ntheme = 'dark'\n\n"
        "[davpunk.mcp]\nenabled = true\ntransport = 'sse'\nport = 9123\n"
        "token_gpg_key_id = 'ABCD1234!'\n\n"
        "[davpunk.mcp.capabilities]\nread = true\n"
    )
    path.chmod(0o600)
    return path


def settings_for(path):
    from davpunk.config import load_config
    from davpunk.ui.settings import SettingsDialog

    return SettingsDialog(load_config(path), path)


def test_the_transport_is_selectable(qapp, sse_config):
    """Previously only reachable by editing config.toml by hand."""
    dialog = settings_for(sse_config)
    assert dialog.transport.currentData() == "sse"
    assert {dialog.transport.itemData(i) for i in range(dialog.transport.count())} == {
        "stdio",
        "sse",
    }


def test_the_port_and_token_are_shown_for_sse(qapp, sse_config):
    dialog = settings_for(sse_config)
    assert dialog.port.value() == 9123
    assert dialog.port.isEnabled()
    assert dialog.show_token.isEnabled()


def test_stdio_disables_the_port_and_token(qapp, sse_config):
    """They mean nothing without a listening socket, and leaving them live
    implies stdio needs a token, which it does not."""
    dialog = settings_for(sse_config)
    dialog.transport.setCurrentIndex(dialog.transport.findData("stdio"))

    assert not dialog.port.isEnabled()
    assert not dialog.show_token.isEnabled()
    assert "no token" in dialog.token_value.placeholderText()


def test_the_token_encryption_key_is_selectable(qapp, sse_config):
    dialog = settings_for(sse_config)
    assert None in {dialog.token_key.itemData(i) for i in range(dialog.token_key.count())}
    assert dialog.token_key.currentData() == "ABCD1234!"


def test_a_configured_token_key_missing_from_the_keyring_is_kept(qapp, sse_config):
    """Saving must not silently drop it and re-key the token."""
    dialog = settings_for(sse_config)
    assert dialog.token_key.currentData() == "ABCD1234!"


def test_the_token_is_not_revealed_until_asked(qapp, sse_config):
    """Showing it can prompt gpg; opening a settings tab should not."""
    dialog = settings_for(sse_config)
    assert dialog.token_value.text() == ""
    assert "press Show" in dialog.token_value.placeholderText()


def test_revealing_a_plain_token_works(qapp, davpunk_home):
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text("[davpunk]\n\n[davpunk.mcp]\nenabled = true\ntransport = 'sse'\n")
    path.chmod(0o600)

    dialog = settings_for(path)
    token = dialog._reveal_token()
    assert token and dialog.token_value.text() == token


def test_transport_and_port_are_saved(qapp, sse_config):
    from davpunk.config import load_config
    from davpunk.ui.settings import write_settings

    write_settings(
        sse_config,
        general={
            "theme": "dark",
            "default_view": "list",
            "show_completed": False,
            "max_resource_bytes": 262144,
        },
        remotes=[],
        mcp={
            "enabled": True,
            "transport": "sse",
            "port": 9999,
            "token_gpg_key_id": "BEEF5678!",
            "capabilities": {"read": True, "write": False, "delete": False, "sync": False},
        },
    )
    mcp = load_config(sse_config).mcp
    assert (mcp.transport, mcp.port, mcp.token_gpg_key_id) == ("sse", 9999, "BEEF5678!")


def test_clearing_the_token_key_removes_it_from_the_config(qapp, sse_config):
    """Otherwise switching back to a plain token would leave the old key
    behind and DavPunk would keep looking for mcp-token.gpg."""
    from davpunk.config import load_config
    from davpunk.ui.settings import write_settings

    write_settings(
        sse_config,
        general={
            "theme": "dark",
            "default_view": "list",
            "show_completed": False,
            "max_resource_bytes": 262144,
        },
        remotes=[],
        mcp={
            "enabled": True,
            "transport": "stdio",
            "port": 8787,
            "token_gpg_key_id": None,
            "capabilities": {"read": True, "write": False, "delete": False, "sync": False},
        },
    )
    mcp = load_config(sse_config).mcp
    assert mcp.token_gpg_key_id is None
    assert not mcp.token_is_encrypted
    assert "token_gpg_key_id" not in sse_config.read_text()


# ------------------------------------------------------------- sync preview


def _dialog(window, monkeypatch, reports):
    """A DryRunDialog whose worker is replaced by canned reports."""
    from davpunk.ui.dry_run_dialog import DryRunDialog

    def fake_dry_run():
        for remote_id, text, writes, local in reports:
            window.sync.worker.dryRunFinished.emit(remote_id, text, writes, local)
        window.sync.worker.dryRunDone.emit()

    monkeypatch.setattr(window.sync, "dry_run", fake_dry_run)
    return DryRunDialog(window.sync, window)


def test_the_preview_shows_each_remotes_report_and_totals_them(window, monkeypatch):
    dialog = _dialog(
        window, monkeypatch, [("work", "work: 1 calendar(s)", 3, 5), ("home", "home: idle", 0, 2)]
    )
    assert "work: 1 calendar(s)" in dialog.body.toPlainText()
    assert "home: idle" in dialog.body.toPlainText()
    assert "3 request(s)" in dialog.headline.text()
    assert "7 task(s)" in dialog.headline.text()


def test_the_preview_says_plainly_when_nothing_would_change(window, monkeypatch):
    dialog = _dialog(window, monkeypatch, [("work", "work: idle", 0, 0)])
    assert "would change nothing" in dialog.headline.text()


def test_sync_now_is_disabled_until_every_remote_has_reported(window, monkeypatch):
    """Pressing it against a half-finished picture is exactly the mistake this
    dialog exists to prevent."""
    from davpunk.ui.dry_run_dialog import DryRunDialog

    monkeypatch.setattr(window.sync, "dry_run", lambda: None)
    dialog = DryRunDialog(window.sync, window)
    assert not dialog.sync_button.isEnabled()

    dialog.add_report("work", "work: 1 calendar(s)", 1, 0)
    assert not dialog.sync_button.isEnabled()

    dialog.finish()
    assert dialog.sync_button.isEnabled()


def test_closing_the_preview_does_not_start_a_sync(window, monkeypatch):
    dialog = _dialog(window, monkeypatch, [("work", "work: 1 calendar(s)", 1, 0)])
    dialog.reject()
    assert not dialog.proceed


def test_the_preview_reports_a_failure_rather_than_pretending(window, monkeypatch):
    """A plan that raises must reach the user, not vanish into the thread."""
    from davpunk.core import dry_run

    def boom(*_args, **_kwargs):
        raise RuntimeError("server on fire")

    monkeypatch.setattr(dry_run, "plan", boom)
    seen = []
    # Direct, not queued: the worker's thread affinity would otherwise park the
    # emission in a queue this thread never drains.
    window.sync.worker.dryRunFinished.connect(
        lambda *a: seen.append(a), Qt.ConnectionType.DirectConnection
    )
    window.sync.worker.dry_run_all()

    assert seen and "server on fire" in seen[0][1]


# ----------------------------------------------------------- automatic sync


def test_automatic_sync_is_on_by_default_and_round_trips(qapp, davpunk_home):
    from davpunk.ui.remote_form import RemoteForm

    assert RemoteForm().values()["auto_sync"] is True

    form = RemoteForm({"id": "work", "url": "https://x.test/", "auto_sync": False}, editing=True)
    assert form.auto_sync.isChecked() is False
    assert form.values()["auto_sync"] is False


def test_the_interval_is_disabled_when_nothing_will_use_it(qapp, davpunk_home):
    """A live interval next to a disabled toggle reads as "still syncing"."""
    from davpunk.ui.remote_form import RemoteForm

    form = RemoteForm()
    assert form.interval.isEnabled()

    form.auto_sync.setChecked(False)
    assert not form.interval.isEnabled()

    form.auto_sync.setChecked(True)
    assert form.interval.isEnabled()


def test_the_poll_never_starts_a_sync(window, monkeypatch):
    """The UI syncs only when asked.  Pinned here because the README claimed
    the opposite for a while, and only the code settles it."""
    monkeypatch.setattr(
        window.sync, "sync_all", lambda: pytest.fail("the poll must not contact a server")
    )
    for _ in range(3):
        window._tick()
