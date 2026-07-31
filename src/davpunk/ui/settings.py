"""Settings: general preferences and the list of remotes.

Reachable from **Edit → Preferences** at any time, not only on first run.

Config is read once at startup and is authoritative for remotes, so
anything changed here is written to ``config.toml`` and takes effect on the
next start.  The dialog says so rather than pretending otherwise.
"""

from __future__ import annotations

import logging

import tomlkit
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from davpunk import paths
from davpunk.config import ConfigError, load_config
from davpunk.core import credentials
from davpunk.ui.remote_form import RemoteForm
from davpunk.ui.widgets import apply_help, help_label, hint

log = logging.getLogger("davpunk.ui.settings")

RESTART_NOTE = (
    "Changes are saved to config.toml and take effect the next time DavPunk "
    "starts. DavPunk reads its configuration once at startup so that a sync "
    "in progress can never be reconfigured underneath itself."
)


class RemoteDialog(QDialog):
    """Add or edit one remote, with the password prompt attached."""

    def __init__(self, values: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.editing = values is not None
        self.setWindowTitle("Edit account" if self.editing else "Add an account")
        self.setMinimumWidth(660)

        layout = QVBoxLayout(self)
        self.form = RemoteForm(values, editing=self.editing)
        layout.addWidget(self.form)

        self.set_password = QPushButton(
            "Change the stored password…" if self.editing else "Set the password…"
        )
        self.set_password.clicked.connect(self._prompt_password)
        apply_help(
            self.set_password,
            "The password is encrypted with the key above and written to its own "
            "file, mode 0600. Run this again after changing the key.",
        )
        layout.addWidget(self.set_password)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.form.validityChanged.connect(self._sync_ok)
        self._sync_ok()

    def _sync_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.form.is_valid())

    def _prompt_password(self) -> None:
        values = self.form.values()
        if not values["gpg_key_id"]:
            QMessageBox.critical(
                self,
                "No encryption key",
                "DavPunk encrypts credentials with GnuPG. Create a key with\n"
                "`gpg --full-generate-key` and reopen this dialog.",
            )
            return

        password, ok = QInputDialog.getText(
            self,
            "CalDAV password",
            f"Password for {values['username'] or values['id']}:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return

        try:
            credentials.store_credential(
                password, paths.credential_file(values["id"]), values["gpg_key_id"]
            )
        except credentials.CredentialError as exc:
            QMessageBox.critical(self, "Could not save the password", str(exc))
            return
        QMessageBox.information(
            self, "Password saved", f"Encrypted to {paths.credential_file(values['id'])}"
        )

    def values(self) -> dict:
        return self.form.values()


class SettingsDialog(QDialog):
    def __init__(self, config, config_path=None, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.config_path = config_path or paths.config_file()
        self.setWindowTitle("DavPunk preferences")
        self.setMinimumSize(680, 460)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._accounts_tab(), "Accounts")
        tabs.addTab(self._agent_tab(), "AI access")
        layout.addWidget(tabs)

        note = QLabel(RESTART_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ----------------------------------------------------------------- tabs

    def _general_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.theme = QComboBox()
        self.theme.addItems(["dark", "light"])
        self.theme.setCurrentText(self.config.theme)

        self.default_view = QComboBox()
        self.default_view.addItems(["list", "kanban"])
        self.default_view.setCurrentText(self.config.default_view)

        self.show_completed = QCheckBox("Show completed tasks on startup")
        self.show_completed.setChecked(self.config.show_completed)

        self.max_bytes = QSpinBox()
        self.max_bytes.setRange(4096, 16 * 1024 * 1024)
        self.max_bytes.setSingleStep(4096)
        self.max_bytes.setValue(self.config.max_resource_bytes)
        self.max_bytes.setSuffix(" bytes")

        form.addRow(
            help_label("Theme", "Which colour scheme DavPunk starts in."),
            apply_help(self.theme, "Which colour scheme DavPunk starts in."),
        )
        form.addRow(
            help_label("Default view", "Which view DavPunk opens on."),
            apply_help(self.default_view, "Which view DavPunk opens on."),
        )
        form.addRow(
            "",
            apply_help(
                self.show_completed,
                "Only the starting state. The in-app toggle is deliberately not "
                "remembered — it is a way of looking at the list, not a preference.",
            ),
        )
        oversize = (
            "A task whose raw iCalendar data is larger than this is shown read-only "
            "rather than rewritten, so DavPunk cannot mangle something it does not "
            "fully understand. It can still be deleted or moved."
        )
        form.addRow(help_label("Oversize limit", oversize), apply_help(self.max_bytes, oversize))
        return page

    def _accounts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            hint("Each account is one CalDAV server; DavPunk finds the task lists on it.")
        )

        self.remote_list = QListWidget()
        self.remotes = [r.model_dump() for r in self.config.remotes]
        self._refresh_remotes()
        layout.addWidget(self.remote_list)

        row = QHBoxLayout()
        add = QPushButton("Add…")
        edit = QPushButton("Edit…")
        remove = QPushButton("Remove")
        add.clicked.connect(self._add_remote)
        edit.clicked.connect(self._edit_remote)
        remove.clicked.connect(self._remove_remote)
        self.remote_list.itemDoubleClicked.connect(lambda _i: self._edit_remote())
        for button in (add, edit, remove):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        apply_help(
            remove,
            "Removing an account only stops DavPunk syncing it. Its cached tasks "
            "are kept until you purge them explicitly, so a mistake here is never "
            "silently destructive.",
        )
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        mcp = self.config.mcp
        self.mcp_enabled = QCheckBox("Run the MCP server")
        self.mcp_enabled.setChecked(mcp.enabled)

        self.cap_read = QCheckBox("Read tasks")
        self.cap_write = QCheckBox("Create and change tasks")
        self.cap_delete = QCheckBox("Delete tasks")
        self.cap_sync = QCheckBox("Trigger a sync")
        for box, value in (
            (self.cap_read, mcp.capabilities.read),
            (self.cap_write, mcp.capabilities.write),
            (self.cap_delete, mcp.capabilities.delete),
            (self.cap_sync, mcp.capabilities.sync),
        ):
            box.setChecked(value)

        mcp_help = (
            "Lets an AI assistant work with your tasks through the Model Context "
            "Protocol. Off by default, and every permission below is off by "
            "default too."
        )
        form.addRow(help_label("MCP server", mcp_help), apply_help(self.mcp_enabled, mcp_help))

        allow_help = (
            "Grant only what you need. Deletion is separate from writing on "
            "purpose: an assistant that tidies your task text does not need to be "
            "able to remove anything."
        )
        form.addRow(help_label("Allow", allow_help), apply_help(self.cap_read, allow_help))
        for box in (self.cap_write, self.cap_delete, self.cap_sync):
            form.addRow("", apply_help(box, allow_help))
        return page

    # -------------------------------------------------------------- remotes

    def _refresh_remotes(self) -> None:
        self.remote_list.clear()
        for remote in self.remotes:
            item = QListWidgetItem(f"{remote.get('name') or remote['id']} — {remote['url']}")
            item.setData(0x0100, remote["id"])
            self.remote_list.addItem(item)

    def _selected_index(self) -> int | None:
        row = self.remote_list.currentRow()
        return row if 0 <= row < len(self.remotes) else None

    def _add_remote(self) -> None:
        dialog = RemoteDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if any(r["id"] == values["id"] for r in self.remotes):
            QMessageBox.warning(
                self, "Duplicate id", f"There is already an account called {values['id']!r}."
            )
            return
        self.remotes.append(values)
        self._refresh_remotes()

    def _edit_remote(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        dialog = RemoteDialog(self.remotes[index], parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.remotes[index] = dialog.values()
        self._refresh_remotes()

    def _remove_remote(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        remote = self.remotes[index]
        confirm = QMessageBox.question(
            self,
            "Remove account",
            f"Stop syncing {remote.get('name') or remote['id']}?\n\n"
            "Its cached tasks stay on disk and can be purged separately.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        del self.remotes[index]
        self._refresh_remotes()

    # ----------------------------------------------------------------- save

    def _save(self) -> None:
        try:
            write_settings(
                self.config_path,
                general={
                    "theme": self.theme.currentText(),
                    "default_view": self.default_view.currentText(),
                    "show_completed": self.show_completed.isChecked(),
                    "max_resource_bytes": self.max_bytes.value(),
                },
                remotes=self.remotes,
                mcp={
                    "enabled": self.mcp_enabled.isChecked(),
                    "capabilities": {
                        "read": self.cap_read.isChecked(),
                        "write": self.cap_write.isChecked(),
                        "delete": self.cap_delete.isChecked(),
                        "sync": self.cap_sync.isChecked(),
                    },
                },
            )
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return

        # Fail loudly here rather than leaving a config that will not load on
        # the next start, when there is no dialog left to explain it.
        try:
            load_config(self.config_path)
        except ConfigError as exc:
            QMessageBox.critical(
                self,
                "Saved, but not valid",
                f"DavPunk wrote {self.config_path} but cannot read it back:\n\n{exc}",
            )
            return

        QMessageBox.information(self, "Saved", RESTART_NOTE)
        self.accept()


def write_settings(config_path, *, general: dict, remotes: list[dict], mcp: dict):
    """Rewrite ``config.toml``, preserving comments and unknown keys.

    tomlkit rather than a re-serialize: anything the user wrote by hand —
    comments, key bindings, kanban columns DavPunk is not editing here — has to
    survive a trip through this dialog.
    """
    from davpunk.ui.first_run import _atomic_write

    path = paths.ensure_dir(config_path.parent) and config_path
    document = (
        tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    )
    davpunk = document.setdefault("davpunk", tomlkit.table(True))

    for key, value in general.items():
        davpunk[key] = value

    mcp_table = davpunk.get("mcp")
    if mcp_table is None:
        mcp_table = tomlkit.table(True)
        davpunk["mcp"] = mcp_table
    mcp_table["enabled"] = mcp["enabled"]

    capabilities = mcp_table.get("capabilities")
    if capabilities is None:
        capabilities = tomlkit.table()
        mcp_table["capabilities"] = capabilities
    for key, value in mcp["capabilities"].items():
        capabilities[key] = value

    array = tomlkit.aot()
    for remote in remotes:
        entry = tomlkit.table()
        for key in (
            "id",
            "name",
            "url",
            "username",
            "gpg_file",
            "gpg_key_id",
            "sync_interval",
            "color",
            "pinned_view",
            "allow_insecure",
            "verify_tls",
        ):
            value = remote.get(key)
            if value is not None:
                entry[key] = value
        array.append(entry)
    davpunk["remotes"] = array

    _atomic_write(path, tomlkit.dumps(document))
    return path
