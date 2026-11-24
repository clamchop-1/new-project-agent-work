import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from coding_status import status_projection
from coding_dashboard import CodingDashboard
from agent_core import Project
from local_agent import HUB


class CodingStatusTests(unittest.TestCase):
    def test_status_projection_covers_workflow_lifecycle_without_writes(self):
        cases = {
            'completed': ('verified', 'passed'),
            'awaiting_approval': ('not_started', 'not_run'),
            'failed': ('not_started', 'failed_or_infrastructure_error'),
            'cancelled': ('not_started', 'cancelled'),
            'interrupted': ('ambiguous_review_required', 'not_run'),
            'new_future_state': ('unverified', 'not_run'),
        }
        for state, expected in cases.items():
            with self.subTest(state=state):
                row = {'run_id': 'a' * 32, 'state': state,
                       'approval': {'used': True} if state == 'completed' else None,
                       'test_result': {'exit_code': 0} if state == 'completed' else None,
                       'role_calls': [], 'coder_attempts': []}
                before = json.dumps(row, sort_keys=True)
                view = status_projection(row, completion_verified=state == 'completed')
                self.assertEqual((view['application_status'], view['test_status']), expected)
                self.assertEqual(json.dumps(row, sort_keys=True), before)
        self.assertTrue(status_projection({'state': 'completed',
            'approval': {'used': True}, 'test_result': {'exit_code': 0}},
            completion_verified=True)['approval_consumed'])

    def test_historical_configuration_a_cli_status_is_read_only_and_consistent(self):
        workflow_id = '3abcc66ad4744709820e5a966d1ac018'
        repo = (HUB / '.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/repo')
        state = repo / '.local/coding-workflows' / workflow_id / 'state.json'
        if not state.is_file():
            self.skipTest('Preserved Configuration A completion record is unavailable')
        before = state.read_bytes()
        command = [sys.executable, str(HUB / 'agent.py'), 'coding-status',
                   '--coding-root', str(repo), '--workflow-id', workflow_id]
        result = subprocess.run(command, cwd=HUB, env=dict(os.environ),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        summary = output['status_summary']
        self.assertEqual(summary['state'], 'completed')
        self.assertTrue(summary['approval_consumed'])
        self.assertEqual(summary['application_status'], 'verified')
        self.assertEqual(summary['test_status'], 'passed')
        self.assertEqual(summary['patch_sha256'],
                         '1600cb92102b639a367f9fda686df0ecfc6393c56d7bebd5d68f7689a79eb45e')
        self.assertIsNotNone(summary['candidate_status'])
        view = CodingDashboard([Project(repo)], allowed_paths=('calculator.py', 'stats.py'),
            allowed_models=('qwen3-coder:30b', 'qwen3:8b'), runner=lambda *args: None
            )._view(workflow_id, Project(repo))
        self.assertEqual(view['status_summary'], summary)
        self.assertTrue(view['application_verified'])
        self.assertTrue(view['test_receipt_verified'])
        self.assertEqual(view['candidate_status'], summary['candidate_status'])
        self.assertEqual(state.read_bytes(), before, 'coding-status must not mutate historical evidence')

    def test_unknown_workflow_status_fails_read_only(self):
        repo = (HUB / '.local/tests/milestone4-reliability-20261123-5b3f8aa8925141aabb861b5b7d3e9c48/repo')
        command = [sys.executable, str(HUB / 'agent.py'), 'coding-status',
                   '--coding-root', str(repo), '--workflow-id', 'f' * 32]
        result = subprocess.run(command, cwd=HUB, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Workflow evidence is missing', result.stderr)


if __name__ == '__main__':
    unittest.main()
