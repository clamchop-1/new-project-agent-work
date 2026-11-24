import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from agent_core import Project, file_hash
from coding_dashboard import CodingDashboard, DashboardError
from coding_workflow import CodingWorkflow
from local_agent import HUB


PINNED = HUB / '.local/tests/unified-coding-demo/7a7871ffb59a4019b9c1877898b4577e/repo/tests/test_progress.py'
PINNED_SHA = '989f840b6db6d9f65823a7fa0725867b0abd8bc28554746d8b020bdc6c8a2af7'


class CodingDashboardTests(unittest.TestCase):
    def setUp(self):
        tests_root = HUB / '.local' / 'tests'
        tests_root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='milestone4-dashboard-', dir=tests_root))
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        (self.repo / '.gitignore').write_text('.local/\n', encoding='utf-8')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/.keep').write_text('', encoding='utf-8')
        (self.repo / 'tests').mkdir()
        shutil.copyfile(PINNED, self.repo / 'tests/test_progress.py')
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
        commands = (
            ['git', 'init', '--initial-branch=main', str(self.repo)],
            ['git', '-C', str(self.repo), 'add', '-A'],
            ['git', '-C', str(self.repo), '-c', 'user.name=Dashboard Fixture',
             '-c', 'user.email=dashboard@example.invalid', 'commit', '-m', 'Pinned baseline'],
        )
        for command in commands:
            result = subprocess.run(command, env=env, capture_output=True)
            if result.returncode:
                self.fail(result.stderr.decode(errors='replace'))
        private = self.repo / '.local'
        private.mkdir()
        (private / 'pinned-tests.json').write_text(json.dumps({
            'test_file': 'tests/test_progress.py', 'test_sha256': PINNED_SHA,
            'modules': ['src.progress']}), encoding='utf-8')
        self.project = Project(self.repo)
        if not PINNED.is_file() or file_hash(PINNED) != PINNED_SHA:
            self.skipTest('Preserved operator-pinned clamp acceptance test is unavailable')
        self.completed = threading.Event()

        def runner(workflow, models, cancel_event):
            workflow.row['state'] = 'failed'
            workflow.row['failure'] = 'dashboard test runner reached fixture'
            workflow._save()
            self.completed.set()

        self.dashboard = CodingDashboard([self.project], allowed_paths=('src/progress.py',),
            allowed_models=('qwen3-coder:30b', 'qwen3:8b'), runner=runner)
        self.server = self.dashboard.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        shutil.rmtree(self.base, ignore_errors=True)

    def request(self, path, *, token=None, method='GET', body=None, origin=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        if origin is not None:
            headers['Origin'] = origin
        raw = json.dumps(body).encode() if body is not None else None
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base_url + path, data=raw,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def valid_submission(self):
        return {'task': 'Implement exact integer clamp behavior.',
                'fixture_id': self.project.identity, 'paths': ['src/progress.py'],
                'models': {'planner': 'qwen3:8b', 'coder': 'qwen3-coder:30b',
                           'reviewer': 'qwen3-coder:30b'},
                'limits': {'timeout': 120, 'attempts': 3}}

    def test_dashboard_is_loopback_and_rejects_other_bind_addresses(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with self.assertRaises(DashboardError):
            self.dashboard.start('0.0.0.0')

    def test_occupied_port_is_reported_without_stopping_existing_dashboard(self):
        with self.assertRaisesRegex(DashboardError, 'existing processes were left untouched'):
            self.dashboard.start(port=self.server.server_port)
        with urllib.request.urlopen(self.base_url + '/', timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_task_and_status_require_bearer_token(self):
        status, _ = self.request('/api/fixtures')
        self.assertEqual(status, 401)
        status, data = self.request('/api/fixtures', token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(data['roles'], ['planner', 'coder', 'reviewer'])
        self.assertEqual(data['models'], ['qwen3-coder:30b', 'qwen3:8b'])
        self.assertEqual(data['approval_mode'], 'read_only_cli_checkpoint')
        status, _ = self.request('/api/overview')
        self.assertEqual(status, 401)
        status, overview = self.request('/api/overview', token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual([row['name'] for row in overview['models']],
                         ['qwen3-coder:30b', 'qwen3:8b'])
        self.assertTrue(all(row['authorized'] for row in overview['models']))
        self.assertNotIn(self.dashboard.token, json.dumps(overview))
        status, skills = self.request('/api/skills', token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertTrue(skills['read_only'])
        self.assertNotIn(self.dashboard.token, json.dumps(skills))

    def test_dashboard_rejects_unregistered_test_profile_and_retains_both_model_options(self):
        with self.assertRaises(DashboardError):
            CodingDashboard([self.project], allowed_paths=('src/progress.py',),
                allowed_models=('qwen3-coder:30b', 'qwen3:8b'), runner=lambda *args: None,
                test_profile='user_command')
        self.assertEqual(self.dashboard.allowed_models,
                         ('qwen3-coder:30b', 'qwen3:8b'))

    def test_submission_requires_same_origin_and_exact_fixture_allowlist(self):
        status, _ = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=self.valid_submission(), origin='https://attacker.invalid')
        self.assertEqual(status, 403)
        body = self.valid_submission(); body['paths'] = ['../../outside.py']
        status, _ = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=body, origin=self.base_url)
        self.assertEqual(status, 400)
        body = self.valid_submission(); body['models']['reviewer'] = 'unlisted-model'
        status, _ = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=body, origin=self.base_url)
        self.assertEqual(status, 400)
        body = self.valid_submission(); body['limits']['attempts'] = 4
        status, _ = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=body, origin=self.base_url)
        self.assertEqual(status, 400)
        body = self.valid_submission(); body['limits']['timeout'] = 601
        status, _ = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=body, origin=self.base_url)
        self.assertEqual(status, 400)

    def test_submission_persists_workflow_and_dashboard_has_no_approval_route(self):
        status, response = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=self.valid_submission(), origin=self.base_url)
        self.assertEqual(status, 202)
        self.assertTrue(response['approval_required'])
        self.assertFalse(response['approval_available_in_dashboard'])
        self.assertTrue(self.completed.wait(5))
        status, view = self.request('/api/workflows/' + response['run_id'], token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(view['state'], 'failed')
        self.assertEqual(view['task'], 'Implement exact integer clamp behavior.')
        self.assertEqual(view['status_summary']['state'], 'failed')
        self.assertEqual(view['status_summary']['test_status'], 'failed_or_infrastructure_error')
        restart = CodingDashboard([self.project], allowed_paths=('src/progress.py',),
            allowed_models=('qwen3-coder:30b', 'qwen3:8b'), runner=lambda *args: None)
        self.assertIn(response['run_id'], restart._known)
        status, _ = self.request('/api/workflows/' + response['run_id'] + '/approve',
            token=self.dashboard.token, method='POST', body={}, origin=self.base_url)
        self.assertEqual(status, 404)

    def test_approval_checkpoint_shows_bound_identity_and_exact_allowlist(self):
        workflow = CodingWorkflow(self.project, 'Update the progress behavior.',
                                  ('src/progress.py',))
        workflow.row.update({'state': 'awaiting_approval', 'files': ['src/progress.py'],
            'patch_sha256': 'a' * 64, 'expected_post_images': {'src/progress.py': 'b' * 64},
            'validation_report': {'valid': True}})
        workflow._save()
        self.dashboard._known[workflow.run_id] = self.project
        status, view = self.request('/api/workflows/' + workflow.run_id,
                                    token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertTrue(view['approval_required'])
        self.assertEqual(view['allowed_paths'], ['src/progress.py'])
        self.assertEqual(view['patch_sha256'], 'a' * 64)
        self.assertEqual(view['expected_post_images'], {'src/progress.py': 'b' * 64})
        self.assertEqual(view['test_config_sha256'],
                         file_hash(self.repo / '.local' / 'pinned-tests.json'))
        self.assertEqual(view['test_profile'], 'clamp_percent_v1')
        self.assertEqual(view['test_command'], 'Broker.execute("tests") -> python -B -I pure_tests.py')

    def test_cancellation_and_unauthorized_evidence_actions(self):
        started = threading.Event()

        def runner(workflow, models, cancel_event):
            started.set()
            if cancel_event.wait(5):
                workflow._transition('cancelled')
            self.completed.set()

        self.dashboard.runner = runner
        status, response = self.request('/api/workflows', token=self.dashboard.token,
            method='POST', body=self.valid_submission(), origin=self.base_url)
        self.assertEqual(status, 202)
        self.assertTrue(started.wait(3))
        status, canceled = self.request('/api/workflows/' + response['run_id'] + '/cancel',
            token=self.dashboard.token, method='POST', body={}, origin=self.base_url)
        self.assertEqual(status, 202)
        self.assertTrue(canceled['cancellation_requested'])
        self.assertTrue(self.completed.wait(3))
        status, view = self.request('/api/workflows/' + response['run_id'], token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(view['state'], 'cancelled')
        for route in ('/api/workflows/' + response['run_id'] + '/approve',
                      '/api/workflows/' + response['run_id'] + '/evidence',
                      '/api/evidence?path=../../secret.txt'):
            status, _ = self.request(route, token=self.dashboard.token,
                method='POST' if route.endswith('/approve') else 'GET',
                body={} if route.endswith('/approve') else None,
                origin=self.base_url if route.endswith('/approve') else None)
            self.assertEqual(status, 404)

    def test_html_uses_csp_and_does_not_embed_a_bearer_token(self):
        with urllib.request.urlopen(self.base_url + '/', timeout=5) as response:
            page = response.read().decode()
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertNotIn(self.dashboard.token, page)
            self.assertIn("textContent", page)
            self.assertIn('Past tasks', page)
            self.assertIn('Start a task', page)
            self.assertIn('Nothing is changed until you approve it', page)
            self.assertIn('overflow-wrap:anywhere', page)
            self.assertIn('Technical details (IDs, repository, models, timing)', page)
            self.assertIn('Recommended: all roles use qwen3-coder:30b', page)
            self.assertIn('Your review is needed before anything is changed', page)
            self.assertIn('id="recent"', page)
            self.assertNotIn('id="approve"', page)

    def test_completion_requires_matching_host_test_receipt_and_output_hash(self):
        run_id = 'a' * 32
        broker = (self.repo / '.local/orchestration/runs' / run_id
                  / 'outputs/coding/broker')
        broker.mkdir(parents=True)
        output = b'Ran 3 tests\nOK\n'
        (broker / 'command-0.txt').write_bytes(output)
        output_hash = hashlib.sha256(output).hexdigest()
        receipt = {'format': 'local-agent-command-receipt', 'command': 'tests',
                   'project_id': self.project.identity, 'exit_code': 0,
                   'output_path': str(broker / 'command-0.txt'),
                   'output_sha256': output_hash}
        (broker / 'command-0.json').write_text(json.dumps(receipt), encoding='utf-8')
        self.assertTrue(CodingDashboard._verified_test_receipt(self.project, run_id, receipt))
        (broker / 'command-0.txt').write_bytes(b'changed after receipt')
        self.assertFalse(CodingDashboard._verified_test_receipt(self.project, run_id, receipt))

    def test_failed_broker_test_receipt_remains_visible_but_is_not_completion(self):
        run_id = 'c' * 32
        broker = (self.repo / '.local/orchestration/runs' / run_id
                  / 'outputs/coding/broker')
        broker.mkdir(parents=True)
        output = b'Ran 3 tests\nFAILED (failures=1)\n'
        (broker / 'command-0.txt').write_bytes(output)
        output_hash = hashlib.sha256(output).hexdigest()
        receipt = {'format': 'local-agent-command-receipt', 'command': 'tests',
                   'project_id': self.project.identity, 'exit_code': 1,
                   'output_path': str(broker / 'command-0.txt'),
                   'output_sha256': output_hash}
        (broker / 'command-0.json').write_text(json.dumps(receipt), encoding='utf-8')
        self.assertTrue(CodingDashboard._verified_test_receipt(self.project, run_id, receipt))
        self.assertEqual(CodingDashboard._test_output(self.project, run_id), output.decode())
        receipt['exit_code'] = 0
        self.assertFalse(CodingDashboard._verified_test_receipt(self.project, run_id, receipt))


if __name__ == '__main__':
    unittest.main()
