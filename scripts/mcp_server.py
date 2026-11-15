"""Dependency-free stdio MCP adapter over the shared CLI definitions."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_commands import cli_arguments, tool_definitions
from assets import owned
from xhh_sdk.exceptions import XhhError
import xhh

VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
MAX_MESSAGE = 2 * 1024 * 1024


class RPCError(ValueError):
    def __init__(self, code, message, data=None):
        self.code, self.data = code, data
        super().__init__(message)


class Server:
    def __init__(self, defaults=None, *, client_factory=None):
        self.defaults = {key: str(value) if isinstance(value, Path) else value for key, value in (defaults or {}).items()
                         if value is not None and value is not False}
        self.tools = {tool["name"]: tool for tool in tool_definitions()}
        self.client_factory = client_factory
        self.version, self.initialized = None, False

    def _version(self, params):
        meta = params.get("_meta", {})
        if not isinstance(meta, dict):
            raise RPCError(-32602, "_meta must be an object")
        requested = meta.get(PROTOCOL_KEY)
        if requested is not None:
            if requested not in VERSIONS:
                raise RPCError(-32022, "Unsupported protocol version", {"supported": list(VERSIONS), "requested": requested})
            if requested == "2026-07-28" and not isinstance(meta.get(CAPABILITIES_KEY), dict):
                raise RPCError(-32602, "2026-07-28 requires clientCapabilities in params._meta")
            return requested
        if self.initialized and self.version != "2026-07-28":
            return self.version
        raise RPCError(-32602, "Protocol metadata or a legacy initialize handshake is required")

    def _result(self, value, version, *, cacheable=False):
        if version == "2026-07-28":
            value = {"resultType": "complete", **value}
            if cacheable:
                value.update(ttlMs=3600000, cacheScope="public")
        return value

    def _public_tools(self):
        result = []
        fixed_account = any(key in self.defaults for key in ("account", "config", "env"))
        for tool in self.tools.values():
            public = copy.deepcopy({key: value for key, value in tool.items() if not key.startswith("_")})
            if fixed_account:
                for constraint in public["inputSchema"].get("allOf", []):
                    branches = constraint.get("oneOf", [])
                    if {branch.get("required", [None])[0] for branch in branches} == {"account", "config", "env"}:
                        branches.append({"not": {"anyOf": [{"required": [name]} for name in ("account", "config", "env")]}})
            result.append(public)
        return result

    def _call(self, name, arguments, version):
        if not isinstance(name, str) or name not in self.tools:
            raise RPCError(-32602, "Unknown tool name")
        if not isinstance(arguments, dict):
            raise RPCError(-32602, "Tool arguments must be an object")
        tool = self.tools[name]
        fields = tool["inputSchema"]["properties"]
        values = {key: value for key, value in self.defaults.items() if key in fields}
        if any(key in arguments for key in ("account", "config", "env")):
            for key in ("account", "config", "env"):
                values.pop(key, None)
        values.update(arguments)
        output, errors = io.StringIO(), io.StringIO()
        try:
            argv = cli_arguments(tool, values)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = xhh.main(argv, **({"client_factory": self.client_factory} if self.client_factory else {}))
            value = json.loads(output.getvalue())
        except SystemExit:
            code = 2
            value = {"ok": False, "error": {"code": "invalid_arguments", "message": errors.getvalue().strip()}}
        except (XhhError, ValueError, TypeError, OSError) as error:
            code = 1
            value = {"ok": False, "error": {"code": "invalid_arguments", "message": str(error)}}
        content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
        if value.get("ok") and tool["_path"] in (("emojis", "get"), ("stickers", "get"), ("stickers", "import")):
            data = value.get("data") or {}
            preview = (data.get("item") or {}).get("preview") or data
            file = preview.get("preview_file")
            if file:
                root = Path(values.get("data_dir") or xhh.data_root())
                image = owned(root, file).read_bytes()
                if len(image) <= 1024 * 1024 and image.startswith(b"\x89PNG\r\n\x1a\n"):
                    content.append({"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": "image/png"})
        if value.get("ok") and tool["_path"] == ("compose",) and values.get("preview_file"):
            preview = (value.get("data") or {}).get("html_preview")
            if preview and Path(preview["file"]).resolve() == Path(values["preview_file"]).resolve():
                text = Path(preview["file"]).read_text(encoding="utf-8")
                content.append({"type": "resource", "resource": {"uri": "xhh-preview://" + preview["sha256"],
                                "mimeType": "text/html", "text": text}})
        result = {"content": content, "isError": code != 0 or not value.get("ok", False)}
        if version != "2025-03-26":
            result["structuredContent"] = value
        return self._result(result, version)

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        method, identifier = request["method"], request.get("id")
        if "id" not in request:
            if method == "notifications/initialized" and self.version:
                self.initialized = True
            return None
        if type(identifier) not in (str, int):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request id must be a string or integer"}}
        params = request.get("params", {})
        try:
            if not isinstance(params, dict):
                raise RPCError(-32602, "params must be an object")
            if method == "initialize":
                requested = params.get("protocolVersion")
                if not isinstance(requested, str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                    raise RPCError(-32602, "initialize requires protocolVersion, capabilities and clientInfo")
                self.version = requested if requested in VERSIONS else "2025-11-25"
                result = {"protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "xiaoheihe-tools", "version": "1.0.0"},
                          "instructions": "Use exact catalog IDs, compose locally, and send only with an explicit account, confirmation and stable operation ID. Readback matching is separate from client icon rendering."}
            else:
                version = self._version(params)
                if method == "server/discover":
                    result = self._result({"supportedVersions": list(VERSIONS), "capabilities": {"tools": {}},
                                          "serverInfo": {"name": "xiaoheihe-tools", "version": "1.0.0"},
                                          "instructions": "Native emojis use official IDs. Image stickers are caller-provided images. Compose never posts; send requires confirmation and a stable operation ID."}, version, cacheable=True)
                elif method == "ping":
                    result = self._result({}, version)
                elif method == "tools/list":
                    if params.get("cursor") is not None:
                        raise RPCError(-32602, "This server returns the complete tool list without a continuation cursor")
                    result = self._result({"tools": self._public_tools()}, version, cacheable=True)
                elif method == "tools/call":
                    result = self._call(params.get("name"), params.get("arguments", {}), version)
                else:
                    raise RPCError(-32601, "Method not found")
            return {"jsonrpc": "2.0", "id": identifier, "result": result}
        except RPCError as error:
            body = {"code": error.code, "message": str(error)}
            if error.data is not None:
                body["data"] = error.data
            return {"jsonrpc": "2.0", "id": identifier, "error": body}
        except (XhhError, ValueError, TypeError, OSError) as error:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32603, "message": str(error)}}


def serve(server, stdin, stdout):
    while True:
        line = stdin.readline(MAX_MESSAGE + 1)
        if not line:
            return 0
        if len(line) > MAX_MESSAGE:
            while line and not line.endswith("\n"):
                line = stdin.readline(MAX_MESSAGE + 1)
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Message exceeds the stdio size limit"}}
        else:
            try:
                response = server.handle(json.loads(line))
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            try:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
            except BrokenPipeError:
                return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--account")
    source.add_argument("--config", type=Path)
    source.add_argument("--env", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--protocol", choices=("web", "app"))
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return serve(Server(vars(args)), sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
