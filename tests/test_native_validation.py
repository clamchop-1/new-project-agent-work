import unittest

from agent_core import AgentError
from local_agent import TOOLS, validate_native_tool_calls


def call(name, args):
    return {"function": {"name": name, "arguments": args}}


class NativeValidationTests(unittest.TestCase):

    def test_valid_read(self):
        result = validate_native_tool_calls(
            [call("read_file", {"path": "calculator.py"})],
            TOOLS
        )
        self.assertEqual(result[0]["function"]["name"], "read_file")

    def test_unknown_tool(self):
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("shell", {"command": "dir"})], TOOLS
            )

    def test_missing_required(self):
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("read_file", {})], TOOLS
            )

    def test_extra_argument(self):
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("read_file", {
                    "path": "calculator.py",
                    "command": "anything"
                })], TOOLS
            )

    def test_nested_arguments_wrapper_is_rejected(self):
        with self.assertRaisesRegex(AgentError, 'Unexpected native tool argument'):
            validate_native_tool_calls(
                [call('finish', {
                    'arguments': '{"summary":"Done","unfinished":[],"next_actions":[]}'
                })],
                TOOLS,
            )

    def test_wrong_type(self):
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("read_file", {"path": 123})], TOOLS
            )

    def test_excessive_calls(self):
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("list_files", {}) for _ in range(9)], TOOLS
            )

    def test_finish_only_mode(self):
        finish_only = [
            tool for tool in TOOLS
            if tool["function"]["name"] == "finish"
        ]
        with self.assertRaises(AgentError):
            validate_native_tool_calls(
                [call("read_file", {"path": "calculator.py"})],
                finish_only
            )

    def test_json_string_arguments(self):
        result = validate_native_tool_calls(
            [call("read_file", '{"path":"calculator.py"}')],
            TOOLS
        )
        self.assertEqual(
            result[0]["function"]["arguments"]["path"],
            "calculator.py"
        )


if __name__ == "__main__":
    unittest.main()
