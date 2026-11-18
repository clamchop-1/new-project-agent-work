import hashlib
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from agent_core import AgentError, Project, file_hash
from fixture_repo import make_fixture
from skill_library import SkillLibrary
from test_shared_memory import FixtureCase


class SkillLibraryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.library = SkillLibrary(self.project)

    def artifact(self, run_id='learn_run_001', name='source.txt', text='Verified source artifact\n'):
        path = self.project.root / '.local' / 'orchestration' / 'runs' / run_id / 'outputs' / 'task' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return {'kind': 'artifact', 'uri': str(path), 'sha256': file_hash(path), 'run_id': run_id}

    def propose(self, *, procedure='Use a focused regression test before changing parsing behavior.',
                evidence=None, run_id='learn_run_001', event_id='candidate_event_1',
                category='debugging', agent='developer'):
        evidence = evidence or [self.artifact(run_id)]
        state_path = self.project.root / '.local' / 'orchestration' / 'runs' / run_id / 'state.json'
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if not state_path.exists():
            state_path.write_text(json.dumps({'schema_version': 1,
                'graph': {'run_id': run_id, 'project_id': self.project.identity},
                'project_baseline': {'project_id': self.project.identity},
                'tasks': {'dev_task_1': {'role': agent, 'status': 'agent_completed'}}}), encoding='utf-8')
        return self.library.propose(category=category,
            problem='A parser regression caused malformed input to be accepted.',
            procedure=procedure, source_refs=evidence, evidence_refs=evidence,
            run_id=run_id, task_id='dev_task_1', source_agent=agent,
            source_event_id=event_id)

    def validate_and_approve(self, skill_id, *, trust='procedure'):
        self.library.validate(skill_id, operator='operator-reviewer', scope='procedure',
            note='I independently checked this procedure against the referenced artifacts and its stated scope.')
        return self.library.approve(skill_id, operator='operator-reviewer', trust_class=trust,
                                    confirm_sensitive=trust in {'authoritative', 'executable'})

    def test_candidate_deduplicates_and_retains_versions(self):
        first = self.propose()
        second = self.propose(event_id='candidate_event_2')
        self.assertEqual(first['skill_id'], second['skill_id'])
        self.assertTrue(second['deduplicated'])
        history = self.library.history(first['skill_id'])
        self.assertEqual([row['version'] for row in history], [1, 2])
        self.assertEqual(history[-1]['status'], 'candidate')
        self.assertEqual(self.library.list_skills(status='candidate')[0]['skill_id'], first['skill_id'])

    def test_evidence_freshness_and_forged_receipt_rejection(self):
        artifact = self.artifact()
        candidate = self.propose(evidence=[artifact])
        artifact_path = Path(artifact['uri'])
        artifact_path.write_text('changed after proposal\n', encoding='utf-8')
        with self.assertRaisesRegex(AgentError, 'missing, stale'):
            self.library.validate(candidate['skill_id'], operator='operator-reviewer',
                                  scope='tests', note='The proposed source evidence is stale.')

        run_id = 'forged_run_002'
        receipt_path = self.project.root / '.local' / 'orchestration' / 'runs' / run_id / 'outputs' / 'qa' / 'receipt.json'
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        forged = {'format': 'local-agent-command-receipt', 'project_id': 'wrong-project',
                  'command': 'tests', 'exit_code': 0, 'output_path': str(receipt_path),
                  'output_sha256': '0' * 64}
        receipt_path.write_text(json.dumps(forged), encoding='utf-8')
        reference = {'kind': 'command_receipt', 'uri': str(receipt_path),
                     'sha256': file_hash(receipt_path), 'run_id': run_id}
        with self.assertRaisesRegex(AgentError, 'forged'):
            self.propose(evidence=[reference], run_id=run_id, event_id='forged_event')

    def test_host_verified_test_receipt_can_validate_only_test_scoped_claims(self):
        run_id = 'test_receipt_run'
        output_dir = self.project.root / '.local' / 'orchestration' / 'runs' / run_id / 'outputs' / 'qa'
        output_dir.mkdir(parents=True)
        output = output_dir / 'test-output.txt'
        output.write_text('2 tests passed\n', encoding='utf-8')
        receipt_path = output_dir / 'command-0.json'
        receipt_path.write_text(json.dumps({'format': 'local-agent-command-receipt',
            'project_id': self.project.identity, 'command': 'tests', 'exit_code': 0,
            'output_path': str(output), 'output_sha256': file_hash(output)}), encoding='utf-8')
        reference = {'kind': 'command_receipt', 'uri': str(receipt_path),
                    'sha256': file_hash(receipt_path), 'run_id': run_id}
        candidate = self.propose(evidence=[reference], run_id=run_id, event_id='test_event')
        with self.assertRaisesRegex(AgentError, 'detailed independent review'):
            self.library.validate(candidate['skill_id'], operator='operator-reviewer',
                                  scope='procedure', note='Tests passed.')
        self.library.validate(candidate['skill_id'], operator='operator-reviewer', scope='tests',
                              note='The host-bound test receipt passes for the stated regression fixture.')
        with self.assertRaisesRegex(AgentError, 'cannot approve its own'):
            self.library.approve(candidate['skill_id'], operator='developer', trust_class='reference')
        with self.assertRaisesRegex(AgentError, 'explicit sensitive approval'):
            self.library.approve(candidate['skill_id'], operator='operator-reviewer',
                                 trust_class='executable')
        self.assertEqual(self.library.approve(candidate['skill_id'], operator='operator-reviewer',
            trust_class='reference')['status'], 'approved')

    def test_correction_revocation_and_retrieval_exclude_closed_or_stale_skills(self):
        candidate = self.propose(procedure='Add a small regression case before changing tokenization.')
        skill_id = candidate['skill_id']
        self.validate_and_approve(skill_id)
        retrieved = self.library.retrieve(category='debugging', problem='parser regression tokenization', limit=2,
                                          run_id='reuse_run_003', task_id='review_task_1')
        self.assertEqual(retrieved[0]['skill_id'], skill_id)
        self.assertEqual(retrieved[0]['trust_class'], 'procedure')
        self.library.correct(skill_id, operator='operator-reviewer', problem='Corrected problem statement.',
                             procedure='Inspect parser boundary behavior, then add a targeted regression test.')
        self.assertEqual(self.library.retrieve(category='debugging', problem='parser tokenization'), [])
        self.assertEqual([row['version'] for row in self.library.history(skill_id)], [1, 2, 3, 4])
        self.library.validate(skill_id, operator='operator-reviewer', scope='procedure',
            note='I independently reviewed the corrected procedure against the still-current source artifact.')
        self.library.approve(skill_id, operator='operator-reviewer', trust_class='procedure')
        Path(self.artifact_path(skill_id)).write_text('stale now\n', encoding='utf-8')
        self.assertEqual(self.library.retrieve(category='debugging', problem='parser boundary'), [])
        self.assertEqual(self.library.inspect(skill_id)['versions'][-1]['status'], 'revoked')

    def artifact_path(self, skill_id):
        return self.library.inspect(skill_id)['versions'][-1]['evidence_refs'][0]['uri']

    def test_interrupted_registry_write_is_blocked_and_operator_recoverable(self):
        candidate_ref = self.artifact(run_id='recover_run_004')
        actual = __import__('skill_library').atomic_write
        def stop_before_registry(path, raw):
            if Path(path) == self.library.registry_path:
                raise OSError('fault injection: registry replacement did not happen')
            return actual(path, raw)
        with patch('skill_library.atomic_write', side_effect=stop_before_registry):
            with self.assertRaisesRegex(OSError, 'fault injection'):
                self.propose(evidence=[candidate_ref], run_id='recover_run_004', event_id='recover_event')
        self.assertEqual(self.library.recovery_status()['classification'], 'before')
        with self.assertRaisesRegex(AgentError, 'unfinished transaction'):
            self.library.list_skills()
        result = self.library.recover(action='finish')
        self.assertEqual(result['action'], 'finished')
        self.assertEqual(len(self.library.list_skills(status='candidate')), 1)

    def test_explicit_revocation_prevents_future_retrieval(self):
        candidate = self.propose()
        self.validate_and_approve(candidate['skill_id'])
        result = self.library.revoke(candidate['skill_id'], operator='operator-reviewer',
                                     reason='Procedure no longer matches current project behavior.')
        self.assertEqual(result['status'], 'revoked')
        self.assertEqual(self.library.retrieve(category='debugging', problem='parser regression'), [])
        self.assertEqual(self.library.history(candidate['skill_id'])[-1]['status'], 'revoked')

    def test_concurrent_writers_serialize_without_lost_candidates(self):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def write(index):
            try:
                evidence = self.artifact(f'parallel_run_{index}', f'{index}.txt')
                barrier.wait(timeout=5)
                results.append(self.propose(evidence=[evidence], run_id=f'parallel_run_{index}',
                    event_id=f'parallel_event_{index}', procedure=f'Keep regression case {index} isolated and deterministic.'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.library.list_skills(status='candidate')), 2)

    def test_cross_project_transfer_requires_local_evidence_and_approval(self):
        source_candidate = self.propose(category='coding', procedure='Add a focused parser regression test before the fix.')
        self.validate_and_approve(source_candidate['skill_id'])
        package = self.library.export_package(source_candidate['skill_id'])
        destination_project, _ = make_fixture(self.base / 'destination')
        destination = SkillLibrary(destination_project)
        self.assertEqual(destination.retrieve(category='coding', problem='parser regression'), [])
        imported = destination.import_package(package, operator='operator-importer')
        self.assertEqual(destination.list_skills(status='candidate')[0]['skill_id'], imported['skill_id'])
        self.assertEqual(destination.retrieve(category='coding', problem='parser regression'), [])
        evidence_path = destination_project.root / '.local' / 'orchestration' / 'runs' / 'local_run_005' / 'outputs' / 'qa' / 'evidence.txt'
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_text('Local validation artifact\n', encoding='utf-8')
        destination.attach_evidence(imported['skill_id'], [{'kind': 'artifact', 'uri': str(evidence_path),
            'sha256': file_hash(evidence_path), 'run_id': 'local_run_005'}], operator='operator-importer')
        destination.validate(imported['skill_id'], operator='operator-reviewer', scope='procedure',
            note='I reviewed this imported procedure against the destination project evidence and scope.')
        destination.approve(imported['skill_id'], operator='operator-reviewer', trust_class='procedure')
        self.assertEqual(len(destination.retrieve(category='coding', problem='parser regression')), 1)
        self.assertEqual(len(self.library.retrieve(category='coding', problem='parser regression')), 1)


if __name__ == '__main__':
    unittest.main()
