import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import agent
from agent_core import AgentError, atomic_write, encoded
from test_shared_memory import FixtureCase


class LauncherCliTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.private = self.base / 'launcher-private'
        self.task_file = self.base / 'approved-task.md'
        self.task_file.write_text('Repair only calculator.py and stats.py.\n', encoding='utf-8')

    def run_cli(self, argv):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['agent.py', *argv]), contextlib.redirect_stdout(output):
            agent.main()
        return output.getvalue()

    def patched_launcher(self):
        projects = dict(agent.PROJECTS)
        projects['LocalAgentRepairFixture'] = str(self.project.root)
        return (patch.object(agent, 'PROJECTS', projects), patch.object(agent, 'PRIVATE', self.private))

    def test_pinned_test_capability_is_opt_in_and_project_scoped(self):
        captured_test_configs = []

        def fake_run(project, model, task, output, endpoint, **kwargs):
            output.mkdir(parents=True)
            summary = {'summary': 'CLI test run', 'unfinished': [], 'next_actions': []}
            atomic_write(output / 'result.json', encoded({
                'project_id': project.identity,
                'summary': summary,
                'commands': [],
            }))
            captured_test_configs.append(kwargs['test_config'])
            return {
                'status': 'complete',
                'summary': summary,
                'operator_policy': {'write_paths': list(kwargs['writes'])},
                'verified_test_status': 'not_run',
            }

        with self.patched_launcher()[0], self.patched_launcher()[1]:
            with patch.object(agent, 'run_agent', side_effect=fake_run):
                self.run_cli([
                    'run', '--project', 'LocalAgentRepairFixture',
                    '--task', 'Inspect source only.',
                ])
                self.assertIsNone(captured_test_configs[-1])

                self.run_cli([
                    'run', '--project', 'LocalAgentRepairFixture',
                    '--task', 'Inspect source and run the pinned suite.',
                    '--allow-pinned-tests',
                ])
                self.assertEqual(
                    captured_test_configs[-1],
                    self.project.root / '.local' / 'pinned-tests.json',
                )

                with patch.object(agent, 'PROJECTS', {'UnapprovedProject': str(self.project.root)}):
                    self.run_cli([
                        'run', '--project', 'UnapprovedProject',
                        '--task', 'Run tests.', '--allow-pinned-tests',
                    ])
                    self.assertIsNone(captured_test_configs[-1])

    def test_approval_is_single_use_and_exact_scope_is_enforced_at_run(self):
        with self.patched_launcher()[0], self.patched_launcher()[1]:
            approved = self.run_cli([
                'approve', '--project', 'LocalAgentRepairFixture', '--resume-held-project',
                '--task-file', str(self.task_file), '--write-path', 'calculator.py',
                '--write-path', 'stats.py',
            ])
            permit_path = approved.strip().split(': ', 1)[1]
            permit = json.loads(Path(permit_path).read_text(encoding='utf-8'))
            self.assertEqual(permit['write_paths'], ['calculator.py', 'stats.py'])
            self.assertFalse(permit['used'])

            def fake_run(project, model, task, output, endpoint, **kwargs):
                output.mkdir(parents=True)
                result = {
                    'project_id': project.identity,
                    'summary': {'summary': 'CLI test run', 'unfinished': [], 'next_actions': []},
                    'commands': [],
                }
                atomic_write(output / 'result.json', encoded(result))
                return {
                    'status': 'complete', 'summary': result['summary'],
                    'operator_policy': {'write_paths': list(kwargs['writes'])},
                    'verified_test_status': 'not_run',
                }

            with patch.object(agent, 'run_agent', side_effect=fake_run) as run_mock:
                self.run_cli(['run', '--project', 'LocalAgentRepairFixture', '--permit', permit_path])
                self.assertEqual(run_mock.call_count, 1)
                self.assertEqual(run_mock.call_args.kwargs['writes'], ('calculator.py', 'stats.py'))
                self.assertEqual(run_mock.call_args.kwargs['provider_name'], 'ollama')

            self.assertTrue(json.loads(Path(permit_path).read_text(encoding='utf-8'))['used'])
            with patch.object(agent, 'run_agent') as run_mock:
                with self.assertRaises(AgentError):
                    self.run_cli(['run', '--project', 'LocalAgentRepairFixture', '--permit', permit_path])
                run_mock.assert_not_called()

    def test_handoff_proposal_requires_run_inside_launcher_private_root(self):
        with self.patched_launcher()[0], self.patched_launcher()[1]:
            outside = self.base / 'outside-run'
            outside.mkdir()
            with self.assertRaises(AgentError):
                self.run_cli([
                    'prepare-handoff', '--project', 'LocalAgentRepairFixture',
                    '--run-directory', str(outside),
                ])

    def test_handoff_save_requires_explicit_flag_and_then_updates_fixture_pair(self):
        with self.patched_launcher()[0], self.patched_launcher()[1]:
            def fake_run(project, model, task, output, endpoint, **kwargs):
                output.mkdir(parents=True)
                result = {
                    'project_id': project.identity,
                    'summary': {'summary': 'Read-only CLI handoff test', 'unfinished': [], 'next_actions': []},
                    'commands': [],
                }
                atomic_write(output / 'result.json', encoded(result))
                return {
                    'status': 'complete', 'summary': result['summary'],
                    'operator_policy': {'write_paths': []},
                    'verified_test_status': 'not_run',
                }

            with patch.object(agent, 'run_agent', side_effect=fake_run):
                run_output = self.run_cli([
                    'run', '--project', 'LocalAgentRepairFixture',
                    '--task', 'Prepare a reviewable handoff.',
                ])
            run_directory = json.loads(run_output)['run']
            prepared = self.run_cli([
                'prepare-handoff', '--project', 'LocalAgentRepairFixture',
                '--run-directory', run_directory,
            ])
            proposal_path = Path(prepared.strip().split(': ', 1)[1])
            proposal_before = proposal_path.read_bytes()
            memory_paths = [
                self.project.root/'.local/PROJECT_CONTEXT.md',
                self.project.root/'.local/DURABLE_MEMORY.md',
            ]
            memory_before = [path.read_bytes() for path in memory_paths]
            with self.assertRaises(AgentError):
                self.run_cli([
                    'save-handoff', '--project', 'LocalAgentRepairFixture',
                    '--proposal', str(proposal_path),
                ])
            self.assertEqual([path.read_bytes() for path in memory_paths], memory_before)
            self.assertEqual(proposal_path.read_bytes(), proposal_before)

            self.run_cli([
                'save-handoff', '--project', 'LocalAgentRepairFixture',
                '--proposal', str(proposal_path), '--authorize-memory-save',
            ])
            memory_after = [path.read_bytes() for path in memory_paths]
            self.assertNotEqual(memory_after, memory_before)
            self.assertTrue(all(b'Handoff-ID:' in raw for raw in memory_after))


if __name__ == '__main__':
    unittest.main()
