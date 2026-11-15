import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from agent_core import AgentError
from local_agent import Broker, model_tool_result, run_agent
from test_shared_memory import FixtureCase


def tool(name, arguments):
    return {'id': 'call-' + name, 'function': {'name': name, 'arguments': arguments}}


class ModelToolResultTests(FixtureCase):
    def present(self, value, name='read_file'):
        return model_tool_result(self.project, name, value)

    def test_absolute_windows_source_path_is_relative_with_contents_and_hash_unchanged(self):
        original = {
            'path': str(self.project.root / 'calculator.py'),
            'sha256': 'a' * 64,
            'text': 'Keep absolute text: ' + str(self.project.root / 'calculator.py'),
            'first_line': 1,
            'metadata': {'path': str(self.project.root / 'README.md')},
        }
        snapshot = copy.deepcopy(original)
        shown = self.present(original)
        self.assertEqual(shown, {**snapshot, 'path': 'calculator.py'})
        self.assertEqual(original, snapshot)
        shown['metadata']['path'] = 'changed copy'
        self.assertEqual(original, snapshot)

    def test_relative_paths_are_preserved_and_windows_separators_normalized(self):
        for value, expected in [('calculator.py', 'calculator.py'),
                                ('tests/test_stats.py', 'tests/test_stats.py'),
                                (r'tests\test_stats.py', 'tests/test_stats.py')]:
            with self.subTest(value=value):
                self.assertEqual(self.present({'path': value})['path'], expected)
        absolute = str(self.project.root / 'tests' / 'test_stats.py')
        self.assertEqual(self.present({'path': absolute})['path'], 'tests/test_stats.py')
        self.assertEqual(self.present({'path': absolute.replace('\\', '/')})['path'], 'tests/test_stats.py')

    def test_nested_known_records_are_normalized_without_rewriting_text(self):
        nested = {'path': str(self.project.root / 'AGENTS.md'), 'sha256': 'b' * 64, 'text': 'unchanged'}
        memory = self.project.root / '.local' / 'PROJECT_CONTEXT.md'
        raw = {'nested_instructions': [nested], 'sections': [
            {'path': str(memory), 'sources': [str(memory)], 'sha256': 'c' * 64},
        ]}
        snapshot = copy.deepcopy(raw)
        shown = self.present(raw)
        self.assertEqual(shown['nested_instructions'][0], {**nested, 'path': 'AGENTS.md'})
        self.assertEqual(shown['sections'][0]['path'], '.local/PROJECT_CONTEXT.md')
        self.assertEqual(shown['sections'][0]['sources'], ['.local/PROJECT_CONTEXT.md'])
        self.assertEqual(raw, snapshot)

    def test_other_known_tool_schemas_and_unrelated_fields(self):
        absolute = str(self.project.root / 'README.md')
        cases = [
            ('list_files', {'files': [absolute]}, {'files': ['README.md']}),
            ('search', {'matches': [{'path': absolute, 'text': absolute}]},
             {'matches': [{'path': 'README.md', 'text': absolute}]}),
            ('create_file', {'path': absolute, 'after': 'hash'}, {'path': 'README.md', 'after': 'hash'}),
            ('replace_text', {'path': absolute, 'before': 'hash'}, {'path': 'README.md', 'before': 'hash'}),
            ('run_command', {'output_path': absolute, 'output': absolute},
             {'output_path': 'README.md', 'output': absolute}),
            ('git_status', {'root': str(self.project.root)}, {'root': '.'}),
            ('finish', {'path': absolute, 'summary': absolute}, {'path': absolute, 'summary': absolute}),
        ]
        for name, raw, expected in cases:
            with self.subTest(tool=name):
                self.assertEqual(self.present(raw, name), expected)

    def test_invalid_outside_traversal_and_drive_paths_are_not_disguised(self):
        outside = str(self.base / 'outside.py')
        sibling_prefix = str(self.project.root) + '-other/secret.py'
        for value in [outside, sibling_prefix, '../outside.py', r'..\outside.py',
                      str(self.project.root / '..' / 'outside.py'),
                      r'C:relative.py', r'\rooted.py', r'\\server\share\file.py',
                      'calculator.py:secret', '', None, 12, 'bad\x00path']:
            with self.subTest(path=value):
                self.assertEqual(self.present({'path': value}), {'path': value})

    def test_error_results_remain_exactly_unchanged(self):
        raw = {'error': 'Outside: ' + str(self.base / 'outside.py'),
               'operation_performed': False, 'path': str(self.project.root / 'README.md')}
        self.assertEqual(self.present(raw), raw)

    def test_hard_link_is_not_represented_as_an_authorized_relative_path(self):
        target = self.project.root / 'alias.py'
        os.link(self.project.root / 'calculator.py', target)
        raw = {'path': str(target)}
        self.assertEqual(self.present(raw), raw)
        with self.assertRaises(AgentError):
            self.project.path(str(target))

    def test_symbolic_link_presentation_check_rejects_alias(self):
        target = self.project.root / 'alias.py'
        raw = {'path': str(target)}
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == target or original(path)):
            self.assertEqual(self.present(raw), raw)

    def test_junction_is_not_represented_as_a_relative_path(self):
        if os.name != 'nt':
            self.skipTest('Windows junctions require Windows.')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret.py').write_text('secret = 1\n', encoding='utf-8')
        junction = self.project.root / 'junction'
        def quoted(path):
            return "'" + str(path).replace("'", "''") + "'"
        command = ('New-Item -ItemType Junction -Path ' + quoted(junction)
                   + ' -Target ' + quoted(outside) + ' -ErrorAction Stop | Out-Null')
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                       check=True, capture_output=True, timeout=10)
        raw = {'path': str(junction / 'secret.py')}
        self.assertEqual(self.present(raw), raw)
        with self.assertRaises(AgentError):
            self.project.path(raw['path'])

    def test_presentation_does_not_change_broker_authorization_or_raw_read(self):
        output = self.base / 'broker'
        output.mkdir()
        broker = Broker(self.project, output)
        absolute = str(self.project.root / 'calculator.py')
        raw = broker.call('read_file', {'path': absolute})
        self.assertEqual(self.present(raw)['path'], 'calculator.py')
        self.assertEqual(raw['path'], absolute)
        with self.assertRaises(AgentError):
            broker.call('create_file', {'path': 'new.py', 'content': 'x = 1\n'})
        for value in ['../outside.py', str(self.base / 'outside.py'), '.git/config']:
            with self.subTest(path=value), self.assertRaises(AgentError):
                broker.call('read_file', {'path': value})
        self.assertEqual(broker.allowed_writes, set())
        self.assertEqual(broker.writes, [])
        self.assertFalse((self.project.root / 'new.py').exists())

    def test_loop_serializes_relative_copy_and_preserves_absolute_audit_receipts(self):
        output = self.project.root / '.local' / 'model-run'
        absolute = str(self.project.root / 'AGENTS.md')
        responses = iter([
            {'role': 'assistant', 'tool_calls': [tool('read_file', {'path': absolute})]},
            {'role': 'assistant', 'tool_calls': [tool('run_tests', {})]},
            {'role': 'assistant', 'tool_calls': [tool('finish', {
                'summary': 'Observed failures', 'unfinished': [], 'next_actions': [],
            })]},
        ])
        seen = []
        def request(client, endpoint, payload):
            seen.append(copy.deepcopy(payload['messages']))
            return {'message': next(responses)}
        before = self.project.state()
        with patch('local_agent.Ollama.model', return_value={'digest': 'mock-local'}), patch(
            'local_agent.Ollama.request', autospec=True, side_effect=request,
        ):
            result = run_agent(self.project, 'mock-local', 'Inspect and test read-only', output,
                               test_config=self.test_config)
        read_message = seen[1][-1]
        self.assertEqual(read_message['role'], 'tool')
        self.assertEqual(read_message['tool_name'], 'read_file')
        self.assertEqual(read_message['tool_call_id'], 'call-read_file')
        shown_read = json.loads(read_message['content'])
        self.assertEqual(shown_read['path'], 'AGENTS.md')
        shown_test = json.loads(seen[2][-1]['content'])
        self.assertEqual(shown_test['output_path'], '.local/model-run/command-0.txt')
        rows = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
        raw_read = next(row['result'] for row in rows if row.get('tool') == 'read_file')
        self.assertEqual(raw_read['path'], absolute)
        self.assertEqual(shown_read, {**raw_read, 'path': 'AGENTS.md'})
        receipt = json.loads((output / 'command-0.json').read_bytes())
        self.assertEqual(receipt['output_path'], str(output / 'command-0.txt'))
        raw_test = next(row['result'] for row in rows if row.get('tool') == 'run_tests')
        self.assertEqual(raw_test['output_path'], receipt['output_path'])
        self.assertEqual(shown_test, {**raw_test, 'output_path': '.local/model-run/command-0.txt'})
        self.assertTrue(result['receipt_integrity']['valid'])
        self.assertEqual(result['verified_test_status'], 'failed')
        self.assertEqual(result['writes'], [])
        self.assertEqual(self.project.state(), before)


if __name__ == '__main__':
    unittest.main()
