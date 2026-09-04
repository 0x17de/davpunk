{
  lib,
  python3Packages,
  gnupg,
  qt6,
  makeWrapper,
  desktop-file-utils,
  withUi ? true,
  withMcp ? true,
  withNotifications ? true,
  # The login keyring backend, as an alternative to GnuPG.  On by default: it
  # is one small pure-Python package, and leaving it out means the option is
  # simply missing from the setup wizard with no explanation.
  withKeyring ? true,
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
    ++ lib.optional withNotifications jeepney
    ++ lib.optional withKeyring secretstorage;

  nativeBuildInputs = [
    makeWrapper
  ]
  ++ lib.optionals withUi [
    qt6.wrapQtAppsHook
    desktop-file-utils
  ];

  # wrapQtAppsHook reads qtPluginPrefix off qtbase; without it in buildInputs
  # it fails the build rather than silently producing an unwrapped binary.
  #
  # qtsvg is here for its imageformats plugin: without it QIcon renders an SVG
  # to nothing, so the scalable app icon loads as a null icon and the window
  # comes up blank in the switcher.  qtbase alone does not carry SVG support.
  buildInputs = lib.optionals withUi [
    qt6.qtbase
    qt6.qtsvg
  ];

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

  # The desktop entry and its icon ship only with the UI build — a headless
  # daemon host has no business advertising a launcher for a binary it does
  # not have.  Both come from share/ so the non-Nix install path can use the
  # same files.
  postInstall = lib.optionalString withUi ''
    install -Dm644 share/de.zeroxseventeen.DavPunk.desktop \
      $out/share/applications/de.zeroxseventeen.DavPunk.desktop
    install -Dm644 share/de.zeroxseventeen.DavPunk.svg \
      $out/share/icons/hicolor/scalable/apps/de.zeroxseventeen.DavPunk.svg

    # `Exec=davpunk` only resolves for someone who has it on PATH, which is not
    # a given for a system-wide install started by a launcher.
    substituteInPlace $out/share/applications/de.zeroxseventeen.DavPunk.desktop \
      --replace-fail 'Exec=davpunk' "Exec=$out/bin/davpunk"

    desktop-file-validate $out/share/applications/de.zeroxseventeen.DavPunk.desktop
  '';

  postFixup = ''
    # GnuPG is a runtime binary dependency for accounts that use it: DavPunk
    # shells out rather than using python-gnupg, because some of that library's
    # paths stage the plaintext through temp files.  An install where every
    # account keeps its password in the login keyring never invokes it — but
    # the two are chosen per account, so the binary comes along regardless.
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
    homepage = "https://github.com/0x17de/davpunk";
    license = lib.licenses.mit;
    mainProgram = "davpunk";
    platforms = lib.platforms.linux;
  };
}
