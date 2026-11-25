import json
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from agent_core import AgentError, digest, encoded, file_hash
from execution_recovery import RecoveryError, RecoveryStore
from local_agent import Broker, run_agent
from inference_provider import ProviderFailure
from test_shared_memory import FixtureCase


class RecoveryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.store = RecoveryStore(self.project)
        self.output = self.base / 'execution'
        self.output.mkdir()
        self.target = self.project.root / 'calculator.py'
        self.before = self.target.read_bytes()
        self.after = b'def average(values):\n    return 42\n'

    def start(self):
        return self.store.start(self.output, ('calculator.py',), None, (), None,
                                self.test_config, ('run_tests',))

    def begin_write(self, journal):
        journal.begin('project_write', path='calculator.py', before=digest(self.before),
                      after=digest(self.after), backup=self.before)

    def test_restart_before_execution_requires_review_and_does_not_write(self):
        with self.store.locked():
            journal = self.start()
        run_id = journal.row['run_id']
        with self.store.locked():
            with self.assertRaisesRegex(RecoveryError, 'unresolved'):
                self.store.assert_clear()
        report = self.store.inspect(run_id)
        self.assertIsNone(report['pending'])
        self.assertEqual(self.store.reconcile(run_id, report['review_sha256'], 'abort')['status'],
                         'aborted')
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_reconciliation_archives_only_reviewed_locks_of_exited_owner(self):
        with self.store.locked():
            journal = self.start()
            journal.row['owner_pid'] = 123
            journal.save()
        run_id = journal.row['run_id']
        with patch('execution_recovery.HUB', self.base), \
                patch('execution_recovery._owner_exited', side_effect=lambda pid: pid == 123):
            local = self.base / '.local'
            locks = local / 'locks'
            locks.mkdir(parents=True)
            project_lock = locks / (self.project.identity + '.lock')
            project_lock.write_bytes(encoded({'pid': 123, 'nonce': 'fixture'}))
            inference_lock = local / 'inference.lock'
            inference_lock.write_bytes(encoded({'pid': 456, 'nonce': 'other owner'}))
            report = self.store.inspect(run_id)
            self.assertEqual(set(report['stale_launcher_locks']), {project_lock.name})
            self.store.reconcile(run_id, report['review_sha256'], 'abort')
            self.assertFalse(project_lock.exists())
            self.assertTrue(inference_lock.exists())
            archive = self.store.path(run_id).with_suffix('.' + project_lock.name + '.archive')
            self.assertEqual(json.loads(archive.read_bytes())['pid'], 123)

    def test_denial_has_no_pending_or_changed_bytes(self):
        with self.store.locked():
            journal = self.start()
            broker = Broker(self.project, self.output)
            broker.journal = journal
            with self.assertRaisesRegex(AgentError, 'authorized'):
                broker.write('calculator.py', self.after.decode(), digest(self.before))
            self.assertIsNone(self.store.inspect(journal.row['run_id'])['pending'])
            journal.close('failed')
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_after_write_before_receipt_needs_explicit_acceptance(self):
        with self.store.locked():
            journal = self.start()
            self.begin_write(journal)
            self.target.write_bytes(self.after)
        run_id = journal.row['run_id']
        report = self.store.inspect(run_id)
        self.assertEqual(report['pending_outcome'], 'matches_after')
        with self.assertRaises(RecoveryError):
            self.store.resume_scope(run_id, (), (), None, self.test_config)
        self.store.reconcile(run_id, report['review_sha256'], 'accept')
        self.store.resume_scope(run_id, ('calculator.py',), (), None, self.test_config)
        with self.assertRaisesRegex(RecoveryError, 'scope'):
            self.store.resume_scope(run_id, ('stats.py',), (), None, self.test_config)
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_partial_write_blocks_accept_and_can_restore_reviewed_backup(self):
        with self.store.locked():
            journal = self.start()
            self.begin_write(journal)
            self.target.write_bytes(self.after[:8])
        run_id = journal.row['run_id']
        report = self.store.inspect(run_id)
        self.assertEqual(report['pending_outcome'], 'conflict')
        with self.assertRaises(RecoveryError):
            self.store.reconcile(run_id, report['review_sha256'], 'accept')
        self.store.reconcile(run_id, report['review_sha256'], 'restore')
        self.assertEqual(self.target.read_bytes(), self.before)
        with self.assertRaises(RecoveryError):
            self.store.reconcile(run_id, report['review_sha256'], 'abort')
        reviewed = self.store.inspect(run_id)
        self.assertEqual(reviewed['pending_outcome'], 'matches_before')
        self.store.reconcile(run_id, reviewed['review_sha256'], 'abort')

    def test_failed_intent_persistence_prevents_write(self):
        with self.store.locked():
            journal = self.start()
            broker = Broker(self.project, self.output, ('calculator.py',))
            broker.journal = journal
            with patch('execution_recovery.atomic_write', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    broker.write('calculator.py', self.after.decode(), digest(self.before))
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_failed_receipt_persistence_leaves_pending_intent(self):
        with self.store.locked():
            journal = self.start()
            self.begin_write(journal)
            self.target.write_bytes(self.after)
            with patch('execution_recovery.atomic_write', side_effect=OSError('disk unavailable')):
                with self.assertRaises(RecoveryError):
                    journal.record({'path': 'calculator.py', 'after': digest(self.after)})
        self.assertEqual(self.store.inspect(journal.row['run_id'])['pending_outcome'], 'matches_after')

    def test_command_without_receipt_is_never_a_passing_test(self):
        with self.store.locked():
            journal = self.start()
            journal.begin('command', command='tests', path=None)
        report = self.store.inspect(journal.row['run_id'])
        self.assertEqual(report['pending_outcome'], 'command_liveness_unknown')
        self.assertFalse(report['safe_to_reconcile'])
        self.assertEqual(report['tests'], 'not_run')

    def test_cancelled_owned_test_process_leaves_unverified_intent(self):
        cancel = threading.Event()
        child = Mock(args=['pinned'], pid=1234)
        child.poll.return_value = None

        def interrupted(timeout):
            if not cancel.is_set():
                cancel.set()
                raise subprocess.TimeoutExpired(child.args, timeout)
            return b'', None

        child.communicate.side_effect = interrupted
        with self.store.locked():
            journal = self.start()
            broker = Broker(self.project, self.output, test_config=self.test_config)
            broker.journal = journal
            broker.cancel_event = cancel
            with patch('local_agent.subprocess.Popen', return_value=child):
                # Checkpoint state uses Git subprocesses outside the mocked tool launch.
                with patch('execution_recovery.checkpoint', return_value=journal.row['checkpoint']):
                    with self.assertRaisesRegex(AgentError, 'cancellation'):
                        broker.execute('tests')
            child.kill.assert_called_once()
            self.assertEqual(broker.commands, [])
        self.assertEqual(self.store.inspect(journal.row['run_id'])['pending_outcome'],
                         'unverified_command')

    def test_timed_out_owned_test_process_is_killed_and_requires_review(self):
        child = Mock(args=['pinned'], pid=1235)
        child.poll.return_value = None
        child.communicate.return_value = (b'', None)
        with self.store.locked():
            journal = self.start()
            broker = Broker(self.project, self.output, test_config=self.test_config)
            broker.journal = journal
            with patch('local_agent.subprocess.Popen', return_value=child), \
                    patch('local_agent.time.monotonic', side_effect=[10.0, 10.0, 71.0]), \
                    patch('execution_recovery.checkpoint', return_value=journal.row['checkpoint']):
                with self.assertRaisesRegex(AgentError, 'timed out'):
                    broker.execute('tests')
            child.kill.assert_called_once()
            self.assertEqual(broker.commands, [])
        report = self.store.inspect(journal.row['run_id'])
        self.assertEqual(report['pending_outcome'], 'unverified_command')
        self.assertTrue(report['safe_to_reconcile'])

    def test_provider_timeout_preserves_recorded_write_for_restart(self):
        output = self.base / 'timeout'
        arguments = {'path': 'calculator.py', 'expected_sha256': file_hash(self.target),
                     'old': self.target.read_text(encoding='utf-8'), 'new': self.after.decode()}
        reply = {'message': {'role': 'assistant', 'tool_calls': [
            {'function': {'name': 'replace_text', 'arguments': arguments}}]}}
        failure = ProviderFailure('timeout', 'timed out', retryable=True)
        with patch('local_agent.Ollama.model', return_value={'digest': 'fixture'}), \
                patch('local_agent.Ollama.request', side_effect=[reply, failure, failure]), \
                patch('local_agent.Ollama.close') as close:
            with self.assertRaises(ProviderFailure):
                run_agent(self.project, 'fixture', 'Edit calculator', output,
                          writes=('calculator.py',))
            close.assert_called_once()
        result = json.loads((output / 'result.json').read_bytes())
        report = self.store.inspect(result['recovery_run_id'])
        self.assertEqual(report['recovery_state'], 'reconciliation_required')
        self.assertEqual(report['last_recorded']['kind'], 'project_write')
        self.assertEqual(self.target.read_bytes(), self.after)
        accepted = self.store.reconcile(result['recovery_run_id'], report['review_sha256'], 'accept')
        finish = {'message': {'role': 'assistant', 'tool_calls': [{
            'function': {'name': 'finish', 'arguments': {
                'summary': 'Verified checkpoint', 'unfinished': [], 'next_actions': [],
            }}}]}}
        with patch('local_agent.Ollama.model', return_value={'digest': 'fixture'}), \
                patch('local_agent.Ollama.request', return_value=finish):
            continued = run_agent(self.project, 'fixture', 'Inspect checkpoint',
                                  self.base / 'continued', resume_from=accepted['run_id'])
        new, _ = self.store.load(continued['recovery_run_id'])
        self.assertEqual(new['resume_from'], result['recovery_run_id'])
        self.assertEqual(new['operations'], [])
        self.assertEqual(self.target.read_bytes(), self.after)


if __name__ == '__main__':
    unittest.main()
