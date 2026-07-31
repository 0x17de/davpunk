"""The main window: sidebar, view switcher, sync bar and the keymap wiring."""

from __future__ import annotations

import logging
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QMainWindow,
    QMenu,
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


def _name_list(tasks: list[Task], limit: int = 3) -> str:
    """A confirmation should name what it is about to do, not count it."""
    names = [f"“{task.summary or task.uid}”" for task in tasks[:limit]]
    rest = len(tasks) - len(names)
    if rest:
        names.append(f"{rest} more")
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


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
        #: What a cut is holding.  In-process: a local task id means nothing
        #: outside it, and the system clipboard would only carry noise.
        self.clipboard = vm.TaskClipboard()

        self.setWindowTitle("DavPunk")
        self.resize(1100, 720)

        self.list_view = ListView(conn, config)
        self.kanban_view = KanbanView(conn, config)
        self.search_view = SearchView(conn)
        for view in (self.list_view, self.kanban_view, self.search_view):
            view.taskActivated.connect(self.open_editor)
        self.list_view.toast.connect(lambda text: self.statusBar().showMessage(text, 3000))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.kanban_view)
        self.stack.addWidget(self.search_view)
        self.setCentralWidget(self.stack)

        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._bind_shortcuts()
        self._install_context_menus()  # after _build_menu: it reuses the handlers

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
        self._action(file_menu, "New &subtask", self.new_subtask, "new_subtask")
        self._action(file_menu, "&Sync now", self.sync_now, "sync_now")
        self._action(file_menu, "&Preview sync (dry run)…", self.preview_sync)
        file_menu.addSeparator()
        self._action(file_menu, "&Quit", self.close, shortcut="Ctrl+Q")

        edit_menu = self.menus["Edit"] = bar.addMenu("&Edit")
        self._action(edit_menu, "&Open task", self.open_selected, "open_editor")
        self._action(edit_menu, "&Move to another list…", self.move_task, "move_task")
        self._action(edit_menu, "&Delete task", self.delete_task, "delete_task")
        edit_menu.addSeparator()
        # A status submenu is what lets you drop the Done and Cancelled columns
        # from the board: the states stay reachable without a lane each.
        # Constructed with a parent rather than through addMenu(str): the
        # menu returned by addMenu is owned on the Python side, and holding it
        # only in self.menus is not enough to keep its C++ half alive.
        status_menu = self.menus["Status"] = QMenu("Change &status", edit_menu)
        edit_menu.addMenu(status_menu)
        for label, status in self.STATUS_ENTRIES:
            self._action(
                status_menu,
                label,
                lambda _checked=False, value=status: self.set_status(value),
                key=f"Status: {label}",
            )
        edit_menu.addSeparator()
        self._action(edit_menu, "Cu&t", self.cut_task, "cut_task")
        self._action(edit_menu, "&Paste", self.paste_task, "paste_task")
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

    def _action(self, menu, text, handler, keymap_action=None, shortcut=None, key=None):
        """Menu entries show the same binding the keymap already defines, so
        the two can never disagree about what a key does.

        ``key`` overrides the ``menu_handlers`` name, for entries whose label
        is only unambiguous inside their own submenu — "Completed" means one
        thing under Change status and another next to "Show completed".
        """
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
        self.menu_handlers[key or text.replace("&", "")] = handler
        menu.addAction(action)
        return action

    #: The status submenu, as ``(label, status)``.  "(no status)" is a real
    #: choice, not a blank: it is how a task goes back to the pool, and with
    #: the Done column hidden it is also the only way back out of one.
    STATUS_ENTRIES = (
        ("(no status)", None),
        ("Needs Action", Status.NEEDS_ACTION),
        ("In Progress", Status.IN_PROCESS),
        ("Completed", Status.COMPLETED),
        ("Cancelled", Status.CANCELLED),
    )

    #: Right-click entries, as ``menu_handlers`` keys — or ``(title, keys)``
    #: for a submenu, and ``None`` for a separator.  Naming them by handler key
    #: rather than by callable is what stops a context entry from quietly
    #: drifting away from its Edit-menu counterpart.
    CONTEXT_ENTRIES = (
        "Open task",
        "New task",
        "New subtask",
        None,
        ("Change status", tuple(f"Status: {label}" for label, _ in STATUS_ENTRIES)),
        None,
        "Cut",
        "Paste",
        None,
        "Indent (make a subtask)",
        "Outdent (promote to root)",
        "Move to another list…",
        None,
        "Delete task",
    )

    def _install_context_menus(self) -> None:
        for widget in (
            self.list_view.tree,
            self.search_view.results,
            *self.kanban_view.lists.values(),
        ):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda point, source=widget: self._show_context_menu(source, point)
            )

    def context_menu_for(self, task: Task | None) -> QMenu:
        """The right-click menu, without showing it — which is what makes it
        testable, and what keeps ``_show_context_menu`` down to placement."""
        menu = QMenu(self)
        for entry in self.CONTEXT_ENTRIES:
            if entry is None:
                menu.addSeparator()
            elif isinstance(entry, tuple):
                title, keys = entry
                # Parented to `menu`, or its C++ half is collected the moment
                # this function returns and every entry raises on access.
                submenu = QMenu(title, menu)
                menu.addMenu(submenu)
                submenu.setEnabled(task is not None)
                for key in keys:
                    self._context_action(submenu, key, task, text=key.split(": ", 1)[-1])
            else:
                self._context_action(menu, entry, task)
        return menu

    def _context_action(self, menu: QMenu, key: str, task: Task | None, text: str | None = None):
        action = menu.addAction(text or key)
        action.setEnabled(self._context_enabled(key, task))
        action.triggered.connect(self.menu_handlers[key])
        return action

    def _context_enabled(self, label: str, task: Task | None) -> bool:
        if label == "New task":
            return True
        if label == "Paste":
            return not self.clipboard.is_empty
        return task is not None

    def _show_context_menu(self, widget, point) -> None:
        item = widget.itemAt(point)
        # Right-clicking inside a multi-selection acts on all of it; outside
        # one, it moves the selection to what was actually clicked.
        if item is not None and not item.isSelected():
            widget.setCurrentItem(item)
        self.context_menu_for(self.selected_task()).exec(widget.viewport().mapToGlobal(point))

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
            "new_subtask": self.new_subtask,
            "cut_task": self.cut_task,
            "paste_task": self.paste_task,
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

    def selected_tasks(self) -> list[Task]:
        view = self.current_view()
        if hasattr(view, "selected_tasks"):
            return view.selected_tasks()
        task = self.selected_task()
        return [task] if task is not None else []

    # --------------------------------------------------------------- actions

    def new_task(self) -> None:
        self._create_task()

    def new_subtask(self) -> None:
        """The same dialog, with the selection already chosen as the parent."""
        selected = self.selected_task()
        if selected is None:
            return
        self._create_task(parent_uid=selected.uid, calendar_id=selected.calendar_id)

    def _create_task(self, *, parent_uid=None, calendar_id=None) -> None:
        calendars = [r for r in cache.calendar_rows(self.conn) if r["available"]]
        if not calendars:
            QMessageBox.information(
                self, "No lists yet", "Sync at least once so DavPunk knows your task lists."
            )
            return

        # Start in the list the user is looking at rather than the first one
        # that happens to exist: a new task almost always belongs beside the
        # one that prompted it.
        selected = self.selected_task()
        start_in = calendar_id or (selected.calendar_id if selected is not None else None)
        if start_in not in {row["id"] for row in calendars}:
            start_in = calendars[0]["id"]

        tasks = vm.load_tasks(self.conn)
        task = Task(uid=uuid.uuid4().hex, calendar_id=start_in, parent_uid=parent_uid)
        editor = TaskEditor(task, self, calendars=calendars, tasks=tasks, creating=True)
        if editor.exec() != TaskEditor.DialogCode.Accepted:
            return

        task.calendar_id = editor.calendar_id()
        for field, value in editor.changed_fields().items():
            if field == "categories":
                task.categories = value
            else:
                setattr(task, field, value)
        task.summary = task.summary or "New task"

        # Ordered among its new siblings, not among everything in the list —
        # an order value is only meaningful inside one sibling group.
        siblings = [
            t
            for t in vm.load_tasks(self.conn, calendar_ids=[task.calendar_id])
            if t.parent_uid == task.parent_uid
        ]
        task.davpunk_order = vm.initial_order(siblings)
        self._guarded(lambda: cache.create_task_local(task, self.conn))
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
        tasks = vm.load_tasks(self.conn)
        editor = TaskEditor(fresh, self, calendars=cache.calendar_rows(self.conn), tasks=tasks)
        if editor.exec() != TaskEditor.DialogCode.Accepted:
            return

        fields = editor.changed_fields()
        if "parent_uid" in fields:
            # Through reparent_fields, so the task also lands at the end of its
            # new siblings: the order it carries belongs to the group it left.
            chosen = fields.pop("parent_uid")
            reparent = vm.reparent_fields(fresh, str(chosen) if chosen else None, tasks)
            if reparent is None:
                QMessageBox.warning(
                    self,
                    "Cannot reparent",
                    "A task cannot become a child of itself or of one of its own subtasks.",
                )
            else:
                fields.update(reparent)
        if not fields:
            return
        self._guarded(lambda: cache.update_task_optimistic(fresh.id, fields, self.conn))
        self.refresh()

    def set_status(self, status: Status | None) -> None:
        """Set STATUS on the selection, and drop the column override with it.

        An override that outlived the status it was set beside would keep a
        card in the column it was once dragged to while claiming a status it
        no longer has, and the next drag would be the only thing to fix it.
        Choosing a status explicitly is exactly the moment to let go of it.
        """
        tasks = self.selected_tasks()
        if not tasks:
            return
        self._guarded(lambda: self._set_status_all(tasks, status))
        self.refresh()

    def _set_status_all(self, tasks: list[Task], status: Status | None) -> None:
        for task in tasks:
            cache.update_task_optimistic(task.id, {"status": status, "kanban_col": None}, self.conn)

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
        tasks = self.selected_tasks()
        if not tasks:
            return

        box = QMessageBox(self)
        box.setWindowTitle("Delete task")
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setText(f"Delete {_name_list(tasks)}?")

        subtree_box = None
        if any(cache.children_of(t.id, self.conn) for t in tasks):
            box.setInformativeText(
                "Subtasks are not deleted with their parent by default; they become root tasks."
            )
            subtree_box = QCheckBox("Delete subtasks as well")
            box.setCheckBox(subtree_box)

        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        subtree = subtree_box is not None and subtree_box.isChecked()
        self._guarded(lambda: self._delete_all(tasks, subtree=subtree))
        self.refresh()

    def _delete_all(self, tasks: list[Task], *, subtree: bool) -> None:
        """Deepest-first when the whole subtree goes.

        Taking the parent out first promotes its children to root and queues a
        ``RELATED-TO`` removal for every one of them — a round trip to the
        server for a link that is about to be deleted anyway.
        """
        seen: set[str] = set()
        for task in tasks:
            below = list(reversed(cache.descendants_of(task.id, self.conn))) if subtree else []
            for task_id in [*below, task.id]:
                if task_id in seen:
                    continue
                seen.add(task_id)
                cache.delete_task_local(task_id, self.conn)

    # ---------------------------------------------------------- cut and paste

    def cut_task(self) -> None:
        tasks = self.selected_tasks()
        if not tasks:
            return
        self.clipboard.cut(tasks)
        self.statusBar().showMessage(
            f"Cut {_name_list(tasks)} — select a parent and paste, or paste with "
            "nothing selected to make it a top-level task",
            8000,
        )

    def paste_task(self) -> None:
        """Reparent the cut tasks under the selection.

        This is how a task reaches a parent the filter is hiding: both sides
        are re-read from the cache rather than taken from the widgets, so the
        two never have to have been on screen together.  That is the whole
        reason to cut instead of drag.
        """
        if self.clipboard.is_empty:
            self.statusBar().showMessage("Nothing has been cut", 3000)
            return

        cut = [t for t in (cache.get_task(i, self.conn) for i in self.clipboard.task_ids) if t]
        if len(cut) != len(self.clipboard.entries):
            self.clipboard.clear()
            QMessageBox.information(
                self,
                "Nothing to paste",
                "What was cut no longer exists. The clipboard has been cleared.",
            )
            return

        selected = self.selected_task()
        target = cache.get_task(selected.id, self.conn) if selected is not None else None
        plans = vm.plan_paste(cut, target, vm.load_tasks(self.conn))
        if not plans:
            if target is None:
                self.statusBar().showMessage("Already at the top level", 3000)
            else:
                QMessageBox.warning(
                    self,
                    "Cannot paste",
                    "A task cannot become a child of itself or of one of its own subtasks.",
                )
            return

        move_subtree = True
        movers = [plan for plan in plans if plan.needs_move]
        if movers:
            dialog = MoveDialog(
                cache.calendar_rows(self.conn),
                movers[0].task.calendar_id,
                any(cache.children_of(plan.task.id, self.conn) for plan in movers),
                self,
                fixed_target=movers[0].calendar_id,
                heading=(
                    f"Pasting here also moves {_name_list([p.task for p in movers])} "
                    "to another list:"
                ),
            )
            if dialog.exec() != MoveDialog.DialogCode.Accepted:
                return
            move_subtree = dialog.wants_subtree()

        if self._guarded(lambda: self._apply_paste(plans, move_subtree=move_subtree)):
            # A cut is a move, not a copy: pasting the same task twice would
            # only ever undo the first paste.
            self.clipboard.clear()
        self.refresh()

    def _apply_paste(self, plans, *, move_subtree: bool) -> None:
        for plan in plans:
            if plan.needs_move:
                # The move retargets the calendar and the reparent then places
                # the task under the target.  A failure between the two leaves
                # it at the root of its new list — visible, rather than lost.
                cache.move_task_local(
                    plan.task.id, plan.calendar_id, self.conn, move_subtree=move_subtree
                )
            cache.update_task_optimistic(plan.task.id, plan.fields, self.conn)

    def move_task(self) -> None:
        tasks = self.selected_tasks()
        if not tasks:
            return

        sources = {task.calendar_id for task in tasks}
        if len(sources) > 1:
            # A move is out of one list and into another, and the dialog's
            # whole job is to exclude the list you are leaving.
            QMessageBox.information(
                self,
                "One list at a time",
                "These tasks are in different lists. Select tasks from a single "
                "list, and DavPunk will offer everywhere else as the destination.",
            )
            return

        has_children = any(cache.children_of(task.id, self.conn) for task in tasks)
        dialog = MoveDialog(
            cache.calendar_rows(self.conn),
            tasks[0].calendar_id,
            has_children,
            self,
            heading=f"Move {_name_list(tasks)} to:" if len(tasks) > 1 else None,
        )
        if dialog.exec() != MoveDialog.DialogCode.Accepted:
            return
        target = dialog.target_calendar_id()
        if not target:
            return

        subtree = dialog.wants_subtree()
        self._guarded(lambda: self._move_all(tasks, target, subtree=subtree))
        self.refresh()

    def _move_all(self, tasks: list[Task], target: str, *, subtree: bool) -> None:
        """One move per task, skipping any an ancestor already carries.

        Moving a task that its own parent's subtree move has already relocated
        would be a second, pointless retarget — and with the subtree declined
        it is exactly the task that was just promoted to root.
        """
        chosen = vm.topmost(tasks, vm.load_tasks(self.conn)) if subtree else tasks
        for task in chosen:
            cache.move_task_local(task.id, target, self.conn, move_subtree=subtree)

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

    def preview_sync(self) -> None:
        """A read-only rehearsal, with the option to go ahead afterwards."""
        from davpunk.ui.dry_run_dialog import DryRunDialog

        dialog = DryRunDialog(self.sync, self)
        dialog.exec()
        if dialog.proceed:
            self.sync_now()

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

    def _guarded(self, action) -> bool:
        """Turn the cache layer's refusals into something a user can read.

        Returns whether the write actually happened, so a caller can hold onto
        state — the clipboard, say — that a failed action must not discard.
        """
        try:
            action()
            return True
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
        return False

    def closeEvent(self, event) -> None:
        """Cancel, wait up to 5 s, then detach — the flock releases at process
        exit regardless."""
        self._poll.stop()
        self.sync.stop()
        cache.close_db(self.conn)
        super().closeEvent(event)
