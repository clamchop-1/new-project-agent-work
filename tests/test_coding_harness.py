import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from coding_harness import (
    EvidenceReference, ExecutionBudgets, HarnessContractError, HarnessEvent,
    HarnessResult, HarnessTaskRequest, LegacyCustomHarness,
    LegacyExecutionContext, SCHEMA_VERSION, SessionReference,
)
from test_shared_memory import FixtureCase


def request_for(project, **overrides):
    values = {
        'task_id': 'task-test-1', 'run_id': 'run-test-1',
        'project_id': project.identity, 'objective': 'Inspect the fixture safely.',
        'model': 'mock-local-model', 'provider': 'ollama',
        'budgets': ExecutionBudgets(max_turns=4, max_tool_calls=3,
                                    wall_seconds=30, cancellation_grace_seconds=2),
    }
    values.update(overrides)
    return HarnessTaskRequest(**values)


class HarnessContractTests(unittest.TestCase):
    def test_request_event_and_result_round_trip(self):
        ref = EvidenceReference('instruction', 'AGENTS.md', 'a' * 64, 'agents-root')
        request = HarnessTaskRequest(
            'task-1', 'run-1', 'project-1', 'Review safely', 'model-a', 'ollama',
            ExecutionBudgets(5, 8, 120, 3), instruction_refs=(ref,),
            memory_refs=(ref,),
            session_ref=SessionReference('legacy_custom', 'session-1', False),
        )
        self.assertEqual(HarnessTaskRequest.from_json(request.to_json()), request)

        event = HarnessEvent(
            'run-1', 7, 'skill.candidate.proposed',
            {'candidate_lesson_ref': 'candidate:42', 'source_refs': ['tests/test_x.py'],
             'evidence_refs': ['receipt:1'], 'validation_status': 'unvalidated',
             'source_hashes': ['b' * 64]}, harness_id='legacy_custom',
        )
        self.assertEqual(HarnessEvent.from_dict(json.loads(event.to_json())), event)
        progress = HarnessEvent(
            'run-1', 8, 'inference.progress',
            {'legacy_step': 2, 'phase': 'awaiting_response_bytes',
             'elapsed_seconds': 15.0}, harness_id='legacy_custom',
        )
        self.assertEqual(HarnessEvent.from_dict(json.loads(progress.to_json())), progress)

        result = HarnessResult(
            SCHEMA_VERSION, 'run-1', 'legacy_custom', '1', 'completed', 'complete',
            {'summary': 'Done'}, None, (ref,), (ref,),
        )
        decoded = HarnessResult.from_json(result.to_json())
        self.assertEqual(decoded, result)
        self.assertEqual(decoded.verification_authority, 'host_pending')

    def test_unknown_schema_event_and_result_verification_claim_are_rejected(self):
        with self.assertRaisesRegex(HarnessContractError, 'schema version'):
            HarnessTaskRequest.from_dict({'schema_version': 99})
        with self.assertRaisesRegex(HarnessContractError, 'Unknown harness event'):
            HarnessEvent.from_dict({
                'schema_version': 1, 'run_id': 'r', 'sequence': 1,
                'event_id': 'e', 'occurred_at': 'now', 'harness_id': 'h',
                'event_type': 'run.magic', 'payload': {},
            })
        with self.assertRaisesRegex(HarnessContractError, 'schema version'):
            HarnessEvent('r', 1, 'run.started', schema_version=2)
        result = HarnessResult(
            1, 'run-1', 'legacy_custom', '1', 'completed', 'complete', {},
            None, (), (),
        ).to_dict()
        result['verification_authority'] = 'independently_verified'
        with self.assertRaisesRegex(HarnessContractError, 'cannot claim'):
            HarnessResult.from_dict(result)

    def test_candidate_event_requires_evidence_hashes_and_validation_state(self):
        base = {'candidate_lesson_ref': 'candidate:1', 'source_refs': ['x.py'],
                'evidence_refs': ['run:1'], 'validation_status': 'unvalidated',
                'source_hashes': ['c' * 64]}
        event = HarnessEvent('r', 1, 'skill.candidate.proposed', base)
        self.assertEqual(event.payload['source_hashes'], ['c' * 64])
        invalid = dict(base, source_hashes=['not-a-hash'])
        with self.assertRaisesRegex(HarnessContractError, 'SHA-256'):
            HarnessEvent('r', 1, 'skill.candidate.proposed', invalid)
        with self.assertRaisesRegex(HarnessContractError, 'Unknown task request field'):
            HarnessTaskRequest.from_dict({
                'schema_version': 1, 'task_id': 't', 'run_id': 'r',
                'project_id': 'p', 'objective': 'o', 'model': 'm', 'provider': 'p',
                'budgets': ExecutionBudgets().to_dict(), 'write_paths': ['x.py'],
            })


class HarnessLifecycleTests(FixtureCase):
    def context(self, name='run'):
        return LegacyExecutionContext(
            self.project, self.base / name, 'http://127.0.0.1:11434',
        )

    def test_cancellation_lifecycle_and_cleanup(self):
        entered = threading.Event()

        def waiting_runner(project, model, task, output, endpoint, **kwargs):
            kwargs['event_callback']('assistant.turn.completed', {
                'legacy_step': 0, 'message': {'role': 'assistant', 'content': 'working'},
            })
            entered.set()
            while not kwargs['cancel_event'].wait(0.01):
                pass
            return {'status': 'failed', 'error': 'cancelled by harness'}

        adapter = LegacyCustomHarness(waiting_runner)
        handle = adapter.start(request_for(self.project), self.context())
        stream = handle.events()
        events = [next(stream)]
        self.assertEqual(events[0].event_type, 'run.started')
        self.assertTrue(entered.wait(2))
        events.append(next(stream))
        self.assertEqual(events[1].event_type, 'assistant.turn.completed')
        self.assertTrue(adapter.cancel('run-test-1'))
        result = handle.wait(3)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, 'cancelled')
        self.assertFalse(handle.cancel())
        events.extend(stream)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertIn('run.cancel_requested', [event.event_type for event in events])
        self.assertEqual(events[-1].event_type, 'run.cancelled')
        self.assertTrue(adapter.close('run-test-1', 1))
        self.assertEqual(handle.state, 'closed')

    def test_legacy_adapter_passes_only_host_grants_and_preserves_broker(self):
        responses = iter([
            {'role': 'assistant', 'tool_calls': [{
                'function': {'name': 'create_file', 'arguments': {
                    'path': 'stats.py', 'content': 'def clamp(value, low, high): return value\n',
                }}
            }]},
            {'role': 'assistant', 'tool_calls': [{
                'function': {'name': 'finish', 'arguments': {
                    'summary': 'Attempted no unauthorized write.',
                    'unfinished': [], 'next_actions': [],
                }}
            }]},
        ])

        def fake_request(client, endpoint, payload):
            return {'message': next(responses)}

        before_state = self.project.state()
        before_memory = self.project.memory_snapshot()
        before_stats = (self.project.root / 'stats.py').exists()
        request = request_for(self.project, budgets=ExecutionBudgets(
            max_turns=4, max_tool_calls=2, wall_seconds=30, cancellation_grace_seconds=2,
        ))
        self.assertFalse(hasattr(request, 'approved_write_paths'))
        context = LegacyExecutionContext(
            self.project, self.base / 'legacy-run', 'http://127.0.0.1:11434',
            approved_write_paths=(),
        )
        adapter = LegacyCustomHarness()
        with patch('local_agent.Ollama.model', return_value={'digest': 'mock-digest'}), \
                patch('local_agent.Ollama.request', autospec=True, side_effect=fake_request):
            handle = adapter.start(request, context)
            result = handle.wait(10)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, 'incomplete', (result.raw_status, result.summary))
        self.assertEqual(result.raw_status, 'incomplete')
        self.assertEqual(result.verification_authority, 'host_pending')
        self.assertEqual(self.project.state(), before_state)
        self.assertEqual(self.project.memory_snapshot(), before_memory)
        self.assertEqual((self.project.root / 'stats.py').exists(), before_stats)
        self.assertTrue(any(e.event_type == 'tool.proposed' for e in handle.events()))
        self.assertTrue(adapter.close(request.run_id, 1))

    def test_tool_call_budget_stops_dispatch_before_the_next_tool(self):
        responses = iter([
            {
                'role': 'assistant',
                'tool_calls': [
                    {'function': {'name': 'list_files', 'arguments': {}}},
                ],
            },
            {
                'role': 'assistant',
                'tool_calls': [
                    {'function': {'name': 'finish', 'arguments': {
                        'summary': 'Should not dispatch after budget.',
                        'unfinished': [], 'next_actions': [],
                    }}},
                ],
            },
        ])

        def fake_request(client, endpoint, payload):
            return {'message': next(responses)}

        request = request_for(self.project, run_id='budget-run', budgets=ExecutionBudgets(
            max_turns=2, max_tool_calls=1, wall_seconds=30, cancellation_grace_seconds=2,
        ))
        adapter = LegacyCustomHarness()
        with patch('local_agent.Ollama.model', return_value={'digest': 'mock-digest'}), \
                patch('local_agent.Ollama.request', autospec=True, side_effect=fake_request):
            handle = adapter.start(request, self.context('budget-run'))
            result = handle.wait(10)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, 'failed')
        self.assertIn('Tool-call execution budget exceeded', handle.error)
        proposed = [event for event in handle.events() if event.event_type == 'tool.proposed']
        self.assertEqual(len(proposed), 1)
        self.assertTrue(adapter.close(request.run_id, 1))

    def test_project_identity_mismatch_and_resume_fail_closed(self):
        adapter = LegacyCustomHarness(lambda *args, **kwargs: {'status': 'complete'})
        request = request_for(self.project, project_id='different-project')
        with self.assertRaisesRegex(HarnessContractError, 'identity'):
            adapter.start(request, self.context())
        self.assertFalse(adapter.capabilities.native_resume)
        with self.assertRaisesRegex(HarnessContractError, 'no native session-resume'):
            adapter.resume(request_for(self.project), SessionReference(
                'legacy_custom', 'old-session', True), self.context())


if __name__ == '__main__':
    unittest.main()
