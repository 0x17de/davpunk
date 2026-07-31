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

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 cannot be imported here")

from PySide6.QtWidgets import QApplication

from davpunk.config import DavPunkConfig, RemoteConfig
from davpunk.conflict.resolver import Mode, Resolution, load_conflict
from davpunk.core import cache
from davpunk.models.task import ReadOnlyReason, Status, SyncState
from davpunk.ui.dialogs import ConflictDialog, MoveDialog, TaskEditor
from davpunk.ui.keymap import Keymap

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
    finally:
        win.sync.stop()
        win._poll.stop()


def test_switching_views_refreshes(window, make_task):
    cache.create_task_local(make_task("t1", summary="Visible"), window.conn)
    window.switch_view(1)
    qapp_process(window)
    assert any(window.kanban_view.lists[c].count() for c in window.kanban_view.lists)


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


# ------------------------------------------------------------------- kanban


def test_a_card_lands_in_the_column_its_status_names(window, make_task):
    cache.create_task_local(
        make_task("t", summary="Working", status=Status.IN_PROCESS), window.conn
    )
    window.switch_view(1)
    window.refresh()
    assert window.kanban_view.lists["inprogress"].count() == 1


def test_an_override_beats_status(window, make_task):
    """"""
    cache.create_task_local(
        make_task("t", status=Status.NEEDS_ACTION, kanban_col="done"), window.conn
    )
    window.switch_view(1)
    window.refresh()
    assert window.kanban_view.lists["done"].count() == 1


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
    """A binding with no handler is a key that silently does nothing."""
    from PySide6.QtGui import QShortcut

    bound = {s.key().toString().lower() for s in window.findChildren(QShortcut)}
    for action in ("new_task", "sync_now", "help_overlay", "move_task"):
        assert window.keymap[action].lower() in bound


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
