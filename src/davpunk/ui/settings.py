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
from davpunk.core import credentials, secret_store
from davpunk.ui import viewmodel as vm
from davpunk.ui.first_run import store_password
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

        # Where the password is *now*, so a switch can offer to clean up after
        # itself.  Read before the form can change any of it.
        self._original_backend = (values or {}).get("credential_backend")
        self._original_key_id = (values or {}).get("gpg_key_id")
        self._original_gpg_file = (values or {}).get("gpg_file")

        layout = QVBoxLayout(self)
        self.form = RemoteForm(values, editing=self.editing)
        layout.addWidget(self.form)

        self.set_password = QPushButton(
            "Change the stored password…" if self.editing else "Set the password…"
        )
        self.set_password.clicked.connect(self._prompt_password)
        apply_help(
            self.set_password,
            "The password goes wherever the choice above says. Run this again "
            "after changing that choice or the encryption key — moving it is "
            "the one thing DavPunk will not do behind your back.",
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
        password, ok = QInputDialog.getText(
            self,
            "CalDAV password",
            f"Password for {values['username'] or values['id']}:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        if not store_password(self, values, password):
            return
        self._offer_to_clean_up(values)
        where = secret_store.for_remote_backend(
            values["credential_backend"],
            values["id"],
            key_id=values.get("gpg_key_id"),
            gpg_file=values.get("gpg_file"),
        ).describe()
        QMessageBox.information(self, "Password saved", f"Stored in {where}")

    def _offer_to_clean_up(self, values: dict) -> None:
        """After a backend switch, the old copy is still sitting there.

        A stale credential is a live password nobody is watching any more, so
        it is offered for deletion — and only offered, because deleting a file
        the user may still be relying on elsewhere is not ours to assume.
        """
        was = self._original_backend
        now = values["credential_backend"]
        if was is None or was == now:
            return

        old = secret_store.for_remote_backend(
            was, values["id"], key_id=self._original_key_id, gpg_file=self._original_gpg_file
        )
        answer = QMessageBox.question(
            self,
            "Remove the old copy?",
            f"The password is now in {secret_store.for_remote_backend(now, values['id']).describe()}.\n\n"
            f"Delete the one still in {old.describe()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            old.delete()
        except credentials.CredentialError as exc:
            QMessageBox.warning(self, "Could not remove the old password", str(exc))

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

        self.show_completed = QComboBox()
        for text, value in vm.SHOW_COMPLETED_CHOICES:
            self.show_completed.addItem(text.replace("&", ""), value)
        self.completed_days = QSpinBox()
        self.completed_days.setRange(1, 3650)
        self.completed_days.setSuffix(" days")
        shown = self.config.show_completed
        self.completed_days.setValue(14 if isinstance(shown, bool) else shown)
        self.show_completed.currentIndexChanged.connect(self._custom_days_enabled)
        self.show_completed.setCurrentIndex(vm.show_completed_choice(shown))
        self._custom_days_enabled()
        completed_row = QWidget()
        row = QHBoxLayout(completed_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.show_completed, 1)
        row.addWidget(self.completed_days)

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
        completed = (
            "Which completed and cancelled tasks the list and the board start out "
            "showing: none, those finished in the last few days, or all of them. "
            "Only the starting state. View → Show completed changes it for the "
            "session and is deliberately not remembered — it is a way of looking "
            "at the list, not a preference."
        )
        apply_help(self.show_completed, completed)
        apply_help(self.completed_days, completed)
        form.addRow(help_label("Show completed", completed), completed_row)
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
        mcp_help = (
            "Lets an AI assistant work with your tasks through the Model Context "
            "Protocol. Off by default, and every permission below is off by "
            "default too."
        )
        form.addRow(help_label("MCP server", mcp_help), apply_help(self.mcp_enabled, mcp_help))

        self.transport = QComboBox()
        self.transport.addItem("stdio — the client launches DavPunk (recommended)", "stdio")
        self.transport.addItem("SSE — DavPunk listens on localhost", "sse")
        index = self.transport.findData(mcp.transport)
        self.transport.setCurrentIndex(max(0, index))
        self.transport.currentIndexChanged.connect(self._sync_transport)
        transport_help = (
            "stdio is simplest and needs no token: the client starts davpunk-mcp "
            "itself and talks to it over a pipe. Choose SSE only if your client "
            "cannot do that — it listens on a port, so it needs a bearer token."
        )
        form.addRow(
            help_label("Transport", transport_help), apply_help(self.transport, transport_help)
        )

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(mcp.port)
        port_help = (
            "The port the SSE server listens on. It always binds 127.0.0.1 and "
            "nothing else — DavPunk refuses a non-loopback address at startup."
        )
        form.addRow(help_label("Port", port_help), apply_help(self.port, port_help))

        # One control, because these are answers to one question — where the
        # token lives — and splitting a backend picker from a key picker makes
        # a key id sit there meaning nothing whenever the backend is not GnuPG.
        self.token_key = QComboBox()
        self.token_key.addItem("Plain file, mode 0600", ("file", None))
        if secret_store.keyring_available():
            self.token_key.addItem("Login keyring", ("keyring", None))
        for key in credentials.encryption_options():
            self.token_key.addItem(f"Encrypted to {key.short_id}", ("gpg", key.recipient))
        at = self.token_key.findData((mcp.resolved_token_backend, mcp.token_gpg_key_id))
        if at < 0:
            # A key or a keyring the config names but this machine cannot
            # offer.  Kept selectable: saving must not silently move a token.
            label = mcp.token_gpg_key_id or mcp.resolved_token_backend
            self.token_key.addItem(
                f"{label} (not available here)",
                (
                    mcp.resolved_token_backend,
                    mcp.token_gpg_key_id,
                ),
            )
            at = self.token_key.count() - 1
        self.token_key.setCurrentIndex(at)
        key_help = (
            "Keeping the token out of a plain file protects it if the config "
            "directory is ever backed up somewhere it should not be. The cost "
            "is the same as for a CalDAV password: GnuPG needs a warm "
            "gpg-agent, and the login keyring needs to be unlocked — the "
            "server will not start until it can read the token back."
        )
        form.addRow(help_label("Token at rest", key_help), apply_help(self.token_key, key_help))

        self.token_value = QLineEdit()
        self.token_value.setReadOnly(True)
        self.token_value.setPlaceholderText("(hidden — press Show)")
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_value)
        self.show_token = QPushButton("Show")
        self.show_token.clicked.connect(self._reveal_token)
        self.copy_token = QPushButton("Copy")
        self.copy_token.clicked.connect(self._copy_token)
        self.regen_token = QPushButton("Regenerate")
        self.regen_token.clicked.connect(self._regenerate_token)
        apply_help(
            self.regen_token,
            "Replaces the token. Any client still holding the old one stops "
            "working until you paste in the new one.",
        )
        for button in (self.show_token, self.copy_token, self.regen_token):
            token_row.addWidget(button)
        container = QWidget()
        container.setLayout(token_row)
        token_help = "The bearer token an SSE client sends as `Authorization: Bearer …`."
        form.addRow(help_label("Token", token_help), container)

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

        allow_help = (
            "Grant only what you need. Deletion is separate from writing on "
            "purpose: an assistant that tidies your task text does not need to be "
            "able to remove anything."
        )
        form.addRow(help_label("Allow", allow_help), apply_help(self.cap_read, allow_help))
        for box in (self.cap_write, self.cap_delete, self.cap_sync):
            form.addRow("", apply_help(box, allow_help))

        self._sync_transport()
        return page

    # ------------------------------------------------------------------ token

    def _sync_transport(self) -> None:
        """The port and the token only mean anything for SSE."""
        sse = self.transport.currentData() == "sse"
        for widget in (
            self.port,
            self.token_key,
            self.token_value,
            self.show_token,
            self.copy_token,
            self.regen_token,
        ):
            widget.setEnabled(sse)
        if not sse:
            self.token_value.clear()
            self.token_value.setPlaceholderText("(stdio needs no token)")
        else:
            self.token_value.setPlaceholderText("(hidden — press Show)")

    def _token_config(self):
        """The MCP config as the dialog currently shows it.

        Reading the token has to follow the *pending* choice, or pressing Show
        after switching keys would decrypt the wrong file.
        """
        backend, key_id = self._token_choice()
        return self.config.mcp.model_copy(
            update={
                "transport": self.transport.currentData(),
                "port": self.port.value(),
                "token_backend": backend,
                "token_gpg_key_id": key_id,
            }
        )

    def _token_choice(self) -> tuple[str, str | None]:
        """``(backend, key id)`` — one combo, two config keys."""
        return self.token_key.currentData() or ("file", None)

    def _reveal_token(self) -> str | None:
        from davpunk.mcp.server import TokenError, ensure_token

        try:
            token = ensure_token(self._token_config())
        except TokenError as exc:
            QMessageBox.warning(self, "Could not read the token", str(exc))
            return None
        self.token_value.setText(token)
        return token

    def _copy_token(self) -> None:
        from PySide6.QtWidgets import QApplication

        token = self.token_value.text() or self._reveal_token()
        if token:
            QApplication.clipboard().setText(token)
            QMessageBox.information(self, "Copied", "The token is on your clipboard.")

    def _regenerate_token(self) -> None:
        from davpunk.mcp.server import TokenError, rotate_token

        confirm = QMessageBox.question(
            self,
            "Regenerate the token",
            "Any client still using the old token will stop working until you "
            "give it the new one. Continue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.token_value.setText(rotate_token(self._token_config()))
        except TokenError as exc:
            QMessageBox.warning(self, "Could not regenerate the token", str(exc))

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

    def _custom_days_enabled(self) -> None:
        self.completed_days.setEnabled(self.show_completed.currentData() is None)

    def chosen_show_completed(self) -> vm.ShowCompleted:
        value = self.show_completed.currentData()
        return self.completed_days.value() if value is None else value

    # ----------------------------------------------------------------- save

    def _save(self) -> None:
        try:
            write_settings(
                self.config_path,
                general={
                    "theme": self.theme.currentText(),
                    "default_view": self.default_view.currentText(),
                    "show_completed": self.chosen_show_completed(),
                    "max_resource_bytes": self.max_bytes.value(),
                },
                remotes=self.remotes,
                mcp={
                    "enabled": self.mcp_enabled.isChecked(),
                    "transport": self.transport.currentData(),
                    "port": self.port.value(),
                    "token_backend": self._token_choice()[0],
                    "token_gpg_key_id": self._token_choice()[1],
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
    # Only what the caller actually supplied: a key left out keeps whatever the
    # file already says, which is the same promise tomlkit is here for.
    for key in ("enabled", "transport", "port"):
        if key in mcp:
            mcp_table[key] = mcp[key]

    if "token_backend" in mcp:
        mcp_table["token_backend"] = mcp["token_backend"]

    if "token_gpg_key_id" in mcp:
        if mcp["token_gpg_key_id"]:
            mcp_table["token_gpg_key_id"] = mcp["token_gpg_key_id"]
        elif "token_gpg_key_id" in mcp_table:
            # Switching away from GnuPG has to remove it, or DavPunk keeps
            # looking for mcp-token.gpg — and the config rejects a key id
            # sitting beside a backend that would not use it.
            del mcp_table["token_gpg_key_id"]

    capabilities = mcp_table.get("capabilities")
    if capabilities is None:
        capabilities = tomlkit.table()
        mcp_table["capabilities"] = capabilities
    for key, value in mcp.get("capabilities", {}).items():
        capabilities[key] = value

    array = tomlkit.aot()
    for remote in remotes:
        entry = tomlkit.table()
        for key in (
            "id",
            "name",
            "url",
            "username",
            "credential_backend",
            "gpg_file",
            "gpg_key_id",
            "auto_sync",
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
