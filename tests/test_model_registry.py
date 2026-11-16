import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_registry import (ModelRegistryError, append_performance_evidence,
                            empty_registry, load_performance_evidence,
                            validate_registry)


def evidence_value(value, source='verified_measurement'):
    refs = ([{'kind': 'test_receipt', 'uri': '.local/runs/sample/result.json',
              'sha256': 'a' * 64,
              'observed_at': datetime.now(timezone.utc).isoformat()}
             ] if source == 'verified_measurement' else [])
    return {'value': value, 'source': source, 'evidence_refs': refs}


def profile(model='model-a', provider='ollama', digest='sha256:a'):
    return {
        'provider_id': provider, 'model_id': model, 'model_digest': digest,
        'capabilities': {
            'code_generation': evidence_value(0.8),
            'test_generation': evidence_value(0.75),
        },
        'tool_calling': evidence_value(True),
        'context_window': evidence_value(32768),
        'estimated_memory_gib': evidence_value(8, 'unverified_estimate'),
        'known_limitations': ['Small sample only.'],
        'benchmark_evidence': [],
    }


def performance(model='model-a', outcome='success', timestamp=None, provider='ollama'):
    return {
        'schema_version': 1, 'provider_id': provider, 'model_id': model,
        'model_digest': 'sha256:a', 'task_category': 'code_generation',
        'test_or_benchmark_id': 'unit-test-sample', 'latency_ms': 1200,
        'tokens_per_second': 21.5, 'context_tokens': 512,
        'outcome': outcome, 'verification': 'test_receipt',
        'timestamp': timestamp or datetime.now(timezone.utc).isoformat(),
        'evidence_ref': {'kind': 'test_receipt', 'uri': '.local/runs/sample/result.json',
                         'sha256': 'b' * 64,
                         'observed_at': datetime.now(timezone.utc).isoformat()},
    }


class ModelRegistryTests(unittest.TestCase):
    def test_registry_is_versioned_and_rejects_unverified_claims_as_verified(self):
        self.assertEqual(validate_registry(empty_registry())['schema_version'], 1)
        bad_version = {'schema_version': 2, 'models': []}
        with self.assertRaisesRegex(ModelRegistryError, 'version'):
            validate_registry(bad_version)
        invalid = profile()
        invalid['capabilities']['code_generation'] = evidence_value(0.9, 'verified_measurement')
        invalid['capabilities']['code_generation']['evidence_refs'] = []
        with self.assertRaisesRegex(ModelRegistryError, 'require evidence'):
            validate_registry({'schema_version': 1, 'models': [invalid]})

    def test_registry_rejects_model_reported_capability_source(self):
        invalid = profile()
        invalid['capabilities']['code_generation']['source'] = 'model_claim'
        with self.assertRaisesRegex(ModelRegistryError, 'source is unsupported'):
            validate_registry({'schema_version': 1, 'models': [invalid]})

    def test_evidence_reference_rejects_credentials_and_query_parameters(self):
        invalid = profile()
        reference = {'kind': 'benchmark', 'uri': 'https://user:pass@example.invalid/report?token=x',
                     'sha256': 'c' * 64,
                     'observed_at': datetime.now(timezone.utc).isoformat()}
        invalid['benchmark_evidence'] = [reference]
        with self.assertRaisesRegex(ModelRegistryError, 'credential-free HTTPS'):
            validate_registry({'schema_version': 1, 'models': [invalid]})

    def test_performance_evidence_requires_independent_receipt_and_ignores_stale_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'performance.jsonl'
            append_performance_evidence(path, performance())
            stale = performance(model='old-model', timestamp=(
                datetime.now(timezone.utc) - timedelta(days=120)).isoformat())
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(stale) + '\n')
            current = load_performance_evidence(path)
            self.assertEqual([row['model_id'] for row in current], ['model-a'])
            unverified = performance()
            unverified['verification'] = 'model_self_report'
            with self.assertRaisesRegex(ModelRegistryError, 'independent verification'):
                append_performance_evidence(path, unverified)


if __name__ == '__main__':
    unittest.main()
