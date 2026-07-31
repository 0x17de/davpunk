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

Folds stick. A subtree starts closed and stays however you left it, across
refreshes and restarts of the view; a folded parent shows how many tasks it is
hiding, so `kaufen  (262)` tells you what you are about to open. **z R** unfolds
everything, **z M** folds it all back, both also under **View**.

On the board a column is a *status* and nesting is a *relation*, so the two are
independent: a parent in "In Progress" whose subtasks are all done shows no
children there — they are in the Done column, nested under nothing. That is the
data, not a display bug.

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
davpunk doctor [--fix]       # every runtime assumption, checked
davpunk-sync                 # the background sync daemon
davpunk-mcp                  # the MCP server (all capabilities off by default)
```

### Background syncing

```sh
./systemd/install.sh
```

The daemon is optional — the UI syncs on its own while it is open. Install it if
you want tasks to keep syncing when the UI is closed. It can only work while
`gpg-agent` still holds your passphrase; `davpunk doctor` will tell you when
that is the problem.

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
