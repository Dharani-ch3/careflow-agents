"""The only path from an agent to a tool.

The gateway holds one MCP client session, logs every call and its result to the audit
trail (masked), and sorts failures into two kinds:

* :class:`ToolRefused`: the tool said no (wrong patient, no refills left, slot taken).
  Retrying won't help; the agents need to see the message and adjust.
* :class:`ToolUnavailable`: transport trouble. LangGraph's retry policy retries these.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from types import TracebackType
from typing import Any

import anyio
from mcp import Client, MCPError
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import MCPServer

from careflow.audit import AppStore


class ToolRefused(Exception):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class ToolUnavailable(Exception):
    """Transient failure talking to the tool server."""


def stdio_target(data_dir: str, today: date | None = None) -> StdioServerParameters:
    """Launch the MCP server as a subprocess, the way an external client would."""
    env = {**os.environ, "CAREFLOW_DATA_DIR": data_dir}
    if today:
        env["CAREFLOW_TODAY"] = today.isoformat()
    return StdioServerParameters(
        command=sys.executable, args=["-m", "careflow.mcp_server"], env=env
    )


class ToolGateway:
    def __init__(self, target: MCPServer | StdioServerParameters, audit: AppStore) -> None:
        self._target = target
        self._audit = audit
        self._client: Client | None = None
        self.tool_names: set[str] = set()

    async def __aenter__(self) -> ToolGateway:
        self._client = Client(self._target)
        await self._client.__aenter__()
        listed = await self._client.list_tools()
        self.tool_names = {t.name for t in listed.tools}
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
            self._client = None

    async def call(self, request_id: str, tool: str, args: dict[str, Any]) -> Any:
        if self._client is None:
            raise RuntimeError("ToolGateway used outside of 'async with'")
        if tool not in self.tool_names:
            raise ToolRefused(tool, "unknown tool")

        self._audit.record(request_id, f"tool:{tool}", "call", args)
        try:
            result = await self._client.call_tool(tool, args)
        except (OSError, TimeoutError, MCPError, anyio.BrokenResourceError) as exc:
            self._audit.record(request_id, f"tool:{tool}", "unavailable", {"error": str(exc)})
            raise ToolUnavailable(f"{tool}: {exc}") from exc

        if result.is_error:
            message = " ".join(getattr(c, "text", "") for c in result.content).strip()
            # The SDK prefixes "Error executing tool <name>: "; keep only the useful part.
            message = (
                message.split(": ", 1)[-1] if message.startswith("Error executing") else message
            )
            self._audit.record(request_id, f"tool:{tool}", "refused", {"error": message})
            raise ToolRefused(tool, message or "tool error")

        data = _unwrap(result.structured_content, result.content)
        self._audit.record(request_id, f"tool:{tool}", "ok", _summarize(data))
        return data


def _unwrap(structured: Any, content: list[Any]) -> Any:
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    texts = [getattr(c, "text", "") for c in content]
    parsed = [json.loads(t) for t in texts if t]
    return parsed[0] if len(parsed) == 1 else parsed


def _summarize(data: Any) -> Any:
    """Keep audit rows small: lists become their length plus the first item."""
    if isinstance(data, list):
        return {"count": len(data), "first": data[0] if data else None}
    return data
