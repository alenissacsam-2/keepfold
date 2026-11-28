"""Every use of the mcp SDK's internals and provisional APIs, in one file (D7 §4.1), so an SDK
bump (pinned exactly in pyproject.toml) touches only this module: the tool manager, the
session's `client_params`, the middleware list (provisional: "the signature may change in a 2.x
minor") and the names the tools' string annotations resolve to. The rest of the package uses
the SDK's public API (MCPServer, add_tool, Context, ...) or these helpers."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

SDK_NAMES = ("Context", "Elicit", "ElicitationResult", "MCPServer", "Resolve")
FIX_NEXT = "Fix the call as the message says and retry."  # mcp_server's default error `next`


def inject_sdk_names(cls: type) -> None:
    """Put SDK_NAMES into the globals of `cls`'s module and each base's (the mcp_tools mixins):
    the tools' string annotations (Context, the click's Resolve) resolve there."""
    from mcp.server import mcpserver as sdk

    names = {n: getattr(sdk, n) for n in SDK_NAMES}
    for c in cls.__mro__[:-1]:  # not `object`
        vars(sys.modules[c.__module__]).update(names)


def client_name(ctx: Any) -> str:
    """The MCP client's name from its `initialize` (or a stateless request's _meta envelope,
    which the SDK copies into client_params); "" when the client sent none or there is no ctx."""
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    return str(getattr(getattr(params, "client_info", None), "name", "") or "")


def input_schemas(server: Any) -> dict[str, dict]:
    """{tool name: its input JSON schema}, the live dicts tools/list serves (edit them in place)."""
    return {t.name: t.parameters for t in server._tool_manager.list_tools()}


def add_middleware(server: Any, fn) -> None:
    """Append `fn(ctx, call_next)` to the server's middleware chain (innermost last)."""
    server.middleware.append(fn)


def add_contract(server: Any) -> None:
    """Refuse a `tools/call` with an argument the tool's schema does not name, with Keepfold's
    usual `invalid_argument` error naming the real ones. Without this the SDK drops an unknown
    argument silently, or (a misspelt required one) fails pydantic validation with its own text.
    Answered here, before validation: a tool wrapper never sees the call when the misspelling
    leaves a required argument missing."""
    async def contract(ctx, call_next):
        p = ctx.params
        if ctx.method != "tools/call" or not isinstance(p, Mapping):
            return await call_next(ctx)
        name, args = p.get("name"), p.get("arguments")
        tool = server._tool_manager.get_tool(name) if isinstance(name, str) else None
        if tool is None or not isinstance(args, Mapping):  # the SDK answers these itself
            return await call_next(ctx)
        props = tool.parameters.get("properties", {})
        if not (unknown := sorted(set(args) - set(props))):
            return await call_next(ctx)
        known = ", ".join(props) or "none"
        bad = ", ".join(f"'{u}'" for u in unknown)
        err = {"error": "invalid_argument",
               "message": f"{name} has no argument{'s' * (len(unknown) > 1)} {bad}; its "
                          f"arguments are: {known}.",
               "next": FIX_NEXT}
        # the tool's own result conversion: the same wire shape as any tool's error dict
        return tool.fn_metadata.convert_result(err)

    add_middleware(server, contract)
