import unittest
from datetime import datetime, timedelta, timezone

from model_registry import empty_registry
from model_router import classify_task, route_model
from test_model_registry import evidence_value, performance, profile
from agent import _may_defer_to_configured_fallback


def candidate(model, provider='ollama', digest='sha256:a', **values):
    return {'provider_id': provider, 'model_id': model, 'model_digest': digest,
            'available': True, 'authorized': True, **values}


class IntelligentRoutingTests(unittest.TestCase):
    def test_classification_supports_multiple_categories_without_model_inference(self):
        result = classify_task('Debug the API and write unit tests for the fix')
        self.assertEqual(result['method'], 'deterministic_keyword_rules_v1')
        self.assertIn('debugging', result['categories'])
        self.assertIn('test_generation', result['categories'])

    def test_verified_capabilities_and_outcomes_drive_routing_explanation(self):
        first = profile('model-a')
        second = profile('model-b')
        second['capabilities']['code_generation']['value'] = 0.6
        records = ([performance('model-a', 'failure') for _ in range(3)]
                   + [performance('model-b', 'success') for _ in range(3)])
        decision = route_model(
            'Implement a function and unit tests',
            [candidate('model-a'), candidate('model-b', digest='sha256:a')],
            {'schema_version': 1, 'models': [first, second]},
            performance_evidence=records,
            requested_context=8192,
            provider_mode='configured',
            authorized_providers=('ollama',),
        )
        self.assertEqual(decision['selected']['model_id'], 'model-b')
        self.assertEqual(decision['status'], 'selected')
        self.assertEqual(decision['selected_evidence']['verified_successes'], 3)
        self.assertTrue(decision['selected_evidence']['outcomes_used_for_ranking'])
        self.assertIn('test_generation', decision['task_classification']['categories'])
        self.assertIn('code_generation', decision['constraints']['minimum_capabilities'])

    def test_context_capability_availability_and_provider_are_hard_constraints(self):
        too_small = profile('small')
        too_small['context_window'] = evidence_value(4096)
        decision = route_model(
            'Write code', [candidate('small')],
            {'schema_version': 1, 'models': [too_small]}, requested_context=8192,
        )
        self.assertIsNone(decision['selected'])
        self.assertIn('context_window_too_small', decision['candidate_models'][0]['excluded_reasons'])

        remote = candidate('remote', provider='ssh_tunnel_ollama')
        denied = route_model('Explain design', [remote], empty_registry(),
                             provider_mode='local_only', authorized_providers=('ssh_tunnel_ollama',))
        self.assertIsNone(denied['selected'])
        self.assertIn('provider_not_authorized', denied['candidate_models'][0]['excluded_reasons'])

        unavailable = route_model('Explain design', [candidate('down', available=False)], empty_registry())
        self.assertIsNone(unavailable['selected'])
        self.assertIn('model_unavailable', unavailable['candidate_models'][0]['excluded_reasons'])

    def test_malformed_digest_cannot_match_an_operator_capability_profile(self):
        decision = route_model(
            'Write code', [candidate('model-a', digest='sha256:a\nforged')],
            {'schema_version': 1, 'models': [profile('model-a', digest=None)]},
        )
        self.assertIsNone(decision['selected'])
        self.assertIn('malformed_model_digest',
                      decision['candidate_models'][0]['excluded_reasons'])

    def test_operator_override_is_exact_and_cannot_bypass_provider_authorization(self):
        decision = route_model(
            'Write code', [candidate('model-a'), candidate('model-b')], empty_registry(),
            model_override='model-b', conservative_default='model-a',
        )
        self.assertEqual(decision['selected']['model_id'], 'model-b')
        self.assertIn('model_override_mismatch', decision['candidate_models'][0]['excluded_reasons'])
        denied = route_model(
            'Write code', [candidate('mac', provider='ssh_tunnel_ollama')], empty_registry(),
            model_override='mac', provider_mode='local_only',
            authorized_providers=('ssh_tunnel_ollama',),
        )
        self.assertIsNone(denied['selected'])

    def test_explicit_capability_minimum_requires_evidence(self):
        decision = route_model(
            'Write code', [candidate('unknown')], empty_registry(),
            minimum_capabilities={'code_generation': 0.8},
        )
        self.assertIsNone(decision['selected'])
        self.assertIn('capability_evidence_missing:code_generation',
                      decision['candidate_models'][0]['excluded_reasons'])

    def test_missing_or_digest_stale_profiles_return_uncertainty_not_name_claims(self):
        stale = profile('model-a', digest='sha256:old')
        decision = route_model(
            'Write code', [candidate('model-a', digest='sha256:new')],
            {'schema_version': 1, 'models': [stale]}, conservative_default='model-a',
        )
        self.assertEqual(decision['status'], 'uncertain')
        self.assertEqual(decision['confidence'], 'low')
        self.assertTrue(decision['candidate_models'][0]['profile_stale_or_digest_mismatch'])

    def test_stale_profile_measurements_are_removed_from_capability_evidence(self):
        stale = profile('model-a')
        stale['capabilities']['code_generation']['evidence_refs'][0]['observed_at'] = (
            datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        decision = route_model(
            'Write code', [candidate('model-a')],
            {'schema_version': 1, 'models': [stale]}, conservative_default='model-a',
        )
        self.assertEqual(decision['status'], 'uncertain')
        self.assertTrue(decision['candidate_models'][0]['profile_stale_evidence'])
        self.assertNotIn('code_generation', decision['selected_evidence']['capability_sources'])

    def test_deterministic_tie_breaking_and_operator_preference(self):
        models = [candidate('z-model'), candidate('a-model')]
        self.assertEqual(route_model('Explain', models, empty_registry())['selected']['model_id'], 'a-model')
        preferred = route_model('Explain', models, empty_registry(), preferred_models=('z-model',))
        self.assertEqual(preferred['selected']['model_id'], 'z-model')

    def test_explicit_resource_limit_excludes_models_above_memory_estimate(self):
        prof = profile('large')
        decision = route_model(
            'Write code', [candidate('large')], {'schema_version': 1, 'models': [prof]},
            resource_limits={'max_memory_gib': 4},
        )
        self.assertIsNone(decision['selected'])
        self.assertIn('memory_limit_exceeded', decision['candidate_models'][0]['excluded_reasons'])

    def test_existing_fallback_stays_preflight_only_and_cannot_bypass_capability_exclusion(self):
        policy = {'mode': 'approved', 'substitutions': [{
            'from_provider': 'ollama', 'from_model': 'model-a',
            'to_provider': 'ssh_tunnel_ollama', 'to_model': 'mac-model',
        }]}
        unavailable = {'candidate_models': [], 'provider_discovery_errors': [
            {'provider_id': 'ollama', 'code': 'unavailable'}]}
        self.assertTrue(_may_defer_to_configured_fallback(
            unavailable, 'ollama', 'model-a', policy))
        rejected = {'candidate_models': [{
            'provider_id': 'ollama', 'model_id': 'model-a',
            'excluded_reasons': ['capability_below_minimum:code_generation'],
        }], 'provider_discovery_errors': []}
        self.assertFalse(_may_defer_to_configured_fallback(
            rejected, 'ollama', 'model-a', policy))


if __name__ == '__main__':
    unittest.main()
