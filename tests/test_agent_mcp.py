import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from mcp_server import Server, serve, PROTOCOL_KEY, CAPABILITIES_KEY


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name) / "isolated data"
        self.server = Server({"data_dir": self.data})

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, method, params=None, identifier=1):
        return self.server.handle({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}})

    def initialize(self):
        self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_legacy_discovery_and_typed_composition_share_the_cli_schema(self):
        self.initialize()
        tools = self.request("tools/list")["result"]["tools"]
        compose = next(tool for tool in tools if tool["name"] == "xhh_compose")
        self.assertIn("spec", compose["inputSchema"]["properties"])
        spec = {"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}
        response = self.request("tools/call", {"name": "xhh_compose", "arguments": {"spec": spec}})
        self.assertFalse(response["result"]["isError"], response)
        prepared = response["result"]["structuredContent"]["data"]
        self.assertEqual(prepared["expected"]["emoji_ids"], ["cube:32"])
        self.assertIn('data-emoji="cube_笑cry"', prepared["preview"]["blocks"][0]["text"])
        self.assertFalse(self.data.exists())

    def test_stateless_2026_discovery_and_protocol_errors(self):
        meta = {PROTOCOL_KEY: "2026-07-28", CAPABILITIES_KEY: {}}
        discovered = self.request("server/discover", {"_meta": meta})
        self.assertEqual(discovered["result"]["resultType"], "complete")
        self.assertIn("2025-11-25", discovered["result"]["supportedVersions"])
        response = self.request("tools/call", {"_meta": meta, "name": "xhh_emojis_search", "arguments": {"query": "无奈", "limit": 2, "avoid": ["sarcasm"]}})
        self.assertFalse(response["result"]["isError"], response)
        self.assertEqual(len(response["result"]["structuredContent"]["data"]["items"]), 2)
        unknown = self.request("tools/list", {"_meta": {PROTOCOL_KEY: "1900-01-01", CAPABILITIES_KEY: {}}})
        self.assertEqual(unknown["error"]["code"], -32022)
        missing = self.request("tools/list", {"_meta": {PROTOCOL_KEY: "2026-07-28"}})
        self.assertEqual(missing["error"]["code"], -32602)

    def test_invalid_calls_and_write_notifications_do_not_execute(self):
        self.initialize()
        invalid = self.request("tools/call", {"name": {"wrong": "type"}})
        self.assertEqual(invalid["error"]["code"], -32602)
        invalid = self.request("tools/call", {"name": "xhh_emojis_search", "arguments": {"query": "test", "unknown": "argument"}})
        self.assertTrue(invalid["result"]["isError"])
        ignored = self.server.handle({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "xhh_send", "arguments": {"confirm": True}}})
        self.assertIsNone(ignored)
        denied = self.request("tools/call", {"name": "xhh_send", "arguments": {"prepared": {}, "operation_id": "fixture"}})
        self.assertTrue(denied["result"]["isError"])
        self.assertFalse(self.data.exists())

    def test_stdio_contains_only_json_responses_and_recovers_after_parse_error(self):
        meta = {PROTOCOL_KEY: "2026-07-28", CAPABILITIES_KEY: {}}
        requests = "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"_meta": meta}}) + "\n"
        output = io.StringIO()
        self.assertEqual(serve(self.server, io.StringIO(requests), output), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"]["resultType"], "complete")

    def test_actual_isolated_stdio_process_runs_without_site_packages(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "xhh_compose", "arguments": {"spec": {"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}}}},
        ]
        result = subprocess.run([sys.executable, "-I", "-S", "-X", "utf8", str(ROOT / "scripts/mcp_server.py"), "--data-dir", str(self.data)],
                                input="".join(json.dumps(message) + "\n" for message in messages), capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertFalse(responses[1]["result"]["isError"], responses)
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
