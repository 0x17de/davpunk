# Using DavPunk

The board and the list are both trees, and most of what is worth knowing is
about moving tasks around them.

## Subtasks

Both the list and the kanban board are trees. **Tab** makes the selected task a
subtask of the one above it, **Shift+Tab** promotes it back out, and on the
board you can drag a card onto another to do the same — it becomes a subtask
*and* moves to that card's column.

Dropping a card **between** two others puts it beside them instead of inside
anything, which is how you take a subtask back out of its parent by dragging:
drop it between two top-level cards and it becomes top-level too. Dropping it
on a column, or on the column's name, only ever changes the status — a column
says nothing about what a task hangs under, so it leaves the nesting alone.

In the list you can drag too: drop a row **onto** another to nest it, or
**between** two rows to place it there. A drop never changes the status — the
groups you see are Overdue / Today / Upcoming, which are computed from the due
date rather than set, so a heading is not a drop target. Every task editor also
carries a **Parent** picker, which offers only tasks in the same list and never
the task's own subtree.

Both views mix your lists together — the board sorts by status, the list by due
date — so the row you are aiming at is often in another list. Drag onto it
anyway: DavPunk asks first, because moving a task to another list is a real
move on the server, and shows you where it is about to land along with the same
**Move subtasks too** question the **m** dialog has. Say no and the task stays
where it was; on the board it still lands in the column you dropped it in.

A drag carries the whole selection, so you can pick up a range and move it in
one go; it keeps the order you picked it up in. Grabbing a row that is *not*
selected drags only that row. Selecting a parent and one of its children and
dragging both moves the parent once — the child comes with it rather than
being torn out of it.

On the board you can also drop straight onto a column's **name**. "Done" is a
far bigger target than the empty space under the last card, which in a full
column is not even on screen. Every column name outlines itself the moment you
start dragging, and fills in when you are over it, so you can see where a drop
would land; a header drop is only ever the column move, since there is no card
under it to nest into.

**n** starts a new task and **Shift+N** a new subtask of the selection; the
editor's **List** and **Parent** pickers decide where it lands. The List picker
starts on the list of the selected task. With nothing selected it starts on the
list your last new task went into. On the board it only uses a list the filter
is showing, and otherwise falls back to the first one the filter shows, so the
new card does not disappear as soon as you save it. Right-clicking
any task offers the same actions. Started from the board, the editor opens on
the status of the column you were in — right-click a card in "To Do" and the
new subtask is a "To Do" too, unless you change it before saving.

## Writing down a list you already have

**Ctrl+Shift+N** opens a plain text box instead: one line, one subtask, all of
them under the selection. It is for the moment you already know the five things
that need doing and want them written down before you forget the third — the
full editor, twelve fields at a time, is for afterwards.

Paste from anywhere. Bullets, `- [ ]` boxes and `1.` numbering are stripped, so
a list copied out of a mail or a README arrives as titles rather than as titles
with punctuation in front of them. Blank lines are skipped, and the dialog says
how many subtasks Save will actually create before you press it. Indentation is
ignored: every line becomes a direct child of the one task you picked.

## Moving a task when a filter is in the way

A drag needs both tasks on screen, and a filter is precisely what stops that.
So **Ctrl+X** cuts the selection and **Ctrl+V** pastes it under whatever is
selected then — change the filter in between, and the two never have to be
visible together. Paste with nothing selected to make the task top-level.

A cut is a move, not a copy: the clipboard empties once it lands, and it never
reaches the system clipboard. Pasting into another list is a real move, so
DavPunk asks first and offers the same "move subtasks too" question the **m**
dialog does.

## Deleting

**d d**, *Edit → Delete task*, or the right-click menu. Subtasks are not deleted
with their parent by default — they become top-level tasks — and the
confirmation offers **Delete subtasks as well** when there are any. Both trees
take a multiple selection, so delete, cut and move act on all of it.

## Moving to another list

**m**, or *Edit → Move to another list…*, on a selection of any size. The
tasks have to share one list: a move is out of one list and into another, and
the dialog's job is to offer everywhere except where you already are. A
selection spanning two lists says so rather than guessing.

For one task, the editor's **List** field does the same thing — which list a
task is in is the same kind of question as which tags it carries, and having to
close the editor to answer it was the odd part. Change it and Save; if the task
has subtasks, a **Move subtasks too** box appears beside the field and comes
alive once the list actually differs. Leaving it unticked leaves them behind as
root tasks in the old list, because a parent link only resolves within one
calendar. Read-only tasks are the exception: the editor is disabled for them
whole, and *Move to another list…* still works.

Folds stick. A subtree starts closed and stays however you left it, across
refreshes and restarts of the view; a folded parent shows how many tasks it is
hiding, so `kaufen  (262)` tells you what you are about to open. **z R** unfolds
everything, **z M** folds it all back, both also under **View**.

On the board a column is a *status* and nesting is a *relation*, so the two are
independent: a parent in "In Progress" can have its subtasks scattered across
every other column. The board shows the family either way: where a subtask
lands without its parent the parent comes along **greyed out** above it, and
where a parent's subtask has gone to another column it stays under the parent,
greyed, instead of leaving a card that looks like it has nothing under it. The
same happens in the list view when a subtask is due today and its parent next
week.

In Progress is the exception, because it is the column you read most and a
parent picked up there brings its whole scattered family in behind it: it pulls
no subtasks down unless you ask for them under **View → Show subtask context in
In Progress**. The greyed *parents* are unaffected — a subtask with nothing
above it is unreadable in any column.

A grey row is a second rendering of a task that really lives elsewhere, so it
cannot be dragged, ticked or dropped onto — those would be claims about a
column the task is only visiting. Its right-click menu works normally though:
*New subtask*, *Change status*, *Open* and the rest name the task, not the row,
and act on the real one.

## Columns, and having fewer of them

The default board is **To Do · Needs Action · In Progress · Done · Cancelled**.
"To Do" is the tasks with no STATUS at all — the pool you pick from — and
"Needs Action" the ones somebody has picked. They used to share a column, which
lost exactly the distinction the board is for. Dropping a card back on "To Do"
clears its status again.

Columns are the one thing Preferences does not edit yet, so this is the
config file: `~/.config/davpunk/config.toml`. DavPunk writes it through
`tomlkit`, so anything you add there survives the next time Preferences saves.

A status with no column of its own is **not shown on the board**. So if Done and
Cancelled are just bloat for you, delete them from `[davpunk.kanban]` and the
finished cards go with them:

```toml
[davpunk.kanban]
columns = [
  {id = "todo",        label = "To Do"                                },
  {id = "needsaction", label = "Needs Action", status = "NEEDS-ACTION"},
  {id = "inprogress",  label = "In Progress",  status = "IN-PROCESS"  },
]
```

Nothing is lost — the list view and search still show everything, and the board
says how many tasks it is not showing rather than hiding them silently. The
states stay reachable through **Edit → Change status**, which is also on the
right-click menu and takes a multiple selection, and through the task editor's
**Status** field — which offers **(no status)** as a real choice, since that is
what puts a task back in the "To Do" pool.

## Folding a column

Deleting a column is the heavy version of "I do not want to look at this", and
it takes the drop target with it — there is then nowhere to *put* a finished
card. Clicking a column's name folds it instead: it collapses to a spine at the
right edge of the board, one line wide, with its name and count turned on their
side. It gives the rest of its width to the columns you are working in and goes
on accepting drops — the whole height of the spine is the target, so `Done` is
easier to hit folded than open. Click it again to bring it back, in the place
the config gives it.

Start it that way with `folded`:

```toml
[davpunk.kanban]
columns = [
  {id = "todo",        label = "To Do"                                              },
  {id = "needsaction", label = "Needs Action", status = "NEEDS-ACTION"              },
  {id = "inprogress",  label = "In Progress",  status = "IN-PROCESS"                },
  {id = "done",        label = "Done",         status = "COMPLETED", folded = true  },
  {id = "cancelled",   label = "Cancelled",    status = "CANCELLED", folded = true  },
]
```

That is the starting state only. Folding and unfolding during a session is not
written back, the same as *show completed* below.

The COMPLETED and CANCELLED columns start folded whatever `folded` says whenever
*show completed* is off at startup: the only cards they could hold are the ones
that setting hides, so open they would be empty width. Clicking their names
still opens them.

## Finished work

Completed and cancelled tasks are hidden by default, in the list *and* on the
board. **View → Show completed** brings them back for as long as you leave it
ticked; it is not saved, so it starts from `show_completed` in the config every
time. Search ignores it — finding something you know you finished is one of the
things search is for.

## Filtering the board

The kanban board filters on three axes at once: which **lists** (pick several),
which **tags** (pick several, matched `any` or `all`), and a free-text substring
over the summary and description. **Reset** clears all three.

Ticking every list is the same as ticking none — every task is in exactly one
list, so that is no restriction and the button says "all". Tags do not work that
way: a task may carry none, so ticking every tag still means "has a tag" and
keeps filtering. Only tags actually in use are offered.

The picked lists and tags are remembered: close DavPunk with "private" ticked
and it opens with "private" ticked, along with the `any`/`all` switch. That is
what a picker is — you work out of the same one or two lists for weeks, and
re-picking them every morning is a chore. A list that has since gone away drops
out of the remembered pick rather than filtering the board down to nothing.

The text box is *not* remembered, for the reason *show completed* is not: a
search is typed for one question and answered, so it starts empty every time.

What is remembered lives in `~/.local/state/davpunk/ui-state.json`, not in your
config — nothing DavPunk writes for itself belongs in a file you own. Delete it
and the board opens unfiltered.

The full config reference — kanban columns, key bindings, MCP — is in
[HLD.md §16](../HLD.md).

## When a task changed in two places

If you edited a task and the server's copy moved too, DavPunk does not pick a
winner. The task is badged `conflict`, refuses further edits, and opens a
**merge window** — after a sync you asked for, or whenever you open the task.

Three columns: the server's version on the left, **the result** in the middle,
yours on the right. One row per field; the rows that actually differ are marked,
the rest are dimmed. The middle starts out as whichever version is newer, and
you change it in two ways:

- the arrows — or `←` / `→`, `j`/`k` to move between fields — copy one side's
  value into the middle;
- or you type into the middle directly, and the result is a value neither side
  had.

The middle column is what gets saved. **Accept** writes it and queues the
update; **Skip** leaves the conflict alone and brings it back at a later sync,
which is the right answer when you want to look at something before deciding.

Values that only mean something together move together: a due date carries its
time zone with it, so you can never end up with the server's time in your zone,
and a status carries the board column it was set beside.

One thing never reaches that window. Dragging a task into a new position is a
change like any other, so two clients tidying the same list collide — but where
a card sits in a list is not a question worth interrupting anyone with. Those
are merged on the spot and the most recent drag wins. You will see them counted
as *auto-merged* in the sync report, and nothing is badged. If the same sync
also brought a real disagreement — a summary, a due date — you still get the
window for that.

When a task exists on only one side — you deleted it and the server changed it,
or the other way round — there is nothing to merge, so those keep a plain
two-button question (*Delete anyway* / *Keep the server's version*, or *Recreate
on server* / *Accept deletion*), with the same *Skip*.

`davpunk conflicts` lists what is open from the command line; resolving is a UI
action.

*Part of [DavPunk](../README.md).*
