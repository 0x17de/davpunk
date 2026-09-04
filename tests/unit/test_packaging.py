"""Packaging: entrypoints, modes, and the systemd unit.  [Step 18]"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The D-Bus name, the .desktop basename, the icon name and the Wayland app_id
# are all this one string, and the window only gets its icon while they agree.
APP_ID = "de.zeroxseventeen.DavPunk"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


# --------------------------------------------------------------- entrypoints


def test_the_three_documented_scripts_are_declared(pyproject):
    scripts = pyproject["project"]["scripts"]
    assert scripts == {
        "davpunk": "davpunk.__main__:main",
        "davpunk-sync": "davpunk.daemon.sync_daemon:main",
        "davpunk-mcp": "davpunk.mcp.server:main",
    }


@pytest.mark.parametrize(
    "target",
    ["davpunk.__main__:main", "davpunk.daemon.sync_daemon:main", "davpunk.mcp.server:main"],
)
def test_every_entrypoint_resolves(target):
    module_name, _, attribute = target.partition(":")
    module = __import__(module_name, fromlist=[attribute])
    assert callable(getattr(module, attribute))


def test_the_runtime_floor_is_declared(pyproject):
    assert pyproject["project"]["requires-python"] == ">=3.11"


def test_the_ui_is_an_extra_not_a_hard_dependency(pyproject):
    """The CLI and the daemon must install on a machine with no Qt."""
    hard = " ".join(pyproject["project"]["dependencies"]).lower()
    assert "pyside6" not in hard
    assert "PySide6>=6.7" in pyproject["project"]["optional-dependencies"]["ui"][0]


def test_mcp_is_an_extra_too(pyproject):
    hard = " ".join(pyproject["project"]["dependencies"]).lower()
    assert not any(d == "mcp" or d.startswith("mcp>") for d in hard.split())
    assert pyproject["project"]["optional-dependencies"]["mcp"]


# ---------------------------------------------------------------- entrypoints


def _run(*argv, home: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DAVPUNK_HOME": str(home)},
        cwd=ROOT,
    )


@pytest.fixture
def config_file(davpunk_home) -> Path:
    from davpunk import paths

    paths.ensure_dir(paths.config_dir())
    path = paths.config_file()
    path.write_text(
        "[davpunk]\ntheme = 'dark'\n\n"
        "[[davpunk.remotes]]\n"
        "id = 'work'\nurl = 'https://cal.example.test/dav/'\nusername = 'u'\n"
    )
    path.chmod(0o600)
    return path


def test_davpunk_status_runs_as_a_module(davpunk_home, config_file):
    result = _run("davpunk", "--config", str(config_file), "status", home=davpunk_home)
    assert result.returncode == 0, result.stderr
    assert "work" in result.stdout


def test_davpunk_doctor_runs_as_a_module(davpunk_home, config_file):
    result = _run("davpunk", "--config", str(config_file), "doctor", home=davpunk_home)
    assert "python" in result.stdout
    assert result.returncode in (0, 1)  # 1 when a check legitimately fails


def test_the_daemon_runs_one_pass_and_exits(davpunk_home, config_file):
    result = _run(
        "davpunk.daemon.sync_daemon", "--config", str(config_file), "--once", home=davpunk_home
    )
    assert result.returncode == 0, result.stderr


def test_the_mcp_server_refuses_to_start_when_disabled(davpunk_home, config_file):
    result = _run("davpunk.mcp.server", "--config", str(config_file), home=davpunk_home)
    assert result.returncode == 2
    assert "enabled = true" in result.stderr


def test_no_entrypoint_writes_to_stdout_when_logging(davpunk_home, config_file):
    """stdout is the MCP stdio protocol channel."""
    result = _run(
        "davpunk.daemon.sync_daemon", "--config", str(config_file), "--once", home=davpunk_home
    )
    assert result.stdout == ""
    assert result.stderr  # the log went to stderr, where it belongs


def test_mcp_print_token_is_the_only_stdout_writer(davpunk_home, config_file):
    result = _run(
        "davpunk.mcp.server", "--config", str(config_file), "--print-token", home=davpunk_home
    )
    assert result.returncode == 0
    assert result.stdout.strip()
    assert "INFO" not in result.stdout  # a token, not a log line


# ---------------------------------------------------------------- file modes


def test_the_credentials_directory_is_created_0700(davpunk_home):
    from davpunk import paths

    paths.ensure_runtime_dirs()
    assert paths.credentials_dir().stat().st_mode & 0o077 == 0


def test_the_mcp_token_is_created_0600(davpunk_home):
    from davpunk import paths
    from davpunk.mcp.server import ensure_token

    ensure_token(paths.mcp_token_file())
    assert paths.mcp_token_file().stat().st_mode & 0o077 == 0


def test_lock_files_are_created_0600(davpunk_home):
    from davpunk import paths
    from davpunk.core.locking import remote_sync_lock

    with remote_sync_lock("work"):
        assert paths.remote_lock_file("work").stat().st_mode & 0o077 == 0


# ------------------------------------------------------------------ systemd


@pytest.fixture(scope="module")
def unit_text() -> str:
    return (ROOT / "systemd" / "davpunk-sync.service").read_text()


def test_the_unit_uses_the_installed_script(unit_text):
    assert "davpunk-sync" in unit_text


def test_the_unit_honours_the_shutdown_contract(unit_text):
    """SIGTERM sets the cancel event; the current item finishes."""
    assert "KillSignal=SIGTERM" in unit_text
    assert "TimeoutStopSec=30" in unit_text


def test_the_unit_documents_the_gpg_agent_caveat(unit_text):
    """"""
    assert "gpg-agent.conf" in unit_text
    assert "default-cache-ttl" in unit_text
    assert "enable-linger" in unit_text


def test_the_unit_routes_stderr_to_the_journal_and_discards_stdout(unit_text):
    assert "StandardError=journal" in unit_text
    assert "StandardOutput=null" in unit_text


def test_the_unit_is_a_user_unit(unit_text):
    assert "WantedBy=default.target" in unit_text
    assert "%h" in unit_text  # $HOME expansion, i.e. per-user


def test_the_installer_is_executable():
    installer = ROOT / "systemd" / "install.sh"
    assert installer.exists()
    assert installer.stat().st_mode & 0o111


# ------------------------------------------------------------------- README


def test_the_readme_documents_every_command():
    readme = (ROOT / "README.md").read_text()
    for command in ("davpunk sync", "davpunk status", "davpunk conflicts", "davpunk doctor"):
        assert command in readme
    assert "davpunk-sync" in readme
    assert "davpunk-mcp" in readme


def test_the_hld_exists_and_covers_the_schema_revision():
    hld = (ROOT / "HLD.md").read_text()
    assert "rev 8" in hld or "revision **8**" in hld
    assert "task_ref" in hld


# ---------------------------------------------------------------------- nix


@pytest.fixture(scope="module")
def flake_text() -> str:
    return (ROOT / "flake.nix").read_text()


def test_the_flake_exists_and_is_locked():
    assert (ROOT / "flake.nix").exists()
    assert (ROOT / "flake.lock").exists(), "an unlocked flake is not reproducible"


def test_the_flake_exposes_both_package_variants(flake_text):
    """A headless server should not have to build Qt."""
    assert "davpunk-headless" in flake_text
    assert "withUi = false" in flake_text
    assert "withMcp = false" in flake_text


def test_the_flake_exposes_the_modules(flake_text):
    assert "nixosModules" in flake_text
    assert "homeModules" in flake_text
    # The name home-manager used before `homeModules`; configs still import it.
    assert "homeManagerModules" in flake_text


def test_the_flake_exposes_an_overlay(flake_text):
    """`home-manager.useGlobalPkgs` hands home modules the *system's* pkgs, so
    that is where DavPunk has to be reachable from."""
    assert "overlays.default" in flake_text


def test_the_dev_shell_sets_up_qt(flake_text):
    """PySide6 dlopen()s libGL at import; without this the UI tests do not run
    at all on a non-FHS system."""
    assert "LD_LIBRARY_PATH" in flake_text
    assert "libglvnd" in flake_text
    assert "QT_QPA_PLATFORM" in flake_text


def test_the_dev_shell_provides_radicale(flake_text):
    """The integration tests import it, and nixpkgs only ships it as an app."""
    assert "toPythonModule pkgs.radicale" in flake_text


def test_the_package_declares_gnupg_as_a_runtime_dependency():
    """GnuPG is a binary dependency, not an optional nicety: DavPunk cannot
    read a single credential without it."""
    text = (ROOT / "nix" / "davpunk.nix").read_text()
    assert "gnupg" in text
    assert "wrapProgram" in text


def test_the_package_wraps_the_ui_for_qt():
    text = (ROOT / "nix" / "davpunk.nix").read_text()
    assert "wrapQtAppsHook" in text
    # wrapQtAppsHook reads qtPluginPrefix off qtbase and fails without it.
    assert "qt6.qtbase" in text
    # qtbase carries no SVG support, and the app icon is an SVG: without the
    # qtsvg imageformats plugin QIcon renders it to a null icon.
    assert "qt6.qtsvg" in text


# ------------------------------------------------------------- desktop entry


@pytest.fixture(scope="module")
def desktop_entry() -> dict[str, str]:
    text = (ROOT / "share" / f"{APP_ID}.desktop").read_text()
    assert text.startswith("[Desktop Entry]\n")
    return dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )


def test_the_desktop_entry_is_named_after_the_bus_name():
    """Qt takes the Wayland app_id from the .desktop basename, and the entry
    only ever gets matched to the window if the two agree."""
    from davpunk.ui.app import BUS_NAME

    assert BUS_NAME == APP_ID
    assert (ROOT / "share" / f"{APP_ID}.desktop").exists()


def test_the_desktop_entry_points_at_the_ui_and_its_icon(desktop_entry):
    assert desktop_entry["Type"] == "Application"
    assert desktop_entry["Name"] == "DavPunk"
    assert desktop_entry["Exec"] == "davpunk"  # the nix build absolutises this
    assert desktop_entry["Terminal"] == "false"
    assert desktop_entry["Icon"] == APP_ID
    # X11 has no app_id; the launcher matches the window by WM_CLASS instead.
    assert desktop_entry["StartupWMClass"] == APP_ID


def test_the_icon_is_scalable_and_self_contained():
    """A launcher renders it at whatever size it likes, and an icon that
    referenced anything outside itself would render as a blank."""
    svg = (ROOT / "share" / f"{APP_ID}.svg").read_text()
    assert "viewBox" in svg
    assert "<image" not in svg
    assert "href" not in svg


def test_the_ui_claims_the_desktop_entry():
    """Without this the window comes up with a generic icon under Wayland."""
    text = (ROOT / "src" / "davpunk" / "ui" / "app.py").read_text()
    assert "setDesktopFileName(BUS_NAME)" in text
    assert "QIcon.fromTheme(BUS_NAME)" in text


def test_the_package_installs_the_entry_only_with_the_ui():
    """A daemon-only host has no UI to launch."""
    text = (ROOT / "nix" / "davpunk.nix").read_text()
    assert "postInstall = lib.optionalString withUi" in text
    assert f"$out/share/applications/{APP_ID}.desktop" in text
    assert f"$out/share/icons/hicolor/scalable/apps/{APP_ID}.svg" in text
    # A typo'd entry is invisible rather than broken, so it gets checked.
    assert "desktop-file-validate" in text


@pytest.mark.parametrize("module", ["nixos-module.nix", "home-module.nix"])
def test_each_module_keeps_the_shutdown_contract(module):
    """The unit has to outlast one item finishing."""
    text = (ROOT / "nix" / module).read_text()
    assert 'KillSignal = "SIGTERM"' in text
    assert "TimeoutStopSec = 30" in text
    assert 'StandardOutput = "null"' in text


@pytest.mark.parametrize("module", ["nixos-module.nix", "home-module.nix"])
def test_each_module_installs_a_user_service(module):
    """Config, cache and credentials are per-user, and the daemon needs that
    user's gpg-agent to decrypt anything."""
    text = (ROOT / "nix" / module).read_text()
    assert "systemd.user.services.davpunk-sync" in text


def test_the_nix_entry_points_are_documented():
    """The flake outputs live in docs/nix.md, which the README links to."""
    assert "docs/nix.md" in (ROOT / "README.md").read_text()
    nix_doc = (ROOT / "docs" / "nix.md").read_text()
    assert "nix develop" in nix_doc
    assert "nixosModules.default" in nix_doc
    assert "homeModules.default" in nix_doc
    assert "overlays.default" in nix_doc
