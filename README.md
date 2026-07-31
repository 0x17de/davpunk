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

```sh
uv sync --all-extras      # everything
uv sync                   # CLI and daemon only — no Qt, no MCP
uv sync --extra ui        # add the desktop UI
uv sync --extra mcp       # add the MCP server
```

The UI and the MCP server are **extras** on purpose: a headless machine running
only the sync daemon should not need Qt installed.

## First run

Launch `davpunk` with no config and the first-run wizard opens: remote details
(with `https://` enforced), a GPG key picker, a password prompt, and a
`config.toml` written 0600 through `tomlkit` so any comments you add later
survive.

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
      --recipient 0x1A2B3C4D5E6F0003 --encrypt \
      -o ~/.config/davpunk/credentials/work.gpg
chmod 600 ~/.config/davpunk/credentials/work.gpg
```

`printf %s`, not `echo`: `echo` appends a newline, which becomes part of the
password. DavPunk deliberately does not strip trailing whitespace from a
decrypted credential, because a password may legitimately end in it.

The full config reference — kanban columns, key bindings, MCP — is in
[HLD.md §16](HLD.md).

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

All four capabilities are **off by default**. Turn on only what you want an
agent to be able to do:

```toml
[davpunk.mcp]
enabled   = true
transport = "stdio"          # stdio (recommended) | sse
audit     = true             # log every write/delete call

[davpunk.mcp.capabilities]
read   = true
write  = false               # create, update, set_*, move
delete = false
sync   = false
```

Tasks are addressed by an opaque `task_ref` (`<calendar_id>:<uid>`) that
survives syncs, restarts and cache rebuilds. The local row id is never exposed.

## Development

```sh
uv run pytest                # the whole suite
uv run pytest -m radicale    # only the tests that spawn a real Radicale
uv run ruff check src tests
```

The Qt tests run offscreen (`QT_QPA_PLATFORM=offscreen`) and skip themselves
when PySide6 cannot initialise, so the rest of the suite runs on a machine with
no display.

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
