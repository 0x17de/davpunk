"""The FastMCP server.  All four capabilities are off by default.

Transport hardening:

* **stdio** is the recommendation, and the default.
* **sse** binds ``127.0.0.1`` only (enforced at config validation) and requires
  a bearer token read from ``token_file``, which is generated 0600 on first
  enable.

Logging goes to **stderr only**.  On the stdio transport stdout *is* the
protocol channel, and a single stray ``print`` corrupts the stream.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from davpunk import logging_setup, paths, preflight
from davpunk.config import ConfigError, DavPunkConfig, load_config
from davpunk.core import cache
from davpunk.mcp import tools
from davpunk.mcp.tools import ToolContext

log = logging.getLogger("davpunk.mcp.server")

TOKEN_BYTES = 32


class TokenError(Exception):
    """The bearer token could not be read or written."""


def ensure_token(config_or_path) -> str:
    """Read the bearer token, generating one on first use.

    Accepts an :class:`McpConfig` or a bare path; the path form keeps the
    plaintext behaviour and exists because that is all the older callers and
    ``doctor`` need.
    """
    config = None if isinstance(config_or_path, (str, Path)) else config_or_path
    if config is None:
        return _ensure_plain_token(Path(config_or_path).expanduser())
    if not config.token_is_encrypted:
        return _ensure_plain_token(config.token_path())
    return _ensure_encrypted_token(config)


def _ensure_plain_token(path: Path) -> str:
    if path.exists():
        if path.stat().st_mode & 0o077:
            log.warning("MCP token %s is not 0600; fix it with davpunk doctor --fix", path)
        return path.read_text().strip()

    paths.ensure_dir(path.parent)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    log.info("Generated an MCP bearer token at %s", path)
    return token


def _ensure_encrypted_token(config) -> str:
    """The token, GnuPG-encrypted at rest and decrypted here.

    Same trade-off as a CalDAV credential: safe if the file is ever backed up
    or synced somewhere it should not be, and unreadable while gpg-agent is
    cold — so the failure is reported as such rather than as a bad token.
    """
    from davpunk.core import credentials

    path = config.token_path()
    if path.exists():
        try:
            return credentials.decrypt_credential(path).strip()
        except credentials.CredentialLocked as exc:
            raise TokenError(
                f"the MCP token in {path} cannot be decrypted right now — "
                f"gpg-agent may be cold. {exc}"
            ) from exc
        except credentials.CredentialError as exc:
            raise TokenError(f"the MCP token in {path} could not be read: {exc}") from exc

    token = secrets.token_urlsafe(TOKEN_BYTES)
    try:
        credentials.store_credential(token, path, config.token_gpg_key_id)
    except credentials.CredentialError as exc:
        raise TokenError(f"could not write the encrypted MCP token: {exc}") from exc
    log.info("Generated an encrypted MCP bearer token at %s", path)
    return token


def rotate_token(config) -> str:
    """Replace the token, invalidating whatever a client currently holds."""
    from davpunk.core import credentials

    for candidate in (config.token_path(), config.plain_token_path()):
        candidate.unlink(missing_ok=True)

    token = secrets.token_urlsafe(TOKEN_BYTES)
    if config.token_is_encrypted:
        try:
            credentials.store_credential(token, config.token_path(), config.token_gpg_key_id)
        except credentials.CredentialError as exc:
            raise TokenError(f"could not write the encrypted MCP token: {exc}") from exc
    else:
        path = config.token_path()
        paths.ensure_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    log.info("Rotated the MCP bearer token")
    return token


def enabled_capabilities(config: DavPunkConfig) -> list[str]:
    caps = config.mcp.capabilities
    return [name for name in ("read", "write", "delete", "sync") if getattr(caps, name)]


def _server_class():
    """The MCP SDK renamed ``FastMCP`` to ``MCPServer`` in 2.0.

    Both expose the ``add_tool`` / ``run`` / ``sse_app`` surface this module
    uses, so supporting the pair is one import, not an abstraction layer.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build_server(ctx: ToolContext):
    """Register only the tools whose capability is enabled.

    A disabled tool is *absent* rather than present-and-erroring: an agent
    should not spend a turn discovering it is not allowed to do something.
    """
    server = _server_class()("davpunk")
    enabled = set(enabled_capabilities(ctx.config))
    exposed = [name for name, (_f, c) in tools.TOOLS.items() if c in enabled]

    for name in exposed:
        server.add_tool(_wrap(name, ctx), name=name, description=_describe(name))

    log.info(
        "Registered %d tool(s) for capabilities: %s",
        len(exposed),
        ", ".join(sorted(enabled)) or "(none)",
    )
    return server


#: Parameters that exist for testing or wiring and must not reach the schema.
_INTERNAL_PARAMS = frozenset({"ctx", "factory"})


def _wrap(name: str, ctx: ToolContext):
    """Bind ``ctx`` and re-publish the tool's own signature.

    The SDK derives each tool's JSON schema by introspecting the callable, so a
    plain ``**kwargs`` forwarder would advertise a single ``kwargs`` field and
    every call would fail validation.  Copying the signature (minus the
    injected parameters) is what makes the tools actually callable.
    """
    fn, _capability = tools.TOOLS[name]

    signature = inspect.signature(fn)
    parameters = [p for p in signature.parameters.values() if p.name not in _INTERNAL_PARAMS]
    public = signature.replace(parameters=parameters, return_annotation=dict)

    def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = public.bind(*args, **kwargs)
        bound.apply_defaults()
        return tools.call(name, ctx, **bound.arguments)

    invoke.__name__ = name
    invoke.__doc__ = fn.__doc__
    invoke.__signature__ = public  # type: ignore[attr-defined]
    invoke.__annotations__ = {
        p.name: p.annotation for p in parameters if p.annotation is not inspect.Parameter.empty
    }
    invoke.__annotations__["return"] = dict
    return invoke


def _describe(name: str) -> str:
    fn, capability = tools.TOOLS[name]
    first_line = (fn.__doc__ or "").strip().split("\n")[0]
    return f"[{capability}] {first_line}" if first_line else f"[{capability}] {name}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="davpunk-mcp", description="DavPunk MCP server (all capabilities off by default)"
    )
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--log-level", help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print the SSE bearer token (generating it if needed) and exit",
    )
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="replace the SSE bearer token, print the new one, and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # stderr only: stdout is the stdio protocol channel.
    logging_setup.setup_logging("mcp", args.log_level)
    preflight.preflight_or_die()
    paths.ensure_runtime_dirs()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.print_token or args.rotate_token:
        try:
            token = rotate_token(config.mcp) if args.rotate_token else ensure_token(config.mcp)
        except TokenError as exc:
            log.error("%s", exc)
            return 2
        # The one place DavPunk writes to stdout, and it is not a log line.
        print(token)
        return 0

    if not config.mcp.enabled:
        log.error("MCP is disabled; set [davpunk.mcp] enabled = true in %s", paths.config_file())
        return 2

    capabilities = enabled_capabilities(config)
    if not capabilities:
        log.warning("MCP is enabled but every capability is off; the server will expose no tools")

    # Open once here to run migrations and recovery, then hand the *path* to
    # the tool context: the SDK dispatches tools onto worker threads, and each
    # of those needs its own connection.
    conn, report = cache.open_or_recover()
    if report is not None:
        log.error("%s", report.summary())
    cache.reconcile_remotes(config.remotes, conn)
    cache.close_db(conn)

    ctx = ToolContext(config=config, db_path=paths.database_file())
    try:
        server = build_server(ctx)
    except ImportError as exc:
        log.error("the MCP server needs the 'mcp' package (%s); try: uv sync --extra mcp", exc)
        ctx.close()
        return 2

    try:
        if config.mcp.transport == "sse":
            token = ensure_token(config.mcp)
            log.info(
                "Serving SSE on %s:%d (bearer token in %s)",
                config.mcp.bind,
                config.mcp.port,
                config.mcp.token_path(),
            )
            _run_sse(server, config, token)
        else:
            log.info("Serving over stdio")
            server.run(transport="stdio")
    except TokenError as exc:
        log.error("%s", exc)
        ctx.close()
        return 2
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        ctx.close()
    return 0


def _run_sse(server, config: DavPunkConfig, token: str) -> None:
    """SSE with a bearer-token gate in front of the app.

    The bind address is validated as loopback at config load, so this cannot be
    exposed to a network by editing one line.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class BearerToken(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(presented, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = server.sse_app()
    app.add_middleware(BearerToken)
    uvicorn.run(app, host=config.mcp.bind, port=config.mcp.port, log_config=None)


if __name__ == "__main__":
    sys.exit(main())
