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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from davpunk.core import cache
from davpunk.models.task import Status, Task
from davpunk.ui import viewmodel as vm
from davpunk.ui.filter_bar import FilterBar

log = logging.getLogger("davpunk.ui.views")

TASK_ROLE = Qt.ItemDataRole.UserRole
#: The identity a fold is remembered under.  Separate from TASK_ROLE because
#: bucket headings fold too and have no task behind them.
FOLD_ROLE = Qt.ItemDataRole.UserRole + 1


def fold_key(task: Task) -> tuple:
    """``uid`` is only unique within a calendar, so a fold must be too."""
    return (task.calendar_id, task.uid)


def task_item(node: vm.TreeNode) -> QTreeWidgetItem:
    """The first column of a task row: markers, and what a fold would hide."""
    task = node.task
    label = task.summary or "(no summary)"
    if node.children:
        # Without this a folded parent looks like a leaf, and the only way to
        # find out it is not is to open every one.
        label += f"  ({vm.subtree_size(node)})"
    if node.linked_parent_elsewhere:
        label += "  ↗ linked parent elsewhere"
    if task.is_read_only:
        label += f"  🔒 {task.read_only_reason.value}"
    if task.sync_state.value == "conflict":
        label += "  ⚠ conflict"

    item = QTreeWidgetItem([label])
    item.setData(0, TASK_ROLE, task)
    item.setData(0, FOLD_ROLE, fold_key(task))
    return item


def _walk(tree: QTreeWidget):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        yield item
        stack.extend(item.child(i) for i in range(item.childCount()))


def apply_folds(tree: QTreeWidget, folds: vm.FoldState) -> None:
    """Restore the remembered folds, *after* the rows are in the widget.

    ``setExpanded`` on a ``QTreeWidgetItem`` that has not been inserted yet is
    silently dropped, so this cannot be folded into the row building — it has
    to be a second pass over the finished tree.

    A heading is open unless it was closed; a subtree is closed unless it was
    opened.  One parent here carries 262 children, and expanding by default
    buries the rest of the column under it.
    """
    for item in _walk(tree):
        if not item.childCount():
            continue
        key = item.data(0, FOLD_ROLE)
        if key is not None:
            item.setExpanded(folds.is_open(key, default=item.data(0, TASK_ROLE) is None))


def all_fold_keys(tree: QTreeWidget) -> list:
    """Every foldable row currently in ``tree``, headings included."""
    keys = []
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.childCount():
            key = item.data(0, FOLD_ROLE)
            if key is not None:
                keys.append(key)
            stack.extend(item.child(i) for i in range(item.childCount()))
    return keys


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

        # Which subtrees are open has to outlive a refresh, or the two-second
        # poll re-opens everything and folding is decorative.
        self.folds = vm.FoldState()
        self._building = False

        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Task", "Due", "Priority", "Tags"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemActivated.connect(self._activated)
        self.tree.itemExpanded.connect(lambda item: self._remember(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._remember(item, False))
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
        self._building = True
        try:
            self.tree.clear()

            tasks = vm.load_tasks(self.conn)
            grouped = vm.group_tasks(tasks, show_completed=self.show_completed)

            for bucket in vm.BUCKET_ORDER:
                items = grouped.get(bucket)
                if not items:
                    continue
                header = QTreeWidgetItem([f"{bucket.value}  ({len(items)})"])
                header.setFirstColumnSpanned(True)
                key = ("bucket", bucket.value)
                header.setData(0, FOLD_ROLE, key)
                self.tree.addTopLevelItem(header)
                # The bucket is a slice of the whole list, so the tree builder
                # needs `tasks` to tell "parent is in another bucket" from
                # "parent is in another calendar".
                for node in vm.build_tree(items, tasks):
                    header.addChild(self._node_item(node))
            apply_folds(self.tree, self.folds)
        finally:
            self._building = False

        if selected is not None:
            self.select_uid(selected.uid)

    def _node_item(self, node: vm.TreeNode) -> QTreeWidgetItem:
        item = task_item(node)
        item.setText(1, vm.relative_due(node.task))
        item.setText(2, node.task.priority_band or "")
        item.setText(3, ", ".join(node.task.categories))
        item.setCheckState(
            0,
            Qt.CheckState.Checked
            if node.task.status is Status.COMPLETED
            else Qt.CheckState.Unchecked,
        )
        for child in node.children:
            item.addChild(self._node_item(child))
        return item

    def _remember(self, item: QTreeWidgetItem, is_open: bool) -> None:
        if self._building:
            return
        key = item.data(0, FOLD_ROLE)
        if key is not None:
            self.folds.remember(key, is_open)

    def set_all_folded(self, is_open: bool) -> None:
        self.folds.set_all(all_fold_keys(self.tree), is_open)
        self.refresh()

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


class _ColumnTree(QTreeWidget):
    """One kanban column, as a tree.

    Drops are interpreted here rather than by Qt: the board is a projection of
    the database, so letting Qt physically move rows would show a state the
    next refresh contradicts.  The event is consumed, the model is told, and
    the redraw comes from the data.
    """

    #: ``(dragged task, task dropped onto or None)``
    dropped = Signal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def dropEvent(self, event) -> None:
        source = event.source()
        dragged = source.currentItem() if isinstance(source, QTreeWidget) else None
        task = dragged.data(0, TASK_ROLE) if dragged is not None else None
        if task is None:
            event.ignore()
            return

        onto = None
        if self.dropIndicatorPosition() == QAbstractItemView.DropIndicatorPosition.OnItem:
            target = self.itemAt(event.position().toPoint())
            onto = target.data(0, TASK_ROLE) if target is not None else None

        event.acceptProposedAction()
        self.dropped.emit(task, onto)


class KanbanView(QWidget):
    """One tree per configured column; a drag sets STATUS **and** the override."""

    taskActivated = Signal(object)

    def __init__(self, conn, config, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.config = config
        self.columns = config.kanban.columns
        self.filter = vm.TaskFilter()

        outer = QVBoxLayout(self)
        self.filter_bar = FilterBar()
        self.filter_bar.filterChanged.connect(self._on_filter_changed)
        outer.addWidget(self.filter_bar)

        board = QWidget()
        layout = QHBoxLayout(board)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(board, 1)
        self.folds = vm.FoldState()
        self._building = False
        self.lists: dict[str, _ColumnTree] = {}
        self.headers: dict[str, QLabel] = {}
        for column in self.columns:
            box = QVBoxLayout()
            header = QLabel(f"<b>{column.label}</b>")
            self.headers[column.id] = header
            box.addWidget(header)
            widget = _ColumnTree()
            widget.itemActivated.connect(self._activated)
            widget.itemExpanded.connect(lambda item: self._remember(item, True))
            widget.itemCollapsed.connect(lambda item: self._remember(item, False))
            widget.dropped.connect(
                lambda task, onto, column_id=column.id: self._on_drop(column_id, task, onto)
            )
            box.addWidget(widget)
            self.lists[column.id] = widget
            layout.addLayout(box)

    def refresh(self) -> None:
        tasks = vm.load_tasks(self.conn)
        # Offer only what the data actually contains, so the picker can never
        # list a tag or a calendar that would match nothing.
        self.filter_bar.set_calendars(self._calendar_names())
        self.filter_bar.set_tags(vm.available_tags(tasks))

        board = vm.kanban_board(tasks, self.columns, self.filter)
        selected = self.selected_task()
        shown = 0
        self._building = True
        try:
            for column_id, widget in self.lists.items():
                widget.clear()
                # A column is a slice, so the tree builder gets the whole set:
                # a card whose parent sits in another column is a root here,
                # not a task whose parent is missing.
                for node in vm.build_tree(board[column_id], tasks):
                    widget.addTopLevelItem(self._node_item(node))
                apply_folds(widget, self.folds)
                shown += len(board[column_id])
        finally:
            self._building = False
        if selected is not None:
            self.select_uid(selected)
        self._update_counts(board, shown, len(tasks))

    def _node_item(self, node: vm.TreeNode) -> QTreeWidgetItem:
        item = task_item(node)
        for child in node.children:
            item.addChild(self._node_item(child))
        return item

    def _remember(self, item: QTreeWidgetItem, is_open: bool) -> None:
        if self._building:
            return
        key = item.data(0, FOLD_ROLE)
        if key is not None:
            self.folds.remember(key, is_open)

    def set_all_folded(self, is_open: bool) -> None:
        for widget in self.lists.values():
            self.folds.set_all(all_fold_keys(widget), is_open)
        self.refresh()

    def select_uid(self, task: Task) -> bool:
        for widget in self.lists.values():
            for item in _walk(widget):
                found = item.data(0, TASK_ROLE)
                if found is not None and fold_key(found) == fold_key(task):
                    widget.setCurrentItem(item)
                    return True
        return False

    def _on_drop(self, column_id: str, task: Task, onto: Task | None) -> None:
        """Onto a card: become its subtask, in its column.  Onto the column: just move.

        Both in a single update, so an interrupted drag cannot leave a task
        nested somewhere it is not shown.
        """
        column = next((c for c in self.columns if c.id == column_id), None)
        if column is None or task.is_read_only:
            return
        fields = dict(vm.drop_fields(column))

        if onto is not None:
            if onto.calendar_id != task.calendar_id:
                # RELATED-TO resolves within one calendar only, so this would
                # be a link that never renders.
                log.info("Refusing to nest %s under a task in another calendar", task.uid[:8])
                return
            reparent = vm.reparent_fields(task, onto.uid, vm.load_tasks(self.conn))
            if reparent is None:
                return
            fields.update(reparent)

        cache.update_task_optimistic(task.id, fields, self.conn)
        self.refresh()

    def _calendar_names(self) -> dict[str, str]:
        return {
            row["id"]: row["display_name"] or row["href"]
            for row in cache.calendar_rows(self.conn)
            if row["available"]
        }

    def _update_counts(self, board: dict, shown: int, total: int) -> None:
        for column in self.columns:
            self.headers[column.id].setText(f"<b>{column.label}</b>  ({len(board[column.id])})")
        if self.filter.is_active:
            self.filter_bar.summary.setText(
                f"{self.filter.describe(self._calendar_names())}  —  {shown} of {total}"
            )

    def _on_filter_changed(self, task_filter) -> None:
        self.filter = task_filter
        self.refresh()

    def _activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        task = item.data(0, TASK_ROLE)
        if task is not None:
            self.taskActivated.emit(task)

    def selected_task(self) -> Task | None:
        for widget in self.lists.values():
            item = widget.currentItem()
            if item is not None and widget.hasFocus():
                return item.data(0, TASK_ROLE)
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
