# Passwords and keys

Where DavPunk keeps a CalDAV password, and how to pick a
GnuPG subkey that will still decrypt tomorrow. The first-run wizard and
**Edit → Preferences** do all of this for you; this is the reference behind it.

## Where your password is kept

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

## Which GPG key

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

*Part of [DavPunk](../README.md).*
