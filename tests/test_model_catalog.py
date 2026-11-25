import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inference_provider import ProviderFailure
from model_catalog import (ModelCatalogError, discover_model_catalog, load_catalog,
                           save_catalog)


class FakeProvider:
    def __init__(self, rows=None, failure=None):
        self.rows = list(rows or [])
        self.failure = failure
        self.closed = False

    def health_check(self):
        if self.failure:
            raise self.failure
        return {'healthy': True, 'version': '0.1'}

    def models(self):
        return self.rows

    def close(self):
        self.closed = True


def ollama_row(name, digest, *, model_format='gguf', family='qwen3'):
    return {'name': name, 'digest': digest, 'details': {
        'format': model_format, 'family': family, 'parameter_size': '8.2B',
        'quantization_level': 'Q4_K_M', 'context_length': 40960,
        'embedding_length': 4096}}


class ModelCatalogTests(unittest.TestCase):
    def test_duplicate_model_names_on_different_hosts_have_distinct_identity(self):
        providers = [FakeProvider([ollama_row('qwen3:8b', 'abc')]),
                     FakeProvider([ollama_row('qwen3:8b', 'abc')])]
        with patch('model_catalog.create_inference_provider', side_effect=providers):
            catalog = discover_model_catalog({
                'ollama': {'endpoint': 'http://127.0.0.1:11434'},
                'ssh_tunnel_ollama': {'endpoint': 'http://127.0.0.1:22001', 'profile': {}},
            }, host_ids={'ollama': 'windows', 'ssh_tunnel_ollama': 'mac_studio'})
        self.assertEqual(len(catalog['models']), 2)
        self.assertNotEqual(catalog['models'][0]['model_key'], catalog['models'][1]['model_key'])
        self.assertEqual({row['model_id'] for row in catalog['models']}, {'qwen3:8b'})
        self.assertTrue(all(provider.closed for provider in providers))

    def test_metadata_is_recorded_without_inventing_capabilities(self):
        provider = FakeProvider([ollama_row('qwen3:8b', 'abc')])
        with patch('model_catalog.create_inference_provider', return_value=provider):
            catalog = discover_model_catalog({'ollama': {'endpoint': 'http://127.0.0.1:11434'}})
        model = catalog['models'][0]
        self.assertEqual(model['metadata']['context_window'], 40960)
        self.assertEqual(model['metadata']['quantization'], 'Q4_K_M')
        self.assertEqual(model['capabilities'], {})
        self.assertEqual(model['capabilities_evidence'], 'unknown')
        self.assertTrue(model['authorized'])
        self.assertTrue(model['available'])

    def test_unsupported_format_is_discovered_but_not_compatible_or_invokable(self):
        provider = FakeProvider([ollama_row('unknown:latest', 'abc', model_format='unknown')])
        with patch('model_catalog.create_inference_provider', return_value=provider):
            catalog = discover_model_catalog({'ollama': {'endpoint': 'http://127.0.0.1:11434'}})
        model = catalog['models'][0]
        self.assertTrue(model['discovered'])
        self.assertFalse(model['compatible'])

    def test_security_failures_are_not_hidden_as_provider_unavailability(self):
        provider = FakeProvider(failure=ProviderFailure('identity_mismatch', 'unsafe', security=True))
        with patch('model_catalog.create_inference_provider', return_value=provider):
            with self.assertRaises(ProviderFailure):
                discover_model_catalog({'ssh_tunnel_ollama': {
                    'endpoint': 'http://127.0.0.1:22001', 'profile': {}}})
        self.assertTrue(provider.closed)

    def test_catalog_persists_atomically_and_roundtrips(self):
        provider = FakeProvider([ollama_row('qwen3:8b', 'abc')])
        with patch('model_catalog.create_inference_provider', return_value=provider):
            catalog = discover_model_catalog({'ollama': {'endpoint': 'http://127.0.0.1:11434'}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            save_catalog(path, catalog)
            self.assertEqual(load_catalog(path), json.loads(path.read_text(encoding='utf-8')))
            path.write_text('{broken', encoding='utf-8')
            with self.assertRaises(ModelCatalogError):
                load_catalog(path)


if __name__ == '__main__':
    unittest.main()
