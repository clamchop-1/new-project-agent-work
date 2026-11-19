import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentError, file_hash
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

    def execute(self, responses, failure=False, assistant_root=None, **run_options):
        output = self.base / "execution"
        run_options.setdefault('max_steps', 2)
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
                        assistant_root=assistant_root, **run_options
                    )
            else:
                run_agent(
                    self.project, "mock-local-model",
                    "Read calculator.py", output,
                    assistant_root=assistant_root, **run_options
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
        executed, result = self.execute([
            {"role": "assistant", "content": answer}
        ], completion_policy='conversational')

        self.assertEqual(executed, [])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["summary"]["summary"], answer)
        self.assertEqual(result["completion_classification"], "completed_not_tested")

    def test_conversational_policy_rejects_execution_requirements(self):
        with self.assertRaises(AgentError):
            self.execute([], completion_policy='conversational',
                         required_operations=('read_file',))

    def test_missing_finish_signal_does_not_complete_tool_required_run(self):
        executed, result = self.execute([
            {"role": "assistant", "content": "The answer is 42."}
        ], max_steps=2)

        self.assertEqual(executed, [])
        self.assertEqual(result['status'], 'step_limit')
        self.assertNotEqual(result['completion_classification'], 'completed_not_tested')

    def test_finish_with_missing_required_test_receipt_is_incomplete(self):
        executed, result = self.execute([
            {"role": "assistant", "tool_calls": [
                tool('run_tests', {})
            ]},
            {"role": "assistant", "tool_calls": [
                tool('finish', {'summary': 'Finished', 'unfinished': [], 'next_actions': []})
            ]},
        ], test_config=self.test_config, required_operations=('run_tests',))

        self.assertEqual([name for name, _ in executed], ['run_tests', 'finish'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['verified_test_status'], 'not_run')
        self.assertIn('required test receipt', ' '.join(result['completion_blockers']))

    def test_finish_with_unfinished_operations_is_not_successful(self):
        executed, result = self.execute([
            {"role": "assistant", "tool_calls": [tool('finish', {
                'summary': 'Partially done', 'unfinished': ['run required tests'],
                'next_actions': ['Run tests'],
            })]}
        ])

        self.assertEqual([name for name, _ in executed], ['finish'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('finish reported unfinished work', result['completion_blockers'])

    def test_file_modification_requires_a_write_receipt(self):
        executed, result = self.execute([
            {"role": "assistant", "tool_calls": [tool('create_file', {
                'path': 'new.txt', 'content': 'new content',
            })]},
            {"role": "assistant", "tool_calls": [tool('finish', {
                'summary': 'Created file', 'unfinished': [], 'next_actions': [],
            })]},
        ], writes=('new.txt',), completion_policy='file_modification')

        self.assertEqual([name for name, _ in executed], ['create_file', 'finish'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('no authorized file-write receipt was produced',
                      result['completion_blockers'])

    def test_repeated_identical_tool_call_is_rejected_and_cannot_complete(self):
        executed, result = self.execute([
            {"role": "assistant", "tool_calls": [
                tool('read_file', {'path': 'calculator.py'})
            ]},
            {"role": "assistant", "tool_calls": [
                tool('read_file', {'path': 'calculator.py'})
            ]},
            {"role": "assistant", "tool_calls": [
                tool('finish', {'summary': 'Finished', 'unfinished': [], 'next_actions': []})
            ]},
        ], max_steps=3)

        self.assertEqual([name for name, _ in executed], ['read_file', 'finish'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(any(row['category'] == 'protocol'
                            for row in result['blocked_operations']))

    def test_required_tests_can_repeat_after_authorized_source_change(self):
        output = self.base / 'tests_after_edit_execution'
        (self.project.root / 'stats.py').write_text(
            'def clamp(value, low, high):\n    return min(max(value, low), high)\n',
            encoding='utf-8',
        )
        self.project.git('add', 'stats.py')
        self.project.git('-c', 'user.name=Local Agent Fixture',
                         '-c', 'user.email=fixture@example.invalid',
                         'commit', '-m', 'Seed passing clamp implementation')
        calculator = self.project.root / 'calculator.py'
        old = calculator.read_text(encoding='utf-8')
        corrected = (
            'def average(values):\n'
            '    if not values:\n'
            '        raise ValueError("average of empty sequence")\n'
            '    return sum(values) / len(values)\n'
        )
        sha = file_hash(calculator)
        replies = iter([
            {'role': 'assistant', 'tool_calls': [tool('run_tests', {})]},
            {'role': 'assistant', 'tool_calls': [tool('read_file', {
                'path': 'calculator.py',
            })]},
            {'role': 'assistant', 'tool_calls': [tool('replace_text', {
                'path': 'calculator.py', 'expected_sha256': sha,
                'old': old, 'new': corrected,
            })]},
            {'role': 'assistant', 'tool_calls': [tool('run_tests', {})]},
            {'role': 'assistant', 'tool_calls': [tool('finish', {
                'summary': 'Corrected average; pinned tests passed.',
                'unfinished': [], 'next_actions': [],
            })]},
        ])

        def fake_request(client, endpoint, payload):
            return {'message': next(replies)}

        with patch('local_agent.Ollama.model',
                   return_value={'digest': 'mock-local-model'}), patch(
            'local_agent.Ollama.request', autospec=True, side_effect=fake_request
        ):
            result = run_agent(
                self.project, 'mock-local-model', 'Fix the fixture defect', output,
                writes=('calculator.py',), test_config=self.test_config,
                completion_policy='file_modification',
                required_operations=('read_file', 'replace_text', 'run_tests'),
                max_steps=5,
            )

        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['verified_test_status'], 'passed')
        self.assertEqual(len(result['writes']), 1)
        self.assertEqual(len(result['commands']), 2)
        self.assertEqual(result['completion_classification'], 'completed_verified')

    def test_unsupported_markup_cannot_complete_as_prose(self):
        executed, result = self.execute([
            {"role": "assistant", "content": '<function=finish>{"summary":"done"}</function>'}
        ], max_steps=1, completion_policy='conversational')

        self.assertEqual(executed, [])
        self.assertEqual(result['status'], 'step_limit')

    def test_finish_is_single_call_and_validates_completion_schema(self):
        expected = {'summary': 'Exact marker: PHASE2A_OK', 'unfinished': [], 'next_actions': []}
        executed, result = self.execute([
            {"role": "assistant", "tool_calls": [tool('finish', expected)]}
        ])

        self.assertEqual(executed, [('finish', expected)])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['summary']['summary'], 'Exact marker: PHASE2A_OK')
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['writes'], [])

    def test_unauthorized_write_attempt_cannot_be_hidden_by_finish(self):
        output = self.base / 'unauthorized_write_execution'
        original = (self.project.root / 'calculator.py').read_bytes()
        replies = iter([
            {'role': 'assistant', 'tool_calls': [tool('create_file', {
                'path': 'calculator.py', 'content': 'unauthorized replacement',
            })]},
            {'role': 'assistant', 'tool_calls': [tool('finish', {
                'summary': 'Completed', 'unfinished': [], 'next_actions': [],
            })]},
        ])

        def fake_request(client, endpoint, payload):
            return {'message': next(replies)}

        with patch('local_agent.Ollama.model',
                   return_value={'digest': 'mock-local-model'}), patch(
            'local_agent.Ollama.request', autospec=True, side_effect=fake_request
        ):
            result = run_agent(self.project, 'mock-local-model', 'Do not write',
                               output, max_steps=2)

        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['writes'], [])
        self.assertEqual((self.project.root / 'calculator.py').read_bytes(), original)
        self.assertTrue(any(row['category'] == 'authorization'
                            for row in result['blocked_operations']))

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
