"""The main window: sidebar, view switcher, sync bar and the keymap wiring."""

from __future__ import annotations

import logging
import uuid

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QToolBar,
)

from davpunk import paths
from davpunk.conflict.resolver import load_conflict, resolve_conflict
from davpunk.core import cache
from davpunk.core.cache import CacheError, ReadOnlyResourceError, TaskConflictError
from davpunk.core.locking import is_syncing
from davpunk.models.task import Status, Task
from davpunk.ui import viewmodel as vm
from davpunk.ui.dialogs import ConflictDialog, KeymapOverlay, MoveDialog, TaskEditor
from davpunk.ui.keymap import Keymap, is_chord
from davpunk.ui.sync_worker import SyncController
from davpunk.ui.views import KanbanView, ListView, SearchView

log = logging.getLogger("davpunk.ui.main_window")

#: The UI refreshes on syncFinished plus a poll combining
#: ``MAX(last_modified), COUNT(*)`` with ``is_syncing()``.
POLL_MS = 2000

#: config `default_view` -> index in the view switcher and the stack.
VIEW_INDEX = {"list": 0, "kanban": 1, "search": 2}


class MainWindow(QMainWindow):
    refreshed = Signal()

    def __init__(self, config, conn, db_path, config_path=None, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.conn = conn
        self.config_path = config_path or paths.config_file()
        self.keymap = Keymap(config)
        self._fingerprint = (0, 0)
        self._chord_prefix = ""

        self.setWindowTitle("DavPunk")
        self.resize(1100, 720)

        self.list_view = ListView(conn, config)
        self.kanban_view = KanbanView(conn, config)
        self.search_view = SearchView(conn)
        for view in (self.list_view, self.kanban_view, self.search_view):
            view.taskActivated.connect(self.open_editor)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.kanban_view)
        self.stack.addWidget(self.search_view)
        self.setCentralWidget(self.stack)

        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._bind_shortcuts()

        self.sync = SyncController(db_path, config.remotes, parent=self)
        self.sync.syncFinished.connect(self._on_sync_finished)
        self.sync.syncFailed.connect(self._on_sync_failed)
        self.sync.progress.connect(self._on_progress)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._tick)
        self._poll.start(POLL_MS)

        # Through switch_view, not stack.setCurrentIndex: setting the stack
        # directly leaves the toolbar combo reading "List" while the Kanban
        # board is on screen, and the first thing you do to the combo then
        # looks like it does nothing.
        self.switch_view(VIEW_INDEX.get(config.default_view, 0))
        self.refresh()

    # ------------------------------------------------------------------ chrome

    def _build_menu(self) -> None:
        """A menu bar, so nothing is reachable only once.

        The preferences dialog in particular was previously only ever shown by
        the first-run wizard, which meant a setting you got wrong on day one
        could not be corrected without editing config.toml by hand.
        """
        bar = self.menuBar()
        # Held on self: a QMenu returned by addMenu() that only a local
        # variable references gets collected on the Python side, and every
        # later menu.actions() call then raises "C++ object already deleted".
        self.menus: dict[str, object] = {}
        #: name -> handler, so a menu entry that does nothing is a test failure
        #: rather than something you find by clicking it.
        self.menu_handlers: dict[str, object] = {}

        file_menu = self.menus["File"] = bar.addMenu("&File")
        self._action(file_menu, "&New task", self.new_task, "new_task")
        self._action(file_menu, "&Sync now", self.sync_now, "sync_now")
        file_menu.addSeparator()
        self._action(file_menu, "&Quit", self.close, shortcut="Ctrl+Q")

        edit_menu = self.menus["Edit"] = bar.addMenu("&Edit")
        self._action(edit_menu, "&Open task", self.open_selected, "open_editor")
        self._action(edit_menu, "&Move to another list…", self.move_task, "move_task")
        self._action(edit_menu, "&Delete task", self.delete_task, "delete_task")
        edit_menu.addSeparator()
        self._action(
            edit_menu,
            "&Indent (make a subtask)",
            lambda: self.reparent_selected(vm.indent_fields),
            "indent",
        )
        self._action(
            edit_menu,
            "&Outdent (promote to root)",
            lambda: self.reparent_selected(vm.outdent_fields),
            "outdent",
        )
        edit_menu.addSeparator()
        self._action(edit_menu, "&Preferences…", self.show_settings, shortcut="Ctrl+,")

        view_menu = self.menus["View"] = bar.addMenu("&View")
        self._action(view_menu, "&List", lambda: self.switch_view(0), "view_list")
        self._action(view_menu, "&Kanban", lambda: self.switch_view(1), "view_kanban")
        self._action(view_menu, "&Search", lambda: self.switch_view(2), "view_search")
        view_menu.addSeparator()
        self.show_completed_action = QAction("Show &completed", self, checkable=True)
        self.show_completed_action.setChecked(self.list_view.show_completed)
        self.show_completed_action.triggered.connect(self._toggle_completed)
        self.menu_handlers["Show completed"] = self._toggle_completed
        view_menu.addAction(self.show_completed_action)
        view_menu.addSeparator()
        self._action(view_menu, "&Unfold all", lambda: self.set_all_folded(True), "expand_all")
        self._action(view_menu, "&Fold all", lambda: self.set_all_folded(False), "collapse_all")

        help_menu = self.menus["Help"] = bar.addMenu("&Help")
        self._action(help_menu, "&Key bindings", self.show_keymap, "help_overlay")
        self._action(help_menu, "&Run diagnostics…", self.show_doctor)
        help_menu.addSeparator()
        self._action(help_menu, "&About DavPunk", self.show_about)

    def _action(self, menu, text, handler, keymap_action=None, shortcut=None):
        """Menu entries show the same binding the keymap already defines, so
        the two can never disagree about what a key does."""
        action = QAction(text, self)
        binding = shortcut or (self.keymap.get(keymap_action) if keymap_action else None)
        if binding:
            # A chord ("d,d") is not a QKeySequence; show it without binding
            # it, since keyPressEvent already handles those.
            if is_chord(binding):
                action.setText(f"{text}\t{binding}")
            else:
                action.setShortcut(QKeySequence(binding))
        action.triggered.connect(handler)
        self.menu_handlers[text.replace("&", "")] = handler
        menu.addAction(action)
        return action

    def _toggle_completed(self) -> None:
        self.list_view.toggle_show_completed()
        self.show_completed_action.setChecked(self.list_view.show_completed)

    def show_settings(self) -> None:
        from davpunk.ui.settings import SettingsDialog

        SettingsDialog(self.config, self.config_path, self).exec()

    def show_doctor(self) -> None:
        """The same checks as `davpunk doctor`, without leaving the app."""
        from davpunk.cli import doctor

        checks = doctor.run_all(self.config_path)
        width = max((len(c.name) for c in checks), default=0)
        body = "\n".join(f"{c.status.value:<4} {c.name:<{width}}  {c.detail}" for c in checks)

        box = QMessageBox(self)
        box.setWindowTitle("Diagnostics")
        failures = [c for c in checks if c.failed]
        box.setIcon(QMessageBox.Icon.Warning if failures else QMessageBox.Icon.Information)
        box.setText(f"{len(failures)} check(s) failed." if failures else "All checks passed.")
        box.setDetailedText(body)
        box.exec()

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About DavPunk",
            "<h3>DavPunk</h3>"
            "<p><i>Work it. Sync it. Check it. Done.</i></p>"
            "<p>A CalDAV VTODO client that keeps everything in a local cache, "
            "so edits land instantly and sync afterwards.</p>"
            f"<p>Config: {self.config_path}</p>",
        )

    def _build_toolbar(self) -> None:
        bar = QToolBar("Views")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.view_selector = QComboBox()
        self.view_selector.addItems(["List", "Kanban", "Search"])
        self.view_selector.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.view_selector.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.view_selector)

        bar.addSeparator()
        bar.addWidget(QLabel("  "))
        self.sync_button = QPushButton("Sync now")
        self.sync_button.clicked.connect(self.sync_now)
        bar.addWidget(self.sync_button)

        self.settings_button = QPushButton("Preferences…")
        self.settings_button.clicked.connect(self.show_settings)
        bar.addWidget(self.settings_button)

    def _build_status_bar(self) -> None:
        self.setStatusBar(QStatusBar())
        self.sync_label = QLabel("")
        self.attention = QPushButton("")
        self.attention.setFlat(True)
        self.attention.clicked.connect(self.show_attention)
        self.attention.hide()
        self.statusBar().addPermanentWidget(self.sync_label)
        self.statusBar().addPermanentWidget(self.attention)

    def _bind_shortcuts(self) -> None:
        """Everything Qt can bind directly; chords are handled in keyPressEvent."""
        handlers = {
            "new_task": self.new_task,
            "open_editor": self.open_selected,
            "toggle_complete": self.toggle_complete,
            "move_task": self.move_task,
            "sync_now": self.sync_now,
            "help_overlay": self.show_keymap,
            "search": self.focus_search,
            "view_list": lambda: self.switch_view(0),
            "view_kanban": lambda: self.switch_view(1),
            "view_search": lambda: self.switch_view(2),
            "reorder_down": lambda: self.reorder_selected(+1),
            "reorder_up": lambda: self.reorder_selected(-1),
            "card_prev_column": lambda: self.kanban_view.shift_selected(-1),
            "card_next_column": lambda: self.kanban_view.shift_selected(+1),
            "indent": lambda: self.reparent_selected(vm.indent_fields),
            "outdent": lambda: self.reparent_selected(vm.outdent_fields),
        }
        for action, handler in handlers.items():
            binding = self.keymap.get(action)
            if binding and not is_chord(binding):
                QShortcut(QKeySequence(binding), self, activated=handler)

    def keyPressEvent(self, event) -> None:
        """Two-keystroke sequences: ``gg`` to the top, ``dd`` to delete."""
        text = event.text()
        if text:
            candidate = f"{self._chord_prefix},{text}" if self._chord_prefix else text
            action = self.keymap.chords().get(candidate)
            if action is not None:
                self._chord_prefix = ""
                self._run_chord(action)
                return
            if any(c.startswith(f"{text},") for c in self.keymap.chords()):
                self._chord_prefix = text
                return
        self._chord_prefix = ""
        super().keyPressEvent(event)

    def _run_chord(self, action: str) -> None:
        if action == "delete_task":
            self.delete_task()
        elif action == "top":
            self.list_view.tree.setCurrentItem(self.list_view.tree.topLevelItem(0))
        elif action == "expand_all":
            self.set_all_folded(True)
        elif action == "collapse_all":
            self.set_all_folded(False)

    def set_all_folded(self, is_open: bool) -> None:
        view = self.current_view()
        if hasattr(view, "set_all_folded"):
            view.set_all_folded(is_open)

    def reparent_selected(self, fields_for) -> None:
        """Indent / outdent, in whichever view has a task selected.

        ``fields_for`` returns ``None`` when the move is not available — the
        first task in a group has nothing to indent under, a root has nothing
        to outdent to — and that is a no-op, not an error worth a dialog.
        """
        task = self.selected_task()
        if task is None or task.is_read_only:
            return
        tasks = vm.load_tasks(self.conn)
        fields = fields_for(task, tasks)
        if fields is None:
            return
        cache.update_task_optimistic(task.id, fields, self.conn)
        self.refresh()

    # ------------------------------------------------------------------ views

    def current_view(self):
        return self.stack.currentWidget()

    def switch_view(self, index: int) -> None:
        self.view_selector.setCurrentIndex(index)

    def focus_search(self) -> None:
        self.switch_view(2)
        self.search_view.query.setFocus()

    def selected_task(self) -> Task | None:
        view = self.current_view()
        return view.selected_task() if hasattr(view, "selected_task") else None

    # --------------------------------------------------------------- actions

    def new_task(self) -> None:
        calendars = [r for r in cache.calendar_rows(self.conn) if r["available"]]
        if not calendars:
            QMessageBox.information(
                self, "No lists yet", "Sync at least once so DavPunk knows your task lists."
            )
            return

        task = Task(uid=uuid.uuid4().hex, calendar_id=calendars[0]["id"])
        editor = TaskEditor(task, self)
        if editor.exec() != TaskEditor.DialogCode.Accepted:
            return

        for field, value in editor.changed_fields().items():
            if field == "categories":
                task.categories = value
            else:
                setattr(task, field, value)
        task.summary = task.summary or "New task"

        siblings = vm.load_tasks(self.conn, calendar_ids=[task.calendar_id])
        task.davpunk_order = vm.initial_order(siblings)
        cache.create_task_local(task, self.conn)
        self.refresh()

    def open_selected(self) -> None:
        task = self.selected_task()
        if task is not None:
            self.open_editor(task)

    def open_editor(self, task: Task) -> None:
        if task.sync_state.value == "conflict":
            self.resolve_conflict_for(task)
            return

        fresh = cache.get_task(task.id, self.conn) or task
        editor = TaskEditor(fresh, self)
        if editor.exec() != TaskEditor.DialogCode.Accepted:
            return

        fields = editor.changed_fields()
        if not fields:
            return
        self._guarded(lambda: cache.update_task_optimistic(fresh.id, fields, self.conn))
        self.refresh()

    def toggle_complete(self) -> None:
        view = self.current_view()
        if hasattr(view, "toggle_complete"):
            self._guarded(view.toggle_complete)
        else:
            task = self.selected_task()
            if task is None:
                return
            new = Status.NEEDS_ACTION if task.status is Status.COMPLETED else Status.COMPLETED
            self._guarded(lambda: cache.update_task_optimistic(task.id, {"status": new}, self.conn))
        self.refresh()

    def delete_task(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete task",
            f"Delete “{task.summary or task.uid}”?\n\n"
            "Subtasks are never deleted with their parent; they become root tasks.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._guarded(lambda: cache.delete_task_local(task.id, self.conn))
        self.refresh()

    def move_task(self) -> None:
        task = self.selected_task()
        if task is None:
            return

        has_children = bool(cache.children_of(task.id, self.conn))
        dialog = MoveDialog(cache.calendar_rows(self.conn), task.calendar_id, has_children, self)
        if dialog.exec() != MoveDialog.DialogCode.Accepted:
            return
        target = dialog.target_calendar_id()
        if not target:
            return

        self._guarded(
            lambda: cache.move_task_local(
                task.id, target, self.conn, move_subtree=dialog.wants_subtree()
            )
        )
        self.refresh()

    def reorder_selected(self, delta: int) -> None:
        task = self.selected_task()
        if task is None or not isinstance(self.current_view(), ListView):
            return
        siblings = sorted(
            (
                t
                for t in vm.load_tasks(self.conn, calendar_ids=[task.calendar_id])
                if t.parent_uid == task.parent_uid
            ),
            key=vm.sort_key,
        )
        index = next((i for i, t in enumerate(siblings) if t.uid == task.uid), None)
        if index is None:
            return
        toast = self.list_view.reorder(task, max(0, index + delta))
        if toast:
            self.statusBar().showMessage(toast, 3000)

    # ------------------------------------------------------------- conflicts

    def resolve_conflict_for(self, task: Task) -> None:
        row = self.conn.execute(
            "SELECT id FROM conflict_queue WHERE task_id = ? AND resolved = 0", (task.id,)
        ).fetchone()
        if row is None:
            QMessageBox.information(self, "No conflict", "This conflict is already resolved.")
            return

        view = load_conflict(row["id"], self.conn)
        dialog = ConflictDialog(view, self.keymap, self)
        if dialog.exec() != ConflictDialog.DialogCode.Accepted or dialog.resolution is None:
            return

        try:
            resolve_conflict(view.conflict_id, dialog.resolution, dialog.merged, self.conn)
        except Exception as exc:
            QMessageBox.critical(self, "Could not resolve", str(exc))
        self.refresh()

    def show_attention(self) -> None:
        rows = list(
            self.conn.execute(
                "SELECT t.summary, t.uid, pc.last_error FROM pending_changes pc "
                "JOIN tasks t ON t.id = pc.task_id WHERE pc.blocked = 1"
            )
        )
        if not rows:
            return
        body = "\n".join(f"• {r['summary'] or r['uid']}: {r['last_error']}" for r in rows)
        QMessageBox.warning(self, "Changes that need attention", body)

    def show_keymap(self) -> None:
        KeymapOverlay(self.keymap, self).exec()

    # ------------------------------------------------------------------ sync

    def sync_now(self) -> None:
        """A user-initiated sync surfaces SyncBusy; the periodic one does not."""
        self.statusBar().showMessage("Syncing…")
        self.sync.sync_all()

    def _on_sync_finished(self, remote_id: str, summary: str) -> None:
        self.statusBar().showMessage(summary, 5000)
        self.refresh()

    def _on_sync_failed(self, remote_id: str, error: str) -> None:
        self.statusBar().showMessage(f"{remote_id}: {error}", 10000)
        self.refresh()

    def _on_progress(self, remote_id, phase, done, total, error) -> None:
        if total:
            self.sync_label.setText(f"{remote_id} {phase} {done}/{total}")
        else:
            self.sync_label.setText(f"{remote_id} {phase}")

    def _tick(self) -> None:
        """The 2 s poll: a cheap fingerprint plus a lock probe."""
        syncing = any(is_syncing(r.id) for r in self.config.remotes)
        self.sync_button.setEnabled(not syncing)
        if not syncing:
            self.sync_label.setText("")

        fingerprint = vm.refresh_fingerprint(self.conn)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self.refresh()

    def refresh(self) -> None:
        self._fingerprint = vm.refresh_fingerprint(self.conn)
        view = self.current_view()
        if hasattr(view, "refresh"):
            view.refresh()

        blocked = cache.blocked_count(self.conn)
        if blocked:
            self.attention.setText(f"⚠ {blocked} change(s) need attention")
            self.attention.show()
        else:
            self.attention.hide()
        self.refreshed.emit()

    # -------------------------------------------------------------- plumbing

    def _guarded(self, action) -> None:
        """Turn the cache layer's refusals into something a user can read."""
        try:
            action()
        except TaskConflictError:
            QMessageBox.warning(
                self,
                "Unresolved conflict",
                "This task diverged from the server. Open it to resolve the conflict first.",
            )
        except ReadOnlyResourceError as exc:
            QMessageBox.warning(
                self,
                "Read-only task",
                f"This task cannot be edited ({exc.reason}). It can still be deleted"
                + (" or moved." if exc.reason != "calendar-unavailable" else "."),
            )
        except CacheError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))

    def closeEvent(self, event) -> None:
        """Cancel, wait up to 5 s, then detach — the flock releases at process
        exit regardless."""
        self._poll.stop()
        self.sync.stop()
        cache.close_db(self.conn)
        super().closeEvent(event)
