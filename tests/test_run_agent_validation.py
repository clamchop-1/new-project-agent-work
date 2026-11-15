import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentError
from local_agent import Broker, run_agent
from test_shared_memory import FixtureCase


def tool(name, arguments):
    return {
        "function": {
            "name": name,
            "arguments": arguments
        }
    }


class RunAgentValidationTests(FixtureCase):

    def execute(self, responses, failure=False, assistant_root=None):
        output = self.base / "execution"
        replies = iter(responses)

        def fake_request(client, endpoint, payload):
            self.assertEqual(endpoint, "/api/chat")
            return {"message": next(
                replies,
                {"role": "assistant", "content": "No further tool calls."}
            )}

        with patch(
            "local_agent.Ollama.model",
            return_value={"digest": "mock-local-model"}
        ), patch(
            "local_agent.Ollama.request",
            autospec=True,
            side_effect=fake_request
        ), patch.object(
            Broker, "call", autospec=True
        ) as broker_call:

            def fake_broker(instance, name, arguments):
                if name == "finish":
                    return {
                        "summary": arguments["summary"],
                        "unfinished": arguments["unfinished"],
                        "next_actions": arguments["next_actions"],
                        "operation_performed": False
                    }
                return {"operation_performed": False}

            broker_call.side_effect = fake_broker

            if failure:
                with self.assertRaises(AgentError):
                    run_agent(
                        self.project, "mock-local-model",
                        "Read calculator.py", output,
                        max_steps=2, assistant_root=assistant_root
                    )
            else:
                run_agent(
                    self.project, "mock-local-model",
                    "Read calculator.py", output,
                    max_steps=2, assistant_root=assistant_root
                )

            executed = [
                call.args[1:]
                for call in broker_call.call_args_list
            ]

        result = json.loads(
            (output / "result.json").read_text(
                encoding="utf-8"
            )
        )
        return executed, result

    def test_unknown_tool_rejected(self):
        executed, result = self.execute([
            {
                "role": "assistant",
                "tool_calls": [
                    tool("delete_file", {"path": "calculator.py"})
                ]
            }
        ], failure=True)

        self.assertEqual(executed, [])
        self.assertEqual(result["status"], "failed")

    def test_assistant_file_scope_can_read_outside_the_git_project(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            location = Path(folder)
            (location / 'note.txt').write_text('Local notes stay local.\n', encoding='utf-8')
            broker = Broker(self.project, self.base / 'execution', assistant_root=location)

            result = broker.call('read_location_file', {'path': 'note.txt'})

            self.assertEqual(result['text'], 'Local notes stay local.')
            self.assertEqual(Path(result['root']), location.resolve())
            with self.assertRaisesRegex(AgentError, 'not authorized for this task'):
                broker.call('edit_location_file', {
                    'path': 'note.txt', 'expected_sha256': result['sha256'], 'content': 'changed\n',
                })

            authorized_broker = Broker(
                self.project, self.base / 'execution', assistant_root=location,
                assistant_write_paths=('note.txt',),
            )
            with self.assertRaisesRegex(AgentError, 'Git worktrees'):
                authorized_broker.call('edit_location_file', {
                    'path': 'note.txt', 'expected_sha256': result['sha256'], 'content': 'changed\n',
                })

    def test_authoritative_memory_verification_ignores_source_line_bounds(self):
        memory_path = self.project.root / '.local' / 'PROJECT_CONTEXT.md'
        broker = Broker(self.project, self.base / 'execution')

        result = broker.call('read_file', {
            'path': str(memory_path), 'start_line': 1, 'line_count': 139,
        })

        self.assertTrue(result['already_loaded_at_startup'])
        self.assertIn('Complete newest text is already in the initial packet', result['guidance'])
        self.assertEqual(len(result['sections']), len(broker.loaded_memory))

    def test_invalid_batch_no_partial_execution(self):
        executed, result = self.execute([
            {
                "role": "assistant",
                "tool_calls": [
                    tool("read_file", {"path": "calculator.py"}),
                    tool("delete_file", {"path": "calculator.py"})
                ]
            }
        ], failure=True)

        self.assertEqual(executed, [])
        self.assertEqual(result["writes"], [])

    def test_unexpected_argument_rejected(self):
        executed, result = self.execute([
            {
                "role": "assistant",
                "tool_calls": [
                    tool("read_file", {
                        "path": "calculator.py",
                        "delete_after_read": True
                    })
                ]
            }
        ], failure=True)

        self.assertEqual(executed, [])
        self.assertEqual(result["status"], "failed")

    def test_valid_read_and_finish(self):
        executed, result = self.execute([
            {
                "role": "assistant",
                "tool_calls": [
                    tool("read_file", {"path": "calculator.py"})
                ]
            },
            {
                "role": "assistant",
                "tool_calls": [
                    tool("finish", {
                        "summary": "Read calculator.py",
                        "unfinished": [],
                        "next_actions": []
                    })
                ]
            }
        ])

        self.assertEqual(
            [name for name, args in executed],
            ["read_file", "finish"]
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["writes"], [])

    def test_general_assistant_prose_finishes_without_coding_recovery(self):
        answer = "The requested reference pages were read successfully."
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            executed, result = self.execute([
                {"role": "assistant", "content": answer}
            ], assistant_root=Path(folder))

        self.assertEqual(executed, [])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"]["summary"], answer)
        self.assertEqual(result["completion_classification"], "completed_not_tested")

    def test_invalid_text_call_not_executed(self):
        executed, result = self.execute([
            {
                "role": "assistant",
                "content": json.dumps({
                    "name": "delete_file",
                    "arguments": {"path": "calculator.py"}
                })
            }
        ])

        self.assertEqual(executed, [])
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["writes"], [])


if __name__ == "__main__":
    unittest.main()
