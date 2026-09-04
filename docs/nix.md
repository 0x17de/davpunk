# Installing with Nix

```sh
nix run github:0x17de/davpunk          # just run the UI
nix profile install github:0x17de/davpunk
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
  url = "github:0x17de/davpunk";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Add `overlays.default` and both modules take their package from your `pkgs` —
your nixpkgs, your overlays, one evaluation. Without the overlay they still
work, they just build DavPunk against this flake's own locked nixpkgs instead.

**NixOS**, in your flake:

```nix
{
  inputs.davpunk.url = "github:0x17de/davpunk";

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

*Part of [DavPunk](../README.md).*
