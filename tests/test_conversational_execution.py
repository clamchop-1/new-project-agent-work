import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import unittest
from unittest.mock import patch

from agent_core import AgentError, Project, file_hash
from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from conversational_capabilities import CapabilityDispatcher
from conversational_execution import ConversationalExecution
from execution_recovery import RecoveryStore
from local_agent import HUB
from project_intelligence import ProjectIntelligence
from skill_library import SkillLibrary


class ConversationalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='conversation-execution-',
                                                dir=HUB / '.local' / 'tests')
        self.base = Path(self.temp.name)
        self.app = self.base / 'application'
        self.app.mkdir()
        self._git(self.app, 'init', '-q')
        self._git(self.app, 'config', 'user.email', 'test@example.invalid')
        self._git(self.app, 'config', 'user.name', 'Test')
        (self.app / '.gitignore').write_text('.local/\n', encoding='utf-8')
        self._git(self.app, 'add', '.gitignore')
        self._git(self.app, 'commit', '-qm', 'initial')
        self.chat = ConversationStore(self.app)
        self.projects = ProjectIntelligence(Project(self.app))
        self.service = ConversationalExecution(self.app, self.projects, self.chat)
        self.service.audit_dispatcher = CapabilityDispatcher(self.projects, self.chat)
        self.conversation = self.chat.create()['id']

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _git(root, *args):
        result = None
        for attempt in range(3):
            result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout
            if 'Permission denied' not in result.stderr or attempt == 2:
                raise AssertionError(result.stderr)
            time.sleep(0.1 * (attempt + 1))
        raise AssertionError(result.stderr)

    def _repo(self, name, average=("if not values:\n        raise ValueError('empty')\n"
                                  '    return sum(values) / len(values)'), parent=None):
        root = (parent or self.base) / name
        (root / 'tests').mkdir(parents=True)
        self._git(root, 'init', '-q')
        self._git(root, 'config', 'user.email', 'test@example.invalid')
        self._git(root, 'config', 'user.name', 'Test')
        (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        (root / 'calculator.py').write_text('def average(values):\n    ' + average + '\n', encoding='utf-8')
        (root / 'stats.py').write_text(
            'def clamp(value, low, high):\n    if value < low:\n        return low\n'
            '    elif value > high:\n        return high\n    return value\n', encoding='utf-8')
        source_test = HUB / '.local' / 'interactive-codex-d1567508' / 'repo' / 'tests' / 'test_stats.py'
        shutil.copyfile(source_test, root / 'tests' / 'test_stats.py')
        self._git(root, 'add', '.')
        self._git(root, 'commit', '-qm', 'fixture')
        return root

    def _multi_change_proposal(self, root, *, conversation=None, runner=None):
        candidate = Project(root)
        try:
            self.projects._record(candidate.identity)
            project_id = candidate.identity
        except AgentError:
            project_id = self.projects.register(str(root), confirmed=True)['project_id']
        self.projects.refresh(project_id)
        conversation = conversation or self.conversation
        self.chat.set_project(conversation, project_id)
        calculator = ("from stats import validate_values\n\n"
                      "def average(values):\n"
                      "    values = validate_values(values)\n"
                      "    return sum(values) / len(values)\n")
        stats = ("def validate_values(values):\n"
                 "    if not values:\n"
                 "        raise ValueError('empty')\n"
                 "    return values\n\n"
                 "def clamp(value, low, high):\n"
                 "    if value < low:\n"
                 "        return low\n"
                 "    elif value > high:\n"
                 "        return high\n"
                 "    return value\n")

        def model_runner(messages, model, cancel_event, on_token):
            request = json.loads(messages[-1]['content'])
            contexts = {item['path']: item for item in request['source_context']}
            self.assertEqual(set(contexts), {'calculator.py', 'stats.py'})
            on_token(json.dumps({'files': [
                {'path': 'calculator.py', 'base_sha256': contexts['calculator.py']['sha256'],
                 'content': calculator},
                {'path': 'stats.py', 'base_sha256': contexts['stats.py']['sha256'],
                 'content': stats},
            ]}))

        task = 'Correct average validation and coordinate the implementation across calculator.py and stats.py.'
        proposal = self.service.propose_patch(conversation, project_id,
            ['calculator.py', 'stats.py'], task,
            {'model_id': 'fixture-model', 'provider_id': 'ollama'}, runner or model_runner)
        return project_id, proposal, model_runner

    def test_conversational_two_file_change_is_approved_written_and_tested(self):
        root = self._repo('conversational-multi-patch',
                          average='return sum(values) / (len(values) + 1)')
        originals = {name: (root / name).read_bytes()
                     for name in ('calculator.py', 'stats.py', '.gitignore')}
        project_id, denied_proposal, runner = self._multi_change_proposal(root)
        intent = self.service.intent(
            'Correct calculator.py and stats.py so the shared average validation works.', True)
        self.assertEqual(intent['kind'], 'multi_patch')
        self.assertEqual(intent['paths'], ['calculator.py', 'stats.py'])
        self.assertEqual(denied_proposal['kind'], 'multi_patch')
        self.assertEqual(denied_proposal['validation']['complete_change_set'], 'passed')
        self.assertEqual(len(denied_proposal['files']), 2)
        self.assertEqual(denied_proposal['authorized_files'], ['calculator.py', 'stats.py'])
        self.assertIn('diff --git a/calculator.py', denied_proposal['diff'])
        self.assertIn('diff --git a/stats.py', denied_proposal['diff'])
        denied = self.service.decide(self.conversation, denied_proposal['operation_id'], 'deny')
        self.assertEqual(denied['status'], 'denied')
        self.assertEqual({name: (root / name).read_bytes() for name in originals}, originals)

        _, proposal, _ = self._multi_change_proposal(root, runner=runner)
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'modified', result)
        self.assertTrue(result['receipt_verified'])
        self.assertEqual(set(result['post_image_sha256_by_path']), {'calculator.py', 'stats.py'})
        journal = RecoveryStore(Project(root))
        row, _ = journal.load(proposal['workflow_id'])
        writes = [item for item in row['operations'] if item['kind'] == 'project_write']
        self.assertEqual(len(writes), 2)
        self.assertEqual({item['path'] for item in writes},
                         {'calculator.py', 'stats.py'})
        self.assertTrue(all(item['state'] == 'recorded' for item in writes))
        self.assertEqual((root / '.gitignore').read_bytes(), originals['.gitignore'])
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

        tests = self.service.propose_tests(self.conversation, project_id, 'auto')
        tested = self.service.decide(self.conversation, tests['operation_id'], 'approve')
        self.assertEqual(tested['status'], 'passed', tested)
        self.assertEqual(tested['test_count'], 3)
        self.assertTrue(tested['receipt_verified'])
        self.assertEqual(tested['learning_candidate']['status'], 'candidate')

    def test_multi_file_interruption_classifies_partial_set_and_blocks_tests(self):
        root = self._repo('multi-patch-interrupted',
                          average='return sum(values) / (len(values) + 1)')
        project_id, proposal, _ = self._multi_change_proposal(root)
        from local_agent import Broker
        real_write = Broker.write
        calls = []

        def fail_between(self, path, content, expected=None):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('fault injected between authorized file writes')
            return real_write(self, path, content, expected)

        with patch.object(Broker, 'write', new=fail_between):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'interrupted', result)
        inspected = self.service.inspect(self.conversation, proposal['operation_id'])
        self.assertEqual(inspected['state'], 'interrupted')
        self.assertIn('file_classifications', inspected.get('recovery') or {}, inspected)
        classifications = {item['path']: item['classification']
                           for item in inspected['recovery']['file_classifications']}
        self.assertEqual(classifications, {'calculator.py': 'applied', 'stats.py': 'unchanged'},
                         {'calls': calls, 'result': result, 'inspection': inspected,
                          'journal': RecoveryStore(Project(root)).load(proposal['workflow_id'])[0]})
        self.assertFalse(inspected['recovery']['safe_to_reconcile'])
        with self.assertRaisesRegex(AgentError, 'needs recovery review'):
            self.service.propose_tests(self.conversation, project_id, 'auto')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

    def test_multi_file_changed_target_is_stale_and_writes_nothing(self):
        root = self._repo('multi-patch-stale',
                          average='return sum(values) / (len(values) + 1)')
        _, proposal, _ = self._multi_change_proposal(root)
        before = (root / 'calculator.py').read_bytes()
        (root / 'stats.py').write_text((root / 'stats.py').read_text(encoding='utf-8')
                                       + '\n# external edit\n', encoding='utf-8')
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['target_changed'])
        self.assertEqual((root / 'calculator.py').read_bytes(), before)

    def test_multi_file_interruption_before_first_write_requires_abort(self):
        root = self._repo('multi-patch-before-first',
                          average='return sum(values) / (len(values) + 1)')
        project_id, proposal, _ = self._multi_change_proposal(root)
        before = {name: file_hash(root / name) for name in ('calculator.py', 'stats.py')}
        from local_agent import Broker
        with patch.object(Broker, 'write', side_effect=OSError('fault before first file write')):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'interrupted')
        inspected = self.service.inspect(self.conversation, proposal['operation_id'])
        recovery = inspected['recovery']
        self.assertEqual(recovery['pending_outcome'], 'matches_before')
        self.assertEqual(recovery['options'], ['abort'], inspected)
        reconciled = self.service.reconcile_patch(self.conversation, proposal['operation_id'],
                                                   'abort', recovery['review_sha256'])
        self.assertEqual(reconciled['state'], 'aborted')
        self.assertEqual({name: file_hash(root / name) for name in before}, before)
        self.assertNotEqual(project_id, '')

    def test_multi_file_interruption_after_all_images_can_reconcile_missing_receipt(self):
        root = self._repo('multi-patch-after-images',
                          average='return sum(values) / (len(values) + 1)')
        _, proposal, _ = self._multi_change_proposal(root)
        from execution_recovery import ExecutionJournal
        real_record = ExecutionJournal.record
        calls = []

        def fail_second_record(self, evidence):
            calls.append(evidence['path'])
            if len(calls) == 2:
                raise OSError('fault after second file write before its receipt')
            return real_record(self, evidence)

        with patch.object(ExecutionJournal, 'record', new=fail_second_record):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'interrupted', result)
        inspected = self.service.inspect(self.conversation, proposal['operation_id'])
        recovery = inspected['recovery']
        self.assertEqual(recovery['pending_outcome'], 'matches_after')
        self.assertIn('accept', recovery['options'])
        with self.assertRaises(AgentError):
            self.service.propose_tests(self.conversation, proposal['project_id'], 'auto')
        recovered = self.service.reconcile_patch(self.conversation, proposal['operation_id'],
            'accept', recovery['review_sha256'])
        self.assertEqual(recovered['state'], 'modified')
        self.assertTrue(recovered['receipt_verified'])
        self.assertTrue(any(item['state'] == 'reconciled'
                            for item in recovered['write_receipts']))
        self.assertEqual(recovered['learning_candidate']['status'], 'not_recorded')

    def test_multi_file_interruption_after_complete_journal_recovers_without_learning_replay(self):
        root = self._repo('multi-patch-complete-journal',
                          average='return sum(values) / (len(values) + 1)')
        _, proposal, _ = self._multi_change_proposal(root)
        from coding_workflow import CodingWorkflow
        real_transition = CodingWorkflow._transition

        def fail_after_journal(self, state):
            if state == 'modified':
                raise RuntimeError('fault after Broker journal close before workflow finalization')
            return real_transition(self, state)

        with patch.object(CodingWorkflow, '_transition', new=fail_after_journal):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'interrupted', result)
        recovered = self.service.inspect(self.conversation, proposal['operation_id'])
        self.assertEqual(recovered['state'], 'modified')
        self.assertTrue(recovered['result']['receipt_verified'])
        self.assertEqual(recovered['result']['learning_candidate']['status'], 'not_recorded')

    def test_multi_file_concurrent_edit_between_writes_is_classified_and_blocks_tests(self):
        root = self._repo('multi-patch-concurrent-edit',
                          average='return sum(values) / (len(values) + 1)')
        project_id, proposal, _ = self._multi_change_proposal(root)
        from local_agent import Broker
        real_write = Broker.write
        calls = []

        def modify_before_second(self, path, content, expected=None):
            calls.append(path)
            if len(calls) == 2:
                target = self.project.path(path)
                target.write_text(target.read_text(encoding='utf-8') + '\n# concurrent user edit\n',
                                  encoding='utf-8')
            return real_write(self, path, content, expected)

        with patch.object(Broker, 'write', new=modify_before_second):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'interrupted', result)
        recovery = self.service.inspect(self.conversation, proposal['operation_id'])['recovery']
        classifications = {item['path']: item['classification']
                           for item in recovery['file_classifications']}
        self.assertEqual(classifications['calculator.py'], 'applied')
        self.assertEqual(classifications['stats.py'], 'externally_modified')
        self.assertFalse(recovery['safe_to_reconcile'])
        with self.assertRaisesRegex(AgentError, 'needs recovery review'):
            self.service.propose_tests(self.conversation, project_id, 'auto')

    def test_multi_file_incomplete_second_patch_invalidates_whole_proposal(self):
        root = self._repo('multi-patch-invalid-member',
                          average='return sum(values) / (len(values) + 1)')
        project_id = self.projects.register(str(root), confirmed=True)['project_id']
        self.projects.refresh(project_id)
        before = {name: file_hash(root / name) for name in ('calculator.py', 'stats.py')}

        def malformed(messages, model, cancel_event, on_token):
            request = json.loads(messages[-1]['content'])
            contexts = {item['path']: item for item in request['source_context']}
            on_token(json.dumps({'files': [
                {'path': 'calculator.py', 'base_sha256': contexts['calculator.py']['sha256'],
                 'content': 'def average(values):\n    return sum(values) / len(values)\n'},
                {'path': 'stats.py', 'base_sha256': '0' * 64,
                 'content': 'def clamp(value, low, high):\n    return value\n'},
            ]}))

        with self.assertRaisesRegex(AgentError, 'strict validation'):
            self.service.propose_patch(self.conversation, project_id,
                ['calculator.py', 'stats.py'], 'Correct calculator.py and stats.py together.',
                {'model_id': 'fixture-model', 'provider_id': 'ollama'}, malformed)
        self.assertEqual({name: file_hash(root / name) for name in before}, before)

    def test_registration_approval_is_conversation_bound_and_single_use(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-replay-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('registration-repo', parent=parent)
        proposal = self.service.propose_registration(self.conversation, str(root))
        restarted = ConversationalExecution(self.app, self.projects, self.chat)
        self.assertEqual(restarted.inspect(self.conversation, proposal['operation_id'])['state'],
                         'awaiting_approval')
        with self.assertRaises(AgentError):
            self.service.decide('f' * 32, proposal['operation_id'], 'approve')
        denied = self.service.decide(self.conversation, proposal['operation_id'], 'deny')
        self.assertEqual(denied['status'], 'denied')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(self.projects.list(), [])

    def test_conversational_single_file_patch_and_separately_approved_tests(self):
        root = self._repo('conversational-patch', average='return sum(values) / (len(values) + 1)')
        registration = self.projects.register(str(root), confirmed=True)
        project_id = registration['project_id']
        self.projects.refresh(project_id)
        self.chat.set_project(self.conversation, project_id)
        original = (root / 'calculator.py').read_text(encoding='utf-8')
        proposed_source = ("def average(values):\n"
            "    if not values:\n"
            "        raise ValueError('empty')\n"
            "    return sum(values) / len(values)\n")

        def model_runner(messages, model, cancel_event, on_token):
            request = json.loads(messages[-1]['content'])
            source = request['source_context'][0]
            on_token(json.dumps({'files': [{'path': 'calculator.py',
                'base_sha256': source['sha256'], 'content': proposed_source}]}))

        intent = self.service.intent('Add input validation to calculator.py', True)
        self.assertEqual(intent['kind'], 'patch')
        denied_proposal = self.service.propose_patch(self.conversation, project_id,
            intent['path'], intent['task'], {'model_id': 'fixture-model', 'provider_id': 'ollama'}, model_runner)
        denied = self.service.decide(self.conversation, denied_proposal['operation_id'], 'deny')
        self.assertEqual(denied['status'], 'denied')
        self.assertEqual((root / 'calculator.py').read_text(encoding='utf-8'), original)

        proposal = self.service.propose_patch(self.conversation, project_id,
            intent['path'], intent['task'], {'model_id': 'fixture-model', 'provider_id': 'ollama'}, model_runner)
        workflow = root / '.local' / 'coding-workflows' / proposal['workflow_id']
        self.assertEqual(proposal['kind'], 'patch')
        self.assertEqual(proposal['path'], 'calculator.py')
        self.assertIn('diff --git a/calculator.py', proposal['diff'])
        self.assertEqual(proposal['validation']['strict_unified_diff'], 'passed')
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'modified', result)
        self.assertTrue(result['receipt_verified'])
        self.assertTrue(result['test_authorization_required'])
        self.assertEqual(file_hash(root / 'calculator.py'), result['post_image_sha256'])
        self.assertNotEqual((root / 'calculator.py').read_text(encoding='utf-8'), original)
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

        test_proposal = self.service.propose_tests(self.conversation, project_id, 'auto')
        test_result = self.service.decide(self.conversation, test_proposal['operation_id'], 'approve')
        self.assertEqual(test_result['status'], 'passed', test_result)
        self.assertTrue(test_result['receipt_verified'])
        self.assertEqual(test_result['test_count'], 3)
        self.assertEqual(test_result['passed_count'], 3)
        self.assertEqual(test_result['learning_candidate']['status'], 'candidate', test_result)

    def test_concurrent_decision_lease_blocks_replay_before_consumption(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-lease-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('lease-repo', parent=parent)
        proposal = self.service.propose_registration(self.conversation, str(root))
        lease = self.service.root / (proposal['operation_id'] + '.decision.lock')
        lease.write_text('held by another dashboard process', encoding='utf-8')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'],
                         'awaiting_approval')
        lease.unlink()
        self.assertEqual(self.service.decide(self.conversation, proposal['operation_id'], 'deny')['status'],
                         'denied')

    def test_approved_registration_indexes_repository(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('indexed-repo', parent=parent)
        proposal = self.service.propose_registration(self.conversation, str(root))
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(self.projects.list()[0]['indexing_status'] in {'indexed', 'ready'})

    def test_registration_approval_is_consumed_when_repository_changes(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-stale-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('changed-before-registration', parent=parent)
        proposal = self.service.propose_registration(self.conversation, str(root))
        (root / 'calculator.py').write_text('def average(values):\n    return 0\n', encoding='utf-8')

        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['target_changed'])
        self.assertFalse(result['reconciliation_required'])
        self.assertEqual(self.projects.list(), [])
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'],
                         'failed')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

    def test_pinned_test_approval_is_consumed_without_dispatch_when_project_changes(self):
        root = self._repo('changed-before-tests')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        (root / 'calculator.py').write_text('def average(values):\n    return 0\n', encoding='utf-8')

        with patch('conversational_execution.Broker.execute') as execute:
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['target_changed'])
        self.assertFalse(result['receipt_verified'])
        self.assertFalse(result['reconciliation_required'])
        execute.assert_not_called()
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'],
                         'failed')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

    def test_pinned_tests_execute_through_broker_and_receipt_verifies(self):
        root = self._repo('passing-repo')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['receipt_verified'])
        self.assertIn('Ran 3 tests', result['output'])
        self.assertEqual((result['test_count'], result['passed_count'], result['failed_count']), (3, 3, 0))
        learned = result['learning_candidate']
        self.assertEqual(learned['status'], 'candidate', learned)
        self.assertFalse(learned['promoted'])
        candidate = SkillLibrary(Project(root)).inspect(learned['skill_id'])['versions'][-1]
        self.assertEqual(candidate['status'], 'candidate')
        self.assertTrue(any(item.get('test_verified') for item in candidate['evidence_refs']))
        self.assertEqual(self._git(root, 'status', '--short'), '')
        self.assertEqual(self.service.previous_run(self.conversation, project['project_id'])['result']['status'], 'passed')

    def test_pinned_test_failure_is_reported_as_a_completed_verified_run(self):
        root = self._repo('failing-repo', "if not values:\n        raise ValueError('empty')\n"
                          '    return sum(values) / (len(values) + 1)')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['receipt_verified'])
        self.assertIn('FAILED', result['output'])
        self.assertEqual((result['test_count'], result['passed_count'], result['failed_count']), (3, 2, 1))
        self.assertEqual(result['failed_tests'], ['test_average'])

    def test_registration_traversal_and_sensitive_directories_are_rejected(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-paths-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('safe-repository', parent=parent)
        traversal = str(root.parent / '..' / root.name)
        with self.assertRaises(AgentError):
            self.service.propose_registration(self.conversation, traversal)
        sensitive = self.base / 'AppData' / 'private-repository'
        sensitive.mkdir(parents=True)
        self._git(sensitive, 'init', '-q')
        with self.assertRaises(AgentError):
            self.service.propose_registration(self.conversation, str(sensitive))

    def test_registration_rejects_windows_junction_escapes(self):
        parent = Path(tempfile.mkdtemp(prefix='registration-junction-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        physical = parent / 'physical'
        physical.mkdir()
        root = self._repo('junction-target', parent=physical)
        junction = parent / 'repository-alias'
        linked = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(physical)],
                                capture_output=True, text=True)
        self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)
        with self.assertRaises(AgentError):
            self.service.propose_registration(self.conversation, str(junction / root.name))
        junction.rmdir()

    def test_expired_approval_cannot_be_consumed(self):
        root = self._repo('expired-project')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        path = self.service._path(proposal['operation_id'])
        record = json.loads(path.read_bytes())
        record['expires'] = 0
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'], 'expired')
        with self.assertRaises(AgentError):
            self.service.decide(self.conversation, proposal['operation_id'], 'approve')

    def test_cancelled_broker_run_is_terminal_and_requires_reconciliation(self):
        root = self._repo('cancelled-project')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        cancelled = threading.Event()
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
        def interrupted(broker, name):
            broker.journal.begin('command', command=name, path=None)
            broker.journal.command_process(child_pid=43210)
            while not cancelled.wait(0.02):
                pass
            broker.journal.command_process(process_stopped=True)
            raise AgentError('Harness cancellation interrupted the owned command.')
        with patch('conversational_execution.Broker.execute', new=interrupted):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve', cancelled)
        timer.join(timeout=2)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'], 'cancelled')

    def test_broker_timeout_is_explicit_and_requires_reconciliation(self):
        root = self._repo('timed-out-project')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        def timed_out(broker, name):
            broker.journal.begin('command', command=name, path=None)
            broker.journal.command_process(child_pid=43211)
            broker.journal.command_process(process_stopped=True)
            raise AgentError('Owned bounded command timed out; PID terminated.')
        with patch('conversational_execution.Broker.execute', new=timed_out):
            result = self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        self.assertEqual(result['status'], 'timed_out', result)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['receipt_verified'])
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(self.service.inspect(self.conversation, proposal['operation_id'])['state'],
                         'timed_out')

    def test_restart_marks_unfinished_broker_operation_for_operator_reconciliation(self):
        root = self._repo('crashed-run-project')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        def interrupted(broker, name):
            broker.journal.begin('command', command=name, path=None)
            broker.journal.command_process(child_pid=99999998)
            live = self.service.inspect(self.conversation, proposal['operation_id'])
            self.assertEqual(live['state'], 'executing')
            self.assertIsNone(live['recovery'])
            raise KeyboardInterrupt('simulated dashboard process loss')
        with patch('conversational_execution.Broker.execute', new=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        operation_path = self.service._path(proposal['operation_id'])
        operation = json.loads(operation_path.read_bytes())
        operation['owner_pid'] = 99999999  # authoritative fixture for a dead dashboard owner
        operation_path.write_bytes(json.dumps(operation).encode())

        restarted = ConversationalExecution(self.app, self.projects, self.chat)
        restarted.audit_dispatcher = self.service.audit_dispatcher
        state = restarted.inspect(self.conversation, proposal['operation_id'])
        self.assertEqual(state['state'], 'interrupted')
        self.assertTrue(state['result']['reconciliation_required'])
        recovery = state['recovery']
        self.assertEqual(recovery['pending_outcome'], 'unverified_command')
        self.assertTrue(recovery['safe_to_reconcile'])
        self.assertIn('abort', recovery['options'])
        self.assertTrue(Path(recovery['evidence_directory']).is_dir())
        self.assertEqual(restarted.inspect(self.conversation, proposal['operation_id'])['state'],
                         'interrupted')

    def test_restart_recovers_closed_broker_receipt_without_rerunning_tests(self):
        root = self._repo('receipt-finalization-project')
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        with patch.object(self.service, '_finish', side_effect=KeyboardInterrupt(
                'simulated dashboard loss after Broker journal close')):
            with self.assertRaises(KeyboardInterrupt):
                self.service.decide(self.conversation, proposal['operation_id'], 'approve')

        operation_path = self.service._path(proposal['operation_id'])
        operation = json.loads(operation_path.read_bytes())
        operation['owner_pid'] = 99999999
        operation_path.write_bytes(json.dumps(operation).encode())
        restarted = ConversationalExecution(self.app, self.projects, self.chat)
        restarted.audit_dispatcher = self.service.audit_dispatcher
        recovered = restarted.inspect(self.conversation, proposal['operation_id'])
        self.assertEqual(recovered['state'], 'passed', recovered)
        self.assertEqual(recovered['result']['status'], 'passed')
        self.assertEqual(recovered['result']['test_count'], 3)
        self.assertTrue(recovered['result']['receipt_verified'], recovered)
        self.assertTrue(recovered['result']['recovered'])
        self.assertEqual(recovered['result']['learning_candidate']['status'], 'not_recorded')
        self.assertEqual(recovered['recovery']['recovery_state'], 'complete')
        self.assertTrue(recovered['recovery']['auto_finalized'])
        self.assertEqual(restarted.previous_run(self.conversation, project['project_id'])
                         ['result']['status'], 'passed')

    def test_restart_recovers_verified_test_failure_from_closed_receipt(self):
        broken = "if not values:\n        raise ValueError('empty')\n    return sum(values) / (len(values) + 1)"
        root = self._repo('recovered-failure-project', broken)
        project = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation, project['project_id'], 'auto')
        with patch.object(self.service, '_finish', side_effect=KeyboardInterrupt(
                'simulated dashboard loss after Broker journal close')):
            with self.assertRaises(KeyboardInterrupt):
                self.service.decide(self.conversation, proposal['operation_id'], 'approve')
        operation_path = self.service._path(proposal['operation_id'])
        operation = json.loads(operation_path.read_bytes())
        operation['owner_pid'] = 99999999
        operation_path.write_bytes(json.dumps(operation).encode())

        restarted = ConversationalExecution(self.app, self.projects, self.chat)
        restarted.audit_dispatcher = self.service.audit_dispatcher
        recovered = restarted.inspect(self.conversation, proposal['operation_id'])
        self.assertEqual(recovered['state'], 'failed', recovered)
        self.assertEqual(recovered['result']['exit_code'], 1)
        self.assertEqual(recovered['result']['failed_tests'], ['test_average'])
        self.assertTrue(recovered['result']['receipt_verified'])
        self.assertTrue(recovered['result']['recovered'])

    def test_chat_stream_presents_authenticated_registration_approval(self):
        parent = Path(tempfile.mkdtemp(prefix='chat-registration-', dir=HUB / 'tests'))
        self.addCleanup(shutil.rmtree, parent, True)
        root = self._repo('chat-registration-repo', parent=parent)
        dashboard = CodingDashboard((), allowed_models=('qwen3:8b',),
            chat_store=self.chat, chat_runner=lambda *args: self.fail('Proposal must not invoke inference'),
            project_intelligence=self.projects)
        server = dashboard.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            conversation = self.chat.create()
            request = urllib.request.Request(base + '/api/chat/conversations/' + conversation['id'] + '/messages',
                data=json.dumps({'message': 'Register the repository at ' + str(root),
                                 'model': 'qwen3:8b'}).encode(), method='POST',
                headers={'Authorization': 'Bearer ' + dashboard.token, 'Origin': base,
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=15) as response:
                events = [json.loads(line) for line in response.read().splitlines() if line]
            proposal = next(event['proposal'] for event in events
                            if event.get('type') == 'operation_proposal')
            self.assertEqual(proposal['kind'], 'register')
            decision = urllib.request.Request(base + '/api/chat/conversations/' + conversation['id']
                + '/operations/' + proposal['operation_id'] + '/decision',
                data=b'{"decision":"approve"}', method='POST',
                headers={'Authorization': 'Bearer ' + dashboard.token, 'Origin': base,
                         'Content-Type': 'application/json'})
            with urllib.request.urlopen(decision, timeout=15) as response:
                self.assertEqual(json.loads(response.read())['status'], 'completed')
            saved = self.chat.load(conversation['id'])['messages'][-1]
            self.assertEqual(saved['operation']['state'], 'awaiting_approval')
            self.assertIn('Approve', __import__('chat_streaming').STREAMING_CLIENT_PATCH)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_authenticated_test_approval_with_concurrent_status_polling(self):
        root = self._repo('http-approved-tests')
        registration = self.projects.register(str(root), confirmed=True)
        proposal = self.service.propose_tests(self.conversation,
            registration['project_id'], 'auto')
        dashboard = CodingDashboard((), allowed_models=('qwen3:8b',),
            chat_store=self.chat,
            chat_runner=lambda *args: self.fail('Test approval must not invoke inference'),
            project_intelligence=self.projects)
        server = dashboard.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        headers = {'Authorization': 'Bearer ' + dashboard.token,
                   'Origin': base, 'Content-Type': 'application/json'}
        decision_error = []
        try:
            route = (base + '/api/chat/conversations/' + self.conversation
                + '/operations/' + proposal['operation_id'])
            decision = urllib.request.Request(route + '/decision',
                data=b'{"decision":"approve"}', method='POST', headers=headers)

            def approve():
                try:
                    with urllib.request.urlopen(decision, timeout=30) as response:
                        self.assertEqual(json.loads(response.read())['status'], 'passed')
                except Exception as exc:
                    decision_error.append(exc)

            worker = threading.Thread(target=approve, daemon=True)
            worker.start()
            observed = []
            deadline = time.monotonic() + 30
            while worker.is_alive() and time.monotonic() < deadline:
                request = urllib.request.Request(route, headers={'Authorization':
                    'Bearer ' + dashboard.token})
                with urllib.request.urlopen(request, timeout=10) as response:
                    observed.append(json.loads(response.read())['state'])
                time.sleep(0.25)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), 'HTTP Broker test approval did not finish.')
            self.assertEqual(decision_error, [])
            self.assertIn('executing', observed)
            final = self.service.inspect(self.conversation, proposal['operation_id'])
            self.assertEqual(final['state'], 'passed', final)
            self.assertTrue(final['result']['receipt_verified'])
            self.assertEqual(final['result']['passed_count'], 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_restart_persists_failed_status_for_abandoned_stream(self):
        self.chat.start_assistant_stream(self.conversation, 'a' * 32)
        dashboard = CodingDashboard((), allowed_models=('qwen3:8b',),
            chat_store=self.chat, chat_runner=lambda *args: None,
            project_intelligence=self.projects)
        server = dashboard.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        headers = {'Authorization': 'Bearer ' + dashboard.token}
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    base + '/api/chat/conversations/' + self.conversation + '/status',
                    headers=headers), timeout=10) as response:
                state = json.loads(response.read())
            self.assertEqual(state['state'], 'interrupted')
            with urllib.request.urlopen(urllib.request.Request(
                    base + '/api/chat/conversations/' + self.conversation,
                    headers=headers), timeout=10) as response:
                row = json.loads(response.read())
            self.assertEqual(row['messages'][-1]['stream_status'], 'failed')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
