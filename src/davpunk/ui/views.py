"""List, kanban and search views.

Each is a thin shell over :mod:`davpunk.ui.viewmodel`: grouping, column
assignment, tree building and reordering are all decided there, so these
classes only turn the result into widgets and turn widget events back into
``cache.py`` helper calls.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from davpunk.core import cache
from davpunk.models.task import Status, Task
from davpunk.ui import viewmodel as vm

log = logging.getLogger("davpunk.ui.views")

TASK_ROLE = Qt.ItemDataRole.UserRole


class ListView(QWidget):
    """Grouped tree with a midnight re-bucket timer."""

    taskActivated = Signal(object)
    editRequested = Signal(object)

    def __init__(self, conn, config, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.config = config
        self.show_completed = config.show_completed  # runtime toggle, not persisted
        self._last_reorder = 0.0
        self._pending_reorder = 0

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Task", "Due", "Priority", "Tags"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemActivated.connect(self._activated)
        layout.addWidget(self.tree)

        # Re-bucket at the next local midnight, or an app left open overnight
        # keeps showing yesterday's groups.
        self._midnight = QTimer(self)
        self._midnight.setSingleShot(True)
        self._midnight.timeout.connect(self._on_midnight)
        self._arm_midnight()

    def _arm_midnight(self) -> None:
        self._midnight.start(int(vm.seconds_to_midnight() * 1000))

    def _on_midnight(self) -> None:
        log.debug("Midnight: re-bucketing the list view")
        self.refresh()
        self._arm_midnight()

    def refresh(self) -> None:
        selected = self.selected_task()
        self.tree.clear()

        tasks = vm.load_tasks(self.conn)
        grouped = vm.group_tasks(tasks, show_completed=self.show_completed)

        for bucket in vm.BUCKET_ORDER:
            items = grouped.get(bucket)
            if not items:
                continue
            header = QTreeWidgetItem([f"{bucket.value}  ({len(items)})"])
            header.setFirstColumnSpanned(True)
            self.tree.addTopLevelItem(header)
            header.setExpanded(True)
            for node in vm.build_tree(items):
                header.addChild(self._node_item(node))

        if selected is not None:
            self.select_uid(selected.uid)

    def _node_item(self, node: vm.TreeNode) -> QTreeWidgetItem:
        task = node.task
        label = task.summary or "(no summary)"
        if node.linked_parent_elsewhere:
            label += "  ↗ linked parent elsewhere"
        if task.is_read_only:
            label += f"  🔒 {task.read_only_reason.value}"
        if task.sync_state.value == "conflict":
            label += "  ⚠ conflict"

        item = QTreeWidgetItem(
            [
                label,
                vm.relative_due(task),
                task.priority_band or "",
                ", ".join(task.categories),
            ]
        )
        item.setData(0, TASK_ROLE, task)
        item.setCheckState(
            0,
            Qt.CheckState.Checked if task.status is Status.COMPLETED else Qt.CheckState.Unchecked,
        )
        for child in node.children:
            item.addChild(self._node_item(child))
        item.setExpanded(True)
        return item

    # ------------------------------------------------------------- selection

    def selected_task(self) -> Task | None:
        item = self.tree.currentItem()
        return item.data(0, TASK_ROLE) if item is not None else None

    def select_uid(self, uid: str) -> bool:
        for index in range(self.tree.topLevelItemCount()):
            if self._select_in(self.tree.topLevelItem(index), uid):
                return True
        return False

    def _select_in(self, item: QTreeWidgetItem, uid: str) -> bool:
        task = item.data(0, TASK_ROLE)
        if task is not None and task.uid == uid:
            self.tree.setCurrentItem(item)
            return True
        return any(self._select_in(item.child(i), uid) for i in range(item.childCount()))

    def _activated(self, item: QTreeWidgetItem, _column: int) -> None:
        task = item.data(0, TASK_ROLE)
        if task is not None:
            self.taskActivated.emit(task)

    # --------------------------------------------------------------- actions

    def toggle_show_completed(self) -> None:
        self.show_completed = not self.show_completed
        self.refresh()

    def toggle_complete(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        new = Status.NEEDS_ACTION if task.status is Status.COMPLETED else Status.COMPLETED
        cache.update_task_optimistic(task.id, {"status": new}, self.conn)
        self.refresh()

    def reorder(self, task: Task, new_index: int) -> str | None:
        """Apply a drag, coalescing rapid ones into a single rebalance."""
        siblings = [
            t
            for t in vm.load_tasks(self.conn, calendar_ids=[task.calendar_id])
            if t.parent_uid == task.parent_uid
        ]
        result = vm.reorder_siblings(siblings, task.uid, new_index)

        by_uid = {t.uid: t for t in siblings}
        for uid, order in result.assignments.items():
            target = by_uid.get(uid)
            if target is not None:
                cache.update_task_optimistic(target.id, {"davpunk_order": order}, self.conn)

        self.refresh()
        now = time.monotonic()  # in-process timing is monotonic
        if result.rebalanced:
            self._pending_reorder += result.touched
            if now - self._last_reorder > vm.REORDER_COALESCE_S:
                self._last_reorder = now
                count = self._pending_reorder
                self._pending_reorder = 0
                return f"reordering {count} tasks"
        return None


class KanbanView(QWidget):
    """One list per configured column; a drag sets STATUS **and** the override."""

    taskActivated = Signal(object)

    def __init__(self, conn, config, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.config = config
        self.columns = config.kanban.columns

        layout = QHBoxLayout(self)
        self.lists: dict[str, QListWidget] = {}
        for column in self.columns:
            box = QVBoxLayout()
            box.addWidget(QLabel(f"<b>{column.label}</b>"))
            widget = QListWidget()
            widget.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
            widget.setDefaultDropAction(Qt.DropAction.MoveAction)
            widget.itemActivated.connect(self._activated)
            box.addWidget(widget)
            self.lists[column.id] = widget
            layout.addLayout(box)

    def refresh(self) -> None:
        board = vm.kanban_board(vm.load_tasks(self.conn), self.columns)
        for column_id, widget in self.lists.items():
            widget.clear()
            for task in board[column_id]:
                item = QListWidgetItem(task.summary or "(no summary)")
                item.setData(TASK_ROLE, task)
                widget.addItem(item)

    def _activated(self, item: QListWidgetItem) -> None:
        task = item.data(TASK_ROLE)
        if task is not None:
            self.taskActivated.emit(task)

    def selected_task(self) -> Task | None:
        for widget in self.lists.values():
            item = widget.currentItem()
            if item is not None and widget.hasFocus():
                return item.data(TASK_ROLE)
        return None

    def move_to_column(self, task: Task, column_id: str) -> None:
        """Sets both STATUS and X-DAVPUNK-KANBAN-COL in one update."""
        column = next((c for c in self.columns if c.id == column_id), None)
        if column is None:
            return
        cache.update_task_optimistic(task.id, vm.drop_fields(column), self.conn)
        self.refresh()

    def shift_selected(self, delta: int) -> None:
        task = self.selected_task()
        if task is None:
            return
        current = vm.column_of(task, self.columns)
        index = self.columns.index(current) + delta
        if 0 <= index < len(self.columns):
            self.move_to_column(task, self.columns[index].id)


class SearchView(QWidget):
    """FTS5-backed search, completed tasks included."""

    taskActivated = Signal(object)

    def __init__(self, conn, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn

        layout = QVBoxLayout(self)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search summaries and descriptions…")
        self.query.textChanged.connect(self._search)
        layout.addWidget(self.query)

        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Task", "Status", "Due", "List"])
        self.results.itemActivated.connect(self._activated)
        layout.addWidget(self.results)

        self.status = QLabel("")
        layout.addWidget(self.status)

    def refresh(self) -> None:
        self._search(self.query.text())

    def _search(self, text: str) -> None:
        self.results.clear()
        text = text.strip()
        if not text:
            self.status.setText("")
            return

        try:
            rows = cache.search_tasks(text, self.conn, limit=200)
        except Exception as exc:
            # A user typing into a search box will produce invalid FTS5 syntax
            # constantly; that is not an error worth a dialog.
            self.status.setText(f"({exc})")
            return

        for row in rows:
            task = cache.row_to_task(row)
            item = QTreeWidgetItem(
                [
                    task.summary or "(no summary)",
                    task.status.value if task.status else "",
                    vm.relative_due(task),
                    task.calendar_id or "",
                ]
            )
            item.setData(0, TASK_ROLE, task)
            self.results.addTopLevelItem(item)
        self.status.setText(f"{len(rows)} result(s)")

    def _activated(self, item: QTreeWidgetItem, _column: int) -> None:
        task = item.data(0, TASK_ROLE)
        if task is not None:
            self.taskActivated.emit(task)

    def selected_task(self) -> Task | None:
        item = self.results.currentItem()
        return item.data(0, TASK_ROLE) if item is not None else None
