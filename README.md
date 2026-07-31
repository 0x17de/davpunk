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
