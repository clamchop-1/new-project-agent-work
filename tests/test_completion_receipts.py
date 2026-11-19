import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import file_hash
from local_agent import Broker, run_agent, verify_test_receipts
from test_shared_memory import FixtureCase


def tool(name, arguments):
    return {'function': {'name': name, 'arguments': arguments}}


class CompletionReceiptIntegrationTests(FixtureCase):
    def run_to_finish(self, edit_after_test):
        output = self.base / ('stale_run' if edit_after_test else 'failed_run')
        original = (self.project.root / 'calculator.py').read_text(encoding='utf-8')
        responses = [
            {'role': 'assistant', 'tool_calls': [tool('run_tests', {})]},
        ]
        if edit_after_test:
            responses.append({
                'role': 'assistant',
                'tool_calls': [tool('replace_text', {
                    'path': 'calculator.py',
                    'expected_sha256': file_hash(self.project.root / 'calculator.py'),
                    'old': original,
                    'new': 'def average(values):\n    if not values:\n        raise ValueError("empty")\n    return sum(values) / len(values)\n',
                })],
            })
        responses.append({'role': 'assistant', 'tool_calls': [tool('finish', {
            'summary': 'The model claims completion.',
            'unfinished': [],
            'next_actions': [],
        })]})
        replies = iter(responses)
        original_broker_call = Broker.call

        def fake_request(client, endpoint, payload):
            return {'message': next(replies), 'prompt_eval_count': 100, 'eval_count': 10}

        def fake_broker(instance, name, arguments):
            if name == 'finish':
                return arguments
            if name == 'run_tests':
                return instance.execute('tests')
            if name == 'replace_text':
                return original_broker_call(instance, name, arguments)
            raise AssertionError('Unexpected tool: ' + name)

        with patch(
            'local_agent.Ollama.model',
            return_value={'digest': 'receipt-test-model'},
        ), patch(
            'local_agent.Ollama.request',
            autospec=True,
            side_effect=fake_request,
        ), patch.object(
            Broker, 'call', autospec=True, side_effect=fake_broker,
        ):
            return run_agent(
                self.project,
                'receipt-test-model',
                'Run the pinned tests and report accurately.',
                output,
                writes=('calculator.py',) if edit_after_test else (),
                test_config=self.test_config,
            )

    def test_finish_after_failed_tests_is_not_classified_as_verified(self):
        result = self.run_to_finish(edit_after_test=False)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verified_test_status'], 'failed')
        self.assertEqual(result['completion_classification'], 'tests_failed')
        self.assertEqual(result['model_metrics']['prompt_tokens'], 200)
        self.assertEqual(result['model_metrics']['completion_tokens'], 20)

    def test_edit_after_test_makes_receipt_stale_even_when_finish_claims_success(self):
        result = self.run_to_finish(edit_after_test=True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['verified_test_status'], 'stale_after_edit')
        self.assertEqual(result['completion_classification'], 'tests_stale_after_edit')

    def test_tampered_command_output_invalidates_test_receipt(self):
        result = self.run_to_finish(edit_after_test=False)
        receipt = json.loads(Path(result['commands'][0]['path']).read_bytes())
        output = Path(receipt['output_path'])
        output.write_bytes(output.read_bytes() + b' tampered')
        status, integrity = verify_test_receipts(result['commands'], self.project, output.parent)
        self.assertEqual(status, 'receipt_invalid')
        self.assertFalse(integrity['valid'])
        self.assertIn('output hash changed', integrity['errors'][0])

    def test_current_passing_suite_can_be_reused_but_tampered_output_cannot(self):
        from local_agent import reuse_successful_call
        root = self.project.root
        (root / 'stats.py').write_text(
            'def clamp(value, low, high):\n    return min(max(value, low), high)\n',
            encoding='utf-8')
        original = (root / 'calculator.py').read_text(encoding='utf-8')
        self.assertIn('len(values) + 1', original)
        (root / 'calculator.py').write_text(
            'def average(values):\n    if not values:\n        raise ValueError("empty")\n'
            '    return sum(values) / len(values)\n', encoding='utf-8')
        output = self.base / 'passing'
        output.mkdir()
        broker = Broker(self.project, output, test_config=self.test_config)
        previous = broker.execute('tests')
        self.assertEqual(previous['exit_code'], 0)
        files = self.project.state()['files']
        self.assertTrue(reuse_successful_call(
            'run_tests', previous, broker, self.project, output, files)['reused_result'])
        Path(previous['output_path']).write_bytes(b'changed')
        self.assertIsNone(reuse_successful_call(
            'run_tests', previous, broker, self.project, output, files))

    def test_unauthorized_write_block_is_recorded_in_incomplete_result(self):
        output = self.base / 'blocked_run'
        messages = iter([
            {'role': 'assistant', 'tool_calls': [tool('create_file', {
                'path': 'extra.py',
                'content': 'def unexpected():\\n    return 1\\n',
            })]},
            {'role': 'assistant', 'content': 'I am blocked.'},
            {'role': 'assistant', 'content': 'I am blocked again.'},
            {'role': 'assistant', 'content': 'Still blocked.'},
        ])

        def fake_request(client, endpoint, payload):
            return {'message': next(messages)}

        with patch(
            'local_agent.Ollama.model',
            return_value={'digest': 'receipt-test-model'},
        ), patch(
            'local_agent.Ollama.request',
            autospec=True,
            side_effect=fake_request,
        ):
            result = run_agent(
                self.project,
                'receipt-test-model',
                'Create extra.py',
                output,
                max_steps=4,
            )

        self.assertEqual(result['completion_classification'], 'blocked_by_authorization')
        self.assertEqual(result['blocked_operations'][0]['category'], 'authorization')
        self.assertFalse((self.project.root/'extra.py').exists())


if __name__ == '__main__':
    unittest.main()
