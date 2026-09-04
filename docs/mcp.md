# MCP — letting an agent work with your tasks

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

## SSE

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

### Encrypting the token at rest

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

## Addressing

Tools take and return an opaque `task_ref` of the form `<calendar_id>:<uid>`.
It survives syncs, restarts and cache rebuilds, because `calendar_id` is derived
from the remote and collection rather than minted at discovery. The local row id
is never exposed. Call `list_calendars` for the `calendar_id` values.

*Part of [DavPunk](../README.md).*
