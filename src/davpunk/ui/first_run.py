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
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from davpunk import paths
from davpunk.core import credentials

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
                "Let's add your first remote."
            )
        )


class RemotePage(QWizardPage):
    """Remote details, with ``https://`` enforced and live validation."""

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Add a remote")
        self.setSubTitle("Where your tasks live.")

        form = QFormLayout(self)
        self.remote_id = QLineEdit("work")
        self.name = QLineEdit("Work")
        self.url = QLineEdit("https://")
        self.url.setPlaceholderText("https://cal.example.com/dav/")
        self.username = QLineEdit()
        self.interval = QSpinBox()
        self.interval.setRange(30, 86400)
        self.interval.setValue(300)
        self.interval.setSuffix(" s")
        self.color = QLineEdit("#4A9EFF")

        self.gpg_key = QComboBox()
        for key_id, uid in credentials.list_secret_keys():
            self.gpg_key.addItem(f"{uid}  ({key_id[-16:]})", key_id)
        if self.gpg_key.count() == 0:
            self.gpg_key.addItem("(no secret keys found — create one with gpg --gen-key)", None)

        self.problem = QLabel()
        self.problem.setStyleSheet("color: palette(link-visited)")

        form.addRow("Remote id", self.remote_id)
        form.addRow("Name", self.name)
        form.addRow("URL", self.url)
        form.addRow("Username", self.username)
        form.addRow("Sync interval", self.interval)
        form.addRow("Colour", self.color)
        form.addRow("GPG key", self.gpg_key)
        form.addRow("", self.problem)

        for widget in (self.remote_id, self.url, self.username):
            widget.textChanged.connect(self._revalidate)
        self._revalidate()

    def _revalidate(self) -> None:
        self.problem.setText(self._problem() or "")
        self.completeChanged.emit()

    def _problem(self) -> str | None:
        if not self.remote_id.text().strip():
            return "A remote id is required; it names the credential and lock files."
        url = self.url.text().strip()
        if url.startswith("http://"):
            return (
                "http:// sends your password in cleartext. Use https://, or set "
                "allow_insecure = true in config.toml if you really mean it."
            )
        if not url.startswith("https://") or len(url) <= len("https://"):
            return "The URL must start with https://"
        if not self.username.text().strip():
            return "A username is required."
        return None

    def isComplete(self) -> bool:
        return self._problem() is None

    def values(self) -> dict[str, object]:
        return {
            "id": self.remote_id.text().strip(),
            "name": self.name.text().strip() or self.remote_id.text().strip(),
            "url": self.url.text().strip(),
            "username": self.username.text().strip(),
            "sync_interval": self.interval.value(),
            "color": self.color.text().strip(),
            "gpg_key_id": self.gpg_key.currentData(),
        }


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
                "No GPG key",
                "DavPunk encrypts credentials with GnuPG. Create a key with "
                "`gpg --gen-key` and run the wizard again.",
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
