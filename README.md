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
| GnuPG | `gpg` on `PATH`, with a running `gpg-agent` |

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
| `packages.davpunk` | the full app — UI, MCP and notifications |
| `packages.davpunk-headless` | no Qt, no MCP: for a server that only runs the daemon |
| `apps.default` / `apps.sync` / `apps.mcp` | `davpunk`, `davpunk-sync`, `davpunk-mcp` |
| `devShells.default` | the test environment, including a real Radicale |
| `nixosModules.default` | `programs.davpunk.*` |
| `homeManagerModules.default` | `services.davpunk.*` |

**NixOS**, in your flake:

```nix
{
  inputs.davpunk.url = "github:mh/DavPunk";

  outputs = { nixpkgs, davpunk, ... }: {
    nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
      modules = [
        davpunk.nixosModules.default
        {
          programs.davpunk.enable = true;
          programs.davpunk.daemon.enable = true;   # optional background sync
        }
      ];
    };
  };
}
```

The daemon is a systemd **user** service, not a system one: the config, cache
and GnuPG-encrypted credentials are all per-user, and it needs that user's
`gpg-agent` to decrypt anything at all. Users start it themselves with
`systemctl --user enable --now davpunk-sync`, or you set
`programs.davpunk.daemon.autoStart = true`.

**Home Manager**:

```nix
{
  imports = [ davpunk.homeManagerModules.default ];

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
that the wizard or `gpg --encrypt` writes.

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
(with `https://` enforced), a GPG key picker, a password prompt, and a
`config.toml` written 0600 through `tomlkit` so any comments you add later
survive. Every field has a `?` beside it — hover for what it means and what
happens if you get it wrong.

To set it up by hand instead, write `~/.config/davpunk/config.toml`:

```toml
[davpunk]
theme          = "dark"
default_view   = "list"      # list | kanban
show_completed = false

[[davpunk.remotes]]
id            = "work"
name          = "Work"
url           = "https://cal.example.com/dav/"
username      = "user@example.com"
gpg_key_id    = "0x1A2B3C4D5E6F0003"
sync_interval = 300
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

Folds stick. A subtree starts closed and stays however you left it, across
refreshes and restarts of the view; a folded parent shows how many tasks it is
hiding, so `kaufen  (262)` tells you what you are about to open. **z R** unfolds
everything, **z M** folds it all back, both also under **View**.

On the board a column is a *status* and nesting is a *relation*, so the two are
independent: a parent in "In Progress" can have its subtasks scattered across
every other column. Where a subtask lands without its parent, the parent comes
along **greyed out** above it — you can see what the task hangs under without
that row pretending to be a card in this column. Grey rows are scaffolding:
they cannot be selected, dragged or ticked, because the task they name is
somewhere else. The same happens in the list view when a subtask is due today
and its parent next week.

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

The filter is not saved. It is how you are looking at the board right now, not a
setting — the same reasoning as *show completed*.

The full config reference — kanban columns, key bindings, MCP — is in
[HLD.md §16](HLD.md).

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

The daemon can only work while `gpg-agent` still holds your passphrase;
`davpunk doctor` will tell you when that is the problem.

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
write  = false               # create_task, update_task, set_status, set_progress, move_task
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

By default the token is a plain file, mode 0600. It can instead be
GnuPG-encrypted, the same way CalDAV passwords are — pick a key under *Token at
rest* in Preferences, or set it directly:

```toml
[davpunk.mcp]
token_gpg_key_id = "1A2B3C4D5E6F0003!"   # same form as a remote's gpg_key_id
```

The token then lives in `mcp-token.gpg` and is decrypted when the server
starts. That protects it if the file is ever backed up or synced somewhere it
should not be — at the same cost as a CalDAV credential: the server can only
start while `gpg-agent` still holds the passphrase. A cold agent is reported as
such rather than as a bad token, and `davpunk doctor` checks it:

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

MIT
