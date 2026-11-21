import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent
import local_agent
from agent_core import AgentError, Project, file_hash
from execution_recovery import RecoveryStore
from local_agent import (
    Broker,
    RESTRICTED_ACCEPTANCE_SPEC_ID,
    RESTRICTED_EVALUATOR_SHA256,
    execution_tools,
    verify_restricted_evaluation_receipts,
)
from test_shared_memory import FixtureCase, make_fixture


GOOD_SOURCE = '''
def calculate_average(numbers):
    if len(numbers) == 0:
        raise ValueError("numbers must not be empty")
    return sum(numbers) / len(numbers)
'''

BAD_SOURCE = '''
def calculate_average(numbers):
    return sum(numbers) / (len(numbers) + 1)
'''


class RestrictedAcceptanceBrokerTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.source_path = 'calculator.py'
        (self.project.root / self.source_path).write_text(GOOD_SOURCE, encoding='utf-8')
        self.run = self.base / 'run'
        self.run.mkdir()
        self.auth = {
            'project_id': self.project.identity,
            'project_root': str(self.project.root),
            'source_path': self.source_path,
            'evaluator_sha256': RESTRICTED_EVALUATOR_SHA256,
            'acceptance_spec_id': RESTRICTED_ACCEPTANCE_SPEC_ID,
        }

    def broker(self, auth=True):
        broker = Broker(
            self.project, self.run,
            restricted_evaluation_authorization=self.auth if auth else None,
        )
        broker.journal = SimpleNamespace(
            pending=None, broken=False,
            row={'project_id': self.project.identity, 'status': 'authorized',
                 'checkpoint': self.project.state()},
        )
        return broker

    def test_capability_is_hidden_by_default_and_requires_separate_authorization(self):
        self.assertEqual(len(local_agent.TOOLS), 23)
        default_names = {tool['function']['name'] for tool in
                         execution_tools(None, 'tool_required', None)}
        authorized_names = {tool['function']['name'] for tool in
                            execution_tools(None, 'tool_required', None, self.auth)}
        self.assertNotIn('evaluate_restricted_source', default_names)
        self.assertIn('evaluate_restricted_source', authorized_names)
        role_limited_names = {tool['function']['name'] for tool in
                              execution_tools(None, 'tool_required', {'finish'}, self.auth)}
        self.assertNotIn('evaluate_restricted_source', role_limited_names)
        with self.assertRaisesRegex(AgentError, 'separate operator authorization'):
            self.broker(auth=False).call('evaluate_restricted_source', {'path': self.source_path})

    def test_launcher_requires_explicit_flag_and_exact_source_path(self):
        config = self.base / 'agent-config.json'
        config.write_text(json.dumps({
            'version': 1,
            'projects': {'fixture': {
                'name': 'Fixture', 'root': str(self.project.root),
            }},
            'inference': {'provider': 'ollama', 'model': 'fixture',
                          'endpoint': 'http://127.0.0.1:11434'},
        }), encoding='utf-8')
        with patch.object(agent, 'config_path', return_value=config), \
                patch.object(sys, 'argv', [
                    'agent.py', 'run', '--project', 'fixture',
                    '--authorize-restricted-evaluation',
                ]):
            with self.assertRaisesRegex(AgentError, 'requires one exact source path'):
                agent.main()
        with patch.object(agent, 'config_path', return_value=config), \
                patch.object(sys, 'argv', [
                    'agent.py', 'run', '--project', 'fixture',
                    '--restricted-source-path', 'calculator.py',
                ]):
            with self.assertRaisesRegex(AgentError, 'requires explicit evaluation authorization'):
                agent.main()
        with patch.object(agent, 'config_path', return_value=config), \
                patch.object(sys, 'argv', [
                    'agent.py', 'run', '--project', 'fixture',
                    '--authorize-restricted-evaluation',
                    '--restricted-source-path', '../outside.py',
                ]):
            with self.assertRaises(AgentError):
                agent.main()

    def test_valid_project_scoped_evaluation_returns_verified_receipt(self):
        broker = self.broker()
        result = broker.call('evaluate_restricted_source', {'path': self.source_path})
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['case_count'], 7)
        self.assertEqual(result['passed_count'], 7)
        self.assertEqual(result['source_sha256'], file_hash(self.project.root / self.source_path))
        status, integrity = verify_restricted_evaluation_receipts(
            broker.restricted_evaluation_receipts, self.project, self.run, self.source_path,
        )
        self.assertEqual(status, 'passed')
        self.assertTrue(integrity['valid'])
        receipt = json.loads(Path(result['receipt_path']).read_bytes())
        self.assertEqual(receipt['project_id'], self.project.identity)
        self.assertEqual(receipt['source_path'], self.source_path)
        self.assertNotIn('source_text', receipt)

    def test_incorrect_implementation_is_a_verified_failed_result(self):
        (self.project.root / self.source_path).write_text(BAD_SOURCE, encoding='utf-8')
        broker = self.broker()
        result = broker.call('evaluate_restricted_source', {'path': self.source_path})
        self.assertEqual(result['status'], 'failed')
        status, integrity = verify_restricted_evaluation_receipts(
            broker.restricted_evaluation_receipts, self.project, self.run, self.source_path,
        )
        self.assertEqual(status, 'failed')
        self.assertTrue(integrity['valid'])

    def test_cross_project_authorization_is_rejected(self):
        other, _ = make_fixture(self.base / 'other-project')
        with self.assertRaisesRegex(AgentError, 'another project'):
            Broker(other, self.run, restricted_evaluation_authorization=self.auth)

    def test_source_path_substitution_and_out_of_root_paths_are_rejected(self):
        broker = self.broker()
        with self.assertRaisesRegex(AgentError, 'differs from the operator-approved path'):
            broker.call('evaluate_restricted_source', {'path': 'stats.py'})
        with self.assertRaises(AgentError):
            Broker(self.project, self.run, restricted_evaluation_authorization={
                **self.auth, 'source_path': '../outside.py',
            })

    def test_reparse_point_is_rejected(self):
        alias = self.project.root / 'alias.py'
        try:
            os.symlink(self.project.root / self.source_path, alias)
        except (OSError, NotImplementedError):
            self.skipTest('Symlink creation is unavailable in this environment.')
        with self.assertRaises(AgentError):
            Broker(self.project, self.run, restricted_evaluation_authorization={
                **self.auth, 'source_path': 'alias.py',
            })

    def test_missing_or_invalid_source_is_rejected(self):
        with self.assertRaisesRegex(AgentError, 'missing or unreadable'):
            self.broker_path('missing.py').call(
                'evaluate_restricted_source', {'path': 'missing.py'},
            )
        (self.project.root / self.source_path).write_text('not Python :', encoding='utf-8')
        result = self.broker().call('evaluate_restricted_source', {'path': self.source_path})
        self.assertEqual(result['status'], 'failed')

    def broker_path(self, source_path):
        auth = {**self.auth, 'source_path': source_path}
        broker = Broker(self.project, self.run, restricted_evaluation_authorization=auth)
        broker.journal = SimpleNamespace(
            pending=None, broken=False,
            row={'project_id': self.project.identity, 'status': 'authorized',
                 'checkpoint': self.project.state()},
        )
        return broker

    def test_evaluator_identity_mismatch_fails_before_evaluation(self):
        with patch.object(local_agent, 'RESTRICTED_EVALUATOR_SHA256', '0' * 64):
            with self.assertRaisesRegex(AgentError, 'evaluator identity mismatch'):
                Broker(self.project, self.run,
                       restricted_evaluation_authorization={
                           **self.auth, 'evaluator_sha256': '0' * 64,
                       })

    def test_recovery_state_blocks_evaluation(self):
        broker = self.broker()
        broker.journal.pending = {'kind': 'project_write'}
        with self.assertRaisesRegex(AgentError, 'recovery state'):
            broker.call('evaluate_restricted_source', {'path': self.source_path})

    def test_authorization_is_single_use_even_after_success(self):
        broker = self.broker()
        broker.call('evaluate_restricted_source', {'path': self.source_path})
        with self.assertRaisesRegex(AgentError, 'single-use'):
            broker.call('evaluate_restricted_source', {'path': self.source_path})

    def test_receipt_integrity_detects_tampering_and_source_edits(self):
        broker = self.broker()
        result = broker.call('evaluate_restricted_source', {'path': self.source_path})
        receipt = Path(result['receipt_path'])
        receipt.write_text('{}', encoding='utf-8')
        status, integrity = verify_restricted_evaluation_receipts(
            broker.restricted_evaluation_receipts, self.project, self.run, self.source_path,
        )
        self.assertEqual(status, 'receipt_invalid')
        self.assertFalse(integrity['valid'])

        self.run = self.base / 'second-run'
        self.run.mkdir()
        broker = self.broker()
        broker.call('evaluate_restricted_source', {'path': self.source_path})
        (self.project.root / self.source_path).write_text(BAD_SOURCE, encoding='utf-8')
        status, integrity = verify_restricted_evaluation_receipts(
            broker.restricted_evaluation_receipts, self.project, self.run, self.source_path,
        )
        self.assertEqual(status, 'stale_after_edit')
        self.assertTrue(integrity['valid'])

    def test_submitted_source_is_not_executed_and_no_shell_or_provider_is_used(self):
        hostile = 'import os\ndef calculate_average(numbers):\n    return 1\n'
        (self.project.root / self.source_path).write_text(hostile, encoding='utf-8')
        broker = self.broker()
        with patch.object(Broker, 'execute', side_effect=AssertionError('no command execution')) as execute, \
                patch('local_agent.create_inference_provider', side_effect=AssertionError('no provider')):
            result = broker.call('evaluate_restricted_source', {'path': self.source_path})
        execute.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['passed_count'], 0)

    def test_run_tests_capability_and_behavior_remain_separate(self):
        names = {tool['function']['name'] for tool in
                 execution_tools(None, 'tool_required', None, self.auth)}
        self.assertNotIn('run_tests', names)
        self.assertIn('evaluate_restricted_source', names)
        with self.assertRaisesRegex(AgentError, 'qualified OS sandbox'):
            self.broker().call('run_tests', {})

    def test_broker_schema_requires_only_one_path_argument(self):
        broker = self.broker()
        with self.assertRaisesRegex(AgentError, 'only the approved source path'):
            broker.call('evaluate_restricted_source', {
                'path': self.source_path, 'specification': 'model-selected',
            })


if __name__ == '__main__':
    unittest.main()
