"""Stdio MCP server the Claude CLI spawns per run: it only relays tool calls to the worker.

Stdlib only and credential-free: the tool list comes from a file in the run workspace and every
call is forwarded over the run's Unix socket to the worker (agent.claude_tools.ToolRelay), which owns DB access,
fences and validation. Protocol subset: initialize, ping, tools/list, tools/call.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Callable, Iterable
from typing import IO, Any

DEFAULT_PROTOCOL = "2025-03-26"
RELAY_TIMEOUT_S = 330.0   # TOOL_WAIT_S (300) plus slack: the worker enforces the real deadline
Forward = Callable[[str, Any], tuple[bool, str]]


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(message: Any, tools: list[dict[str, Any]], forward: Forward) -> dict[str, Any] | None:
    """One JSON-RPC message -> its response (None for notifications)."""
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600,
                      "invalid request")
    method, msg_id, params = message["method"], message.get("id"), message.get("params") or {}
    if "id" not in message:
        return None
    if method == "initialize":
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(msg_id, {"protocolVersion": version if isinstance(version, str) else DEFAULT_PROTOCOL,
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "aibroker-bridge", "version": "1"}})
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        if name not in {tool["name"] for tool in tools}:
            return _error(msg_id, -32602, "unknown tool")
        arguments = params.get("arguments")
        is_error, text = forward(name, {} if arguments is None else arguments)
        return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _error(msg_id, -32601, "method not found")


def serve(lines: Iterable[str], out: IO[str], tools: list[dict[str, Any]], forward: Forward) -> None:
    """Newline-delimited JSON-RPC loop until EOF."""
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response: dict[str, Any] | None = _error(None, -32700, "parse error")
        else:
            response = handle_message(message, tools, forward)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()


def socket_forward(path: str) -> Forward:
    """Forward one call per connection to the worker relay; failures become tool errors."""
    def forward(name: str, arguments: Any) -> tuple[bool, str]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(RELAY_TIMEOUT_S)
                conn.connect(path)
                conn.sendall((json.dumps({"name": name, "arguments": arguments}) + "\n").encode())
                reply = json.loads(conn.makefile("rb").readline())
            return bool(reply["is_error"]), str(reply["text"])
        except (OSError, ValueError, KeyError, TypeError):
            return True, "the broker could not deliver this tool call"
    return forward


def load_tools(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("tools file must hold a list")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aibroker.agent.mcp_bridge")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--tools", required=True)
    args = parser.parse_args(argv)
    serve(sys.stdin, sys.stdout, load_tools(args.tools), socket_forward(args.socket))
    return 0


if __name__ == "__main__":
    sys.exit(main())
