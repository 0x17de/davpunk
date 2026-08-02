"""The remote details form, shared by the first-run wizard and settings.

Every field carries an explanation behind a ``?`` badge.  A CalDAV URL, a GPG
key id and a sync interval are not self-explanatory, and a setup screen that
just lists them leaves you guessing which of them matters — but printing all
seven explanations inline cost two or three wrapped lines each, which is more
vertical space than the wizard has, so every one of them was clipped.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from davpunk import paths
from davpunk.core import credentials, secret_store
from davpunk.ui.widgets import apply_help, help_label, hint

log = logging.getLogger("davpunk.ui.remote_form")

#: Shown under each field.  Written for someone who has never set up CalDAV.
HELP = {
    "id": (
        "A short name for this account, used for its lock and credential "
        "files. Letters, digits, dot, dash and underscore only — and it cannot "
        "be changed later without re-adding the account."
    ),
    "name": "What you want to see in the sidebar. Purely cosmetic.",
    "url": (
        "Your CalDAV server's address. Usually the account root rather than a "
        "single list — DavPunk discovers the task lists under it. Nextcloud "
        "looks like https://cloud.example.com/remote.php/dav/, Radicale like "
        "https://cal.example.com/. Must be https:// unless you set "
        "allow_insecure in config.toml."
    ),
    "username": "The username you log into that server with.",
    "password": (
        "Never stored in plain text, never in config.toml, and never in your "
        "shell history. Where it does go is the choice below."
    ),
    "backend": (
        "Two ways to keep a password, and neither is the beginner's option.\n\n"
        "GnuPG encrypts it to a key only you hold, so a backup or a stolen "
        "disk gives nothing away — but background sync only works while "
        "gpg-agent still holds your passphrase, and you need a key to begin "
        "with.\n\n"
        "The login keyring is your desktop's own store, opened when you log "
        "in. Nothing to set up and the sync daemon just works — but once it is "
        "open, any program running as you can ask it for the password."
    ),
    "gpg_key": (
        "Which GPG encryption subkey protects your password. Only subkeys whose "
        "private half is available on this machine are listed, because GnuPG "
        "will happily encrypt to one you cannot decrypt with. If you later "
        "rotate this subkey, come back here and set the password again."
    ),
    "auto_sync": (
        "Let the background daemon sync this account on a timer. Turning it "
        "off does not cut the account off: Sync now, File → Preview sync and "
        "`davpunk sync` all still work. It only stops DavPunk contacting this "
        "server on its own."
    ),
    "sync_interval": (
        "How often to sync in the background. Edits are saved locally the "
        "instant you make them, so this only affects how fast they reach the "
        "server. 300 seconds suits most people."
    ),
    "color": "A colour to tint this account's tasks with, as #RRGGBB.",
}


class RemoteForm(QWidget):
    """The fields for one remote, with live validation.

    ``validityChanged`` fires whenever :meth:`problem` would start or stop
    returning something, so a wizard page can enable its Next button and a
    dialog its OK button from the same source.
    """

    validityChanged = Signal()

    def __init__(self, values: dict | None = None, *, editing: bool = False, parent=None) -> None:
        super().__init__(parent)
        # A stored remote carries explicit None for anything unset, so `.get`
        # with a default is not enough — it only fires on a *missing* key.
        values = {k: v for k, v in (values or {}).items() if v is not None}
        self._editing = editing

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        outer.addLayout(form)

        self.remote_id = QLineEdit(values.get("id", ""))
        self.remote_id.setPlaceholderText("work")
        # The id names the credential and lock files, so changing it on an
        # existing account would orphan both.
        self.remote_id.setEnabled(not editing)

        self.name = QLineEdit(values.get("name", ""))
        self.name.setPlaceholderText("Work")

        self.url = QLineEdit(values.get("url", "https://"))
        self.url.setPlaceholderText("https://cal.example.com/dav/")

        self.username = QLineEdit(values.get("username", ""))
        self.username.setPlaceholderText("you@example.com")

        self.auto_sync = QCheckBox("Sync this account automatically")
        self.auto_sync.setChecked(values.get("auto_sync", True))

        self.interval = QSpinBox()
        self.interval.setRange(30, 86400)
        self.interval.setValue(values.get("sync_interval", 300))
        self.interval.setSuffix(" seconds")
        # An interval that governs nothing invites the reading that background
        # syncing is still on somewhere.
        self.auto_sync.toggled.connect(self.interval.setEnabled)
        self.interval.setEnabled(self.auto_sync.isChecked())

        self.color = QLineEdit(values.get("color", "#4A9EFF"))

        self.gpg_key = QComboBox()
        self.skipped_note = hint("")
        self.skipped_note.hide()
        self._populate_keys(values.get("gpg_key_id"))
        self._build_backend_choice(values.get("credential_backend"))

        #: The label widget of each row, so a row can be hidden whole — a field
        #: hidden without its label leaves the label naming the row below it.
        self.labels: dict[str, QWidget] = {}
        for label, widget, key in (
            ("Account id", self.remote_id, "id"),
            ("Display name", self.name, "name"),
            ("Server URL", self.url, "url"),
            ("Username", self.username, "username"),
            ("Password kept in", self.backend_box, "backend"),
            ("Encryption key", self.gpg_key, "gpg_key"),
            (None, self.skipped_note, None),
            ("Automatic sync", self.auto_sync, "auto_sync"),
            ("Sync every", self.interval, "sync_interval"),
            ("Colour", self.color, "color"),
        ):
            if key is None:
                # Not a field: something the user may need to act on, so it
                # stays visible rather than hiding behind a hover.  Spanning
                # both columns keeps it to one line.
                form.addRow(widget)
                continue
            row_label = help_label(label, HELP[key])
            self.labels[key] = row_label
            form.addRow(row_label, apply_help(widget, HELP[key]))

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: palette(highlight); font-weight: bold;")
        outer.addWidget(self.problem_label)

        for widget in (self.remote_id, self.url, self.username):
            widget.textChanged.connect(self._revalidate)
        # Last, because it hides rows and validates: both need every widget it
        # touches to exist, and the problem label is the last one built.
        self._on_backend_changed()

    # --------------------------------------------------------------- backend

    #: ``(backend, title, the one line that decides it)``.  Both are spelled
    #: out where they are chosen: this is a trade-off, not a default with an
    #: advanced alternative, and burying half of it behind a hover would be
    #: picking for the user while pretending not to.
    BACKENDS = (
        (
            "gpg",
            "GnuPG",
            "Encrypted to a key only you hold — safe in a backup. Background "
            "sync works only while gpg-agent is warm.",
        ),
        (
            "keyring",
            "Login keyring",
            "Kept by your desktop, unlocked when you log in. Nothing to set up "
            "and the daemon just works. Anything running as you can read it.",
        ),
    )

    def _build_backend_choice(self, selected: str | None) -> None:
        self.backend_box = QWidget()
        # The rationale under each radio wraps, and a container only passes a
        # child's height-for-width up to the form if its own policy says it
        # has one.  Without this the last line of the longer note is cut off.
        policy = self.backend_box.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Policy.MinimumExpanding)
        self.backend_box.setSizePolicy(policy)
        layout = QVBoxLayout(self.backend_box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.backend_group = QButtonGroup(self)
        self._backend_buttons: dict[str, QRadioButton] = {}
        for backend, title, why in self.BACKENDS:
            button = QRadioButton(title)
            self.backend_group.addButton(button)
            self._backend_buttons[backend] = button
            layout.addWidget(button)
            note = hint(why)
            note.setContentsMargins(20, 0, 0, 6)
            layout.addWidget(note)
            button.toggled.connect(self._on_backend_changed)

            unavailable = self._backend_unavailable(backend)
            if unavailable and backend != selected:
                # Still selectable when the config already names it — silently
                # moving an account's password somewhere else is worse than
                # showing a choice that needs something fixed first.
                button.setEnabled(False)
                note.setText(f"{why}  ✗ {unavailable}")

        # An existing account starts on what it uses; a new one starts on
        # neither, because there is no honest default between these two.
        if selected in self._backend_buttons:
            self._backend_buttons[selected].setChecked(True)
        self._on_backend_changed()

    def _backend_unavailable(self, backend: str) -> str | None:
        """Why this backend cannot be used here, if it cannot."""
        if backend == "keyring":
            if not secret_store.keyring_available():
                return "no keyring is running on this desktop"
            return None
        if not credentials.encryption_options():
            return "no usable GnuPG encryption key — make one with `gpg --full-generate-key`"
        return None

    def _on_backend_changed(self, _checked: bool = False) -> None:
        """The GPG key picker is GnuPG's business and nobody else's."""
        label = self.labels.get("gpg_key") if hasattr(self, "labels") else None
        if label is None:
            return  # still building; the rows are hidden once they exist
        gpg = self.selected_backend() == "gpg"
        self.gpg_key.setVisible(gpg)
        label.setVisible(gpg)
        self.skipped_note.setVisible(gpg and bool(self.skipped_note.text()))
        self._revalidate()

    def selected_backend(self) -> str | None:
        for backend, button in self._backend_buttons.items():
            if button.isChecked():
                return backend
        return None

    # ------------------------------------------------------------------ keys

    def _populate_keys(self, selected: str | None) -> None:
        keys = credentials.list_secret_keys()
        options = credentials.encryption_options(keys)
        skipped = credentials.unusable_encryption_keys(keys)

        if skipped:
            # "My key is right there, why is it not in the list?" is otherwise
            # a mystery — and the usual answer is that its private half is on
            # another machine.
            self.skipped_note.setText(
                "Not offered: "
                + ", ".join(k.short_id for k in skipped)
                + f" — {credentials.describe_unusable(skipped[0])}"
            )
            self.skipped_note.setToolTip(
                "\n".join(f"{k.short_id}: {credentials.describe_unusable(k)}" for k in skipped)
            )
            self.skipped_note.show()

        if not options:
            self.gpg_key.addItem(
                "No usable encryption key found — create one with `gpg --full-generate-key`",
                None,
            )
            self.gpg_key.setEnabled(False)
            if selected:
                # Still offer what the config names.  Dropping it here would
                # silently re-key an existing account the next time this form
                # is saved, and the old credential would stop decrypting.
                self.gpg_key.insertItem(0, f"{selected} (not in this keyring)", selected)
                self.gpg_key.setCurrentIndex(0)
            return

        for key in options:
            self.gpg_key.addItem(key.label(), key.recipient)

        if selected:
            index = self.gpg_key.findData(selected)
            if index >= 0:
                self.gpg_key.setCurrentIndex(index)
            else:
                # A key the config names but the keyring no longer offers.
                # Keep it selectable so saving does not silently change it.
                self.gpg_key.insertItem(0, f"{selected} (not in this keyring)", selected)
                self.gpg_key.setCurrentIndex(0)

    # ------------------------------------------------------------ validation

    def _revalidate(self) -> None:
        self.problem_label.setText(self.problem() or "")
        self.validityChanged.emit()

    def problem(self) -> str | None:
        """The first thing wrong with the form, phrased as advice."""
        remote_id = self.remote_id.text().strip()
        if not remote_id:
            return "An account id is required; it names this account's credential and lock files."
        if not _safe_id(remote_id):
            return "The account id may only contain letters, digits, dot, dash and underscore."

        url = self.url.text().strip()
        if url.startswith("http://"):
            return (
                "http:// sends your password across the network in cleartext. Use "
                "https://, or set allow_insecure = true in config.toml if this is a "
                "server you control on a trusted network."
            )
        if not url.startswith("https://") or len(url) <= len("https://"):
            return "The server URL must start with https://"
        if not self.username.text().strip():
            return "A username is required."
        if self.selected_backend() is None:
            # No default is offered, so this is a real question rather than a
            # box left unticked: the two answers protect different things.
            return "Choose where to keep the password — the two options protect different things."
        return None

    def is_valid(self) -> bool:
        return self.problem() is None

    # ---------------------------------------------------------------- values

    def values(self) -> dict:
        remote_id = self.remote_id.text().strip()
        backend = self.selected_backend()
        return {
            "id": remote_id,
            "name": self.name.text().strip() or remote_id,
            "url": self.url.text().strip(),
            "username": self.username.text().strip(),
            "auto_sync": self.auto_sync.isChecked(),
            "sync_interval": self.interval.value(),
            "color": self.color.text().strip(),
            "credential_backend": backend,
            # A key id beside a keyring account would encrypt nothing, and the
            # config rejects the pair rather than let it look meaningful.
            "gpg_key_id": self.gpg_key.currentData() if backend == "gpg" else None,
            "gpg_file": str(paths.credential_file(remote_id)) if backend == "gpg" else None,
        }


def _safe_id(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value))
