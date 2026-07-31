"""Per-entrypoint logging handlers.

Library modules call ``logging.getLogger("davpunk.<module>")`` and configure no
handlers.  Only these functions install them.

**stdout is never used.**  The MCP stdio transport owns stdout; a stray write
there corrupts the protocol stream, so every entrypoint logs to stderr or to a
file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Literal

from davpunk import paths

ROOT = "davpunk"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

#: Redacted wholesale, at every level.
_NEVER_LOG = ("authorization", "password", "passphrase")

Entrypoint = Literal["ui", "daemon", "mcp", "cli"]


class _CredentialFilter(logging.Filter):
    """Belt-and-braces guard against a credential reaching a handler.

    Nothing in DavPunk logs credential bytes deliberately; this catches a
    future mistake rather than a current one.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage().lower()
        except Exception:
            return True
        return not any(token in message for token in _NEVER_LOG)


def resolve_level(explicit: str | None = None) -> int:
    name = explicit or os.environ.get("DAVPUNK_LOG_LEVEL") or "INFO"
    return logging.getLevelNamesMapping().get(name.upper(), logging.INFO)


def _reset(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def setup_logging(entrypoint: Entrypoint, level: str | None = None) -> logging.Logger:
    """Install handlers for one entrypoint and return the ``davpunk`` logger."""
    logger = logging.getLogger(ROOT)
    _reset(logger)
    logger.setLevel(resolve_level(level))
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    handlers: list[logging.Handler] = []

    if entrypoint == "ui":
        try:
            paths.ensure_dir(paths.state_dir())
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    paths.log_file(), maxBytes=1_048_576, backupCount=5, encoding="utf-8"
                )
            )
        except OSError:
            # A read-only state dir must not stop the UI from starting.
            handlers.append(logging.StreamHandler(sys.stderr))
    else:
        # daemon → stderr → journald;  mcp → stderr ONLY;  cli → stderr
        handlers.append(logging.StreamHandler(sys.stderr))

    credential_filter = _CredentialFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(credential_filter)
        logger.addHandler(handler)

    return logger


def uid_prefix(uid: str | None, length: int = 8) -> str:
    """INFO-level task identification: a UID prefix, never a summary."""
    if not uid:
        return "<no-uid>"
    return uid[:length]
