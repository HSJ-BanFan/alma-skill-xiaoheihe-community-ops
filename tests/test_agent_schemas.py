import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jsonschema import Draft202012Validator, ValidationError
from agent_commands import command_definitions
from messages import draft_schema, prepared_schema, compose, MessageError
from xhh_sdk.exceptions import XhhConfigError
import xhh


class AgentSchemaTests(unittest.TestCase):
    def test_all_exposed_command_schemas_are_valid_json_schema(self):
        commands = command_definitions()
        self.assertGreater(len(commands), 10)
        for command in commands:
            with self.subTest(command=command["name"]):
                Draft202012Validator.check_schema(command["input_schema"])
                Draft202012Validator.check_schema(command["output_schema"])

    def test_message_schema_matches_post_comment_and_reply_requirements(self):
        validator = Draft202012Validator(draft_schema())
        content = [{"type": "native_emoji", "id": "cube:32"}]
        valid = [{"kind": "post", "content": content},
                 {"kind": "comment", "content": content, "target": {"link_id": "501"}},
                 {"kind": "reply", "content": content, "target": {"link_id": "501", "root_id": "601", "reply_id": "602"}}]
        for value in valid:
            validator.validate(value)
        invalid = [{"kind": "post", "content": content, "target": {}},
                   {"kind": "comment", "content": content},
                   {"kind": "reply", "content": content, "target": {"link_id": "501"}},
                   {"kind": "post", "content": [{"type": "native_emoji", "shortcode": "made-up"}]},
                   {"kind": "post", "content": content, "schema_version": True}]
        for value in invalid:
            with self.assertRaises(ValidationError):
                validator.validate(value)
            with self.assertRaises((XhhConfigError, ValueError)):
                compose(value, Path("unused-fixture-data"))

    def test_send_schema_requires_confirmation_and_one_account_source(self):
        command = next(command for command in command_definitions() if command["name"] == "send")
        validator = Draft202012Validator(command["input_schema"])
        prepared = compose({"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}, Path("unused-fixture-data"))
        value = {"prepared": prepared, "operation_id": "fixture", "confirm": True, "account": "fixture"}
        validator.validate(value)
        for change in ({"confirm": False}, {"config": "another-source.json"}):
            with self.assertRaises(ValidationError):
                validator.validate({**value, **change})
        no_account = {key: item for key, item in value.items() if key != "account"}
        with self.assertRaises(ValidationError):
            validator.validate(no_account)
        Draft202012Validator(prepared_schema()).validate(prepared)

    def test_non_post_schema_rejects_post_only_metadata(self):
        validator = Draft202012Validator(draft_schema())
        for kind, target in (("comment", {"link_id": "501"}),
                             ("reply", {"link_id": "501", "root_id": "601", "reply_id": "602"})):
            value = {"kind": kind, "target": target, "content": [{"type": "text", "text": "fixture"}], "title": "post-only"}
            with self.assertRaises(ValidationError):
                validator.validate(value)
            with self.assertRaises(MessageError):
                compose(value, Path("unused-fixture-data"))

    def test_compose_actual_output_matches_the_discovered_output_schema(self):
        command = next(command for command in command_definitions() if command["name"] == "compose")
        validator = Draft202012Validator(command["output_schema"])
        with tempfile.TemporaryDirectory() as temporary:
            spec = {"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = xhh.main(["--data-dir", temporary, "compose", "--spec-json", json.dumps(spec)])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            validator.validate(result)
            result["data"].pop("catalog_fingerprint")
            with self.assertRaises(ValidationError):
                validator.validate(result)


class AgentCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.data = self.folder / "isolated data"
        self.entry = Path(__file__).resolve().parents[1] / "scripts" / "xhh.py"
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("XHH_") and key != "PYTHONPATH"}

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-I", "-S", "-B", "-X", "utf8", str(self.entry),
                               "--data-dir", str(self.data), *arguments], cwd=self.folder,
                              env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def discover(self, name):
        result = self.run_cli("describe", "--command", name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        described = json.loads(result.stdout)
        self.assertEqual(described["kind"], "describe")
        self.assertEqual(len(described["data"]["commands"]), 1)
        return described["data"]["commands"][0]

    def test_default_cli_discovery_is_a_compact_offline_index(self):
        result = self.run_cli("describe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(len(result.stdout.encode("utf-8")), 12000)
        payload = json.loads(result.stdout)
        self.assertIn("compose", {command["name"] for command in payload["data"]["commands"]})
        self.assertEqual(payload["data"]["schema_lookup"], "describe --command <name>")
        self.assertFalse(self.data.exists())

    def test_creator_rules_and_post_preview_are_discoverable_without_account(self):
        creator = self.discover("creator-options")
        self.assertTrue(creator["requires_account"])
        self.assertIn("link_id", creator["input_schema"]["properties"])
        preview = self.discover("preview-post")
        self.assertFalse(preview["requires_account"])
        self.assertIn("spec_file", preview["input_schema"]["required"])
        self.assertNotIn("account", preview["input_schema"]["properties"])
        self.assertEqual(preview["parameters"]["public"]["flags"], ["--public"])
        self.assertFalse(self.data.exists())

    def test_isolated_cli_discovery_drives_native_only_file_preparation(self):
        command = self.discover("compose")
        spec = {"kind": "post", "content": [{"type": "native_emoji", "id": "cube:32"}]}
        source = self.folder / "native spec.json"
        output = self.folder / "prepared result.json"
        source.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        Draft202012Validator(command["input_schema"]).validate({"spec_file": str(source), "out": str(output)})
        self.assertEqual(command["parameters"]["spec_file"]["flags"], [])
        self.assertTrue(command["parameters"]["data_dir"]["global"])
        result = self.run_cli(*command["argv"], str(source), command["parameters"]["out"]["flags"][0], str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads(result.stdout)
        Draft202012Validator(command["output_schema"]).validate(prepared)
        self.assertEqual(prepared["data"]["expected"]["emoji_ids"], ["cube:32"])
        self.assertIn('data-emoji="cube_笑cry"', prepared["data"]["preview"]["blocks"][0]["text"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), prepared["data"])
        self.assertFalse(self.data.exists())

    def test_cli_success_and_domain_errors_conform_to_the_discovered_schema(self):
        command = self.discover("emojis get")
        validator = Draft202012Validator(command["output_schema"])
        for value, code in (("cube:43", 0), ("cube:999", 1), ("--help", 1)):
            with self.subTest(emoji_id=value):
                result = self.run_cli(*command["argv"], "--", value)
                self.assertEqual(result.returncode, code, result.stderr)
                payload = json.loads(result.stdout)
                validator.validate(payload)
                self.assertEqual(payload["ok"], code == 0)
                if code:
                    self.assertEqual(payload["error"]["code"], "unknown_emoji")
        self.assertFalse(self.data.exists())

    def test_nested_cli_search_uses_discovered_flags_and_bounded_pages(self):
        command = self.discover("emojis search")
        inputs = {"query": "无奈", "limit": 2, "offset": 2, "avoid": ["sarcasm"]}
        Draft202012Validator(command["input_schema"]).validate(inputs)
        self.assertTrue(command["parameters"]["avoid"]["repeatable"])
        parameters = command["parameters"]
        result = self.run_cli(*command["argv"], inputs["query"], parameters["limit"]["flags"][0], "2",
                              parameters["offset"]["flags"][0], "2", parameters["avoid"]["flags"][0], "sarcasm")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        Draft202012Validator(command["output_schema"]).validate(payload)
        self.assertLessEqual(len(payload["data"]["items"]), 2)
        self.assertFalse(any("sarcasm" in item["cautions"] for item in payload["data"]["items"]))
        self.assertFalse(self.data.exists())

    def test_cli_argument_errors_exit_before_local_or_platform_actions(self):
        for arguments in (("emojis", "get"), ("compose", "--spec-json", "{}", "another.json"),
                          ("emojis", "search", "test", "--limit", "101")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
