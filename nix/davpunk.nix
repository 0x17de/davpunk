{
  lib,
  python3Packages,
  gnupg,
  qt6,
  makeWrapper,
  withUi ? true,
  withMcp ? true,
  withNotifications ? true,
}:

python3Packages.buildPythonApplication {
  pname = "davpunk";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    src = ./..;
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      !(builtins.elem base [
        ".git"
        ".venv"
        "dist"
        "result"
        "__pycache__"
        ".pytest_cache"
        ".ruff_cache"
        ".mypy_cache"
      ]);
  };

  build-system = [ python3Packages.hatchling ];

  dependencies =
    with python3Packages;
    [
      requests
      icalendar
      pydantic
      pydantic-settings
      tomlkit
      tzdata
    ]
    ++ lib.optional withUi pyside6
    ++ lib.optional withMcp mcp
    ++ lib.optional withNotifications jeepney;

  nativeBuildInputs = [ makeWrapper ] ++ lib.optionals withUi [ qt6.wrapQtAppsHook ];

  # wrapQtAppsHook reads qtPluginPrefix off qtbase; without it in buildInputs
  # it fails the build rather than silently producing an unwrapped binary.
  buildInputs = lib.optionals withUi [ qt6.qtbase ];

  # Qt's own plugin discovery only happens for binaries wrapQtAppsHook can see;
  # the console scripts are Python, so they are wrapped manually below.
  dontWrapQtApps = true;

  # The suite spawns a real Radicale and drives Qt offscreen; neither belongs
  # in a package build.  `nix flake check` runs them instead.
  doCheck = false;

  pythonImportsCheck = [
    "davpunk"
    "davpunk.core.cache"
    "davpunk.core.sync_engine"
  ]
  ++ lib.optional withMcp "davpunk.mcp.tools";

  postFixup = ''
    # GnuPG is a runtime binary dependency: DavPunk shells out to it rather
    # than using python-gnupg, because some of that library's paths stage the
    # plaintext through temp files.
    for bin in $out/bin/davpunk $out/bin/davpunk-sync $out/bin/davpunk-mcp; do
      [ -e "$bin" ] || continue
      wrapProgram "$bin" --prefix PATH : ${lib.makeBinPath [ gnupg ]}
    done
  ''
  + lib.optionalString withUi ''
    # Only the UI entrypoint needs the Qt environment.
    wrapProgram $out/bin/davpunk ''${qtWrapperArgs[@]}
  '';

  meta = {
    description = "Work it. Sync it. Check it. Done. — a CalDAV VTODO client for Linux";
    longDescription = ''
      A power-user CalDAV VTODO client. Syncs with CalDAV servers (primarily
      Radicale), caches offline in SQLite, and optionally exposes an MCP server
      for AI agent integration. The UI targets density and keyboard-driven
      workflows.
    '';
    homepage = "https://github.com/mh/DavPunk";
    license = lib.licenses.mit;
    mainProgram = "davpunk";
    platforms = lib.platforms.linux;
  };
}
