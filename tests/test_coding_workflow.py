import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_core import Project, file_hash
from coding_workflow import CodingWorkflow, CodingWorkflowError
from inference_provider import ProviderFailure
from local_agent import Broker, HUB


M2_REPO = (HUB / '.local/tests/unified-coding-demo/7a7871ffb59a4019b9c1877898b4577e/repo')
PINNED_TEST = M2_REPO / 'tests/test_progress.py'
PINNED_SHA = '989f840b6db6d9f65823a7fa0725867b0abd8bc28554746d8b020bdc6c8a2af7'
STATS_BASE = HUB / '.local/tests/milestone4-review-20261123-df64251f96a44e2287ba2357e171420d/repo'

GOOD_PATCH = (
    b'diff --git a/src/progress.py b/src/progress.py\n'
    b'new file mode 100644\n'
    b'--- /dev/null\n+++ b/src/progress.py\n'
    b'@@ -0,0 +1,8 @@\n'
    b'+def clamp_percent(value):\n'
    b'+    if type(value) is not int:\n'
    b'+        raise TypeError\n'
    b'+    if value < 0:\n'
    b'+        return 0\n'
    b'+    if value > 100:\n'
    b'+        return 100\n'
    b'+    return value\n'
)
GOOD_PROPOSAL = json.dumps({'files': [{
    'path': 'src/progress.py',
    'base_sha256': None,
    'content': ('def clamp_percent(value):\n'
                '    if type(value) is not int:\n'
                '        raise TypeError\n'
                '    if value < 0:\n'
                '        return 0\n'
                '    if value > 100:\n'
                '        return 100\n'
                '    return value\n'),
}]})


class FakeProvider:
    def __init__(self, content=None, failure=None):
        self.content, self.failure, self.calls = content, failure, 0

    def chat(self, model, messages, tools, context, options, timeout=None):
        self.calls += 1
        if self.failure:
            raise self.failure
        self.assertions = (model, tools, timeout)
        content = self.content.decode() if isinstance(self.content, bytes) else self.content
        return {'message': {'role': 'assistant', 'content': content}}


PLANNER_JSON = json.dumps({'summary': 'Create an exact-integer percent clamp.',
    'files': ['src/progress.py'],
    'acceptance_criteria': ['Reject booleans and non-integers.', 'Clamp below 0 and above 100.'],
    'test_requirements': ['Run the pinned Broker acceptance tests.'],
    'steps': ['Validate the exact built-in integer type.', 'Clamp to the inclusive range.']})
REVIEWER_JSON = json.dumps({'summary': 'The patch satisfies the requested contract.',
    'recommendation': 'changes_requested', 'findings': [
        {'severity': 'low', 'path': 'src/progress.py', 'line': 2,
         'message': 'The exception can include a more descriptive message.'}]})


class SequentialProvider:
    def __init__(self, planner=PLANNER_JSON, review=REVIEWER_JSON,
                 failure_at=None, cancel_after=None):
        self.responses = [planner, GOOD_PROPOSAL, review]
        self.failure_at = failure_at
        self.cancel_after = cancel_after
        self.calls = []
        self.cancel_event = None

    def chat(self, model, messages, tools, context, options, timeout=None):
        index = len(self.calls)
        self.calls.append({'model': model, 'messages': messages, 'tools': tools, 'timeout': timeout})
        if self.failure_at == index:
            raise ProviderFailure('timeout', 'bounded injected timeout')
        if index >= len(self.responses):
            raise AssertionError('unexpected model invocation')
        response = self.responses[index]
        if self.cancel_after == index and self.cancel_event is not None:
            self.cancel_event.set()
        if isinstance(response, bytes):
            response = response.decode()
        return {'message': {'role': 'assistant', 'content': response}}


class CodingWorkflowTests(unittest.TestCase):
    def setUp(self):
        if not PINNED_TEST.is_file() or file_hash(PINNED_TEST) != PINNED_SHA:
            self.skipTest('Preserved operator-pinned clamp acceptance test is unavailable')
        tests_root = HUB / '.local' / 'tests'
        tests_root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix='milestone3-unit-', dir=tests_root))
        self.base.rename(self.base.with_name(self.base.name.replace('milestone3-unit-', 'milestone3-')))
        self.base = self.base.with_name(self.base.name.replace('milestone3-unit-', 'milestone3-'))
        self.root = self.base / 'repo'
        self.root.mkdir()
        (self.root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        (self.root / 'src').mkdir()
        (self.root / 'src/.keep').write_text('', encoding='utf-8')
        (self.root / 'tests').mkdir()
        shutil.copyfile(PINNED_TEST, self.root / 'tests/test_progress.py')
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
        for argv in (['git', 'init', '--initial-branch=main', str(self.root)],
                     ['git', '-C', str(self.root), 'add', '-A'],
                     ['git', '-C', str(self.root), '-c', 'user.name=Workflow Fixture',
                      '-c', 'user.email=workflow@example.invalid', 'commit', '-m', 'Pinned test baseline']):
            result = subprocess.run(argv, env=env, capture_output=True)
            if result.returncode:
                self.fail(result.stderr.decode(errors='replace'))
        private = self.root / '.local'
        private.mkdir()
        config = {'test_file': 'tests/test_progress.py', 'test_sha256': PINNED_SHA,
                  'modules': ['src.progress']}
        (private / 'pinned-tests.json').write_text(json.dumps(config), encoding='utf-8')
        self.project = Project(self.root)
        self.workflow = CodingWorkflow(self.project,
            'Implement clamp_percent: exact int only; clamp to [0,100].',
            ('src/progress.py',))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def propose(self, content=GOOD_PROPOSAL):
        return self.workflow.generate(FakeProvider(content), 'fake-qwen')

    def approve(self, proposal=None):
        proposal = proposal or self.propose()
        self.workflow.approve(patch_sha256=proposal['patch_sha256'],
            repository=str(self.root), operator='operator-test',
            test_command=proposal['test_command'])
        return proposal

    def test_patch_hash_mismatch_stops_at_approval(self):
        proposal = self.propose()
        with self.assertRaisesRegex(CodingWorkflowError, 'does not exactly match'):
            self.workflow.approve(patch_sha256='0' * 64, repository=str(self.root),
                operator='operator-test', test_command=proposal['test_command'])
        self.assertEqual(self.workflow.row['state'], 'awaiting_approval')

    def test_approval_rejects_substituted_fixed_test_command(self):
        proposal = self.propose()
        with self.assertRaisesRegex(CodingWorkflowError, 'does not exactly match'):
            self.workflow.approve(patch_sha256=proposal['patch_sha256'],
                repository=str(self.root), operator='operator-test',
                test_command=proposal['test_command'] + ' && arbitrary-command')
        self.assertEqual(self.workflow.row['state'], 'awaiting_approval')

    def test_approval_requires_predefined_simulation_tests(self):
        proposal = self.propose()
        self.workflow.row.pop('simulation_test_result', None)
        with self.assertRaisesRegex(CodingWorkflowError, 'pre-approval simulation tests'):
            self.workflow.approve(patch_sha256=proposal['patch_sha256'],
                repository=str(self.root), operator='operator-test',
                test_command=proposal['test_command'])

    def test_repository_state_mismatch_rejects_stale_approval(self):
        proposal = self.propose()
        (self.root / 'README.md').write_text('unrelated change\n')
        with self.assertRaisesRegex(CodingWorkflowError, 'changed since proposal'):
            self.workflow.approve(patch_sha256=proposal['patch_sha256'],
                repository=str(self.root), operator='operator-test',
                test_command=proposal['test_command'])

    def test_unauthorized_file_is_rejected(self):
        bad = GOOD_PATCH + (
            b'diff --git a/extra.py b/extra.py\nnew file mode 100644\n'
            b'--- /dev/null\n+++ b/extra.py\n@@ -0,0 +1 @@\n+x\n')
        with self.assertRaisesRegex(CodingWorkflowError, 'strict validation'):
            self.propose(bad)

    def test_structured_correction_is_bounded_and_validation_failure_is_preserved(self):
        class CorrectionProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.responses = [
                    json.dumps({'files': [{
                        'path': 'outside.py', 'base_sha256': None, 'content': 'x = 1\n'}]}),
                    GOOD_PROPOSAL,
                ]
                self.messages = []
            def chat(self, model, messages, tools, context, options, timeout=None):
                self.calls += 1
                self.messages.append(messages)
                return {'message': {'role': 'assistant', 'content': self.responses[self.calls - 1]}}
        provider = CorrectionProvider()
        proposal = self.workflow.generate(provider, 'fake-qwen', max_attempts=2)
        self.assertEqual(proposal['state'], 'awaiting_approval')
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(self.workflow.row['validation_failures']), 1)
        feedback = json.loads(provider.messages[1][1]['content'])['correction_feedback']
        self.assertEqual(feedback['code'], 'strict_proposal_validation_failed')
        self.assertIn('unauthorized', feedback['message'])

    def test_truncated_structured_response_is_recorded_and_not_reviewed(self):
        class TruncatedProvider(FakeProvider):
            def chat(self, model, messages, tools, context, options, timeout=None):
                self.calls += 1
                return {'done': False, 'done_reason': 'length',
                        'message': {'role': 'assistant', 'content': GOOD_PROPOSAL}}
        with self.assertRaisesRegex(CodingWorkflowError, 'strict validation'):
            self.workflow.generate(TruncatedProvider(), 'fake-qwen', max_attempts=1)
        self.assertEqual(self.workflow.row['validation_failures'][0]['code'], 'proposal_truncated')
        self.assertEqual(self.workflow.row['state'], 'failed')

    def test_approved_patch_applies_and_runs_fixed_tests_and_records_candidate(self):
        proposal = self.approve()
        result = self.workflow.apply_and_test(operator='operator-test')
        self.assertEqual(result['exit_code'], 0, result['output'])
        self.assertEqual(self.workflow.row['state'], 'completed')
        self.assertEqual(file_hash(self.root / 'src/progress.py'),
                         proposal['expected_post_images']['src/progress.py'])
        from skill_library import SkillLibrary
        candidates = SkillLibrary(self.project).list_skills(status='candidate')
        self.assertEqual(len(candidates), 1)
        self.workflow._record_candidate(result, success=True)
        self.assertEqual(len(SkillLibrary(self.project).list_skills(status='candidate')), 1)
        with self.assertRaises(CodingWorkflowError):
            self.workflow.apply_and_test(operator='operator-test')

    def test_failed_trusted_test_is_preserved_and_records_unpromoted_candidate(self):
        incorrect = json.dumps({'files': [{
            'path': 'src/progress.py', 'base_sha256': None,
            'content': 'def clamp_percent(value):\n    return value\n'}]})
        with self.assertRaisesRegex(CodingWorkflowError, 'failed strict validation'):
            self.propose(incorrect)
        self.assertNotEqual(self.workflow.row['simulation_test_result']['exit_code'], 0)
        self.assertEqual(self.workflow.row['state'], 'failed')
        from skill_library import SkillLibrary
        self.assertEqual(len(SkillLibrary(self.project).list_skills(status='candidate')), 1)
        lesson = self.project.root / '.local/orchestration/runs' / self.workflow.run_id / 'outputs/coding/candidate-lesson.md'
        self.assertIn('failed', lesson.read_text(encoding='utf-8').lower())

    def test_post_application_test_failure_is_terminal_and_records_candidate(self):
        self.approve()
        original = Broker.execute
        def fail_after_write(broker, name, path=None):
            if broker.project.root == self.root and name == 'tests':
                return {'exit_code': 1, 'output': 'Ran 3 tests\nFAILED (failures=1, errors=0)\n'}
            return original(broker, name, path)
        with patch.object(Broker, 'execute', fail_after_write):
            result = self.workflow.apply_and_test(operator='operator-test')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(self.workflow.row['state'], 'failed')
        self.assertTrue((self.root / 'src/progress.py').is_file())
        from skill_library import SkillLibrary
        self.assertEqual(len(SkillLibrary(self.project).list_skills(status='candidate')), 1)

    def test_candidate_evidence_persistence_failure_does_not_reapply(self):
        self.approve()
        with patch.object(self.workflow, '_record_candidate', side_effect=OSError('injected evidence storage failure')):
            with self.assertRaisesRegex(OSError, 'evidence storage failure'):
                self.workflow.apply_and_test(operator='operator-test')
        self.assertEqual(self.workflow.row['state'], 'failed')
        self.assertTrue((self.root / 'src/progress.py').is_file())
        self.assertTrue(self.workflow.row['approval']['used'])
        with self.assertRaises(CodingWorkflowError):
            self.workflow.apply_and_test(operator='operator-test')

    def test_reused_approval_and_changed_expected_post_image_are_rejected(self):
        proposal = self.approve()
        self.workflow.row['approval']['expected_post_images']['src/progress.py'] = 'f' * 64
        with self.assertRaisesRegex(CodingWorkflowError, 'modified after operator approval'):
            self.workflow.apply_and_test(operator='operator-test')

    def test_patch_bytes_and_repository_are_rechecked_after_approval(self):
        self.approve()
        (self.workflow.run_dir / 'approved.patch').write_bytes(GOOD_PATCH + b'\n')
        with self.assertRaisesRegex(CodingWorkflowError, 'patch bytes changed'):
            self.workflow.apply_and_test(operator='operator-test')

        second = CodingWorkflow(self.project, 'second task', ('src/progress.py',))
        proposal = second.generate(FakeProvider(GOOD_PROPOSAL), 'fake-qwen')
        second.approve(patch_sha256=proposal['patch_sha256'], repository=str(self.root),
            operator='operator-test', test_command=proposal['test_command'])
        (self.root / 'README.md').write_text('changed after approval\n')
        with self.assertRaisesRegex(CodingWorkflowError, 'changed since proposal'):
            second.apply_and_test(operator='operator-test')
        self.assertEqual(self.workflow.row['state'], 'approved')
        self.workflow.row['approval']['expected_post_images'] = proposal['expected_post_images']
        self.workflow.row['approval']['binding_sha256'] = '0' * 64
        with self.assertRaisesRegex(CodingWorkflowError, 'modified after operator approval'):
            self.workflow.apply_and_test(operator='operator-test')

    def test_partial_application_is_interrupted_and_never_replayed(self):
        proposal = self.approve()
        original = Broker.write
        def partial(broker, path, content, expected=None):
            if path == 'src/progress.py':
                target = broker.project.path(path)
                target.parent.mkdir(exist_ok=True)
                target.write_text('partial\n')
                raise OSError('fault injection after partial write')
            return original(broker, path, content, expected)
        with patch.object(Broker, 'write', partial):
            with self.assertRaises(OSError):
                self.workflow.apply_and_test(operator='operator-test')
        self.assertEqual(self.workflow.row['state'], 'interrupted')
        with self.assertRaises(CodingWorkflowError):
            self.workflow.apply_and_test(operator='operator-test')

    def test_interrupted_recovery_and_cancellation_do_not_replay(self):
        proposal = self.propose()
        self.workflow.row['state'] = 'applying'
        self.workflow.row['approval'] = {'used': True}
        self.workflow._save()
        recovered = CodingWorkflow.load(self.project, self.workflow.run_id)
        self.assertEqual(recovered.row['state'], 'interrupted')
        with self.assertRaises(CodingWorkflowError):
            recovered.apply_and_test(operator='operator-test')

        second = CodingWorkflow(self.project, 'another task', ('src/progress.py',))
        second.cancel()
        self.assertEqual(second.row['state'], 'cancelled')

    def test_inference_timeout_is_terminal_and_tool_proposals_are_not_accepted(self):
        timed = CodingWorkflow(self.project, 'timeout task', ('src/progress.py',))
        with self.assertRaises(CodingWorkflowError):
            timed.generate(FakeProvider(failure=ProviderFailure('timeout', 'bounded timeout')),
                           'fake-qwen', max_attempts=1)
        self.assertEqual(timed.row['state'], 'timed_out')
        malformed = CodingWorkflow(self.project, 'tool task', ('src/progress.py',))
        class ToolProvider(FakeProvider):
            def chat(self, *args, **kwargs):
                return {'message': {'role': 'assistant', 'content': GOOD_PATCH.decode(),
                                    'tool_calls': [{'function': {'name': 'x'}}]}}
        with self.assertRaises(CodingWorkflowError):
            malformed.generate(ToolProvider(), 'fake-qwen', max_attempts=1)

    def test_collaborative_roles_are_sequential_tool_free_and_advisory(self):
        provider = SequentialProvider()
        models = {'planner': 'planner-model', 'coder': 'coder-model', 'reviewer': 'reviewer-model'}
        proposal = self.workflow.generate_collaborative(provider, role_models=models,
                                                        max_attempts=1, provider_id='ssh_tunnel_ollama')
        self.assertEqual([call['model'] for call in provider.calls],
                         ['planner-model', 'coder-model', 'reviewer-model'])
        self.assertTrue(all(call['tools'] == [] for call in provider.calls))
        self.assertEqual(self.workflow.row['state'], 'awaiting_approval')
        self.assertTrue(self.workflow.row['review_advisory_only'])
        self.assertEqual(proposal['review']['recommendation'], 'changes_requested')
        self.assertEqual(self.workflow.row['reviewed_patch_sha256'], proposal['patch_sha256'])
        self.assertEqual(self.workflow.row['coder_attempts'][0]['model'], 'coder-model')
        self.assertEqual([call['role'] for call in self.workflow.row['role_calls']],
                         ['architect', 'reviewer'])
        reviewer_request = json.loads(provider.calls[2]['messages'][1]['content'])
        self.assertEqual(reviewer_request['task'], self.workflow.task)
        self.assertEqual(reviewer_request['acceptance_criteria'], PLANNER_JSON and json.loads(PLANNER_JSON)['acceptance_criteria'])
        self.assertIn('validation_report', reviewer_request)
        approved = self.workflow.approve(patch_sha256=proposal['patch_sha256'],
            repository=str(self.root), operator='operator-test', test_command=proposal['test_command'])
        self.assertEqual(approved['patch_sha256'], proposal['patch_sha256'])

    def test_planner_scope_violation_and_reviewer_bad_schema_fail_closed(self):
        outside = json.dumps({'summary': 'Plan.', 'files': ['secrets.py'],
            'acceptance_criteria': ['one'], 'test_requirements': ['one'], 'steps': ['one']})
        with self.assertRaises(CodingWorkflowError):
            self.workflow.generate_collaborative(SequentialProvider(planner=outside),
                role_models={'planner': 'p', 'coder': 'c', 'reviewer': 'r'}, max_attempts=1)
        self.assertEqual(self.workflow.row['state'], 'failed')
        self.assertNotIn('approved.patch', [item.name for item in self.workflow.run_dir.iterdir()])

        reviewer = CodingWorkflow(self.project, 'independent review test', ('src/progress.py',))
        invalid_review = json.dumps({'summary': 'review complete', 'recommendation': 'approve', 'findings': []})
        with self.assertRaisesRegex(CodingWorkflowError, 'Reviewer summary or recommendation'):
            reviewer.generate_collaborative(SequentialProvider(review=invalid_review),
                role_models={'planner': 'p', 'coder': 'c', 'reviewer': 'r'}, max_attempts=1)
        self.assertEqual(reviewer.row['state'], 'failed')
        with self.assertRaises(CodingWorkflowError):
            reviewer.approve(patch_sha256=reviewer.row['patch_sha256'],
                repository=str(self.root), operator='operator-test',
                test_command=reviewer.row['test_command'])

    def test_collaboration_timeout_and_cancellation_do_not_advance(self):
        timed = CodingWorkflow(self.project, 'timeout collaboration', ('src/progress.py',))
        with self.assertRaises(CodingWorkflowError):
            timed.generate_collaborative(SequentialProvider(failure_at=0),
                role_models={'planner': 'p', 'coder': 'c', 'reviewer': 'r'}, max_attempts=1)
        self.assertEqual(timed.row['state'], 'timed_out')

        cancelled = CodingWorkflow(self.project, 'cancel collaboration', ('src/progress.py',))
        import threading
        cancel = threading.Event()
        with self.assertRaises(CodingWorkflowError):
            cancelled.generate_collaborative(SequentialProvider(cancel_after=0),
                role_models={'planner': 'p', 'coder': 'c', 'reviewer': 'r'},
                max_attempts=1, cancel_event=cancel)
        self.assertEqual(cancelled.row['state'], 'cancelled')

    def test_two_module_stats_workflow_reaches_approval_checkpoint_with_pinned_tests(self):
        if not (STATS_BASE / 'tests/test_stats.py').is_file():
            self.skipTest('Preserved operator-pinned multi-module stats fixture is unavailable')
        tests_root = HUB / '.local' / 'tests'
        base = Path(tempfile.mkdtemp(prefix='milestone4-unit-', dir=tests_root))
        base.rename(base.with_name(base.name.replace('milestone4-unit-', 'milestone4-')))
        base = base.with_name(base.name.replace('milestone4-unit-', 'milestone4-'))
        root = base / 'repo'
        root.mkdir()
        (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        old_calculator = b'def average(values):\r\n    return sum(values) / (len(values) + 1)\r\n'
        (root / 'calculator.py').write_bytes(old_calculator)
        (root / 'stats.py').write_text('', encoding='utf-8')
        (root / 'tests').mkdir()
        shutil.copyfile(STATS_BASE / 'tests/test_stats.py', root / 'tests/test_stats.py')
        private = root / '.local'; private.mkdir()
        shutil.copyfile(STATS_BASE / '.local/pinned-tests.json', private / 'pinned-tests.json')
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
        for argv in (['git', 'init', '--initial-branch=main', str(root)],
                     ['git', '-C', str(root), 'add', '-A'],
                     ['git', '-C', str(root), '-c', 'user.name=Workflow Fixture',
                      '-c', 'user.email=workflow@example.invalid', 'commit', '-m', 'Pinned stats baseline']):
            result = subprocess.run(argv, env=env, capture_output=True)
            if result.returncode:
                self.fail(result.stderr.decode(errors='replace'))
        old_hash = file_hash(root / 'calculator.py')
        workflow = CodingWorkflow(Project(root),
            'Implement calculator.average(values) with ValueError for empty input and correct arithmetic mean; implement inclusive stats.clamp(value, low, high).',
            ('calculator.py', 'stats.py'), test_profile='stats_multi_module_v1')
        plan = json.dumps({'summary': 'Implement both pinned pure utility functions.',
            'files': ['calculator.py', 'stats.py'],
            'acceptance_criteria': ['Average returns the arithmetic mean.',
                'Empty average input raises ValueError.', 'Clamp returns the inclusive bounded value.'],
            'test_requirements': ['Run all pinned tests/test_stats.py cases.'],
            'steps': ['Correct average and empty-input behavior.', 'Implement inclusive clamp.']})
        coder = json.dumps({'files': [
            {'path': 'calculator.py', 'base_sha256': old_hash,
             'content': 'def average(values):\n    if not values:\n        raise ValueError("empty input")\n    return sum(values) / len(values)\n'},
            {'path': 'stats.py', 'base_sha256': file_hash(root / 'stats.py'),
             'content': 'def clamp(value, low, high):\n    if value < low:\n        return low\n    if value > high:\n        return high\n    return value\n'}]})
        review = json.dumps({'summary': 'Both functions match the criteria and tests.',
            'recommendation': 'no_blocking_findings', 'findings': []})
        provider = SequentialProvider(planner=plan, review=review)
        provider.responses = [plan, coder, review]
        try:
            proposal = workflow.generate_collaborative(provider,
                role_models={'planner': 'qwen3-coder:30b', 'coder': 'qwen3-coder:30b',
                             'reviewer': 'qwen3-coder:30b'},
                max_attempts=1, provider_id='ssh_tunnel_ollama')
            self.assertEqual(workflow.row['state'], 'awaiting_approval')
            self.assertEqual(proposal['files'], ['calculator.py', 'stats.py'])
            self.assertEqual(workflow.row['simulation_test_result']['exit_code'], 0)
            self.assertEqual(workflow.row['simulation_test_result']['test_count'], 3)
            self.assertIsNone(workflow.row['approval'])
            self.assertEqual([call['model'] for call in provider.calls],
                ['qwen3-coder:30b', 'qwen3-coder:30b', 'qwen3-coder:30b'])
            self.assertIn('authorized_source_context',
                          json.loads(provider.calls[2]['messages'][1]['content']))
            from skill_library import SkillLibrary
            candidates = SkillLibrary(Project(root)).list_skills(status='candidate')
            self.assertEqual(len(candidates), 1)
            candidate = SkillLibrary(Project(root)).history(candidates[0]['skill_id'])[-1]
            self.assertEqual(candidate['status'], 'candidate')
            self.assertEqual(len(candidate['evidence_refs']), 3)
            self.assertFalse(any(item.get('test_verified') for item in candidate['evidence_refs']))
            self.assertEqual((root / 'calculator.py').read_bytes(), old_calculator)
            self.assertFalse((root / 'stats.py').read_bytes())
        finally:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
