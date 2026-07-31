self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.davpunk;
  defaultPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.davpunk;
in
{
  options.services.davpunk = {
    enable = lib.mkEnableOption "the DavPunk CalDAV VTODO client";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalMD "the flake's `davpunk` package";
      description = "The DavPunk package to install.";
    };

    daemon.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Run the background sync daemon as a systemd user service.

        Optional: the UI syncs on its own while it is open. Enable this if you
        want tasks to keep syncing when the UI is closed.

        The daemon inherits the standard GnuPG environment and never forces a
        pinentry, so it can only sync while `gpg-agent` still holds the
        passphrase. See {option}`services.davpunk.daemon.gpgAgentTtls`.
      '';
    };

    daemon.logLevel = lib.mkOption {
      type = lib.types.enum [
        "DEBUG"
        "INFO"
        "WARNING"
        "ERROR"
      ];
      default = "INFO";
      description = "Log level for the daemon. Logs go to the journal.";
    };

    daemon.gpgAgentTtls = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Raise `gpg-agent`'s cache lifetimes so the daemon can keep syncing
        through a working day without a re-prompt.

        This is a security trade-off, so it is opt-in: it keeps your CalDAV
        password decryptable for as long as the agent is running.
      '';
    };

    settings = lib.mkOption {
      type = lib.types.nullOr (lib.types.attrsOf lib.types.anything);
      default = null;
      example = lib.literalExpression ''
        {
          davpunk = {
            theme = "dark";
            default_view = "list";
            remotes = [{
              id = "work";
              url = "https://cal.example.com/dav/";
              username = "user@example.com";
              gpg_key_id = "0x1A2B3C4D5E6F0003";
            }];
          };
        }
      '';
      description = ''
        Contents of `~/.config/davpunk/config.toml`.

        Leave this `null` (the default) to manage the file yourself — the
        first-run wizard writes it, and DavPunk reads it once at startup.
        Setting it makes the file read-only, which means the wizard cannot
        add a remote and you must declare them here instead.

        Credentials are never part of this: they live in GnuPG-encrypted files
        that the wizard or `gpg --encrypt` writes.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    # Written only when the user asked for declarative config; otherwise the
    # file stays writable so the first-run wizard can create it.
    xdg.configFile."davpunk/config.toml" = lib.mkIf (cfg.settings != null) {
      source = (pkgs.formats.toml { }).generate "davpunk-config.toml" cfg.settings;
    };

    services.gpg-agent = lib.mkIf cfg.daemon.gpgAgentTtls {
      enable = lib.mkDefault true;
      defaultCacheTtl = lib.mkDefault 28800; # 8 h
      maxCacheTtl = lib.mkDefault 86400; # 24 h
    };

    systemd.user.services.davpunk-sync = lib.mkIf cfg.daemon.enable {
      Unit = {
        Description = "DavPunk CalDAV sync daemon";
        After = [ "network-online.target" ];
        Wants = [ "network-online.target" ];
      };

      Service = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/davpunk-sync --log-level ${cfg.daemon.logLevel}";
        Restart = "on-failure";
        RestartSec = 30;

        # SIGTERM sets the cancel event; the current item finishes, the
        # per-remote flock releases, and the daemon exits 0.
        KillSignal = "SIGTERM";
        TimeoutStopSec = 30;

        # stdout is never a log sink in DavPunk — on the MCP stdio transport it
        # is the protocol channel.
        StandardOutput = "null";
        StandardError = "journal";

        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        LockPersonality = true;
      };

      Install.WantedBy = [ "default.target" ];
    };
  };
}
