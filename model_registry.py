"""Versioned capability profiles and locally stored verified model evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


REGISTRY_VERSION = 1
EVIDENCE_VERSION = 1
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024
EVIDENCE_MAX_RECORDS = 10000
PROFILE_SOURCES = frozenset({'verified_measurement', 'operator_provided', 'unverified_estimate'})
CAPABILITY_NAMES = frozenset({
    'code_generation', 'debugging', 'code_review', 'architecture_planning',
    'test_generation', 'qa_analysis', 'documentation', 'summarization',
    'general_reasoning', 'research_extraction', 'structured_output',
})
_WRITE_LOCK = threading.Lock()


class ModelRegistryError(ValueError):
    """Invalid registry or performance evidence."""


def _valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(
        char in '0123456789abcdef' for char in value
    )


def validate_evidence_reference(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'uri', 'sha256', 'observed_at'}:
        raise ModelRegistryError('Evidence reference must contain kind, uri, sha256, and observed_at.')
    if (not isinstance(value['kind'], str) or not value['kind'].strip()
            or not isinstance(value['uri'], str) or not value['uri'].strip()
            or len(value['uri']) > 1000 or '\x00' in value['uri']
            or not _valid_hash(value['sha256'])):
        raise ModelRegistryError('Evidence reference fields are invalid.')
    if '://' in value['uri']:
        try:
            parsed = urlsplit(value['uri'])
        except ValueError as exc:
            raise ModelRegistryError('Evidence reference URI is invalid.') from exc
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ModelRegistryError('Evidence URLs must use credential-free HTTPS without query or fragment.')
    try:
        observed = datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise ModelRegistryError('Evidence reference timestamp is invalid.') from exc
    if observed.tzinfo is None or observed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ModelRegistryError('Evidence reference timestamp must be timezone-aware and current.')
    return dict(value)


def _validate_profile_value(value, label, kind):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'value', 'source', 'evidence_refs'}:
        raise ModelRegistryError(f'{label} must contain value, source, and evidence_refs.')
    if not isinstance(value['source'], str) or value['source'] not in PROFILE_SOURCES:
        raise ModelRegistryError(f'{label} source is unsupported.')
    refs = value['evidence_refs']
    if not isinstance(refs, list) or len(refs) > 32:
        raise ModelRegistryError(f'{label} evidence references must be a bounded list.')
    refs = [validate_evidence_reference(ref) for ref in refs]
    if value['source'] == 'verified_measurement' and not refs:
        raise ModelRegistryError(f'{label} verified measurements require evidence references.')
    measured = value['value']
    if kind == 'score' and (type(measured) not in (int, float) or not 0 <= measured <= 1):
        raise ModelRegistryError(f'{label} score must be between zero and one.')
    if kind == 'boolean' and type(measured) is not bool:
        raise ModelRegistryError(f'{label} value must be boolean.')
    if kind == 'context' and (type(measured) is not int or not 1 <= measured <= 131072):
        raise ModelRegistryError(f'{label} context window is outside supported bounds.')
    if kind == 'memory' and (type(measured) not in (int, float) or not 0 < measured <= 1024):
        raise ModelRegistryError(f'{label} memory estimate is outside supported bounds.')
    return {'value': measured, 'source': value['source'], 'evidence_refs': refs}


def validate_registry(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'models'}:
        raise ModelRegistryError('Model registry must contain schema_version and models.')
    if type(value['schema_version']) is not int or value['schema_version'] != REGISTRY_VERSION:
        raise ModelRegistryError('Unsupported model registry version.')
    if not isinstance(value['models'], list) or len(value['models']) > 512:
        raise ModelRegistryError('Model registry models must be a bounded list.')
    models = []
    seen = set()
    fields = {'provider_id', 'model_id', 'model_digest', 'capabilities',
              'tool_calling', 'context_window', 'estimated_memory_gib',
              'known_limitations', 'benchmark_evidence'}
    for original in value['models']:
        if not isinstance(original, dict) or set(original) != fields:
            raise ModelRegistryError('Model profile has missing or unknown fields.')
        row = dict(original)
        if (row['provider_id'] not in ('ollama', 'ssh_tunnel_ollama')
                or not isinstance(row['model_id'], str) or not row['model_id'].strip()
                or len(row['model_id']) > 160 or 'cloud' in row['model_id'].casefold()):
            raise ModelRegistryError('Model profile identity is unsupported.')
        digest = row['model_digest']
        if digest is not None and (not isinstance(digest, str) or len(digest) > 256
                                   or '\x00' in digest):
            raise ModelRegistryError('Model digest is invalid.')
        key = (row['provider_id'], row['model_id'], digest)
        if key in seen:
            raise ModelRegistryError('Duplicate provider/model/digest profile.')
        seen.add(key)
        if not isinstance(row['capabilities'], dict) or row['capabilities'].keys() - CAPABILITY_NAMES:
            raise ModelRegistryError('Model profile contains an unknown capability.')
        row['capabilities'] = {
            name: _validate_profile_value(value, name, 'score')
            for name, value in row['capabilities'].items()
        }
        row['tool_calling'] = _validate_profile_value(row['tool_calling'], 'tool_calling', 'boolean')
        row['context_window'] = _validate_profile_value(row['context_window'], 'context_window', 'context')
        row['estimated_memory_gib'] = _validate_profile_value(
            row['estimated_memory_gib'], 'estimated_memory_gib', 'memory'
        )
        limitations = row['known_limitations']
        if (not isinstance(limitations, list) or len(limitations) > 64
                or any(not isinstance(item, str) or len(item) > 500 for item in limitations)):
            raise ModelRegistryError('Known model limitations must be a bounded string list.')
        benchmarks = row['benchmark_evidence']
        if not isinstance(benchmarks, list) or len(benchmarks) > 128:
            raise ModelRegistryError('Benchmark evidence references must be a bounded list.')
        row['benchmark_evidence'] = [validate_evidence_reference(ref) for ref in benchmarks]
        models.append(row)
    return {'schema_version': REGISTRY_VERSION, 'models': models}


def empty_registry():
    return {'schema_version': REGISTRY_VERSION, 'models': []}


def load_registry(path):
    """Load operator-maintained profiles; absence means no capability claims."""
    path = Path(path)
    if not path.exists():
        return empty_registry()
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ModelRegistryError('Model registry is unsafe or exceeds the configured limit.')
    try:
        return validate_registry(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelRegistryError('Model registry could not be read as valid JSON.') from exc


def validate_performance_record(record):
    fields = {'schema_version', 'provider_id', 'model_id', 'model_digest',
              'task_category', 'test_or_benchmark_id', 'latency_ms',
              'tokens_per_second', 'context_tokens', 'outcome', 'verification',
              'timestamp', 'evidence_ref'}
    if not isinstance(record, dict) or set(record) != fields:
        raise ModelRegistryError('Performance evidence record has missing or unknown fields.')
    if type(record['schema_version']) is not int or record['schema_version'] != EVIDENCE_VERSION:
        raise ModelRegistryError('Unsupported performance evidence version.')
    if record['provider_id'] not in ('ollama', 'ssh_tunnel_ollama'):
        raise ModelRegistryError('Performance evidence provider is unsupported.')
    for name in ('model_id', 'task_category', 'test_or_benchmark_id'):
        if not isinstance(record[name], str) or not record[name].strip() or len(record[name]) > 200:
            raise ModelRegistryError(f'Performance evidence {name} is invalid.')
    if record['model_digest'] is not None and not isinstance(record['model_digest'], str):
        raise ModelRegistryError('Performance evidence model digest is invalid.')
    if type(record['latency_ms']) not in (int, float) or not 0 <= record['latency_ms'] <= 86400000:
        raise ModelRegistryError('Performance latency is outside supported bounds.')
    throughput = record['tokens_per_second']
    if throughput is not None and (type(throughput) not in (int, float) or not 0 < throughput <= 100000):
        raise ModelRegistryError('Performance throughput is outside supported bounds.')
    if type(record['context_tokens']) is not int or not 0 <= record['context_tokens'] <= 131072:
        raise ModelRegistryError('Performance context usage is outside supported bounds.')
    if record['outcome'] not in ('success', 'failure'):
        raise ModelRegistryError('Performance outcome is unsupported.')
    if record['verification'] not in ('test_receipt', 'operator_verified'):
        raise ModelRegistryError('Performance outcomes require independent verification.')
    validate_evidence_reference(record['evidence_ref'])
    try:
        stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
    except (ValueError, AttributeError) as exc:
        raise ModelRegistryError('Performance evidence timestamp is invalid.') from exc
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ModelRegistryError('Performance evidence timestamp must be timezone-aware and current.')
    return dict(record)


def load_performance_evidence(path, *, now=None, max_age_days=90):
    path = Path(path)
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > EVIDENCE_MAX_BYTES:
        raise ModelRegistryError('Performance evidence store is unsafe or exceeds its limit.')
    now = now or datetime.now(timezone.utc)
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines()[-EVIDENCE_MAX_RECORDS:]:
        try:
            row = validate_performance_record(json.loads(line))
            age = now - datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            if age <= timedelta(days=max_age_days):
                rows.append(row)
        except (json.JSONDecodeError, ModelRegistryError):
            continue
    return rows


def append_performance_evidence(path, record):
    row = validate_performance_record(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        if path.exists() and (path.is_symlink() or path.stat().st_size > EVIDENCE_MAX_BYTES):
            raise ModelRegistryError('Performance evidence store is unsafe or exceeds its limit.')
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush()
    return row
