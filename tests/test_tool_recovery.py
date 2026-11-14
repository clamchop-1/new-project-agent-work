import json
import unittest
from unittest.mock import patch

from local_agent import Broker, run_agent
from test_shared_memory import FixtureCase


def tool(name, arguments):
    return {
        "function": {
            "name": name,
            "arguments": arguments,
        }
    }


def finish_call():
    return tool("finish", {
        "summary": "Recovery test completed.",
        "unfinished": [],
        "next_actions": [],
    })


class ToolRecoveryTests(FixtureCase):

    def execute_recovery(self, first_error):
        output = self.base / "recovery_execution"
        captured_messages = []
        executed = []

        responses = iter([
            {
                "role": "assistant",
                "tool_calls": [
                    tool("create_file", {
                        "path": "calculator.py",
                        "content": "replacement",
                    })
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    tool("replace_text", {
                        "path": "calculator.py",
                        "expected_sha256": "a" * 64,
                        "old": "old",
                        "new": "new",
                    })
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [finish_call()],
            },
        ])

        def fake_request(client, endpoint, payload):
            self.assertEqual(endpoint, "/api/chat")
            captured_messages.append(
                json.loads(json.dumps(payload["messages"]))
            )
            return {
                "message": next(responses)
            }

        def fake_broker(instance, name, arguments):
            executed.append(name)

            if name == "create_file":
                return {
                    "error": first_error,
                    "operation_performed": False,
                }

            if name == "finish":
                return {
                    **arguments,
                    "operation_performed": False,
                }

            return {"operation_performed": False}

        with patch(
            "local_agent.Ollama.model",
            return_value={"digest": "mock-local-model"},
        ), patch(
            "local_agent.Ollama.request",
            autospec=True,
            side_effect=fake_request,
        ), patch.object(
            Broker, "call",
            autospec=True,
            side_effect=fake_broker,
        ):
            result = run_agent(
                self.project,
                "mock-local-model",
                "Test tool error recovery",
                output,
                max_steps=3,
            )

        return result, captured_messages, executed

    def test_existing_file_recovery(self):
        result, messages, executed = self.execute_recovery(
            "Existing file preserved; creation cannot overwrite."
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(
            executed,
            ["create_file", "replace_text", "finish"],
        )

        feedback = json.dumps(messages[1])
        self.assertIn("Do not retry create_file", feedback)
        self.assertIn("replace_text", feedback)

    def test_authorization_error_not_treated_as_overwrite(self):
        result, messages, executed = self.execute_recovery(
            "Path is not authorized for writing."
        )

        self.assertEqual(result["status"], "complete")

        feedback = json.dumps(messages[1])
        self.assertNotIn("Do not retry create_file", feedback)
        self.assertNotIn("Tool recovery guidance:", feedback)


if __name__ == "__main__":
    unittest.main()
