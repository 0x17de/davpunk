"""UI entrypoint: single instance, first run, recovery dialog, main window."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from davpunk import paths
from davpunk.config import ConfigError, DavPunkConfig, load_config
from davpunk.core import cache
from davpunk.core.locking import UiAlreadyRunning, ui_lock

log = logging.getLogger("davpunk.ui.app")

BUS_NAME = "de.zeroxseventeen.DavPunk"


def run(args) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    if args.new_instance:
        return _run_locked(args, None)

    try:
        with ui_lock() as fd:
            return _run_locked(args, fd)
    except UiAlreadyRunning:
        # A second launch raises and focuses the existing window, then exits 0.
        if _raise_existing_window():
            return 0
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(
            None,
            "DavPunk is already running",
            "Another DavPunk window is open. Use --new-instance to start a second one anyway.",
        )
        del app
        return 0


def _raise_existing_window() -> bool:
    """Ask the running instance to come forward, over D-Bus."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False

    address = DBusAddress("/de/zeroxseventeen/DavPunk", bus_name=BUS_NAME, interface=BUS_NAME)
    try:
        connection = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        connection.send_and_get_reply(new_method_call(address, "Raise", "", ()))
        return True
    except Exception as exc:
        log.debug("Could not raise the running instance: %s", exc)
        return False
    finally:
        connection.close()


def _run_locked(args, _lock_fd) -> int:
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName("DavPunk")
    app.setOrganizationName("DavPunk")

    config_path = Path(args.config).expanduser() if args.config else paths.config_file()
    config, config_error = _load(config_path)

    conn, report = cache.open_or_recover()
    if report is not None:
        # A blocking dialog naming the corrupt file, the recovery JSON, and
        # how much was recovered vs lost.
        QMessageBox.critical(None, "Task cache recovered", report.summary())

    if config_error is not None or not config.remotes:
        config = _first_run(args, config, config_error)
        if config is None:
            cache.close_db(conn)
            return 0

    cache.reconcile_remotes(config.remotes, conn)

    from davpunk.ui.main_window import MainWindow

    window = MainWindow(config, conn, paths.database_file(), config_path)
    window.show()
    return app.exec()


def _load(path) -> tuple[DavPunkConfig, ConfigError | None]:
    try:
        return load_config(path), None
    except ConfigError as exc:
        log.error("%s", exc)
        return DavPunkConfig(), exc


def _first_run(args, config, error) -> DavPunkConfig | None:
    from davpunk.ui.first_run import ConfigErrorDialog, FirstRunWizard

    config_path = Path(args.config).expanduser() if args.config else paths.config_file()
    if error is not None:
        ConfigErrorDialog(error, config_path).exec()

    wizard = FirstRunWizard(config_path)
    if wizard.exec() != FirstRunWizard.DialogCode.Accepted:
        return None

    # The one place the config is re-read after startup.
    try:
        return load_config(config_path)
    except ConfigError as exc:
        ConfigErrorDialog(exc, config_path).exec()
        return None
