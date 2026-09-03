"""View state that outlives the process.

Distinct from ``config.toml``, which is the user's file: they wrote it, they
own its comments, and nothing here belongs in it.  This is the other kind of
setting — the one nobody types, the one the window simply *remembers* — so it
lives under ``XDG_STATE_HOME`` as machine-written JSON and can be deleted at
any time without losing anything but a convenience.

Which filter the board was left on is the first of those.  It was runtime state
on the argument that a filter is a way of looking at the board right now,
and for the text box that still holds — a search is typed for one question and
answered.  A list picker is not a search: people work out of one calendar for
weeks at a time, and re-picking "private" at every launch is not a fresh start,
it is a chore.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from davpunk import paths

log = logging.getLogger("davpunk.ui.ui_state")

#: Bumped only if a rewrite makes the old file unreadable rather than merely
#: incomplete; unknown keys are ignored, so adding to it needs no bump.
VERSION = 1

#: What was last read or written, and from where, so a keystroke in the text
#: box costs no syscall.  One process owns the UI at a time (``ui.lock``), so
#: nothing else is writing this file underneath us.  Keyed by path
#: because ``DAVPUNK_HOME`` can move under a running interpreter — which is
#: what every test does.
_cache: tuple[Path, dict[str, Any]] | None = None


def load() -> dict[str, Any]:
    """The remembered state, or an empty one.

    Never raises: a state file that is missing, unreadable or corrupt means the
    window opens the way it did before there was one, which is a perfectly good
    outcome and not worth a dialog.
    """
    global _cache
    path = paths.ui_state_file()
    if _cache is not None and _cache[0] == path:
        return dict(_cache[1])

    state: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == VERSION:
            state = {k: v for k, v in raw.items() if k != "version"}
        elif isinstance(raw, dict):
            log.debug("ignoring ui state written by another version: %s", raw.get("version"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable ui state at %s: %s", path, exc)

    _cache = (path, dict(state))
    return state


def get(key: str) -> dict[str, Any]:
    """One section, always a dict — callers read it with ``.get`` defaults."""
    value = load().get(key)
    return value if isinstance(value, dict) else {}


def remember(key: str, value: Any) -> None:
    """Store one section, writing only when it actually changed.

    Every filter change comes through here, and most of them change nothing
    that is kept — every keystroke in the text box, for one — so the compare is
    what keeps this from being a write per keypress.
    """
    state = load()
    if state.get(key) == value:
        return
    state[key] = value
    _write(state)


def _write(state: dict[str, Any]) -> None:
    global _cache
    path = paths.ui_state_file()
    _cache = (path, dict(state))
    text = json.dumps({"version": VERSION, **state}, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        paths.ensure_dir(path.parent)
        tmp.unlink(missing_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # A read-only state directory is a reason to forget the last filter,
        # not a reason to interrupt the user mid-tick.
        log.warning("could not write ui state to %s: %s", path, exc)
