import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jsonschema import Draft202012Validator, ValidationError
from agent_commands import tool_definitions
from messages import draft_schema, prepared_schema, compose
from xhh_sdk.exceptions import XhhConfigError


class AgentSchemaTests(unittest.TestCase):
    def test_all_exposed_tool_schemas_are_valid_json_schema(self):
        tools = tool_definitions()
        self.assertGreater(len(tools), 10)
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                Draft202012Validator.check_schema(tool["inputSchema"])

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

    def test_tool_send_requires_confirmation_and_one_account_source(self):
        tool = next(tool for tool in tool_definitions() if tool["name"] == "xhh_send")
        validator = Draft202012Validator(tool["inputSchema"])
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


if __name__ == "__main__":
    unittest.main()
