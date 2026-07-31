"""Config validation: https enforcement, duplicate keys, MCP caps."""

from __future__ import annotations

import pytest

from davpunk.config import (
    MCP_LIST_LIMIT_CAP,
    ConfigError,
    DavPunkConfig,
    detect_duplicate_bindings,
    load_config,
    normalize_binding,
)

MINIMAL = """
[davpunk]
theme = "dark"

[[davpunk.remotes]]
id = "work"
url = "https://cal.example.com/dav/"
username = "user@example.com"
"""


def write(tmp_path, text, name="config.toml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_missing_config_yields_defaults(tmp_path):
    config = load_config(tmp_path / "absent.toml")
    assert config.remotes == []
    assert config.mcp.enabled is False
    assert config.mcp.capabilities.read is False


def test_minimal_config_loads(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    assert len(config.remotes) == 1
    assert config.remotes[0].id == "work"
    assert config.remotes[0].sync_interval == 300


def test_all_mcp_capabilities_default_off(tmp_path):
    caps = load_config(write(tmp_path, MINIMAL)).mcp.capabilities
    assert (caps.read, caps.write, caps.delete, caps.sync) == (False, False, False, False)


# ------------------------------------------------------------------ transport


def test_http_without_allow_insecure_is_rejected(tmp_path):
    text = MINIMAL.replace("https://cal.example.com", "http://cal.example.com")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "allow_insecure" in str(excinfo.value)


def test_http_with_allow_insecure_is_permitted(tmp_path, caplog):
    text = (
        MINIMAL.replace("https://cal.example.com", "http://cal.example.com")
        + "allow_insecure = true\n"
    )
    config = load_config(write(tmp_path, text))
    assert config.remotes[0].allow_insecure is True


def test_non_http_scheme_is_rejected(tmp_path):
    text = MINIMAL.replace("https://cal.example.com/dav/", "ftp://cal.example.com/dav/")
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


# ------------------------------------------------------------------ key binds


def test_duplicate_binding_in_the_same_context_is_an_error():
    with pytest.raises(ValueError, match="duplicate key bindings"):
        detect_duplicate_bindings({"new_task": "n", "delete_task": "n"})


def test_same_binding_in_different_contexts_is_fine():
    """'l' is next-kanban-column and take-local; the contexts never coexist."""
    detect_duplicate_bindings({"card_next_column": "l", "take_local": "l"})


def test_defaults_have_no_duplicates():
    DavPunkConfig()  # the model validator runs detect_duplicate_bindings


def test_case_distinguishes_single_key_bindings():
    """h/H and a/A are distinct bindings; the default keymap relies on it."""
    detect_duplicate_bindings({"card_prev_column": "h", "focus_prev_column": "H"})


def test_modifiers_are_case_insensitive():
    assert normalize_binding("ctrl+R") == normalize_binding("Ctrl+R")
    with pytest.raises(ValueError, match="duplicate key bindings"):
        detect_duplicate_bindings({"sync_now": "ctrl+r", "new_task": "Ctrl+R"})


def test_modifier_order_does_not_create_a_second_binding():
    assert normalize_binding("Ctrl+Shift+K") == normalize_binding("Shift+Ctrl+K")


def test_override_colliding_with_a_default_is_rejected(tmp_path):
    text = MINIMAL + '\n[davpunk.keys]\nsync_now = "n"\n'
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "duplicate key bindings" in str(excinfo.value)


def test_unknown_key_action_is_rejected(tmp_path):
    text = MINIMAL + '\n[davpunk.keys]\nlaunch_missiles = "Ctrl+M"\n'
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "launch_missiles" in str(excinfo.value)


def test_keymap_applies_overrides():
    config = DavPunkConfig(keys={"sync_now": "Ctrl+S"})
    assert config.keymap()["sync_now"] == "Ctrl+S"
    assert config.keymap()["new_task"] == "n"


# ----------------------------------------------------------------------- misc


def test_duplicate_remote_id_is_rejected(tmp_path):
    text = MINIMAL + '\n[[davpunk.remotes]]\nid = "work"\nurl = "https://other.example.com/"\n'
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_remote_id_must_be_filename_safe(tmp_path):
    text = MINIMAL.replace('id = "work"', 'id = "../etc/passwd"')
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_duplicate_kanban_column_id_is_rejected(tmp_path):
    text = (
        MINIMAL
        + """
[davpunk.kanban]
columns = [
  {id = "todo", label = "A", status = "NEEDS-ACTION"},
  {id = "todo", label = "B", status = "IN-PROCESS"},
]
"""
    )
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_two_columns_may_share_a_status(tmp_path):
    """That is exactly what X-DAVPUNK-KANBAN-COL exists to disambiguate."""
    text = (
        MINIMAL
        + """
[davpunk.kanban]
columns = [
  {id = "todo",   label = "To Do",   status = "NEEDS-ACTION"},
  {id = "triage", label = "Triage",  status = "NEEDS-ACTION"},
]
"""
    )
    assert len(load_config(write(tmp_path, text)).kanban.columns) == 2


def test_mcp_bind_must_be_loopback(tmp_path):
    text = MINIMAL + '\n[davpunk.mcp]\nbind = "0.0.0.0"\n'
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "loopback" in str(excinfo.value)


def test_mcp_list_limit_is_hard_capped(tmp_path):
    text = MINIMAL + "\n[davpunk.mcp]\nlist_limit = 100000\n"
    assert load_config(write(tmp_path, text)).mcp.list_limit == MCP_LIST_LIMIT_CAP


def test_unparseable_toml_reports_the_file_and_does_not_crash(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, "[davpunk\nthis is not toml"))
    assert "config.toml" in str(excinfo.value)


def test_unknown_key_in_davpunk_table_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "[davpunk]\nnot_a_real_key = 1\n"))


def test_short_sync_interval_is_rejected(tmp_path):
    text = MINIMAL + "sync_interval = 5\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
