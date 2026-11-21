import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_core import AgentError, Project
import temporary_project_registry as registry
from local_agent import TOOLS, execution_tools


class TemporaryProjectRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lca-temp-project-')
        self.base = Path(self.temp.name)
        self.root = self.base / 'fixture'
        self.root.mkdir()
        subprocess.run(['git', 'init', str(self.root)], check=True,
                       capture_output=True, timeout=15)
        self.store = self.base / 'registry'
        self.acl_patches = [
            patch.object(registry, '_ensure_private_permissions'),
            patch.object(registry, '_validate_private_directory'),
            patch.object(registry, '_validate_private_file'),
            patch.object(registry, '_atomic_create', self.atomic_create),
        ]
        for item in self.acl_patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def atomic_create(directory, name, data):
        target = Path(directory) / name
        with target.open('xb') as stream:
            stream.write(data)
        return target

    def register(self, root=None, **kwargs):
        return registry.register(
            root or self.root, confirmed=True, registry_root=self.store,
            **kwargs,
        )

    def test_valid_registration_binds_canonical_root_and_worktree(self):
        record = self.register()
        current = registry.inspect(record['registration_id'], registry_root=self.store)
        self.assertEqual(current, record)
        self.assertEqual(current['project_root'], str(self.root.resolve()))
        self.assertEqual(current['allowed_source_path'], 'calculator.py')
        self.assertFalse(current['write_authorized'])
        self.assertFalse(current['test_execution_authorized'])
        self.assertEqual(current['capabilities'], ['restricted_acceptance_evaluation'])

    def test_registration_requires_explicit_operator_confirmation(self):
        with self.assertRaises(AgentError):
            registry.register(self.root, confirmed=False, registry_root=self.store)

    def test_configured_project_overlap_is_rejected(self):
        with self.assertRaises(AgentError):
            self.register(protected_roots=(self.root.parent,))

    def test_second_active_registration_cannot_replace_or_duplicate_project(self):
        self.register()
        with self.assertRaises(AgentError):
            self.register()

    def test_project_identity_mismatch_is_rejected(self):
        record = self.register()
        path = self.store / (record['registration_id'] + '.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        value['project_id'] = '0' * 64
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(AgentError):
            registry.inspect(record['registration_id'], registry_root=self.store)

    def test_worktree_substitution_is_rejected(self):
        record = self.register()
        git_dir = self.root / '.git'
        old_git_dir = self.root / '.git-old'
        git_dir.rename(old_git_dir)
        subprocess.run(['git', 'init', str(self.root)], check=True,
                       capture_output=True, timeout=15)
        with self.assertRaises(AgentError):
            registry.inspect(record['registration_id'], registry_root=self.store)

    def test_reparse_point_project_root_is_rejected(self):
        target = self.base / 'alias'
        try:
            target.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlink creation is unavailable.')
        with self.assertRaises(AgentError):
            self.register(root=target)

    def test_project_path_traversal_is_rejected(self):
        project = Project(self.root)
        with self.assertRaises(AgentError):
            project.path('../outside.py')

    def test_expired_registration_is_rejected(self):
        record = self.register(ttl_seconds=60)
        path = self.store / (record['registration_id'] + '.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        value['expires_at'] = '2000-01-01T00:00:00+00:00'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(AgentError):
            registry.inspect(record['registration_id'], registry_root=self.store)

    def test_revoked_registration_is_rejected_and_marker_is_preserved(self):
        record = self.register()
        result = registry.revoke(record['registration_id'], registry_root=self.store)
        self.assertTrue(result['revoked'])
        self.assertTrue((self.store / (record['registration_id'] + '.revoked')).is_file())
        with self.assertRaises(AgentError):
            registry.inspect(record['registration_id'], registry_root=self.store)

    def test_registration_scope_does_not_enable_writes_or_tests(self):
        record = self.register()
        self.assertIs(record['write_authorized'], False)
        self.assertIs(record['test_execution_authorized'], False)

    def test_evaluator_is_not_exposed_without_separate_authorization(self):
        record = self.register()
        tools = execution_tools(None, 'tool_required', None,
                                temporary_project_registration=record)
        names = {item['function']['name'] for item in tools}
        self.assertNotIn('evaluate_restricted_source', names)
        self.assertNotIn('run_tests', names)
        self.assertNotIn('run_command', names)

    def test_temporary_toolset_has_no_network_or_shell_capability(self):
        record = self.register()
        names = {item['function']['name'] for item in execution_tools(
            None, 'tool_required', None, temporary_project_registration=record
        )}
        self.assertFalse(names & {
            'search_web', 'read_web_page', 'read_youtube_transcript',
            'run_command', 'run_tests', 'list_location', 'find_location_files',
            'search_location_text', 'read_location_file', 'read_location_pdf',
            'edit_location_file', 'create_location_file',
        })

    def test_explicit_evaluator_capability_is_scoped_and_still_excludes_commands(self):
        record = self.register()
        auth = {'project_id': record['project_id'], 'project_root': record['project_root'],
                'source_path': 'calculator.py', 'evaluator_sha256': 'a' * 64,
                'acceptance_spec_id': 'calculator-average-v1',
                'registration_id': record['registration_id'],
                'worktree_identity': record['worktree']['identity']}
        names = {item['function']['name'] for item in execution_tools(
            None, 'tool_required', None, auth, record
        )}
        self.assertIn('evaluate_restricted_source', names)
        self.assertNotIn('run_tests', names)
        self.assertNotIn('run_command', names)

    def test_scope_is_exact_and_fixed_to_calculator_source(self):
        record = self.register()
        self.assertEqual(record['allowed_source_path'], registry.SOURCE_PATH)
        self.assertNotIn('acceptance_cases', record)
        self.assertEqual(record['capabilities'], ['restricted_acceptance_evaluation'])

    def test_identity_does_not_alias_an_existing_configured_project(self):
        record = self.register()
        self.assertNotEqual(record['registration_id'], record['project_id'])
        self.assertNotEqual(record['registration_id'], self.root.name)

    def test_expiration_is_bounded(self):
        with self.assertRaises(AgentError):
            self.register(ttl_seconds=registry.MAX_TTL_SECONDS + 1)

    def test_existing_global_tool_schema_is_unchanged(self):
        self.assertEqual(len(TOOLS), 23)
        self.assertNotIn('evaluate_restricted_source', {
            item['function']['name'] for item in TOOLS
        })

    def test_no_provider_or_network_is_constructed_by_registration_tools(self):
        record = self.register()
        tools = execution_tools(None, 'tool_required', None,
                                temporary_project_registration=record)
        self.assertTrue(tools)
        self.assertTrue(all(item['function']['name'] not in {'run_command', 'run_tests'}
                            for item in tools))


if __name__ == '__main__':
    unittest.main()
