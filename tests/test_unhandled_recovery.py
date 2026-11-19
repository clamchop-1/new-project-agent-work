import json
import unittest
from unittest.mock import patch

from local_agent import Broker, run_agent
from test_shared_memory import FixtureCase


class UnhandledRecoveryTests(FixtureCase):

    def test_alternating_prose_exhausts_recovery_budget(self):
        output = self.base / "unhandled_recovery"
        responses = [
            "Please provide the file paths.",
            "I will read the files once paths are provided.",
            "Please provide the file paths.",
            "I will read the files once paths are provided.",
        ]
        seen_messages = []
        executed = []

        def fake_request(client, endpoint, payload):
            self.assertEqual(endpoint, "/api/chat")
            seen_messages.append(
                json.loads(json.dumps(payload["messages"]))
            )
            index = len(seen_messages) - 1
            return {
                "message": {
                    "role": "assistant",
                    "content": responses[index],
                }
            }

        def fake_broker(instance, name, arguments):
            executed.append(name)
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
                "Read calculator.py and run pinned tests",
                output,
                max_steps=8,
            )

        self.assertEqual(
            result["status"],
            "repeated_unhandled_response",
        )
        self.assertEqual(result["steps"], 4)
        self.assertEqual(executed, [])

        recovery_messages = [
            message["content"]
            for messages in seen_messages
            for message in messages
            if message.get("role") == "user"
            and "Completion contract:" in message.get("content", "")
        ]
        self.assertTrue(recovery_messages)
        self.assertTrue(all('registered finish tool' in text for text in recovery_messages))
        self.assertTrue(all('list_files' not in text for text in recovery_messages))


    def test_valid_tool_call_resets_recovery_budget(self):
        output = self.base / "recovery_reset"
        responses = [
            {"role": "assistant", "content": "Please provide paths."},
            {"role": "assistant", "content": "Waiting for file paths."},
            {
                "role": "assistant",
                "tool_calls": [{
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "calculator.py"},
                    }
                }],
            },
            {"role": "assistant", "content": "Please provide paths."},
            {"role": "assistant", "content": "Waiting for file paths."},
            {
                "role": "assistant",
                "tool_calls": [{
                    "function": {
                        "name": "finish",
                        "arguments": {
                            "summary": "Read completed.",
                            "unfinished": [],
                            "next_actions": [],
                        },
                    }
                }],
            },
        ]

        replies = iter(responses)
        executed = []

        def fake_request(client, endpoint, payload):
            return {"message": next(replies)}

        def fake_broker(instance, name, arguments):
            executed.append(name)
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
            Broker,
            "call",
            autospec=True,
            side_effect=fake_broker,
        ):
            result = run_agent(
                self.project,
                "mock-local-model",
                "Read calculator.py",
                output,
                max_steps=8,
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["steps"], 6)
        self.assertEqual(executed, ["read_file", "finish"])


if __name__ == "__main__":
    unittest.main()
