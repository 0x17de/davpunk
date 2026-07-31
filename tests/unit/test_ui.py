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
        # The selector has to agree with the stack, or the first switch back is
        # a no-op: the combo already reads "List" while Kanban is on screen.
        assert win.view_selector.currentText() == "Kanban"
    finally:
        win.sync.stop()
        win._poll.stop()


# -------------------------------------------------------------- kanban filter


def _kanban(window):
    window.switch_view(1)
    return window.kanban_view


def _tick(button, values):
    for action in button._checkable_actions():
        action.setChecked(action.data() in values)


def _shown(kanban):
    return sum(kanban.lists[c].count() for c in kanban.lists)


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
    for key in ("id", "url", "username", "gpg_key", "sync_interval", "name", "color"):
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
