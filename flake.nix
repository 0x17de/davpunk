{
  description = "DavPunk — Work it. Sync it. Check it. Done. A CalDAV VTODO client";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      # ------------------------------------------------------------ packages

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          davpunk = pkgs.callPackage ./nix/davpunk.nix { };
        in
        {
          default = davpunk;
          inherit davpunk;

          # No Qt, no MCP: for a server that only runs the sync daemon.
          davpunk-headless = davpunk.override {
            withUi = false;
            withMcp = false;
          };
        }
      );

      # ------------------------------------------------------------ dev shell

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3;
        in
        {
          default = pkgs.mkShell {
            name = "davpunk-dev";

            packages = [
              pkgs.uv
              pkgs.gnupg
              pkgs.radicale # for the integration tests
              pkgs.ruff
              (python.withPackages (
                ps: with ps; [
                  # runtime
                  requests
                  icalendar
                  pydantic
                  pydantic-settings
                  tomlkit
                  tzdata
                  pyside6
                  mcp
                  jeepney
                  # dev
                  pytest
                  pytest-asyncio
                  responses
                  mypy
                  # so `import radicale` works in the integration tests
                  (ps.toPythonModule pkgs.radicale)
                ]
              ))
            ];

            # PySide6 dlopen()s libGL and friends at import time, and finds
            # nothing on a non-FHS system without this.  Getting it wrong is
            # the difference between "43 UI tests pass" and "ImportError:
            # libGL.so.1".
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (
              with pkgs;
              [
                libglvnd
                fontconfig
                freetype
                libx11
                libxcb
                libxext
                libxrender
                libxkbcommon
                dbus
                zlib
              ]
            );

            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              # The Qt tests skip themselves without a display; offscreen runs
              # them for real instead.
              export QT_QPA_PLATFORM=''${QT_QPA_PLATFORM:-offscreen}
              echo "DavPunk dev shell — python $(python3 --version | cut -d' ' -f2)"
              echo "  pytest          run the suite (Qt tests included, offscreen)"
              echo "  ruff check src tests"
            '';
          };
        }
      );

      # ---------------------------------------------------------------- checks

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          davpunk = self.packages.${system}.davpunk;
          davpunk-headless = self.packages.${system}.davpunk-headless;

          lint =
            pkgs.runCommand "davpunk-lint"
              {
                nativeBuildInputs = [ pkgs.ruff ];
              }
              ''
                # ruff wants to write a cache next to the sources, and the
                # store is read-only, so lint a copy.
                cp -r ${./.} source && chmod -R u+w source && cd source
                export RUFF_CACHE_DIR="$TMPDIR/ruff"
                ruff check src tests
                ruff format --check src tests
                touch $out
              '';
        }
      );

      # --------------------------------------------------------------- modules

      nixosModules.default = import ./nix/nixos-module.nix self;
      homeManagerModules.default = import ./nix/home-module.nix self;

      # ------------------------------------------------------------------ apps

      apps = forAllSystems (
        system:
        let
          davpunk = self.packages.${system}.davpunk;
        in
        {
          default = {
            type = "app";
            program = "${davpunk}/bin/davpunk";
          };
          sync = {
            type = "app";
            program = "${davpunk}/bin/davpunk-sync";
          };
          mcp = {
            type = "app";
            program = "${davpunk}/bin/davpunk-mcp";
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt-tree);
    };
}
