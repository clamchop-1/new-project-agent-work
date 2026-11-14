import unittest
from unittest.mock import patch
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
        self.assertFalse(result["cloud_used"])

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
