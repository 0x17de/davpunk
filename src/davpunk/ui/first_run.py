"""The first-run wizard.

Shown when the config file is missing, unparseable, or has no remotes.  An
unparseable config shows the Pydantic validation error **verbatim**, naming the
file and the offending key, and does not crash.

The config is written with ``tomlkit`` so an existing file keeps its comments
and formatting — this is the one place DavPunk writes config, and the sole
exception to "config is read once at startup".
"""

from __future__ import annotations

import logging
from pathlib import Path

import tomlkit
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from davpunk import paths
from davpunk.core import credentials
from davpunk.ui.remote_form import RemoteForm

log = logging.getLogger("davpunk.ui.first_run")


def needs_first_run(config, error: Exception | None = None) -> bool:
    return error is not None or not config.remotes


class ConfigErrorDialog(QMessageBox):
    """The Pydantic message, verbatim.  Nothing is guessed or summarised."""

    def __init__(self, error: Exception, path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(QMessageBox.Icon.Critical)
        self.setWindowTitle("Configuration problem")
        self.setText(f"DavPunk could not read {path}.")
        self.setDetailedText(str(error))
        self.setInformativeText(
            "Fix the file and restart, or continue to add a remote from scratch."
        )


class WelcomePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Welcome to DavPunk")
        self.setSubTitle("Work it. Sync it. Check it. Done.")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "DavPunk keeps your CalDAV task lists in a local cache, so every\n"
                "edit lands instantly and syncs afterwards.\n\n"
                "To add your first account you will need:\n"
                "  •  your CalDAV server's address\n"
                "  •  the username and password you log in with\n"
                "  •  a GPG key, which DavPunk uses to encrypt that password\n\n"
                "Hover the ? beside any field on the next page to see what it\n"
                "means, and change all of it later under Edit → Preferences."
            )
        )


class RemotePage(QWizardPage):
    """Remote details.  The fields, their explanations and the validation all
    live in :class:`~davpunk.ui.remote_form.RemoteForm`, so the wizard and the
    preferences dialog cannot drift apart."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Add an account")
        self.setSubTitle("Where your tasks live. Hover the ? beside a field to see what it means.")

        layout = QVBoxLayout(self)
        self.form = RemoteForm()
        self.form.validityChanged.connect(self.completeChanged.emit)
        layout.addWidget(self.form)

    def isComplete(self) -> bool:
        return self.form.is_valid()

    def values(self) -> dict[str, object]:
        return self.form.values()

    # Kept so existing callers and tests can reach the widgets directly.
    @property
    def url(self):
        return self.form.url

    @property
    def remote_id(self):
        return self.form.remote_id

    @property
    def username(self):
        return self.form.username

    @property
    def gpg_key(self):
        return self.form.gpg_key

    @property
    def problem(self):
        return self.form.problem_label


class FirstRunWizard(QWizard):
    def __init__(self, config_path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DavPunk — first run")
        self.config_path = config_path or paths.config_file()
        self.remote_page = RemotePage()
        self.addPage(WelcomePage())
        self.addPage(self.remote_page)

    def accept(self) -> None:
        values = self.remote_page.values()
        if not self._store_credential(values):
            return
        write_remote(self.config_path, values)
        super().accept()

    def _store_credential(self, values: dict) -> bool:
        """Qt password dialog → gpg stdin.  Never argv, never shell history."""
        password, ok = QInputDialog.getText(
            self,
            "CalDAV password",
            f"Password for {values['username']}:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return False

        key_id = values.get("gpg_key_id")
        if not key_id:
            QMessageBox.critical(
                self,
                "No encryption key",
                "DavPunk encrypts credentials with GnuPG, so it needs a key.\n\n"
                "Create one with `gpg --full-generate-key`, then run DavPunk again.",
            )
            return False

        try:
            credentials.store_credential(password, paths.credential_file(values["id"]), key_id)
        except credentials.CredentialError as exc:
            QMessageBox.critical(self, "Could not save the credential", str(exc))
            return False
        return True


def write_remote(config_path: Path, values: dict) -> Path:
    """Append a ``[[davpunk.remotes]]`` block, preserving comments."""
    config_path = Path(config_path).expanduser()
    paths.ensure_dir(config_path.parent)

    document = (
        tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    )
    davpunk = document.setdefault("davpunk", tomlkit.table(True))
    remotes = davpunk.get("remotes")
    if remotes is None:
        remotes = tomlkit.aot()
        davpunk["remotes"] = remotes

    entry = tomlkit.table()
    entry["id"] = values["id"]
    entry["name"] = values["name"]
    entry["url"] = values["url"]
    entry["username"] = values["username"]
    entry["gpg_file"] = str(paths.credential_file(values["id"]))
    if values.get("gpg_key_id"):
        entry["gpg_key_id"] = values["gpg_key_id"]
    if not values.get("auto_sync", True):
        entry["auto_sync"] = False
    entry["sync_interval"] = values["sync_interval"]
    if values.get("color"):
        entry["color"] = values["color"]
    remotes.append(entry)

    _atomic_write(config_path, tomlkit.dumps(document))
    return config_path


def _atomic_write(path: Path, text: str) -> None:
    """0600, and written through a temp file so an interrupted run cannot
    truncate an existing config."""
    import os

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


class DiscoveryPreview(QDialog):
    """Step 6: the calendars discovery found, for confirmation."""

    def __init__(self, calendars, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calendars found")
        layout = QVBoxLayout(self)
        if calendars:
            layout.addWidget(QLabel("DavPunk found these task lists:"))
            for row in calendars:
                layout.addWidget(QLabel(f"  • {row['display_name'] or row['href']}"))
        else:
            layout.addWidget(
                QLabel(
                    "No VTODO collections were found.\n"
                    "DavPunk only syncs collections that advertise VTODO support."
                )
            )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
