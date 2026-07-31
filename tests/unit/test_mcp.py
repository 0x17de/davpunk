"""The MCP surface: refs, capability gating, pagination, projections, audit."""

from __future__ import annotations

import pytest

from davpunk.config import MCP_LIST_LIMIT_CAP, DavPunkConfig, McpCapabilities, McpConfig
from davpunk.core import cache
from davpunk.mcp import server, tools
from davpunk.mcp.refs import AmbiguousRef, RefError, format_ref, parse_ref, resolve
from davpunk.mcp.tools import ToolContext
from davpunk.models.calendar import calendar_id as compute_calendar_id
from davpunk.models.task import ReadOnlyReason, Status, SyncState


def context(conn, **caps) -> ToolContext:
    config = DavPunkConfig(
        mcp=McpConfig(enabled=True, capabilities=McpCapabilities(**caps), audit=True)
    )
    return ToolContext(config=config, conn=conn)


@pytest.fixture
def ctx(conn):
    """Everything enabled — capability gating has its own tests."""
    return context(conn, read=True, write=True, delete=True, sync=True)


@pytest.fixture
def seeded(conn, calendar_id, synced_task):
    def _seed(n=3):
        return [synced_task(f"t{i}", summary=f"Task {i}") for i in range(n)]

    return _seed


# ------------------------------------------------------------------ task_ref


def test_a_ref_is_calendar_id_colon_uid(calendar_id):
    assert format_ref(calendar_id, "abc-123") == f"{calendar_id}:abc-123"


def test_a_ref_round_trips(calendar_id):
    ref = parse_ref(format_ref(calendar_id, "abc-123"))
    assert (ref.calendar_id, ref.uid) == (calendar_id, "abc-123")


def test_a_uid_containing_colons_survives(calendar_id):
    """UIDs from other clients routinely contain colons."""
    uid = "urn:uuid:9f8e:7d6c"
    assert parse_ref(format_ref(calendar_id, uid)).uid == uid


@pytest.mark.parametrize(
    "bad",
    [
        "no-separator",
        ":missing-calendar",
        "short:uid",
        "NOTHEXNOTHEXNOTH:uid",
        "0123456789abcdef:",
        "",
    ],
)
def test_malformed_refs_are_structured_errors(bad):
    with pytest.raises(RefError) as excinfo:
        parse_ref(bad)
    assert excinfo.value.as_dict()["error"] == "malformed_task_ref"


def test_a_ref_survives_a_cache_rebuild(conn, remote_id):
    """A rebuild is a normal event; a ref an agent holds must still work."""
    before = compute_calendar_id(remote_id, "/dav/tasks/")
    with cache.tx(conn):
        cache.upsert_calendar(remote_id, "/dav/tasks/", conn)
    conn.execute("DELETE FROM calendars")
    with cache.tx(conn):
        after = cache.upsert_calendar(remote_id, "/dav/tasks/", conn)
    assert after == before


def test_an_ambiguous_ref_lists_the_candidates(conn, calendar_id, synced_task):
    """uid is not unique within a calendar; a bad server can duplicate it."""
    synced_task("dup")
    second = synced_task("dup-2")
    conn.execute("UPDATE tasks SET uid = 'dup' WHERE id = ?", (second,))

    with pytest.raises(AmbiguousRef) as excinfo:
        resolve(format_ref(calendar_id, "dup"), conn)

    payload = excinfo.value.as_dict()
    assert payload["error"] == "ambiguous_task_ref"
    assert sorted(payload["candidates"]) == ["dup-2.ics", "dup.ics"]


def test_an_href_disambiguates(conn, calendar_id, synced_task):
    synced_task("dup")
    second = synced_task("dup-2")
    conn.execute("UPDATE tasks SET uid = 'dup' WHERE id = ?", (second,))

    row = resolve(format_ref(calendar_id, "dup"), conn, href="dup-2.ics")
    assert row["id"] == second


def test_an_unknown_ref_is_task_not_found(conn, calendar_id):
    with pytest.raises(RefError) as excinfo:
        resolve(format_ref(calendar_id, "nope"), conn)
    assert excinfo.value.as_dict()["error"] == "task_not_found"


# --------------------------------------------------- the local id is private


def test_the_local_id_never_appears_in_any_payload(ctx, conn, calendar_id, seeded):
    """tasks.id is a local UUID that changes on a delete-and-reimport."""
    task_ids = seeded()
    payloads = [
        tools.list_calendars(ctx),
        tools.list_tasks(ctx),
        tools.get_task(ctx, format_ref(calendar_id, "t0")),
        tools.search_tasks(ctx, "Task"),
        tools.sync_status(ctx),
    ]
    blob = repr(payloads)
    for task_id in task_ids:
        assert task_id not in blob


def test_create_and_update_payloads_also_hide_the_local_id(ctx, calendar_id):
    created = tools.create_task(ctx, calendar_id, "Fresh")
    updated = tools.update_task(ctx, created["task_ref"], summary="Renamed")
    assert "id" not in created["task"]
    assert "id" not in updated["task"]


# ------------------------------------------------------------- capabilities


def test_there_are_exactly_twelve_tools():
    assert len(tools.TOOLS) == 12


def test_every_capability_is_off_by_default():
    caps = DavPunkConfig().mcp.capabilities
    assert not any((caps.read, caps.write, caps.delete, caps.sync))


@pytest.mark.parametrize("name", sorted(tools.TOOLS))
def test_every_tool_is_gated(conn, name):
    """With nothing enabled, no tool does anything."""
    ctx = context(conn)
    result = tools.call(name, ctx)
    assert result["error"] == "capability_disabled"


def test_a_read_only_agent_cannot_write(conn, calendar_id):
    ctx = context(conn, read=True)
    assert tools.list_tasks(ctx)["items"] == []
    assert tools.call("create_task", ctx, calendar_id=calendar_id, summary="x")["error"] == (
        "capability_disabled"
    )


def test_write_does_not_imply_delete(conn, calendar_id, synced_task):
    ctx = context(conn, read=True, write=True)
    synced_task("t0")
    assert tools.call("delete_task", ctx, task_ref=format_ref(calendar_id, "t0"))["error"] == (
        "capability_disabled"
    )


def test_the_error_names_the_capability_to_enable(conn):
    result = tools.call("sync_now", context(conn))
    assert result["capability"] == "sync"
    assert "capabilities" in result["message"]


def test_a_disabled_tool_is_not_registered_at_all(conn):
    """An agent should not spend a turn discovering it is not allowed."""
    ctx = context(conn, read=True)
    enabled = server.enabled_capabilities(ctx.config)
    exposed = [n for n, (_f, c) in tools.TOOLS.items() if c in enabled]
    assert set(exposed) == {"list_calendars", "list_tasks", "get_task", "search_tasks"}


# ------------------------------------------------------------------ reading


def test_list_calendars_returns_the_ids_refs_are_built_from(ctx, calendar_id):
    result = tools.list_calendars(ctx)
    assert result["calendars"][0]["calendar_id"] == calendar_id
    assert result["calendars"][0]["name"] == "Tasks"


def test_list_tasks_returns_the_compact_projection(ctx, seeded):
    seeded(1)
    item = tools.list_tasks(ctx)["items"][0]
    assert set(item) == {"task_ref", "summary", "status", "due", "priority", "calendar"}
    assert "description" not in item
    assert "raw_ics" not in item


def test_get_task_returns_full_detail(ctx, calendar_id, conn, synced_task):
    task_id = synced_task("t0", description="the long text")
    result = tools.get_task(ctx, format_ref(calendar_id, "t0"))
    assert result["description"] == "the long text"
    assert result["raw_ics"] is not None
    assert task_id not in repr(result)


def test_list_tasks_hides_completed_by_default(ctx, conn, synced_task, calendar_id):
    synced_task("open")
    done = synced_task("done")
    cache.update_task_optimistic(done, {"status": Status.COMPLETED}, conn)

    assert {i["task_ref"] for i in tools.list_tasks(ctx)["items"]} == {
        format_ref(calendar_id, "open")
    }
    assert len(tools.list_tasks(ctx, include_completed=True)["items"]) == 2


def test_list_tasks_filters(ctx, conn, calendar_id, make_task):
    task = make_task("tagged", priority=1)
    task.categories = ["urgent"]
    cache.create_task_local(task, conn)
    cache.create_task_local(make_task("plain", priority=9), conn)

    assert len(tools.list_tasks(ctx, tag="urgent")["items"]) == 1
    assert len(tools.list_tasks(ctx, priority=9)["items"]) == 1
    assert len(tools.list_tasks(ctx, calendar_id=calendar_id)["items"]) == 2
    assert len(tools.list_tasks(ctx, calendar_id="0" * 16)["items"]) == 0


def test_a_task_pending_deletion_is_not_listed(ctx, conn, synced_task):
    task_id = synced_task("gone")
    cache.delete_task_local(task_id, conn)
    assert tools.list_tasks(ctx)["items"] == []


# ---------------------------------------------------------------- pagination


def test_the_envelope_shape(ctx, seeded):
    seeded(5)
    result = tools.list_tasks(ctx, limit=2)
    assert set(result) == {"items", "total", "limit", "offset", "truncated"}
    assert result["total"] == 5
    assert len(result["items"]) == 2
    assert result["truncated"] is True


def test_the_last_page_is_not_truncated(ctx, seeded):
    seeded(5)
    assert tools.list_tasks(ctx, limit=2, offset=4)["truncated"] is False


def test_paging_walks_the_whole_set_without_repeats(ctx, seeded):
    seeded(10)
    seen = []
    for offset in range(0, 10, 3):
        seen.extend(i["task_ref"] for i in tools.list_tasks(ctx, limit=3, offset=offset)["items"])
    assert len(seen) == len(set(seen)) == 10


def test_the_limit_is_hard_capped(ctx, seeded):
    seeded(1)
    assert tools.list_tasks(ctx, limit=100_000)["limit"] == MCP_LIST_LIMIT_CAP


def test_the_default_limit_comes_from_config(conn, seeded):
    ctx = ToolContext(
        config=DavPunkConfig(mcp=McpConfig(list_limit=2, capabilities=McpCapabilities(read=True))),
        conn=conn,
    )
    seeded(5)
    assert tools.list_tasks(ctx)["limit"] == 2


def test_search_defaults_to_fifty(ctx, seeded):
    seeded(1)
    assert tools.search_tasks(ctx, "Task")["limit"] == tools.SEARCH_DEFAULT_LIMIT


def test_a_negative_offset_is_clamped(ctx, seeded):
    seeded(2)
    assert tools.list_tasks(ctx, offset=-5)["offset"] == 0


# -------------------------------------------------------------------- search


def test_search_finds_by_summary_and_description(ctx, conn, make_task):
    cache.create_task_local(make_task("s1", summary="unmistakable zorblat"), conn)
    cache.create_task_local(make_task("s2", description="mentions zorblat too"), conn)
    assert tools.search_tasks(ctx, "zorblat")["total"] == 2


def test_search_includes_completed_tasks(ctx, conn, synced_task):
    task_id = synced_task("done", summary="zorblat")
    cache.update_task_optimistic(task_id, {"status": Status.COMPLETED}, conn)
    assert tools.search_tasks(ctx, "zorblat")["total"] == 1


def test_invalid_fts_syntax_is_a_structured_error(ctx):
    """An agent will send FTS5 syntax the parser rejects sooner or later."""
    result = tools.call("search_tasks", ctx, query='"unterminated')
    assert result["error"] == "invalid_query"


# -------------------------------------------------------------------- writes


def test_create_returns_a_usable_ref(ctx, calendar_id):
    result = tools.create_task(ctx, calendar_id, "Write the HLD", priority=2, due="20260731")
    detail = tools.get_task(ctx, result["task_ref"])
    assert detail["summary"] == "Write the HLD"
    assert detail["priority"] == 2
    assert detail["due"] == "20260731"
    assert detail["sync_state"] == SyncState.NEW.value


def test_create_into_an_unknown_calendar_is_refused(ctx):
    result = tools.call("create_task", ctx, calendar_id="0" * 16, summary="x")
    assert result["error"] == "calendar_not_found"


def test_create_can_attach_a_parent(ctx, calendar_id, synced_task):
    synced_task("parent")
    child = tools.create_task(
        ctx, calendar_id, "Child", parent_ref=format_ref(calendar_id, "parent")
    )
    assert tools.get_task(ctx, child["task_ref"])["parent"] == format_ref(calendar_id, "parent")


def test_a_parent_in_another_calendar_is_refused(ctx, calendar_id, other_calendar_id, synced_task):
    """Parent resolution is per-calendar; a cross-calendar link would render as
    an orphan."""
    synced_task("parent")
    result = tools.call(
        "create_task",
        ctx,
        calendar_id=other_calendar_id,
        summary="Child",
        parent_ref=format_ref(calendar_id, "parent"),
    )
    assert result["error"] == "parent_in_other_calendar"


def test_update_goes_through_the_cache_helper(ctx, conn, calendar_id, synced_task):
    task_id = synced_task("t0")
    tools.update_task(ctx, format_ref(calendar_id, "t0"), summary="Renamed")

    row = cache.get_task_row(task_id, conn)
    assert row["summary"] == "Renamed"
    assert row["sync_state"] == SyncState.DIRTY.value
    assert conn.execute("SELECT COUNT(*) FROM pending_changes").fetchone()[0] == 1


def test_an_empty_update_is_refused(ctx, calendar_id, synced_task):
    synced_task("t0")
    assert tools.call("update_task", ctx, task_ref=format_ref(calendar_id, "t0"))["error"] == (
        "empty_update"
    )


def test_set_status_canonicalizes(ctx, conn, calendar_id, synced_task):
    """"""
    task_id = synced_task("t0", percent_complete=40)
    tools.set_status(ctx, format_ref(calendar_id, "t0"), "completed")

    row = cache.get_task_row(task_id, conn)
    assert row["status"] == Status.COMPLETED.value
    assert row["percent_complete"] == 100
    assert row["completed"] is not None


def test_an_invalid_status_lists_the_allowed_ones(ctx, calendar_id, synced_task):
    synced_task("t0")
    result = tools.call("set_status", ctx, task_ref=format_ref(calendar_id, "t0"), status="ALMOST")
    assert result["error"] == "invalid_status"
    assert "COMPLETED" in result["allowed"]


def test_set_progress_range_is_enforced(ctx, calendar_id, synced_task):
    synced_task("t0")
    ref = format_ref(calendar_id, "t0")
    assert tools.call("set_progress", ctx, task_ref=ref, percent_complete=140)["error"] == (
        "invalid_progress"
    )
    assert tools.set_progress(ctx, ref, 60)["task"]["task_ref"] == ref


# --------------------------------------------------------------- error paths


def test_writing_to_a_conflicted_task_names_the_cause(ctx, conn, calendar_id, synced_task):
    """So an agent leaves it alone rather than retrying."""
    task_id = synced_task("t0")
    with cache.tx(conn):
        cache.upsert_conflict(task_id, "l", "r", None, conn)
        cache.set_sync_state(task_id, SyncState.CONFLICT, conn)

    result = tools.call("update_task", ctx, task_ref=format_ref(calendar_id, "t0"), summary="nope")
    assert result["error"] == "task_conflicted"
    assert "DavPunk UI" in result["message"]


@pytest.mark.parametrize(
    "reason",
    [ReadOnlyReason.MULTIPART, ReadOnlyReason.OVERSIZE, ReadOnlyReason.CALENDAR_UNAVAILABLE],
)
def test_writing_to_a_read_only_task_names_the_reason(ctx, conn, calendar_id, synced_task, reason):
    """"""
    task_id = synced_task("t0")
    conn.execute("UPDATE tasks SET read_only_reason = ? WHERE id = ?", (reason.value, task_id))

    result = tools.call("update_task", ctx, task_ref=format_ref(calendar_id, "t0"), summary="nope")
    assert result["error"] == "task_read_only"
    assert result["reason"] == reason.value


# ---------------------------------------------------------------------- move


def test_move_returns_the_new_ref(ctx, calendar_id, other_calendar_id, synced_task):
    """calendar_id is part of the ref, so a move mints a new one."""
    synced_task("m1")
    result = tools.move_task(ctx, format_ref(calendar_id, "m1"), other_calendar_id)

    assert result["task_ref"] == format_ref(other_calendar_id, "m1")
    assert result["previous_task_ref"] == format_ref(calendar_id, "m1")
    assert tools.get_task(ctx, result["task_ref"])["calendar"] == other_calendar_id


@pytest.mark.parametrize("reason", [ReadOnlyReason.MULTIPART, ReadOnlyReason.OVERSIZE])
def test_a_quarantined_task_is_still_movable(
    ctx, conn, calendar_id, other_calendar_id, synced_task, reason
):
    """A move never re-serializes."""
    task_id = synced_task("m1")
    conn.execute("UPDATE tasks SET read_only_reason = ? WHERE id = ?", (reason.value, task_id))
    result = tools.move_task(ctx, format_ref(calendar_id, "m1"), other_calendar_id)
    assert "error" not in result


def test_move_subtree_defaults_to_true(ctx, conn, calendar_id, other_calendar_id, synced_task):
    synced_task("p")
    synced_task("c", parent_uid="p")
    tools.move_task(ctx, format_ref(calendar_id, "p"), other_calendar_id)
    assert tools.get_task(ctx, format_ref(other_calendar_id, "c"))["calendar"] == (
        other_calendar_id
    )


def test_move_subtree_can_be_declined(ctx, conn, calendar_id, other_calendar_id, synced_task):
    synced_task("p")
    synced_task("c", parent_uid="p")
    tools.move_task(ctx, format_ref(calendar_id, "p"), other_calendar_id, move_subtree=False)
    assert tools.get_task(ctx, format_ref(calendar_id, "c"))["calendar"] == calendar_id


# -------------------------------------------------------------------- delete


def test_deleting_a_synced_task_queues_it(ctx, conn, calendar_id, synced_task):
    task_id = synced_task("t0")
    result = tools.delete_task(ctx, format_ref(calendar_id, "t0"))

    assert result["queued"] is True
    assert result["purged_locally"] is False
    assert cache.get_task_row(task_id, conn)["sync_state"] == SyncState.PENDING_DELETE.value


def test_deleting_a_never_synced_task_purges_it(ctx, conn, calendar_id, make_task):
    """"""
    cache.create_task_local(make_task("local"), conn)
    result = tools.delete_task(ctx, format_ref(calendar_id, "local"))

    assert result["purged_locally"] is True
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


# ---------------------------------------------------------------------- sync


def test_sync_status_reports_per_remote_counts(ctx, conn, remote_id, synced_task):
    task_id = synced_task("t0")
    cache.update_task_optimistic(task_id, {"summary": "x"}, conn)
    conn.execute("UPDATE pending_changes SET blocked = 1")

    entry = tools.sync_status(ctx)["remotes"][0]
    assert entry["remote"] == "work"
    assert entry["blocked_changes"] == 1
    assert entry["syncing"] is False


def test_sync_status_uses_a_lock_probe(ctx, conn, remote_id):
    """"""
    from davpunk.core.locking import remote_sync_lock

    with remote_sync_lock("work"):
        assert tools.sync_status(ctx)["remotes"][0]["syncing"] is True


def test_sync_now_reports_busy_rather_than_failing(ctx, conn, monkeypatch):
    """SyncBusy is not an error: another holder legitimately has the lock."""
    from davpunk.core.locking import remote_sync_lock

    ctx.config.remotes.append(
        __import__("davpunk.config", fromlist=["RemoteConfig"]).RemoteConfig(
            id="work", url="https://cal.example.test/dav/"
        )
    )
    with remote_sync_lock("work"):
        result = tools.sync_now(ctx)

    assert result["results"][0]["busy"] is True


def test_sync_now_with_an_unknown_remote_is_an_error(ctx):
    assert tools.call("sync_now", ctx, remote_id="nope")["error"] == "remote_not_found"


# --------------------------------------------------------------------- audit


def test_writes_are_audited_by_ref(ctx, conn, calendar_id, synced_task):
    """"""
    synced_task("t0")
    tools.update_task(ctx, format_ref(calendar_id, "t0"), summary="Renamed")

    row = conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["tool"] == "update_task"
    assert row["task_ref"] == format_ref(calendar_id, "t0")


def test_the_audit_does_not_record_field_contents(ctx, conn, calendar_id, synced_task):
    """Task text is DEBUG-only everywhere else; the audit log is not an exception."""
    synced_task("t0")
    tools.update_task(
        ctx, format_ref(calendar_id, "t0"), description="private notes about a person"
    )
    row = conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert "private notes" not in row["detail"]
    assert "description" in row["detail"]


def test_reads_are_not_audited(ctx, conn, seeded):
    seeded(1)
    tools.list_tasks(ctx)
    tools.search_tasks(ctx, "Task")
    assert conn.execute("SELECT COUNT(*) FROM mcp_audit").fetchone()[0] == 0


def test_audit_can_be_switched_off(conn, calendar_id, synced_task):
    ctx = ToolContext(
        config=DavPunkConfig(
            mcp=McpConfig(audit=False, capabilities=McpCapabilities(read=True, write=True))
        ),
        conn=conn,
    )
    synced_task("t0")
    tools.update_task(ctx, format_ref(calendar_id, "t0"), summary="x")
    assert conn.execute("SELECT COUNT(*) FROM mcp_audit").fetchone()[0] == 0


# ------------------------------------------------------------------- server


def test_the_token_is_generated_0600_on_first_enable(davpunk_home):
    from davpunk import paths

    path = paths.mcp_token_file()
    token = server.ensure_token(path)

    assert path.stat().st_mode & 0o077 == 0
    assert len(token) > 30
    assert server.ensure_token(path) == token  # stable across calls


def test_sse_binding_is_restricted_to_loopback():
    """"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        McpConfig(bind="0.0.0.0")
    assert McpConfig(bind="127.0.0.1").bind == "127.0.0.1"


def test_mcp_disabled_exits_with_a_message(davpunk_home, capsys):
    assert server.main([]) == 2
    assert "enabled = true" in capsys.readouterr().err


def test_print_token_writes_to_stdout_and_exits(davpunk_home, capsys):
    """The one place DavPunk writes to stdout, and it is not a log line."""
    assert server.main(["--print-token"]) == 0
    out = capsys.readouterr()
    assert out.out.strip()
    assert "INFO" not in out.out


def test_the_tool_descriptions_name_their_capability():
    for name in tools.TOOLS:
        assert server._describe(name).startswith("[")


# ----------------------------------------------------- the real SDK surface


def test_a_tool_context_needs_a_connection_or_a_path():
    with pytest.raises(ValueError):
        ToolContext(config=DavPunkConfig())


def test_each_thread_gets_its_own_connection(db_path):
    """The SDK dispatches synchronous tools onto a worker thread pool, so one
    shared connection raises 'SQLite objects created in a thread can only be
    used in that same thread' on the very first call.
    """
    import threading

    cache.close_db(cache.open_db(db_path))
    ctx = ToolContext(
        config=DavPunkConfig(mcp=McpConfig(capabilities=McpCapabilities(read=True))),
        db_path=db_path,
    )
    try:
        main_conn = ctx.conn
        seen: list[object] = []

        def in_worker():
            seen.append(ctx.conn)
            seen.append(tools.list_calendars(ctx))

        thread = threading.Thread(target=in_worker)
        thread.start()
        thread.join(10)

        assert len(seen) == 2, "the worker thread raised"
        assert seen[0] is not main_conn
        assert seen[1] == {"calendars": []}
    finally:
        ctx.close()


def test_the_registered_schema_exposes_the_real_parameters(conn):
    """A **kwargs forwarder would advertise one 'kwargs' field and every call
    would fail validation."""
    import asyncio

    ctx = context(conn, read=True, write=True)
    registered = asyncio.run(server.build_server(ctx).list_tools())
    by_name = {t.name: t for t in registered}

    update = by_name["update_task"]
    schema = getattr(update, "input_schema", None) or update.inputSchema
    assert "task_ref" in schema["properties"]
    assert "summary" in schema["properties"]
    assert schema["required"] == ["task_ref"]
    assert "kwargs" not in schema["properties"]


def test_internal_parameters_are_not_advertised(conn):
    """`factory` exists to let the tests inject a runner, not for agents."""
    import asyncio

    ctx = context(conn, sync=True)
    registered = asyncio.run(server.build_server(ctx).list_tools())
    sync_tool = next(t for t in registered if t.name == "sync_now")
    schema = getattr(sync_tool, "input_schema", None) or sync_tool.inputSchema
    assert "factory" not in schema.get("properties", {})
    assert "ctx" not in schema.get("properties", {})


def test_the_server_registers_only_enabled_tools(conn):
    import asyncio

    ctx = context(conn, read=True)
    names = {t.name for t in asyncio.run(server.build_server(ctx).list_tools())}
    assert names == {"list_calendars", "list_tasks", "get_task", "search_tasks"}


def test_calling_a_tool_through_the_sdk_returns_the_structured_error(conn, calendar_id):
    import asyncio

    ctx = context(conn, read=True)
    result = asyncio.run(server.build_server(ctx).call_tool("get_task", {"task_ref": "nope"}))
    text = result.content[0].text if hasattr(result, "content") else str(result)
    assert "malformed_task_ref" in text


def test_the_token_lands_beside_the_config_by_default(davpunk_home):
    """A hardcoded ~/... default ignores XDG_CONFIG_HOME and puts the token
    somewhere other than the config.toml it belongs to."""
    from davpunk import paths

    path = McpConfig().token_path()
    assert path == paths.mcp_token_file()
    assert str(davpunk_home) in str(path)


def test_an_explicit_token_file_is_still_honoured(davpunk_home, tmp_path):
    config = McpConfig(token_file=str(tmp_path / "elsewhere.token"))
    assert config.token_path() == tmp_path / "elsewhere.token"


def test_a_tilde_in_the_token_path_is_expanded():
    assert not str(McpConfig(token_file="~/x.token").token_path()).startswith("~")
