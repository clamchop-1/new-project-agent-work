import unittest
from unittest.mock import patch
import model_router
from model_router import choose_model, get_models

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
        self.assertEqual(result["model"], "qwen2.5-coder:7b-instruct")
        self.assertEqual(result['provider'], 'ollama')
        self.assertFalse(result["cloud_used"])

    def test_model_discovery_uses_configured_provider_contract(self):
        provider = type('FakeProvider', (), {
            'models': lambda self: [{'name': 'qwen3.5:4b'}],
        })()
        with patch.object(model_router, 'create_inference_provider',
                          return_value=provider) as factory:
            self.assertEqual(get_models('http://127.0.0.1:11434', 'ollama'),
                             ['qwen3.5:4b'])
        factory.assert_called_once_with('ollama', 'http://127.0.0.1:11434')

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
        self.assertEqual(result["model"], "qwen3.8:27b")

    def test_general_assistance(self):
        result = choose_model(MODELS, "Explain this project")
        self.assertEqual(result["model"], "qwen3.5:4b")

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
