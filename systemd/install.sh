#!/bin/sh
# Install the DavPunk sync daemon as a systemd *user* unit.
#
# The daemon is optional: the UI syncs on its own while it is open. Install it
# if you want tasks to keep syncing when the UI is closed.
set -eu

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SOURCE="$(cd "$(dirname "$0")" && pwd)/davpunk-sync.service"

mkdir -p "$UNIT_DIR"
install -m 0644 "$SOURCE" "$UNIT_DIR/davpunk-sync.service"

systemctl --user daemon-reload
systemctl --user enable --now davpunk-sync.service

cat <<'NOTE'

Installed and started davpunk-sync.service.

  systemctl --user status davpunk-sync
  journalctl --user -u davpunk-sync -f

The daemon can only sync while gpg-agent still holds your passphrase. If syncs
stop with "credentials locked", raise the agent's cache lifetimes in
~/.gnupg/gpg-agent.conf:

    default-cache-ttl 28800
    max-cache-ttl     86400

`davpunk doctor` checks this, among everything else.
NOTE
