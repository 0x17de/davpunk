# DavPunk

**Work it. Sync it. Check it. Done.**

A power-user CalDAV VTODO client for Linux (Wayland-primary, X11 compatible).
Syncs with CalDAV servers — primarily [Radicale](https://radicale.org/) — caches
everything offline in SQLite, and optionally exposes an MCP server so an AI
agent can read and manipulate tasks. The UI targets information density and
keyboard-driven workflows.

[HLD.md](HLD.md) is the design authority; the tests assert it.

## Requirements

| | |
|---|---|
| Python | ≥ 3.11 |
| SQLite | ≥ 3.35, built with FTS5 |
| tz data | the system database, or the `tzdata` wheel |
| A place for passwords | either `gpg` on `PATH` with a running `gpg-agent`, **or** a Secret Service (gnome-keyring, KWallet, KeePassXC). Chosen per account — see [Where your password is kept](#where-your-password-is-kept). |

`davpunk doctor` checks every one of these individually, so a machine that is
missing two things tells you about both on the first run.

## Install

### Nix

```sh
nix run github:mh/DavPunk          # just run the UI
nix profile install github:mh/DavPunk
nix develop                        # dev shell: Qt, Radicale and gpg all wired up
```

The flake exposes:

| Output | |
|---|---|
| `packages.davpunk` | the full app — UI, MCP and notifications, plus a launcher entry |
| `packages.davpunk-headless` | no Qt, no MCP: for a server that only runs the daemon |
| `apps.default` / `apps.sync` / `apps.mcp` | `davpunk`, `davpunk-sync`, `davpunk-mcp` |
| `devShells.default` | the test environment, including a real Radicale |
| `overlays.default` | `pkgs.davpunk` and `pkgs.davpunk-headless` |
| `nixosModules.default` | `programs.davpunk.*` |
| `homeModules.default` | `services.davpunk.*` (also as `homeManagerModules.default`) |

The one input is `nixpkgs`, so a config with its own pin needs a single line:

```nix
inputs.davpunk = {
  url = "github:mh/DavPunk";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Add `overlays.default` and both modules take their package from your `pkgs` —
your nixpkgs, your overlays, one evaluation. Without the overlay they still
work, they just build DavPunk against this flake's own locked nixpkgs instead.

**NixOS**, in your flake:

```nix
{
  inputs.davpunk.url = "github:mh/DavPunk";

  outputs = { nixpkgs, davpunk, ... }: {
    nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
      modules = [
        davpunk.nixosModules.default
        {
          nixpkgs.overlays = [ davpunk.overlays.default ];
          programs.davpunk.enable = true;
          programs.davpunk.daemon.enable = true;   # optional background sync
        }
      ];
    };
  };
}
```

The daemon is a systemd **user** service, not a system one: the config, cache
and credentials are all per-user, and it needs that user's `gpg-agent` or
login keyring to read a password at all. Users start it themselves with
`systemctl --user enable --now davpunk-sync`, or you set
`programs.davpunk.daemon.autoStart = true`.

**Home Manager**, in a home configuration:

```nix
{
  imports = [ davpunk.homeModules.default ];

  services.davpunk = {
    enable = true;
    daemon.enable = true;
    daemon.gpgAgentTtls = true;   # let the agent hold the passphrase for a working day

    # Optional: declare the config instead of using the first-run wizard.
    # Leaving this out keeps config.toml writable so the wizard can create it.
    settings.davpunk = {
      theme = "dark";
      remotes = [{
        id = "work";
        url = "https://cal.example.com/dav/";
        username = "user@example.com";
        gpg_key_id = "0x1A2B3C4D5E6F0003";
      }];
    };
  };
}
```

Credentials are never part of `settings` — they live in GnuPG-encrypted files
that the wizard or `gpg --encrypt` writes, or in your login keyring.

**Home Manager as a NixOS module**, which is how most configs run it — the
module goes in `sharedModules`, the overlay goes in the system's nixpkgs, and
`useGlobalPkgs` then hands both to every user:

```nix
{
  nixpkgs.overlays = [ davpunk.overlays.default ];

  home-manager.useGlobalPkgs = true;
  home-manager.sharedModules = [ davpunk.homeModules.default ];

  home-manager.users.you = {
    services.davpunk = {
      enable = true;
      daemon.enable = true;
      daemon.gpgAgentTtls = true;
    };
  };
}
```

`sharedModules` only adds the options — every user who does not set
`services.davpunk.enable` gets nothing installed.

Either module puts DavPunk in your application launcher: the UI build installs
`de.zeroxseventeen.DavPunk.desktop` and a scalable icon under
`share/icons/hicolor`. The app sets its Wayland `app_id` to the same
`de.zeroxseventeen.DavPunk`, so compositor window rules can name it:

```
windowrulev2 = workspace 4, class:^(de\.zeroxseventeen\.DavPunk)$
```

`davpunk-headless` ships neither — a daemon-only host has no UI to launch.

Pick **one** of the two modules per user. `programs.davpunk` (NixOS) and
`services.davpunk` (Home Manager) both install the package and both write a
`davpunk-sync` user unit, so enabling both gives you two units with the same
name, and the Home Manager one wins.

### uv

```sh
uv sync --all-extras      # everything
uv sync                   # CLI and daemon only — no Qt, no MCP
uv sync --extra ui        # add the desktop UI
uv sync --extra mcp       # add the MCP server
```

The UI and the MCP server are **extras** on purpose: a headless machine running
only the sync daemon should not need Qt installed.

## First run

Launch `davpunk` with no config and the first-run wizard opens: account details
(with `https://` enforced), a choice of where to keep the password, a password
prompt, and a `config.toml` written 0600 through `tomlkit` so any comments you
add later survive. Every field has a `?` beside it — hover for what it means
and what happens if you get it wrong.

To set it up by hand instead, write `~/.config/davpunk/config.toml`:

```toml
[davpunk]
theme          = "dark"      # dark | light
default_view   = "kanban"    # kanban | list
show_completed = false

[[davpunk.remotes]]
id                 = "work"
name               = "Work"
url                = "https://cal.example.com/dav/"
username           = "user@example.com"
credential_backend = "gpg"          # gpg | keyring
gpg_key_id         = "0x1A2B3C4D5E6F0003"
sync_interval      = 300
```

…and store the password, **without** a trailing newline:

```sh
printf %s 'your-password' |
  gpg --batch --yes --trust-model always \
      --recipient '1A2B3C4D5E6F0003!' --encrypt \
      -o ~/.config/davpunk/credentials/work.gpg
chmod 600 ~/.config/davpunk/credentials/work.gpg
```

`printf %s`, not `echo`: `echo` appends a newline, which becomes part of the
password. DavPunk deliberately does not strip trailing whitespace from a
decrypted credential, because a password may legitimately end in it.

### Where your password is kept

Two options, per account, and neither is the beginner's one. The wizard asks
outright rather than picking for you, because they protect different things.

**GnuPG** (`credential_backend = "gpg"`) encrypts it to a key only you hold, in
`~/.config/davpunk/credentials/<id>.gpg`, mode 0600. A backup, a synced
directory or a stolen disk gives nothing away. The cost: background sync only
works while `gpg-agent` still holds your passphrase, and you need a key to
begin with.

**Login keyring** (`credential_backend = "keyring"`) hands it to your desktop's
Secret Service — gnome-keyring, KWallet, KeePassXC — which PAM opens when you
log in. Nothing to set up and the sync daemon just works. The cost: once the
keyring is open, anything running as you can ask it for the password.

```toml
[[davpunk.remotes]]
id                 = "work"
url                = "https://cal.example.com/dav/"
username           = "user@example.com"
credential_backend = "keyring"      # and no gpg_key_id — it would encrypt nothing
```

Set the password from the wizard or **Edit → Preferences**; there is no
hand-written equivalent of the `gpg --encrypt` line above, but `secret-tool`
can inspect what was stored:

```sh
secret-tool search application davpunk
```

DavPunk **never unlocks the keyring itself.** A locked keyring is reported the
same way a cold `gpg-agent` is — the sync is skipped, once-an-hour warning, and
the UI says "credentials locked" — because a daemon that raises a password
prompt at you from the background is worse than one that waits.

Switching an account between the two always asks for the password again: it
will not silently decrypt what you stored under the other one. Afterwards it
offers to delete the copy left behind.

### Which GPG key

Name an **encryption subkey**, with a trailing `!`.

Given a primary key id, GnuPG picks an encryption subkey by its own rules. If
your key has more than one — common after a rotation, or with one subkey per
device — it may pick the one whose private half lives on your *other* machine.
Encryption then succeeds and decryption never does, so every sync fails
authentication with nothing explaining why.

The `!` pins one subkey. The first-run wizard and **Edit → Preferences** list
only subkeys this machine can actually decrypt with, already pinned, and say
why any others were skipped:

```
$ gpg --list-secret-keys --keyid-format LONG
ssb>  rsa4096/0x1A2B3C4D5E6F0003  [E]     ← on a smartcard: usable
ssb#  rsa4096/0x1A2B3C4D5E6F0005  [E]     ← "#": private half is elsewhere
```

`davpunk doctor` checks this by asking GnuPG what it would really encrypt to,
rather than guessing:

```
PASS gpg-key[work]  1A2B3C4D5E6F0003! encrypts to 1A2B3C4D5E6F0003 (on a smartcard)
```

If you later rotate that subkey, open Preferences and set the password again.

### Subtasks

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
editor's **List** and **Parent** pickers decide where it lands. Right-clicking
any task offers the same actions. Started from the board, the editor opens on
the status of the column you were in — right-click a card in "To Do" and the
new subtask is a "To Do" too, unless you change it before saving.

### Writing down a list you already have

**Ctrl+Shift+N** opens a plain text box instead: one line, one subtask, all of
them under the selection. It is for the moment you already know the five things
that need doing and want them written down before you forget the third — the
full editor, twelve fields at a time, is for afterwards.

Paste from anywhere. Bullets, `- [ ]` boxes and `1.` numbering are stripped, so
a list copied out of a mail or a README arrives as titles rather than as titles
with punctuation in front of them. Blank lines are skipped, and the dialog says
how many subtasks Save will actually create before you press it. Indentation is
ignored: every line becomes a direct child of the one task you picked.

### Moving a task when a filter is in the way

A drag needs both tasks on screen, and a filter is precisely what stops that.
So **Ctrl+X** cuts the selection and **Ctrl+V** pastes it under whatever is
selected then — change the filter in between, and the two never have to be
visible together. Paste with nothing selected to make the task top-level.

A cut is a move, not a copy: the clipboard empties once it lands, and it never
reaches the system clipboard. Pasting into another list is a real move, so
DavPunk asks first and offers the same "move subtasks too" question the **m**
dialog does.

### Deleting

**d d**, *Edit → Delete task*, or the right-click menu. Subtasks are not deleted
with their parent by default — they become top-level tasks — and the
confirmation offers **Delete subtasks as well** when there are any. Both trees
take a multiple selection, so delete, cut and move act on all of it.

### Moving to another list

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

### Columns, and having fewer of them

The default board is **To Do · Needs Action · In Progress · Done · Cancelled**.
"To Do" is the tasks with no STATUS at all — the pool you pick from — and
"Needs Action" the ones somebody has picked. They used to share a column, which
lost exactly the distinction the board is for. Dropping a card back on "To Do"
clears its status again.

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

### Folding a column

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

### Finished work

Completed and cancelled tasks are hidden by default, in the list *and* on the
board. **View → Show completed** brings them back for as long as you leave it
ticked; it is not saved, so it starts from `show_completed` in the config every
time. Search ignores it — finding something you know you finished is one of the
things search is for.

### Filtering the board

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
[HLD.md §16](HLD.md).

### When a task changed in two places

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

## Changing settings later

Everything from the wizard is editable at any time under **Edit → Preferences**:
accounts, the encryption key, the password, the default view, and which
permissions the MCP server exposes. **Help → Run diagnostics** is `davpunk
doctor` without leaving the app.

Configuration is read once at startup, so changes take effect on the next
start — DavPunk will not reconfigure a sync that is already running.

## Run

```sh
davpunk                      # the UI (single instance; --new-instance to override)
davpunk sync [--remote ID]   # one sync cycle, then a summary
davpunk status               # per-remote: last sync, pending / blocked / conflicts
davpunk conflicts            # list open conflicts (resolution is a UI action)
davpunk forget ID            # drop an orphaned account and its cached tasks
davpunk doctor [--fix]       # every runtime assumption, checked
davpunk-sync                 # the background sync daemon
davpunk-mcp                  # the MCP server (all capabilities off by default)
```

### Removing an account

Deleting an account's `[[davpunk.remotes]]` block does **not** remove its cached
tasks. DavPunk hides the account rather than purging it, so a typo in
`config.toml` cannot cost you data — `davpunk status` keeps listing it as
`[orphaned]`, with whatever error it last saw.

To get rid of it for good, once you are sure:

```sh
davpunk forget ox-privat      # prompts, and asks you to type the id back
davpunk forget ox-privat --yes
```

It refuses an account that is still in `config.toml`, since every process
re-creates its configured remotes at startup and it would simply come back.

### Trying a sync without doing it

```sh
davpunk sync --dry-run [--remote ID] [--limit N]
```

It reports every request it *would* send to your server and every row it
*would* change in your cache, and writes neither. **File → Preview sync…** is
the same thing in the UI, with a *Sync now* button if you like what you see.

```
ox: 8 calendar(s)
  to the server: 2 request(s)
    CREATE Buy milk           (/dav/ox/einkaufen/a1b2.ics, 412 bytes)
    UPDATE Fix the gate       (/dav/ox/haus/c3d4.ics, 508 bytes)
  to the local cache: 12 new, 3 updated, 0 removed, 1 conflict(s)
    new      Bagger ausleihen
    upd      Regentonne  [summary, due_value]
    CONFLICT Kraut und Rüben
```

The read side is real — discovery, ETags and resource fetches all happen — so a
wrong password, a vanished collection or an unparseable task shows up in a dry
run exactly as it would in a real one. What it cannot predict is how the server
answers a write it never sent: an ETag collision or a quota rejection is only
knowable by writing.

Two things make the promise structural rather than a matter of remembering to
check a flag: the CalDAV client is wrapped so `PUT` and `DELETE` cannot reach
the network, and the cycle runs against a throwaway copy of the database, so
your real cache is never opened for writing. It is otherwise the *same* code
path as `davpunk sync` — the same runner, the same engine — rather than a
second implementation that could drift.

If a collection is skipped because its ctag has not moved, the report says so.
"Nothing to do" and "I did not look" must not read the same.

### Background syncing

```sh
./systemd/install.sh
```

The daemon is the **only** thing that syncs on a timer. The UI does not sync by
itself: it polls the local cache so it shows what the daemon pulls in, but it
contacts a server only when you ask it to — *Sync now*, `Ctrl+R`, or *File →
Preview sync…*. Without the daemon installed, nothing reaches your server until
you press something.

The daemon can only work while it can read your passwords: `gpg-agent` still
holding the passphrase, or the login keyring still open. It never forces a
pinentry and never unlocks the keyring — it skips the cycle, warns once an
hour, and the UI says "credentials locked". `davpunk doctor` tells you when
that is the problem.

`loginctl enable-linger` makes this worse for both backends rather than better:
a daemon running with no login session has no warm agent, no unlocked keyring,
and often no session bus at all.

#### Excluding an account

```toml
[[davpunk.remotes]]
id        = "work"
auto_sync = false          # default: true
```

or untick **Automatic sync** for that account in the wizard or under **Edit →
Preferences**. The daemon then leaves that account alone; *Sync now*, `davpunk
sync` and MCP `sync_now` all still work on it, because those are things you
asked for. `davpunk status` marks such an account `[manual only]`, so a
months-old "last sync" does not read as a fault.

### MCP

Every capability is **off by default**, and DavPunk exposes only the tools you
have granted — a disabled tool is absent rather than present-and-refusing, so an
agent never burns a turn discovering it is not allowed.

**1. Turn it on.** Either in **Edit → Preferences → AI access**, or in
`config.toml`:

```toml
[davpunk.mcp]
enabled   = true
transport = "stdio"          # stdio (recommended) | sse
audit     = true             # record every write/delete call in the database

[davpunk.mcp.capabilities]
read   = true                # list_calendars, list_tasks, get_task, search_tasks
write  = false               # create_task, create_tasks, update_task, set_status,
                             # set_progress, move_task
delete = false               # delete_task
sync   = false               # sync_now, sync_status
```

Grant only what you need. `delete` is separate from `write` on purpose: an
assistant that tidies your task text has no reason to be able to remove
anything. Restart DavPunk afterwards — configuration is read once at startup.

**2. Point your client at it.** For anything that speaks stdio (Claude Desktop,
Claude Code, …):

```json
{
  "mcpServers": {
    "davpunk": {
      "command": "davpunk-mcp"
    }
  }
}
```

Use an absolute path if the binary is not on the client's `PATH` —
`~/.nix-profile/bin/davpunk-mcp` after `nix profile install`, or
`/path/to/DavPunk/.venv/bin/davpunk-mcp` with uv. In Claude Code:

```sh
claude mcp add davpunk -- davpunk-mcp
```

**3. Check it.** The tool list should match the capabilities you enabled:

```sh
davpunk-mcp --log-level DEBUG     # logs to stderr; stdout is the protocol
```

#### SSE

Only if your client cannot speak stdio. It binds `127.0.0.1` and nothing else —
a non-loopback `bind` is rejected at config load — and always requires a bearer
token:

```toml
[davpunk.mcp]
enabled   = true
transport = "sse"
port      = 8787
```

```sh
davpunk-mcp --print-token      # generate (on first use) and print it
davpunk-mcp --rotate-token     # replace it; old clients stop working
```

The client sends it as `Authorization: Bearer <token>`; anything else gets a
401. All of this is also in **Edit → Preferences → AI access**, which can show,
copy and regenerate the token for you.

##### Encrypting the token at rest

By default the token is a plain file, mode 0600. It can instead go wherever a
CalDAV password can — pick one under *Token at rest* in Preferences, or set it
directly:

```toml
[davpunk.mcp]
token_backend    = "gpg"                 # file | gpg | keyring
token_gpg_key_id = "1A2B3C4D5E6F0003!"   # gpg only; same form as a remote's
```

With `gpg` the token lives in `mcp-token.gpg`; with `keyring` it is not on disk
at all. Either protects it if the config directory is ever backed up or synced
somewhere it should not be, at the same cost as a CalDAV credential: the server
starts only once it can read the token back. A cold agent or a locked keyring
is reported as such rather than as a bad token — and never causes a *second*
token to be minted, which would silently invalidate the one your client holds.
`davpunk doctor` checks it:

```
PASS mcp-token  …/mcp-token.gpg is readable (encrypted to 1A2B3C4D5E6F0003!)
```

The token lives beside `config.toml` unless you set `token_file`.

#### Addressing

Tools take and return an opaque `task_ref` of the form `<calendar_id>:<uid>`.
It survives syncs, restarts and cache rebuilds, because `calendar_id` is derived
from the remote and collection rather than minted at discovery. The local row id
is never exposed. Call `list_calendars` for the `calendar_id` values.

## Development

```sh
nix develop          # Qt, Radicale, gpg and every dependency, already wired up
pytest               # the whole suite
pytest -m radicale   # only the tests that spawn a real Radicale
ruff check src tests
nix flake check      # builds both packages and lints
```

Or with uv, if you would rather not use Nix:

```sh
uv run pytest
uv run ruff check src tests
```

The Qt tests run offscreen (`QT_QPA_PLATFORM=offscreen`, which the dev shell
sets for you) and skip themselves when PySide6 cannot initialise, so the rest of
the suite runs on a machine with no display. Outside the dev shell PySide6 needs
`libGL` and friends on `LD_LIBRARY_PATH`; inside it, that is handled.

The suite passes on both dependency sets it is expected to meet — Python 3.12
with `mcp` 2.x under uv, and Python 3.14 with `mcp` 1.x from nixpkgs.

## Notable implementation choices

Two deviations from the plan's suggested dependency list, both deliberate:

- **`requests` rather than the `caldav` library.** The sync protocol needs
  direct control over `If-None-Match`, `If-Match`, the `Location` header,
  per-href statuses inside a 207, and `calendar-multiget` batching — none of
  which the higher-level abstraction exposes.
- **`jeepney` rather than `dbus-python`.** `dbus-python` needs a C toolchain and
  `pkg-config` for `dbus-1`; `jeepney` is pure Python and speaks the same
  `org.freedesktop.Notifications` interface. There is a `notify-send` fallback
  behind it.

## Licence

MIT — see [LICENSE](LICENSE).
