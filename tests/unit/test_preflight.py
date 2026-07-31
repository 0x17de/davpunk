"""Preflight: version floors, FTS5 availability, tzdata resolution."""

from __future__ import annotations

import sqlite3

import pytest

from davpunk import preflight as pf


def test_all_checks_pass_in_this_environment():
    pf.preflight()


def test_python_floor(monkeypatch):
    monkeypatch.setattr(pf.sys, "version_info", (3, 10, 0, "final", 0))
    with pytest.raises(pf.PreflightError, match=r"Python 3\.11"):
        pf.check_python()


def test_sqlite_version_floor(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 24, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.24.0")
    with pytest.raises(pf.PreflightError, match=r"SQLite 3\.35"):
        pf.check_sqlite_version()


def test_fts5_missing(monkeypatch):
    class NoFts:
        def execute(self, _sql):
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self):
            pass

    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: NoFts())
    with pytest.raises(pf.PreflightError, match="FTS5"):
        pf.check_fts5()


def test_tzdata_missing(monkeypatch):
    def boom(_name):
        raise pf.zoneinfo.ZoneInfoNotFoundError("no tzdata")

    monkeypatch.setattr(pf.zoneinfo, "ZoneInfo", boom)
    with pytest.raises(pf.PreflightError, match="time-zone database"):
        pf.check_tzdata()


def test_preflight_or_die_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(pf, "preflight", lambda: (_ for _ in ()).throw(pf.PreflightError("nope")))
    with pytest.raises(SystemExit) as excinfo:
        pf.preflight_or_die()
    assert excinfo.value.code == 1
    assert "nope" in capsys.readouterr().err


def test_checks_returns_a_copy():
    assert pf.checks() is not pf.CHECKS
    assert [label for label, _ in pf.checks()] == [
        "python",
        "sqlite-version",
        "sqlite-fts5",
        "tzdata",
    ]
