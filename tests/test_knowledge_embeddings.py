import unittest
from unittest.mock import patch

from agent_core import AgentError
from knowledge_embeddings import OllamaEmbeddingProvider


class FakeOllama:
    def __init__(self):
        self.calls = []
        self.closed = False

    def model(self, name):
        return {'name': name, 'digest': 'sha256:fixture-model', 'modified_at': 'fixture-time',
                'details': {'format': 'gguf'}}

    def request(self, path, payload, timeout=180):
        self.calls.append((path, payload, timeout))
        return {'embeddings': [[1.0, 2.0], [3.0, 4.0]]}

    def close(self):
        self.closed = True


class KnowledgeEmbeddingTests(unittest.TestCase):
    def test_ollama_embedding_uses_installed_digest_and_fixed_local_api(self):
        client = FakeOllama()
        with patch('knowledge_embeddings.Ollama', return_value=client):
            provider = OllamaEmbeddingProvider('local-embed:1b')
            descriptor = provider.descriptor()
            vectors = provider.embed(['one source excerpt', 'one bounded search query'])
        self.assertEqual(descriptor, {'provider': 'ollama_loopback', 'model_id': 'local-embed:1b',
                                     'digest': 'sha256:fixture-model', 'modified_at': 'fixture-time'})
        self.assertEqual(client.calls[0][0], '/api/embed')
        self.assertEqual(client.calls[0][1]['model'], 'local-embed:1b')
        self.assertFalse(client.calls[0][1].get('stream', False))
        self.assertEqual(len(vectors), 2)

    def test_invalid_model_and_unbounded_embedding_batches_are_rejected(self):
        with self.assertRaisesRegex(AgentError, 'installed local'):
            OllamaEmbeddingProvider('cloud-embedding')
        client = FakeOllama()
        with patch('knowledge_embeddings.Ollama', return_value=client):
            provider = OllamaEmbeddingProvider('local-embed:1b')
            with self.assertRaisesRegex(AgentError, 'between one and eight'):
                provider.embed([])
            with self.assertRaisesRegex(AgentError, 'per-text byte limit'):
                provider.embed(['x' * 3501])
        self.assertEqual(client.calls, [])

    def test_embedding_does_not_follow_model_digest_changes_or_request_download(self):
        client = FakeOllama()
        with patch('knowledge_embeddings.Ollama', return_value=client):
            provider = OllamaEmbeddingProvider('local-embed:1b')
            client.model = lambda name: {'name': name, 'digest': 'sha256:replaced',
                                         'modified_at': 'new', 'details': {'format': 'gguf'}}
            with self.assertRaisesRegex(AgentError, 'identity changed'):
                provider.embed(['a bounded text'])
        self.assertEqual(client.calls, [])


if __name__ == '__main__':
    unittest.main()
