"""Logging: the never-stdout rule, per-entrypoint handlers, redaction."""

from __future__ import annotations

import logging
import logging.handlers
import sys

import pytest

from davpunk import logging_setup


@pytest.fixture(autouse=True)
def _restore_root():
    yield
    logging_setup.setup_logging("cli")


@pytest.mark.parametrize("entrypoint", ["daemon", "mcp", "cli"])
def test_non_ui_entrypoints_log_to_stderr_never_stdout(entrypoint):
    """stdout is the MCP stdio protocol channel; a stray write corrupts it."""
    logger = logging_setup.setup_logging(entrypoint)
    streams = [h.stream for h in logger.handlers if isinstance(h, logging.StreamHandler)]
    assert streams
    assert all(s is sys.stderr for s in streams)
    assert sys.stdout not in streams


def test_ui_logs_to_a_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVPUNK_HOME", str(tmp_path))
    logger = logging_setup.setup_logging("ui")
    handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 1_048_576
    assert handlers[0].backupCount == 5


def test_setup_is_idempotent():
    first = logging_setup.setup_logging("cli")
    count = len(first.handlers)
    second = logging_setup.setup_logging("cli")
    assert len(second.handlers) == count


def test_credential_shaped_records_are_dropped(capsys):
    logging_setup.setup_logging("cli", level="DEBUG")
    log = logging.getLogger("davpunk.test")
    log.info("Authorization: Basic c2VjcmV0")
    log.info("plain message")
    err = capsys.readouterr().err
    assert "c2VjcmV0" not in err
    assert "plain message" in err


def test_level_from_environment(monkeypatch):
    monkeypatch.setenv("DAVPUNK_LOG_LEVEL", "DEBUG")
    assert logging_setup.resolve_level() == logging.DEBUG


def test_explicit_level_beats_environment(monkeypatch):
    monkeypatch.setenv("DAVPUNK_LOG_LEVEL", "DEBUG")
    assert logging_setup.resolve_level("WARNING") == logging.WARNING


def test_unknown_level_falls_back_to_info():
    assert logging_setup.resolve_level("LOUD") == logging.INFO


def test_uid_prefix_never_reveals_a_whole_uid():
    assert logging_setup.uid_prefix("0123456789abcdef") == "01234567"
    assert logging_setup.uid_prefix(None) == "<no-uid>"
