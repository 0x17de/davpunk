# Security policy

DavPunk holds CalDAV passwords, talks to a server over the network, and can
expose your tasks to an AI agent over MCP. Reports about any of that are very
welcome.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting — the **Security** tab on
[0x17de/davpunk](https://github.com/0x17de/davpunk), then *Report a
vulnerability*. That opens a private thread with the maintainer and is the
preferred route. If you would rather use email, <0@0x17.de> reaches the same
person.

This is a one-maintainer project, so please do not expect a same-day reply.
I will acknowledge a report within a week, and I would rather hear about
something you are only half-sure of than not hear about it.

Useful things to include, as far as you have them: what an attacker has to
control to begin with (a hostile CalDAV server? a local process? a shared
machine?), what they get out of it, the DavPunk version, and `davpunk doctor`
output if it is relevant.

## Supported versions

DavPunk is at 0.1.0 and pre-1.0: fixes land on `main` and go out in the next
release. There are no maintained back-branches.

## What is in scope

DavPunk's threat model is a single-user Linux desktop, and the interesting
boundaries are these:

- **The CalDAV server is not trusted with more than your task data.** It
  already knows your password — you send it one — but a hostile or compromised
  server should not be able to make DavPunk leak that password to a *third*
  party, exhaust your memory, write outside its own directories, or execute
  anything. Responses are parsed defensively for that reason: hrefs are refused
  when they leave the account's origin, and XML carrying a DOCTYPE is refused
  rather than expanded.
- **Secrets stay out of reach of a casual read.** The plaintext of a CalDAV
  password or an MCP token is never written to disk by DavPunk, never placed in
  `argv`, never logged, and never stored in SQLite. Config, credential files
  and the token are 0600, their directory 0700, and `davpunk doctor --fix`
  restores that. A report that finds a secret somewhere it should not be is
  squarely in scope.
- **The MCP server is off by default, and so is each of its four
  capabilities.** SSE binds loopback only (enforced when the config loads, not
  merely by default) and requires a bearer token. A way to reach the tools
  without the token, to bind off-loopback, or to act beyond an enabled
  capability is in scope.

## What is not

- **Anything already running as your user.** It can read the SQLite cache, the
  config, and — while `gpg-agent` is warm or the login keyring is open — ask
  for the password itself. [docs/credentials.md](docs/credentials.md) says so
  plainly rather than implying a boundary that is not there.
- **`allow_insecure = true`.** Plaintext `http://` has to be turned on by hand
  and logs a warning every time. That it is interceptable is the documented
  consequence, not a vulnerability.
- **An agent doing something you gave it the capability to do.** `write`,
  `delete` and `sync` are off until you turn them on; the tools are the
  boundary, and an agent using an enabled one as designed is working. Being
  able to *escape* the enabled set is a different matter, and is in scope.
