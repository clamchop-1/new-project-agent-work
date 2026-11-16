import unittest
from unittest.mock import patch
import model_router
from model_router import choose_model, get_models
from inference_provider import ProviderFailure

MODELS = [
    "qwen3.8:27b",
    "deepseek-r1:8b",
    "qwen3:8b",
    "qwen2.5-coder:7b-instruct",
    "qwen3.5:4b",
]

class ModelRouterTests(unittest.TestCase):
    def test_coding_task(self):
        result = choose_model(MODELS, "Write Python unit tests")
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertEqual(result['provider'], 'ollama')
        self.assertFalse(result["cloud_used"])
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['confidence'], 'low')

    def test_uncertain_routing_uses_only_configured_default_then_available_candidate(self):
        result = choose_model(["qwen2.5-coder:7b-instruct"], "Write Python tests")
        self.assertEqual(result["model"], "qwen2.5-coder:7b-instruct")

    def test_name_alone_never_asserts_a_capability_or_excludes_a_model(self):
        result = choose_model(["qwen3.5:4b"], "Write Python tests")
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['selected_evidence']['capability_sources'], {})
        self.assertIn('capabilities_context_tool_calling_memory',
                      result['candidate_models'][0]['uncertainties'])
    def test_model_discovery_uses_configured_provider_contract(self):
        provider = type('FakeProvider', (), {
            'models': lambda self: [{'name': 'qwen3.5:4b'}],
        })()
        with patch.object(model_router, 'create_inference_provider',
                          return_value=provider) as factory:
            self.assertEqual(get_models('http://127.0.0.1:11434', 'ollama'),
                             ['qwen3.5:4b'])
        factory.assert_called_once_with('ollama', 'http://127.0.0.1:11434')

    def test_candidate_discovery_checks_health_records_digest_and_closes(self):
        class FakeProvider:
            closed = False
            def health_check(self): return {'healthy': True}
            def models(self): return [{'name': 'model-a', 'digest': 'sha256:abc'}]
            def close(self): self.closed = True
        provider = FakeProvider()
        with patch.object(model_router, 'create_inference_provider', return_value=provider):
            candidates, errors = model_router.discover_candidates({
                'ollama': {'endpoint': 'http://127.0.0.1:11434'},
            })
        self.assertEqual(errors, [])
        self.assertEqual(candidates[0]['model_digest'], 'sha256:abc')
        self.assertTrue(provider.closed)

    def test_candidate_discovery_does_not_hide_authentication_failures(self):
        class FakeProvider:
            def health_check(self):
                raise ProviderFailure('authentication', 'Authentication denied.')
            def models(self): return []
            def close(self): pass
        with patch.object(model_router, 'create_inference_provider', return_value=FakeProvider()):
            with self.assertRaises(ProviderFailure):
                model_router.discover_candidates({
                    'ollama': {'endpoint': 'http://127.0.0.1:11434'},
                })

    def test_model_discovery_rejects_unsupported_provider_without_fallback(self):
        with self.assertRaises(ValueError):
            get_models('http://127.0.0.1:11434', 'remote_ollama')

    def test_model_discovery_excludes_cloud_and_remote_ollama_entries(self):
        provider = type('FakeProvider', (), {
            'models': lambda self: [
                {'name': 'qwen3.5:4b'},
                {'name': 'qwen2:cloud'},
                {'name': 'qwen3.8:27b', 'remote_host': 'https://example.invalid'},
                {'name': 'qwen3:8b', 'remote_model': 'qwen3:8b'},
            ],
        })()
        with patch.object(model_router, 'create_inference_provider',
                          return_value=provider):
            self.assertEqual(
                get_models('http://127.0.0.1:11434', 'ollama'),
                ['qwen3.5:4b'],
            )

    def test_complex_reasoning(self):
        result = choose_model(MODELS, "Debug complex architecture")
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertIn('debugging', result['task_classification']['categories'])
        self.assertIn('architecture_planning', result['task_classification']['categories'])

    def test_general_assistance(self):
        result = choose_model(MODELS, "Explain this project")
        self.assertEqual(result["model"], "qwen3:8b")

    def test_missing_models_fail_closed(self):
        with self.assertRaises(RuntimeError):
            choose_model([], "Write Python code")

    def test_remote_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            get_models("http://192.168.1.50:11434")

    def test_cloud_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            get_models("https://api.example.com")

if __name__ == "__main__":
    unittest.main()
