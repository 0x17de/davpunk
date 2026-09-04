"""List, kanban and search views.

Each is a thin shell over :mod:`davpunk.ui.viewmodel`: grouping, column
assignment, tree building and reordering are all decided there, so these
classes only turn the result into widgets and turn widget events back into
``cache.py`` helper calls.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from davpunk.config import KanbanColumn
from davpunk.core import cache
from davpunk.models.task import Status, Task, stored
from davpunk.ui import ui_state
from davpunk.ui import viewmodel as vm
from davpunk.ui.filter_bar import FilterBar

log = logging.getLogger("davpunk.ui.views")

#: Where the board's filter is remembered between runs.
FILTER_STATE_KEY = "kanban_filter"

#: The window's cross-list move prompt, as the views see it: the tasks that
#: came from another list and the list they were dropped in, in; the tasks it
#: actually moved, out.  See :func:`carry_into`.
MoveHook = Callable[[list[Task], str], list[Task]]

TASK_ROLE = Qt.ItemDataRole.UserRole
#: The identity a fold is remembered under.  Separate from TASK_ROLE because
#: bucket headings fold too and have no task behind them.
FOLD_ROLE = Qt.ItemDataRole.UserRole + 1
#: The task a grey context row stands in for.  Deliberately not TASK_ROLE:
#: that role means "a row of this slice's own", and drags, ticks and fold
#: defaults all key off it.  What merely *names* a task reads this one as
#: well — menu actions, and the row a drop landed on — see :func:`task_of`.
CONTEXT_ROLE = Qt.ItemDataRole.UserRole + 2

#: Qt's drop indicator, in the viewmodel's terms.  ``OnViewport`` is absent on
#: purpose: a drop into empty space has no target, and falls through to ON with
#: nothing under it, which every handler already refuses.
DROP_POSITIONS = {
    QAbstractItemView.DropIndicatorPosition.OnItem: vm.DropPosition.ON,
    QAbstractItemView.DropIndicatorPosition.AboveItem: vm.DropPosition.ABOVE,
    QAbstractItemView.DropIndicatorPosition.BelowItem: vm.DropPosition.BELOW,
}


def fold_key(task: Task) -> tuple:
    """``uid`` is only unique within a calendar, so a fold must be too."""
    return (task.calendar_id, task.uid)


def context_item(task: Task) -> QTreeWidgetItem:
    """A relative this view is not showing here, drawn as grey scaffolding.

    It carries no ``TASK_ROLE``: the gestures that mean "this row, here" —
    dragging it, ticking it — would be claims about a slice the task is not
    in, and every handler in this module refuses a row with no task already,
    the same way it refuses a bucket heading.

    The menu is the other kind of gesture.  "New subtask", "Change status",
    "Open" name the *task*, not the row, and there is exactly one task they
    can mean; refusing them here would only mean walking to another column to
    do the obvious thing.  So the row is selectable and carries its task under
    ``CONTEXT_ROLE``, and every action reads it through :func:`task_of`.

    A drop is that same kind of gesture.  "Under this one" names the task it
    landed on, and the grey row is often the only place the two are on screen
    together — a parent in In Progress and the card you want under it are in
    different columns by definition, and that grey stand-in is what the whole
    idea of context rows put in reach.  Whatever the drop says about *this*
    slice — the column's status — it says by where it landed, which is a
    question about the row and not about the task standing in it.
    """
    item = QTreeWidgetItem([task.summary or "(no summary)"])
    # Its own fold key: closing the real row in the column it lives in has
    # nothing to do with closing the grey stand-in over here.
    item.setData(0, FOLD_ROLE, ("context", task.calendar_id, task.uid))
    item.setData(0, CONTEXT_ROLE, task)
    # Drop-enabled, and not only for drops onto the row itself: Qt asks the
    # *parent* row whether a drop between two of its children is allowed, so
    # without this the cards under a grey ancestor could not be reordered.
    item.setFlags(
        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled
    )
    item.setToolTip(
        0,
        "Shown for context — this task itself is not among these rows.\n"
        "The right-click menu still acts on it.",
    )

    grey = QApplication.palette().brush(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    item.setForeground(0, QBrush(grey))
    font = item.font(0)
    font.setItalic(True)
    item.setFont(0, font)
    return item


def task_item(node: vm.TreeNode) -> QTreeWidgetItem:
    """The first column of a task row: markers, and what a fold would hide."""
    task = node.task
    if node.is_context:
        return context_item(task)

    label = task.summary or "(no summary)"
    if node.children:
        # Without this a folded parent looks like a leaf, and the only way to
        # find out it is not is to open every one.
        label += f"  ({vm.subtree_size(node)})"
    if node.linked_parent_elsewhere:
        label += "  ↗ linked parent elsewhere"
    if task.read_only_reason is not None:
        label += f"  🔒 {task.read_only_reason.value}"
    if task.sync_state.value == "conflict":
        label += "  ⚠ conflict"

    item = QTreeWidgetItem([label])
    item.setData(0, TASK_ROLE, task)
    item.setData(0, FOLD_ROLE, fold_key(task))
    return item


def task_of(item) -> Task | None:
    """The task a row *acts on*: its own, or the one it stands in for.

    This is what every menu-driven action asks, and what a drop asks about the
    row it landed on — the two gestures that name a task rather than claim a
    row.  Everything that means "this row, in this slice" — dragging it,
    ticking it, counting it — keeps reading ``TASK_ROLE`` directly and keeps
    refusing a grey row.
    """
    if item is None:
        return None
    task = item.data(0, TASK_ROLE)
    return task if task is not None else item.data(0, CONTEXT_ROLE)


def _tasks_of(items) -> list[Task]:
    """The tasks a drag carries: this slice's own rows, headings dropped."""
    found = (item.data(0, TASK_ROLE) for item in items)
    return [task for task in found if task is not None]


def _acting_tasks(items) -> list[Task]:
    """The tasks a menu acts on, grey stand-ins included.

    A task cannot be both a row of its own and a grey stand-in inside one
    tree, so this cannot hand the same task to an action twice.
    """
    found = (task_of(item) for item in items)
    return [task for task in found if task is not None]


def dragged_tasks(source) -> list[Task]:
    """What a drag out of ``source`` is carrying.

    The whole selection, so a drag is as fast as the multi-select delete and
    cut beside it.  Grabbing a row *outside* the selection carries only that
    row — Qt normally reselects on press, but a drag that silently took rows
    the user did not grab would be the worst possible surprise.
    """
    if not isinstance(source, QTreeWidget):
        return []
    grabbed = source.currentItem()
    task = grabbed.data(0, TASK_ROLE) if grabbed is not None else None
    if task is None:
        return []

    selected = _tasks_of(source.selectedItems())
    key = (task.calendar_id, task.uid)
    if key not in {(t.calendar_id, t.uid) for t in selected}:
        return [task]
    return sorted(selected, key=vm.sort_key)


def _write_orders(orders: dict[str, int], candidates: list[Task], conn) -> None:
    by_uid = {t.uid: t for t in candidates}
    for uid, order in orders.items():
        target = by_uid.get(uid)
        if target is not None:
            cache.update_task_optimistic(stored(target.id), {"davpunk_order": order}, conn)


def carry_into(
    conn, dragged: list[Task], calendar_id: str, move_into: MoveHook | None
) -> list[Task]:
    """``dragged``, with whatever came from another list carried into ``calendar_id``.

    A drop names a place, and in both views that place is as likely to be in
    another list as not: the board interleaves the lists by status and the list
    view by due date, so the row you are aiming at is rarely one you picked the
    calendar of.  Refusing such a drag outright was the old behaviour, and it
    made the obvious gesture do nothing at all.

    Crossing lists is a real move, though — a two-stage PUT/DELETE with the
    subtree question attached — so it is not something a drag may do silently.
    ``move_into`` is the window's prompt: it takes the tasks from elsewhere and
    the list they landed in, asks, moves, and returns what it actually moved.
    A view nobody wired one up to crosses nothing, which is the old refusal.

    Every task from elsewhere is re-read once a move happened, not only the
    ones the prompt named: a subtree move carries children that were never in
    the drag.  A task left behind comes back in its own list and is refused
    downstream exactly the way it always was.
    """
    strangers = [task for task in dragged if task.calendar_id != calendar_id]
    if not strangers or move_into is None:
        return dragged
    if not move_into(strangers, calendar_id):
        return dragged

    fresh = {task.id: task for task in vm.load_tasks(conn)}
    return [fresh.get(task.id, task) for task in dragged]


def apply_sibling_drop(
    conn,
    dragged: list[Task],
    onto: Task,
    position,
    extra: dict[str, object] | None = None,
    move_into: MoveHook | None = None,
) -> tuple[int, bool]:
    """Place ``dragged`` beside ``onto``, at ``onto``'s level.

    Shared by both views, because a drop *between* two rows means the same
    thing in each: become a sibling of what you landed beside.  On the board
    that is also the gesture that takes a subtask out of its parent — dropping
    it on a column can only ever be a status change, so without this there is
    no drag that unnests a card at all.

    A task from another list is carried into ``onto``'s first — see
    :func:`carry_into` — so landing beside a row in another calendar is a move
    and then an ordinary reorder.  Declining the move leaves it where it was,
    and the reparent is refused the way it always was.

    ``extra`` is written with the parent and the order in a single update, so
    an interrupted drop cannot leave a card nested where its column says it is
    not.  It is written even for a task the reparent refuses — a move
    declined, or a drop inside the task's own subtree — because the column move
    that came with it is still a thing the user asked for, and silently doing
    nothing is the bug this whole function exists to fix.

    Several dragged tasks land in the order they were in, each just below the
    one before it.  A refused task does not become the anchor: the next one
    still aims at the row that was actually dropped on.

    Returns ``(tasks touched, whether a gap had to be rebalanced)``.
    """
    carried = carry_into(
        conn, [t for t in dragged if not t.is_read_only], stored(onto.calendar_id), move_into
    )
    # After the move, or `topmost` reads the calendar a parent has just left
    # and stops recognising its own children.
    tasks = vm.load_tasks(conn)
    movable = vm.topmost(carried, tasks)

    anchor, where, touched, rebalanced = onto, position, 0, False
    for task in movable:
        key = (task.calendar_id, task.uid)
        # The refusals that mean "not here, ever": a list the task did not
        # move into, or a drop that would put the anchor inside its own subtree.
        refused = task.calendar_id != anchor.calendar_id or (
            anchor.uid != task.uid and anchor.uid in vm.descendants(task, tasks)
        )
        siblings = [t for t in tasks if t.calendar_id == task.calendar_id]
        moved = next((t for t in siblings if t.uid == task.uid), None)
        plan = None if refused or moved is None else vm.plan_list_drop(task, anchor, where, tasks)

        fields = dict(extra or {})
        if plan is not None:
            fields["parent_uid"] = plan.parent_uid
            fields["davpunk_order"] = plan.orders[task.uid]
        if fields and moved is not None:
            cache.update_task_optimistic(stored(moved.id), fields, conn)
            if plan is not None:
                _write_orders(
                    {uid: order for uid, order in plan.orders.items() if uid != task.uid},
                    siblings,
                    conn,
                )
                touched += plan.touched
                rebalanced = rebalanced or plan.rebalanced
            tasks = vm.load_tasks(conn)

        if refused:
            continue
        # Moved or already exactly there, the next one goes below it — that is
        # what keeps a dragged block in the order it was picked up in.
        anchor = next((t for t in tasks if (t.calendar_id, t.uid) == key), anchor)
        where = vm.DropPosition.BELOW
    return touched, rebalanced


def _top_level(tree: QTreeWidget) -> list[QTreeWidgetItem]:
    """The tree's top-level rows.

    ``topLevelItem`` and ``child`` are declared Optional because an
    out-of-range index returns null; every index here comes from the matching
    count, so the ``None`` filter only quiets that — it never hides a row.
    """
    return [
        item for i in range(tree.topLevelItemCount()) if (item := tree.topLevelItem(i)) is not None
    ]


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    """One row's children.  See :func:`_top_level`."""
    return [child for i in range(item.childCount()) if (child := item.child(i)) is not None]


def _walk(tree: QTreeWidget):
    stack = _top_level(tree)
    while stack:
        item = stack.pop()
        yield item
        stack.extend(_children(item))


def current_row_key(tree: QTreeWidget):
    """Which *row* is current, not which task.

    A refresh rebuilds every row, so the selection has to be found again
    afterwards — and a task can be on screen twice, as its own card and as a
    grey stand-in under a parent in another column.  Restoring by task would
    quietly move the selection to the other one of the two, on a timer.
    ``FOLD_ROLE`` already distinguishes them, so it is the identity used here.
    """
    item = tree.currentItem()
    return item.data(0, FOLD_ROLE) if item is not None else None


def select_row_key(tree: QTreeWidget, key) -> bool:
    for item in _walk(tree):
        if item.data(0, FOLD_ROLE) == key:
            tree.setCurrentItem(item)
            return True
    return False


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
    stack = _top_level(tree)
    while stack:
        item = stack.pop()
        if item.childCount():
            key = item.data(0, FOLD_ROLE)
            if key is not None:
                keys.append(key)
            stack.extend(_children(item))
    return keys


class ListView(QWidget):
    """Grouped tree with a midnight re-bucket timer."""

    taskActivated = Signal(object)
    editRequested = Signal(object)
    #: A coalesced "reordering N tasks" message for the status bar.
    toast = Signal(str)

    def __init__(self, conn, config, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.config = config
        self.show_completed = config.show_completed  # runtime toggle, not persisted
        self._last_reorder = 0.0
        self._pending_reorder = 0
        #: What a drop into another list goes through — see :func:`carry_into`.
        #: The window sets it, because the move asks a question and can fail in
        #: ways only the window knows how to report.
        self.move_into: MoveHook | None = None

        # Which subtrees are open has to outlive a refresh, or the two-second
        # poll re-opens everything and folding is decorative.
        self.folds = vm.FoldState()
        self._building = False

        layout = QVBoxLayout(self)
        self.tree = _DropTree()
        self.tree.setHeaderLabels(["Task", "Due", "Priority", "Tags"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.dropped.connect(self._on_drop)
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
        selected = current_row_key(self.tree)
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
            select_row_key(self.tree, selected)

    def _node_item(self, node: vm.TreeNode) -> QTreeWidgetItem:
        item = task_item(node)
        # A context row is scaffolding: repeating its due date and tags would
        # give a row you cannot act on the weight of one you can.
        if not node.is_context:
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
        return task_of(self.tree.currentItem())

    def selected_tasks(self) -> list[Task]:
        return _acting_tasks(self.tree.selectedItems())

    def select_uid(self, uid: str) -> bool:
        return any(self._select_in(item, uid) for item in _top_level(self.tree))

    def _select_in(self, item: QTreeWidgetItem, uid: str) -> bool:
        task = item.data(0, TASK_ROLE)
        if task is not None and task.uid == uid:
            self.tree.setCurrentItem(item)
            return True
        return any(self._select_in(item.child(i), uid) for i in range(item.childCount()))

    def _activated(self, item: QTreeWidgetItem, _column: int) -> None:
        task = task_of(item)
        if task is not None:
            self.taskActivated.emit(task)

    # --------------------------------------------------------------- actions

    def set_show_completed(self, show: bool) -> None:
        self.show_completed = show
        self.refresh()

    def toggle_show_completed(self) -> None:
        self.set_show_completed(not self.show_completed)

    def toggle_complete(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        new = Status.NEEDS_ACTION if task.status is Status.COMPLETED else Status.COMPLETED
        cache.update_task_optimistic(stored(task.id), {"status": new}, self.conn)
        self.refresh()

    def reorder(self, task: Task, new_index: int) -> str | None:
        """Apply a keyboard reorder, coalescing rapid ones into one toast."""
        siblings = [
            t
            for t in vm.load_tasks(self.conn, calendar_ids=[stored(task.calendar_id)])
            if t.parent_uid == task.parent_uid
        ]
        result = vm.reorder_siblings(siblings, task.uid, new_index)

        self._write_orders(result.assignments, siblings)
        self.refresh()
        return self._coalesce(result.touched, result.rebalanced)

    def _on_drop(self, dragged: list[Task], onto: Task | None, position) -> None:
        """A list drop reparents and reorders.  It never touches STATUS.

        The buckets are a computed view of DUE, not a settable field, so a
        heading is not a drop target — it reaches here as ``onto is None``,
        which is also what empty space looks like.

        A grey context row does reach here, as the task it stands in for: a
        drop names a task, and there is only one it can mean.  Nesting under
        an ancestor the bucket only borrowed is a perfectly good thing to ask
        for, and the row lands back in whichever bucket its own DUE says.

        A row in another list reaches here like any other: the buckets are cut
        across every calendar, so "tomorrow" holds all of them side by side and
        a drop between two of them means what it looks like.  It asks first —
        crossing lists is a move.
        """
        if onto is None:
            return

        touched, rebalanced = apply_sibling_drop(
            self.conn, dragged, onto, position, move_into=self.move_into
        )
        self.refresh()
        message = self._coalesce(touched, rebalanced)
        if message:
            self.toast.emit(message)

    def _write_orders(self, orders: dict[str, int], candidates: list[Task]) -> None:
        _write_orders(orders, candidates, self.conn)

    def _coalesce(self, touched: int, rebalanced: bool) -> str | None:
        """Drags inside a 2 s window collapse into a single toast."""
        if not rebalanced:
            return None
        self._pending_reorder += touched
        now = time.monotonic()  # in-process timing is monotonic
        if now - self._last_reorder <= vm.REORDER_COALESCE_S:
            return None
        self._last_reorder = now
        count = self._pending_reorder
        self._pending_reorder = 0
        return f"reordering {count} tasks"


class _DropTree(QTreeWidget):
    """A tree whose drops are interpreted by the model rather than by Qt.

    Every view is a projection of the database, so letting Qt physically move
    rows would show a state the next refresh contradicts.  The event is
    consumed, the model is told, and the redraw comes from the data.

    A drag carries the whole selection — see :func:`dragged_tasks`.
    """

    #: ``(dragged tasks, task dropped onto or None, DropPosition)``
    dropped = Signal(object, object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Every row is one line in the same face — the grey context rows are
        # italic, not larger — so Qt may take the first row's height for all of
        # them instead of measuring each.  On a list long enough to need the
        # scrollbar, that is the whole cost of drawing it.
        self.setUniformRowHeights(True)

    def dropEvent(self, event) -> None:
        tasks = dragged_tasks(event.source())
        if not tasks:
            event.ignore()
            return

        position = DROP_POSITIONS.get(self.dropIndicatorPosition(), vm.DropPosition.ON)
        target = self.itemAt(event.position().toPoint())
        # A bucket heading carries no task, so it lands here as None and is
        # refused by every handler — a bucket is a computed view of DUE.  A
        # grey stand-in does carry one, under CONTEXT_ROLE: what a drop names
        # is the task under the cursor, whether or not this slice owns the row.
        onto = task_of(target)

        event.acceptProposedAction()
        self.dropped.emit(tasks, onto, position)


class _ColumnTree(_DropTree):
    """One kanban column, as a tree.

    ``startDrag`` blocks for the whole drag, which is what makes it the one
    place that knows a drag is *in progress* — the board uses it to light up
    the headers, which are otherwise drop targets nothing on screen mentions.
    """

    dragStarted = Signal()
    dragEnded = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)

    def startDrag(self, actions) -> None:
        self.dragStarted.emit()
        try:
            super().startDrag(actions)
        finally:
            # In a finally, or a drag cancelled with Escape — or one that
            # throws inside a drop handler — leaves the board lit up forever.
            self.dragEnded.emit()


#: The header's three states.  A transparent border in the resting one, so
#: arming it cannot shift the row by a pixel.
_HEADER_STYLES = {
    "rest": "border: 1px solid transparent; border-radius: 4px; padding: 3px;",
    "armed": (
        "border: 1px dashed palette(highlight); border-radius: 4px; padding: 3px;"
        "background: palette(alternate-base);"
    ),
    "hover": (
        "border: 1px solid palette(highlight); border-radius: 4px; padding: 3px;"
        "background: palette(highlight); color: palette(highlighted-text);"
    ),
}


#: Border plus padding, from :data:`_HEADER_STYLES`.  A rotated label paints
#: its own text, so it also has to keep its own hands off the frame.
_HEADER_INSET = 4


class _ColumnHeader(QLabel):
    """The column's name, its drop target, and its fold handle.

    Aiming at the word "Done" is a much bigger target than the empty space
    under the last card — which in a full column is not on screen at all.  A
    header drop is only ever the column move: there is no card under it to
    nest into.

    That target is invisible until you have already found it, so a drag arms
    every header and the one under the cursor fills in.  A drop target nobody
    can see is a feature only its author uses.

    Clicking it folds the column — see :meth:`KanbanView.toggle_column`.  The
    name is the one part of a folded column still on screen, so it is also the
    only thing left to click to get it back.

    Folded, it turns on its side: the text runs top to bottom and the widget is
    one line *tall* and one line *wide*, which is what makes a folded column a
    spine rather than a stub.  Horizontally it would still cost the width of
    the word "Cancelled" — five columns of that is the board back again.
    """

    #: ``(dragged tasks,)`` — the column is implied by which header it is.
    dropped = Signal(object)
    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.vertical = False
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_drop_state("rest")

    def set_vertical(self, vertical: bool) -> None:
        """Sideways, and as tall as the board lets it be.

        The height is what makes the bar aimable: rotated text is a few
        characters wide, and a drop target that thin is one you fight with.
        Full height, the whole right edge of the board is Done.
        """
        if vertical == self.vertical:
            return
        self.vertical = vertical
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed if vertical else QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding if vertical else QSizePolicy.Policy.Preferred,
        )
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        if not self.vertical:
            return super().sizeHint()
        return QSize(self._line_height(), self._text_length())

    def minimumSizeHint(self) -> QSize:
        if not self.vertical:
            return super().minimumSizeHint()
        # Shorter than the name, so a small window shrinks the bar and elides
        # rather than forcing the board wider than the screen.
        return QSize(self._line_height(), min(self._text_length(), 6 * self._line_height()))

    def _bold_font(self) -> QFont:
        """What :meth:`paintEvent` draws with, so the measuring and the drawing
        cannot disagree — the open header is bold too, and measuring the plain
        face asks the layout for a bar the name does not fit in."""
        font = self.font()
        font.setBold(True)
        return font

    def _line_height(self) -> int:
        return QFontMetrics(self._bold_font()).height() + 2 * _HEADER_INSET

    def _text_length(self) -> int:
        metrics = QFontMetrics(self._bold_font())
        return metrics.horizontalAdvance(self.text()) + 2 * _HEADER_INSET

    def paintEvent(self, event) -> None:
        if not self.vertical:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        # The stylesheet is drawn by the QLabel paintEvent this one replaces —
        # without repeating it by hand, the armed and hovered borders would go
        # missing exactly when the bar is the thing being dropped on.
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)
        painter.setPen(option.palette.color(self.foregroundRole()))
        font = self._bold_font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        # Clockwise about the top-right corner, so the text reads downwards and
        # the box it is drawn in is this one transposed.
        painter.translate(self.width(), 0)
        painter.rotate(90)
        box = QRect(
            _HEADER_INSET,
            _HEADER_INSET,
            self.height() - 2 * _HEADER_INSET,
            self.width() - 2 * _HEADER_INSET,
        )
        text = metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, box.width())
        painter.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def mousePressEvent(self, event) -> None:
        # A QLabel ignores presses, and Qt then delivers the release to
        # whatever is behind it — so there would be no click at all.
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        """A press and release on the name, the way a button behaves: dragging
        off it before letting go is how you change your mind."""
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        event.accept()

    def set_drop_state(self, state: str) -> None:
        self.drop_state = state
        self.setStyleSheet(_HEADER_STYLES[state])

    def dragEnterEvent(self, event) -> None:
        # Without accepting the enter and every move, Qt never delivers the
        # drop at all — the cursor just shows "no".
        if dragged_tasks(event.source()):
            self.set_drop_state("hover")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:
        # Back to armed, not to rest: the drag is still running, and this is
        # still somewhere it could land.
        self.set_drop_state("armed")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        tasks = dragged_tasks(event.source())
        if not tasks:
            event.ignore()
            return
        event.acceptProposedAction()
        self.dropped.emit(tasks)


class KanbanView(QWidget):
    """One tree per configured column; a drag sets STATUS **and** the override."""

    taskActivated = Signal(object)

    def __init__(self, conn, config, parent=None) -> None:
        super().__init__(parent)
        self.conn = conn
        self.config = config
        self.columns = config.kanban.columns
        self.filter = vm.TaskFilter()
        #: What a drop into another list goes through — see :func:`carry_into`.
        self.move_into: MoveHook | None = None
        # The same runtime view state the list view has, for the same reason:
        # a board with the Done column configured shows finished work until
        # you say otherwise.
        self.show_completed = config.show_completed
        #: Context rows for subtasks *below* a card in the In Progress column:
        #: off, because that column is the one you read most often and a parent
        #: picked up there drags its whole scattered family in behind it.
        #: Context *ancestors* are not covered — a subtask sitting there with
        #: nothing above it is unreadable whichever column it is in.  Runtime
        #: state, like ``show_completed``.
        self.show_inprogress_context_children = False

        outer = QVBoxLayout(self)
        self.filter_bar = FilterBar()
        self.filter_bar.filterChanged.connect(self._on_filter_changed)
        # Whichever lists were picked last time, ticked again as soon as the
        # first refresh has something to tick.
        self.filter_bar.restore(ui_state.get(FILTER_STATE_KEY))
        outer.addWidget(self.filter_bar)

        board = QWidget()
        layout = self._board = QHBoxLayout(board)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(board, 1)
        self.folds = vm.FoldState()
        self._building = False
        self.lists: dict[str, _ColumnTree] = {}
        self.headers: dict[str, _ColumnHeader] = {}
        self._boxes: dict[str, QVBoxLayout] = {}
        self._counts: dict[str, int] = {column.id: 0 for column in self.columns}
        #: Collapsed to a strip of name and count.  Runtime state, seeded from
        #: the config the way ``show_completed`` is.
        #:
        #: With "show completed" off, the finished columns start folded whatever
        #: the config says: they can hold nothing but the cards that setting
        #: hides, so open they are empty width.  Only the startup seed — the
        #: header still unfolds them, and the toggle leaves the fold alone.
        self.folded_columns: set[str] = {c.id for c in self.columns if c.folded}
        if not self.show_completed:
            self.folded_columns |= {c.id for c in self.finished_columns()}
        for column in self.columns:
            box = QVBoxLayout()
            header = _ColumnHeader("")
            # Dropping on the name is the same as dropping in the column, and
            # a much easier thing to aim at.
            header.dropped.connect(
                lambda tasks, column_id=column.id: self._on_drop(column_id, tasks, None)
            )
            header.clicked.connect(lambda column_id=column.id: self.toggle_column(column_id))
            self.headers[column.id] = header
            box.addWidget(header)
            widget = _ColumnTree()
            widget.dragStarted.connect(self._arm_headers)
            widget.dragEnded.connect(self._disarm_headers)
            widget.itemActivated.connect(self._activated)
            widget.itemExpanded.connect(lambda item: self._remember(item, True))
            widget.itemCollapsed.connect(lambda item: self._remember(item, False))
            # Onto a card nests, between two cards places it beside them: the
            # position is what says which, so it has to reach the handler.
            widget.dropped.connect(
                lambda tasks, onto, position, column_id=column.id: self._on_drop(
                    column_id, tasks, onto, position
                )
            )
            box.addWidget(widget, 1)
            self.lists[column.id] = widget
            self._boxes[column.id] = box
            layout.addLayout(box)

        self._apply_folds()
        self._paint_headers()

    # ---------------------------------------------------------- folded columns

    def toggle_column(self, column_id: str) -> None:
        self.set_column_folded(column_id, column_id not in self.folded_columns)

    def set_column_folded(self, column_id: str, folded: bool) -> None:
        """Collapse a column to a strip of its own name and count.

        Done and Cancelled are what this is for: a board that keeps them is
        two fifths finished work, and the alternative on offer — dropping the
        columns from the config — takes the drop target away with them, so
        there is no way left to *put* anything there.  Folded, they are still
        a target, still counted, and no longer half the board.

        The count is the part that has to survive folding.  A column you
        cannot see is one you will forget, and "Done (12)" is the difference
        between out of the way and out of mind.
        """
        if column_id not in self.lists:
            return
        self.folded_columns.discard(column_id)
        if folded:
            self.folded_columns.add(column_id)
        self._apply_folds()
        self._paint_headers()

    def _apply_folds(self) -> None:
        """Hide the cards, turn the names on their side, and gather the folded
        columns at the right edge.

        They gather there rather than staying put because a spine standing
        between two open columns is a seam down the middle of the board, and
        because what gets folded is the finished work — which is where the eye
        looks for it anyway.  Their order among themselves is the configured
        one, so unfolding a column puts it back where it came from.
        """
        for column in sorted(self.columns, key=lambda c: c.id in self.folded_columns):
            folded = column.id in self.folded_columns
            self.lists[column.id].setHidden(folded)
            self.headers[column.id].set_vertical(folded)
            box = self._boxes[column.id]
            # Every box taken out and put back at the end, in the order wanted,
            # which walks the whole board into that order.  The stretch is the
            # other half of the fold: without it the spine keeps its equal
            # share of the width and folding wins nothing.
            self._board.removeItem(box)
            self._board.addLayout(box, 0 if folded else 1)

    def column_order(self) -> list[str]:
        """The columns as the board has them, left to right — which is the
        configured order only until something is folded."""
        placed: list[str] = []
        for index in range(self._board.count()):
            item = self._board.itemAt(index)
            placed += [column_id for column_id, box in self._boxes.items() if box is item]
        return placed

    def _paint_headers(self) -> None:
        """The name, the marker and the count, for folded and open alike."""
        for column in self.columns:
            folded = column.id in self.folded_columns
            header = self.headers[column.id]
            count = self._counts.get(column.id, 0)
            # Folded, the text is painted by hand and rotated with it, so it is
            # plain rather than markup — and the "▾" turns into an arrow
            # pointing left, back at the board the column unfolds into.
            name = column.label if folded else f"<b>{column.label}</b>"
            header.setText(f"▾ {name}  ({count})")
            header.setToolTip(
                f"Click to {'unfold' if folded else 'fold'} this column.\n"
                "Cards can still be dropped on the name either way."
            )

    def _arm_headers(self) -> None:
        for header in self.headers.values():
            header.set_drop_state("armed")

    def _disarm_headers(self) -> None:
        for header in self.headers.values():
            header.set_drop_state("rest")

    def refresh(self) -> None:
        tasks = vm.load_tasks(self.conn)
        # Offer only what the data actually contains, so the picker can never
        # list a tag or a calendar that would match nothing.
        self.filter_bar.set_calendars(self._calendar_names())
        self.filter_bar.set_tags(vm.available_tags(tasks))

        visible = tasks if self.show_completed else [t for t in tasks if not vm.is_finished(t)]
        board = vm.kanban_board(visible, self.columns, self.filter)
        # Every card on the board, so a column can also show the subtasks that
        # went to *other* columns — greyed, but not gone.
        on_board = [task for cards in board.values() for task in cards]
        focused = self._focused_list()
        selected = current_row_key(focused) if focused is not None else None
        shown = 0
        self._building = True
        try:
            for column_id, widget in self.lists.items():
                widget.clear()
                # A column is a slice, so the tree builder gets the whole set:
                # a card whose parent sits in another column arrives with that
                # parent as a grey context row, not as a root of its own.
                # Withholding `on_board` is what turns the context children
                # off: the walk *down* is the only thing that reads it, so the
                # walk up still brings the parents along.
                below = None if column_id in self._skip_child_context() else on_board
                for node in vm.build_tree(board[column_id], tasks, below):
                    widget.addTopLevelItem(self._node_item(node))
                apply_folds(widget, self.folds)
                shown += len(board[column_id])
        finally:
            self._building = False
        if selected is not None:
            self._select_row_key(selected)
        stranded = sum(
            1 for t in vm.apply_filter(tasks, self.filter) if vm.column_of(t, self.columns) is None
        )
        self._update_counts(board, shown, len(visible), stranded)

    def set_show_completed(self, show: bool) -> None:
        self.show_completed = show
        self.refresh()

    def toggle_show_completed(self) -> None:
        self.set_show_completed(not self.show_completed)

    def inprogress_columns(self) -> list[KanbanColumn]:
        """The configured columns that stand for IN-PROCESS.

        By status, not by id or label: the column is the user's to rename and
        renumber, and the toggle is about the *state*, not the word.
        """
        return [c for c in self.columns if c.status == Status.IN_PROCESS.value]

    def finished_columns(self) -> list[KanbanColumn]:
        """The configured columns "show completed" empties.

        The same pair :func:`vm.is_finished` calls finished, matched by status
        for the same reason :meth:`inprogress_columns` does: the label is the
        user's to change.
        """
        done = {Status.COMPLETED.value, Status.CANCELLED.value}
        return [c for c in self.columns if c.status in done]

    def _skip_child_context(self) -> set[str]:
        """Columns that pull no subtasks down as context this refresh."""
        if self.show_inprogress_context_children:
            return set()
        return {column.id for column in self.inprogress_columns()}

    def set_show_inprogress_context_children(self, show: bool) -> None:
        self.show_inprogress_context_children = show
        self.refresh()

    def toggle_show_inprogress_context_children(self) -> None:
        self.set_show_inprogress_context_children(not self.show_inprogress_context_children)

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

    def _select_row_key(self, key) -> bool:
        """The row itself, in whichever column it was — see
        :func:`current_row_key`."""
        return any(select_row_key(widget, key) for widget in self.lists.values())

    def _on_drop(
        self,
        column_id: str,
        dragged: list[Task],
        onto: Task | None,
        position=vm.DropPosition.ON,
    ) -> None:
        """Onto a card: become its subtasks, in its column.  **Between** two
        cards: become their sibling, in its column.  Onto the column itself or
        its header: just the move.

        The middle case is how a subtask leaves its parent on the board.  A
        column is a status, so dropping on one cannot say anything about
        nesting; without a drop that places a card *beside* another there is no
        drag on the whole board that unnests anything, and a card dragged out
        of its parent silently snapped back under it.

        Onto a grey stand-in: the same two, against the task it stands in for.
        That is the drop the board was missing — a parent in In Progress and
        the card you want under it can never be cards in the same column, so
        the stand-in is the only place they are ever side by side.  The status
        still comes from the column the row was drawn in, not from the one its
        task lives in, so the card lands where it was dropped.

        Onto a card in another list: the move first, asked for through
        :func:`carry_into`, and then the same nesting.  A column is a status
        and holds every list at once, so "under this one" lands across lists
        constantly, and refusing it was the board doing nothing at all.

        One update per task carrying both fields, so an interrupted drag
        cannot leave one nested somewhere it is not shown.
        """
        column = next((c for c in self.columns if c.id == column_id), None)
        if column is None:
            return
        movable = [task for task in dragged if not task.is_read_only]
        if not movable:
            return

        if onto is not None and position is not vm.DropPosition.ON:
            apply_sibling_drop(
                self.conn,
                movable,
                onto,
                position,
                vm.drop_fields(column),
                move_into=self.move_into,
            )
            self.refresh()
            return

        nesting: dict[str, dict[str, object]] = {}
        if onto is not None:
            # A column spans every list, so the card you are nesting under is
            # often in another one.  It is carried across first — asking as it
            # goes — and the nesting is then an ordinary same-list one.
            movable = carry_into(self.conn, movable, stored(onto.calendar_id), self.move_into)
            for plan in vm.plan_paste(movable, onto, vm.load_tasks(self.conn)):
                if plan.needs_move:
                    # The move was declined or refused, and RELATED-TO resolves
                    # within one calendar only: this would be a link that never
                    # renders.
                    log.info(
                        "Refusing to nest %s under a task in another calendar",
                        plan.task.uid[:8],
                    )
                    continue
                nesting[plan.task.uid] = plan.fields
            if not nesting:
                return
            movable = [task for task in movable if task.uid in nesting]

        for task in movable:
            fields = dict(vm.drop_fields(column))
            fields.update(nesting.get(task.uid, {}))
            cache.update_task_optimistic(stored(task.id), fields, self.conn)
        self.refresh()

    def _calendar_names(self) -> dict[str, str]:
        return {
            row["id"]: row["display_name"] or row["href"]
            for row in cache.calendar_rows(self.conn)
            if row["available"]
        }

    def _update_counts(self, board: dict, shown: int, total: int, stranded: int) -> None:
        self._counts = {column.id: len(board[column.id]) for column in self.columns}
        self._paint_headers()

        if self.filter.is_active:
            self.filter_bar.summary.setText(
                f"{self.filter.describe(self._calendar_names())}  —  {shown} of {total}"
            )
        elif stranded:
            # A status with no column of its own is off the board entirely, so
            # the board has to say so — otherwise dropping the Done column
            # silently swallows every finished task.  Counted on its own rather
            # than as "everything not on screen": what "show completed" hides
            # is hidden on purpose, and does not need reporting back.
            self.filter_bar.summary.setText(f"{stranded} task(s) have a status no column shows")
        else:
            self.filter_bar.summary.setText("")

    def _on_filter_changed(self, task_filter) -> None:
        self.filter = task_filter
        # Written here rather than on close, so a crash or a kill costs the
        # picked lists nothing.  Most changes through here keep nothing new —
        # every keystroke in the text box — and those cost no write.
        ui_state.remember(FILTER_STATE_KEY, self.filter_bar.state())
        self.refresh()

    def _activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        task = task_of(item)
        if task is not None:
            self.taskActivated.emit(task)

    def _focused_list(self) -> _ColumnTree | None:
        """Which column the user means.

        Focus may have moved to the filter bar or a menu since the click, and
        a card the user can still see selected is the one they are acting on —
        without the fallback, every menu-driven action on the board silently
        does nothing.

        A folded column is skipped: its rows are still selected from before it
        was folded, and "a card the user can still see" is the whole basis of
        the fallback.  ``isHidden`` rather than ``isVisible`` — the latter is
        false for the entire board whenever another view is on top.
        """
        for widget in self.lists.values():
            if widget.hasFocus():
                return widget
        return next(
            (w for w in self.lists.values() if not w.isHidden() and w.selectedItems()), None
        )

    def selected_task(self) -> Task | None:
        widget = self._focused_list()
        return task_of(widget.currentItem()) if widget is not None else None

    def selected_tasks(self) -> list[Task]:
        widget = self._focused_list()
        return _acting_tasks(widget.selectedItems()) if widget is not None else []

    def selected_column(self) -> KanbanColumn | None:
        """The column the selection is *shown* in, which is not always its own.

        A grey context row stands in a column its task did not choose, and the
        column it is drawn in is the one the user is looking at and pointing
        at.  Asking the task where it belongs would answer about somewhere
        else entirely, so anything that means "here, on the board" asks this.
        """
        widget = self._focused_list()
        if widget is None:
            return None
        column_id = next((cid for cid, w in self.lists.items() if w is widget), None)
        return next((c for c in self.columns if c.id == column_id), None)

    def move_to_column(self, task: Task, column_id: str) -> None:
        """Sets both STATUS and X-DAVPUNK-KANBAN-COL in one update."""
        column = next((c for c in self.columns if c.id == column_id), None)
        if column is None:
            return
        cache.update_task_optimistic(stored(task.id), vm.drop_fields(column), self.conn)
        self.refresh()

    def shift_selected(self, delta: int) -> None:
        task = self.selected_task()
        if task is None:
            return
        current = vm.column_of(task, self.columns)
        if current is None:
            return
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
        self.results.setUniformRowHeights(True)
        self.results.setHeaderLabels(["Task", "Status", "Due", "List"])
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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

    def selected_tasks(self) -> list[Task]:
        return _tasks_of(self.results.selectedItems())
