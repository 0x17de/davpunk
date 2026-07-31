"""**File → Preview sync…** — what a sync would do, before it does it.

The plan runs on the sync thread, because it makes real network requests, and
arrives here as text: the worker boundary carries strings and ints only, never
Python objects.

The **Sync now** button stays disabled until every remote has reported, so it
can never be pressed against a half-finished picture.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

log = logging.getLogger("davpunk.ui.dry_run_dialog")


class DryRunDialog(QDialog):
    """Collects one report per remote, then offers to go ahead."""

    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview sync")
        self.resize(760, 520)
        self.controller = controller
        self.proceed = False
        self._writes = 0
        self._local = 0
        self._reports: list[str] = []

        layout = QVBoxLayout(self)

        self.headline = QLabel("Asking every server what a sync would do…")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # A report is columnar; a proportional font makes it unreadable.
        self.body.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.body, 1)

        self.footer = QLabel(
            "Nothing has been written to the server or to your cache — this is a read-only preview."
        )
        self.footer.setWordWrap(True)
        self.footer.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.footer)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.sync_button = QPushButton("Sync now")
        self.sync_button.setEnabled(False)
        self.sync_button.clicked.connect(self._accept_and_sync)
        self.buttons.addButton(self.sync_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        controller.dryRunFinished.connect(self.add_report)
        controller.dryRunDone.connect(self.finish)
        controller.dry_run()

    # ------------------------------------------------------------------ slots

    def add_report(self, _remote_id: str, text: str, writes: int, local: int) -> None:
        self._reports.append(text)
        self._writes += writes
        self._local += local
        self.body.setPlainText("\n\n".join(self._reports))

    def finish(self) -> None:
        self.sync_button.setEnabled(True)
        if not self._reports:
            self.headline.setText("No remotes are configured, so there is nothing to preview.")
            self.sync_button.setEnabled(False)
            return

        if self._writes or self._local:
            self.headline.setText(
                f"A sync would send {self._writes} request(s) to your server(s) and "
                f"change {self._local} task(s) in the local cache."
            )
        else:
            self.headline.setText("A sync would change nothing on either side.")

    def _accept_and_sync(self) -> None:
        self.proceed = True
        self.accept()

    def closeEvent(self, event) -> None:
        # Leaving the dialog must not leave a plan running against a server.
        self.controller.worker.cancel.set()
        super().closeEvent(event)


__all__ = ["DryRunDialog"]
