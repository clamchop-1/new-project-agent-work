import unittest
from unittest.mock import patch

from local_agent import (
    AgentError,
    TOOLS,
    validate_native_tool_calls,
    defer_premature_finish,
)


class ExecutionValidationTests(unittest.TestCase):

    def execute_batch(self, calls, active_tools=TOOLS):
        """Simulate the validation boundary before broker execution."""
        executed = []

        def fake_broker_call(name, args):
            executed.append((name, args))
            return {"operation_performed": False}

        validated = validate_native_tool_calls(
            calls, active_tools
        )
        validated = defer_premature_finish(validated)

        for call in validated:
            function = call["function"]
            fake_broker_call(
                function["name"],
                function["arguments"]
            )

        return executed

    def test_unknown_tool_never_executes(self):
        calls = [
            {
                "function": {
                    "name": "delete_file",
                    "arguments": {"path": "calculator.py"}
                }
            }
        ]

        with self.assertRaises(AgentError):
            self.execute_batch(calls)

    def test_invalid_batch_does_not_partially_execute(self):
        calls = [
            {
                "function": {
                    "name": "list_files",
                    "arguments": {}
                }
            },
            {
                "function": {
                    "name": "delete_file",
                    "arguments": {"path": "calculator.py"}
                }
            }
        ]

        with self.assertRaises(AgentError):
            self.execute_batch(calls)

    def test_unexpected_argument_rejected(self):
        calls = [
            {
                "function": {
                    "name": "read_file",
                    "arguments": {
                        "path": "calculator.py",
                        "delete_after_read": True
                    }
                }
            }
        ]

        with self.assertRaises(AgentError):
            self.execute_batch(calls)

    def test_valid_read_executes(self):
        calls = [
            {
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "calculator.py"}
                }
            }
        ]

        executed = self.execute_batch(calls)

        self.assertEqual(
            executed,
            [("read_file", {"path": "calculator.py"})]
        )

    def test_premature_finish_is_deferred(self):
        calls = [
            {
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "calculator.py"}
                }
            },
            {
                "function": {
                    "name": "finish",
                    "arguments": {
                        "summary": "Finished",
                        "unfinished": [],
                        "next_actions": []
                    }
                }
            }
        ]

        executed = self.execute_batch(calls)

        self.assertEqual(
            [name for name, _ in executed],
            ["read_file"]
        )

    def test_finish_only_mode_rejects_read(self):
        finish_only = [
            tool for tool in TOOLS
            if tool["function"]["name"] == "finish"
        ]

        calls = [
            {
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "calculator.py"}
                }
            }
        ]

        with self.assertRaises(AgentError):
            self.execute_batch(calls, finish_only)


if __name__ == "__main__":
    unittest.main()
