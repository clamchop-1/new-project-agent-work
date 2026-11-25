"""Persistent, provider-neutral inventory of explicitly authorized model endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from inference_provider import ProviderFailure
from local_agent import create_inference_provider


CATALOG_VERSION = 1
MAX_PROVIDERS = 8
MAX_MODELS = 512
MAX_MODEL_LIST_BYTES = 2 * 1024 * 1024
SUPPORTED_OLLAMA_FORMATS = frozenset({'gguf', 'safetensors'})


class ModelCatalogError(ValueError):
    """Invalid or unsafe persistent model inventory."""


def _safe_text(value, maximum=256):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _model_key(provider_id, host_id, model_id, digest):
    identity = json.dumps([provider_id, 'ollama', host_id, model_id, digest],
                          ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _catalog_row(provider_id, host_id, row):
    if not isinstance(row, dict) or not _safe_text(row.get('name'), 160):
        return None
    model_id = row['name']
    if 'cloud' in model_id.casefold() or row.get('remote_host') or row.get('remote_model'):
        return None
    digest = row.get('digest')
    if digest is not None and not _safe_text(digest, 256):
        return None
    details = row.get('details', {})
    if not isinstance(details, dict):
        return None
    model_format = details.get('format')
    if model_format not in SUPPORTED_OLLAMA_FORMATS:
        compatible = False
    else:
        compatible = True
    context = details.get('context_length')
    if type(context) is not int or not 1 <= context <= 1048576:
        context = None
    dimensions = details.get('embedding_length')
    if type(dimensions) is not int or not 1 <= dimensions <= 65536:
        dimensions = None
    metadata = {
        'architecture': details.get('family') if _safe_text(details.get('family'), 100) else None,
        'parameter_count': details.get('parameter_size') if _safe_text(details.get('parameter_size'), 40) else None,
        'quantization': details.get('quantization_level') if _safe_text(details.get('quantization_level'), 40) else None,
        'format': model_format if _safe_text(model_format, 40) else None,
        'context_window': context,
        'embedding_dimensions': dimensions,
    }
    return {
        'model_key': _model_key(provider_id, host_id, model_id, digest),
        'provider_id': provider_id,
        'runtime': 'ollama',
        'host_id': host_id,
        'model_id': model_id,
        'model_digest': digest,
        'metadata': metadata,
        'capabilities': {},
        'capabilities_evidence': 'unknown',
        'discovered': True,
        'installed': True,
        'compatible': compatible,
        'authorized': True,
        'available': True,
        'loaded': None,
        'tested': False,
        'recommended': False,
    }


def discover_model_catalog(provider_specs, *, host_ids=None, now=None):
    """List only providers supplied by trusted host configuration; never downloads or loads models."""
    if (not isinstance(provider_specs, dict) or not provider_specs
            or len(provider_specs) > MAX_PROVIDERS):
        raise ModelCatalogError('Authorized provider specifications must be a bounded nonempty mapping.')
    host_ids = dict(host_ids or {})
    models, providers, errors = [], [], []
    for provider_id, spec in provider_specs.items():
        host_id = host_ids.get(provider_id, provider_id)
        if provider_id not in ('ollama', 'ssh_tunnel_ollama') or not _safe_text(host_id, 80):
            raise ModelCatalogError('Provider identity is unsupported.')
        if not isinstance(spec, dict) or set(spec) - {'endpoint', 'profile'}:
            raise ModelCatalogError('Provider specification contains unsupported fields.')
        provider = (create_inference_provider(provider_id, spec['endpoint'], spec.get('profile'))
                    if 'profile' in spec else create_inference_provider(provider_id, spec['endpoint']))
        try:
            health = provider.health_check()
            rows = provider.models()
            providers.append({'provider_id': provider_id, 'runtime': 'ollama', 'host_id': host_id,
                              'available': True, 'version': health.get('version')
                              if isinstance(health, dict) and _safe_text(health.get('version'), 64) else None})
            if not isinstance(rows, (list, tuple)) or len(rows) > MAX_MODELS:
                raise ModelCatalogError('Provider returned an invalid or oversized model inventory.')
            for row in rows:
                normalized = _catalog_row(provider_id, host_id, row)
                if normalized is not None:
                    models.append(normalized)
        except ProviderFailure as exc:
            if exc.security or exc.code == 'authentication':
                raise
            errors.append({'provider_id': provider_id, 'error_code': exc.code,
                           'retryable': exc.retryable})
            providers.append({'provider_id': provider_id, 'runtime': 'ollama', 'host_id': host_id,
                              'available': False, 'version': None})
        finally:
            provider.close()
    if len(models) > MAX_MODELS:
        raise ModelCatalogError('Discovered model inventory exceeds its configured limit.')
    keys = [row['model_key'] for row in models]
    if len(keys) != len(set(keys)):
        raise ModelCatalogError('Provider returned duplicate model identities.')
    return {'schema_version': CATALOG_VERSION,
            'observed_at': (now or datetime.now(timezone.utc)).isoformat(),
            'providers': providers, 'models': models, 'errors': errors}


def save_catalog(path, catalog):
    _validate_catalog(catalog)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ModelCatalogError('Model catalog path must not follow symbolic links.')
    payload = json.dumps(catalog, ensure_ascii=True, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_MODEL_LIST_BYTES:
        raise ModelCatalogError('Model catalog exceeds its storage limit.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                prefix=path.name + '.new-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return catalog


def load_catalog(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_MODEL_LIST_BYTES:
        raise ModelCatalogError('Model catalog is unsafe or exceeds its storage limit.')
    try:
        catalog = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelCatalogError('Model catalog could not be read as JSON.') from exc
    return _validate_catalog(catalog)


def _validate_catalog(catalog):
    fields = {'schema_version', 'observed_at', 'providers', 'models', 'errors'}
    model_fields = {'model_key', 'provider_id', 'runtime', 'host_id', 'model_id',
                    'model_digest', 'metadata', 'capabilities', 'capabilities_evidence',
                    'discovered', 'installed', 'compatible', 'authorized', 'available',
                    'loaded', 'tested', 'recommended'}
    metadata_fields = {'architecture', 'parameter_count', 'quantization', 'format',
                       'context_window', 'embedding_dimensions'}
    if (not isinstance(catalog, dict) or set(catalog) != fields
            or type(catalog['schema_version']) is not int
            or catalog['schema_version'] != CATALOG_VERSION
            or not _safe_text(catalog['observed_at'], 64)
            or not isinstance(catalog['providers'], list) or len(catalog['providers']) > MAX_PROVIDERS
            or not isinstance(catalog['models'], list) or len(catalog['models']) > MAX_MODELS
            or not isinstance(catalog['errors'], list) or len(catalog['errors']) > MAX_PROVIDERS):
        raise ModelCatalogError('Model catalog structure is invalid.')
    try:
        observed = datetime.fromisoformat(catalog['observed_at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise ModelCatalogError('Model catalog timestamp is invalid.') from exc
    if observed.tzinfo is None:
        raise ModelCatalogError('Model catalog timestamp must include a timezone.')
    for provider in catalog['providers']:
        if (not isinstance(provider, dict)
                or set(provider) != {'provider_id', 'runtime', 'host_id', 'available', 'version'}
                or provider.get('provider_id') not in ('ollama', 'ssh_tunnel_ollama')
                or provider.get('runtime') != 'ollama'
                or not _safe_text(provider.get('host_id'), 80)
                or type(provider.get('available')) is not bool
                or (provider.get('version') is not None and not _safe_text(provider['version'], 64))):
            raise ModelCatalogError('Model catalog provider entry is invalid.')
    keys = set()
    for model in catalog['models']:
        if (not isinstance(model, dict) or set(model) != model_fields
                or model.get('provider_id') not in ('ollama', 'ssh_tunnel_ollama')
                or model.get('runtime') != 'ollama'
                or not _safe_text(model.get('host_id'), 80)
                or not _safe_text(model.get('model_id'), 160)
                or 'cloud' in model.get('model_id', '').casefold()
                or (model.get('model_digest') is not None
                    and not _safe_text(model['model_digest'], 256))
                or not isinstance(model.get('model_key'), str)
                or len(model['model_key']) != 64
                or any(char not in '0123456789abcdef' for char in model['model_key'])
                or model['model_key'] in keys
                or not isinstance(model.get('metadata'), dict)
                or set(model['metadata']) != metadata_fields
                or model.get('capabilities') != {}
                or model.get('capabilities_evidence') != 'unknown'
                or any(type(model.get(name)) is not bool for name in
                       ('discovered', 'installed', 'compatible', 'authorized',
                        'available', 'tested', 'recommended'))
                or (model.get('loaded') is not None and type(model.get('loaded')) is not bool)):
            raise ModelCatalogError('Model catalog model entry is invalid.')
        if model['model_key'] != _model_key(model['provider_id'], model['host_id'],
                                            model['model_id'], model['model_digest']):
            raise ModelCatalogError('Model catalog identity key does not match its provider/model identity.')
        for name in ('architecture', 'parameter_count', 'quantization', 'format'):
            if model['metadata'][name] is not None and not _safe_text(model['metadata'][name], 256):
                raise ModelCatalogError('Model catalog metadata contains invalid text.')
        for name, maximum in (('context_window', 1048576), ('embedding_dimensions', 65536)):
            value = model['metadata'][name]
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise ModelCatalogError('Model catalog metadata contains an invalid numeric value.')
        if model['compatible'] and model['metadata']['format'] not in SUPPORTED_OLLAMA_FORMATS:
            raise ModelCatalogError('Model catalog marks an unsupported runtime format compatible.')
        keys.add(model['model_key'])
    for error in catalog['errors']:
        if (not isinstance(error, dict)
                or set(error) != {'provider_id', 'error_code', 'retryable'}
                or error.get('provider_id') not in ('ollama', 'ssh_tunnel_ollama')
                or not _safe_text(error.get('error_code'), 80)
                or type(error.get('retryable')) is not bool):
            raise ModelCatalogError('Model catalog provider error entry is invalid.')
    return catalog
