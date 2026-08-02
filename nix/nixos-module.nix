self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.davpunk;

  # `pkgs.davpunk` when the consumer added `davpunk.overlays.default`, so the
  # package follows their nixpkgs and overlays.  Without it, this flake's own
  # locked nixpkgs gets evaluated to produce one.
  defaultPackage = pkgs.davpunk or self.packages.${pkgs.stdenv.hostPlatform.system}.davpunk;
in
{
  options.programs.davpunk = {
    enable = lib.mkEnableOption "the DavPunk CalDAV VTODO client";

    package = lib.mkOption {
      type = lib.types.package;
      default = defaultPackage;
      defaultText = lib.literalMD "`pkgs.davpunk`, or the flake's own build of it";
      description = "The DavPunk package to install system-wide.";
    };

    daemon.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install the sync daemon as a systemd **user** service for every user.

        It is a user service and not a system one on purpose: DavPunk's config,
        cache and GnuPG-encrypted credentials are all per-user, and the daemon
        needs that user's `gpg-agent` to decrypt anything at all.

        Users still have to start it themselves:

        ```
        systemctl --user enable --now davpunk-sync
        ```

        or set {option}`programs.davpunk.daemon.autoStart`.
      '';
    };

    daemon.autoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Start the daemon automatically for every user who logs in.

        Note that a user whose `gpg-agent` has no cached passphrase will see
        the daemon skip every remote with a rate-limited "credentials locked"
        warning until they unlock it.
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
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];

    # GnuPG is a runtime binary dependency for any account that uses it, and it
    # is the default for a new one.  mkDefault, so a host that keeps every
    # password in the login keyring can turn it off.
    programs.gnupg.agent.enable = lib.mkDefault true;

    # The other backend needs a Secret Service on the session bus.  Not forced
    # on: most desktop profiles already provide one (gnome-keyring, KWallet),
    # and turning it on here would be a second opinion about the user's
    # desktop.  `davpunk doctor` says plainly when nothing is answering.

    systemd.user.services.davpunk-sync = lib.mkIf cfg.daemon.enable {
      description = "DavPunk CalDAV sync daemon";
      documentation = [ "man:davpunk(1)" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = lib.optional cfg.daemon.autoStart "default.target";

      serviceConfig = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/davpunk-sync --log-level ${cfg.daemon.logLevel}";
        Restart = "on-failure";
        RestartSec = 30;

        # SIGTERM sets the cancel event; the current item finishes, the
        # per-remote flock releases, and the daemon exits 0.
        KillSignal = "SIGTERM";
        TimeoutStopSec = 30;

        # stdout is never a log sink in DavPunk.
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
    };
  };
}
