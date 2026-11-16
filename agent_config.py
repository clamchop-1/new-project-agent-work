"""Validated, host-local configuration for project roots and Ollama defaults."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from agent_core import AgentError

CONFIG_VERSION = 1
REMOTE_CONFIG_VERSION = 2
MANAGED_TUNNEL_CONFIG_VERSION = 3
FALLBACK_CONFIG_VERSION = 4
PROJECT_ID = re.compile(r'[a-z][a-z0-9_-]{2,63}\Z')
DEFAULT_MODEL = 'qwen3:8b'
DEFAULT_ENDPOINT = 'http://127.0.0.1:11434'


def default_projects(hub, host_platform=None):
    """Return compatibility defaults for Windows or the current checkout elsewhere."""
    hub = Path(hub).resolve()
    host_platform = host_platform or sys.platform
    if host_platform != 'win32':
        return {hub.name: str(hub)}

    repair = (hub / '.local/tests/live-repair-001/repo').resolve()
    return {
        'KronosStage': 'D:/Workspaces/KronosStage',
        'PhotoLibraryTools': 'D:/Workspaces/ChasingLight',
        'PhotoLibraryPlanning': 'D:/Workspaces/ChasingLight-Planning',
        'LocalAgentSandbox': 'D:/Workspaces/LocalAgentSandbox',
        'LocalAgentRepairFixture': str(repair),
    }

def config_path(hub):
    override = os.environ.get('LOCAL_CODING_AGENT_CONFIG')
    return Path(override).expanduser() if override else Path(hub) / '.local' / 'agent-config.json'


def validate_endpoint(endpoint):
    """Validate and canonicalize the loopback-only Ollama endpoint."""
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise AgentError('Configured endpoint must be a canonical local URL.')
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise AgentError('Configured endpoint URL is invalid.') from exc
    if (parsed.scheme != 'http' or parsed.netloc != '127.0.0.1:11434'
            or parsed.hostname != '127.0.0.1' or port != 11434
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise AgentError('Configured Ollama endpoint must be http://127.0.0.1:11434.')
    return DEFAULT_ENDPOINT


def validate_forwarded_endpoint(endpoint, port):
    """Validate a dedicated SSH-forwarded loopback endpoint.

    This is intentionally separate from validate_endpoint: an arbitrary local
    port is not trusted as a remote inference service.
    """
    if type(port) is not int or not 20000 <= port <= 29999:
        raise AgentError('SSH forwarded endpoint port must be in the reserved 20000-29999 range.')
    expected = f'http://127.0.0.1:{port}'
    if endpoint not in (expected, expected + '/'):
        raise AgentError('Remote Ollama endpoint must exactly match its configured SSH-forwarded loopback port.')
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.port != port or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('noncanonical forwarded URL')
    except ValueError as exc:
        raise AgentError('Remote Ollama forwarded endpoint URL is invalid.') from exc
    return expected


def _validate_fallback_policy(value):
    if not isinstance(value, dict) or set(value) != {'mode', 'substitutions'}:
        raise AgentError('Fallback policy must contain exactly mode and substitutions.')
    mode = value['mode']
    if mode not in ('disabled', 'ask', 'local_only', 'approved'):
        raise AgentError('Fallback mode is unsupported.')
    rows = value['substitutions']
    if not isinstance(rows, list) or len(rows) > 16:
        raise AgentError('Fallback substitutions must be a bounded list.')
    seen = set()
    normalized = []
    fields = {'from_provider', 'from_model', 'to_provider', 'to_model',
              'max_context', 'operator_approved', 'data_sharing_authorized'}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields:
            raise AgentError('Each fallback substitution has missing or unknown fields.')
        if row['from_provider'] not in ('ollama', 'ssh_tunnel_ollama') or row['to_provider'] not in ('ollama', 'ssh_tunnel_ollama'):
            raise AgentError('Fallback provider is unsupported.')
        source_model = row['from_model']
        if (not isinstance(source_model, str) or not source_model.strip()
                or len(source_model) > 160 or (source_model != '*' and 'cloud' in source_model.casefold())):
            raise AgentError('Fallback source must name a local model or the auto-routing wildcard.')
        target_model = row['to_model']
        if not isinstance(target_model, str) or not target_model.strip() or len(target_model) > 160 or 'cloud' in target_model.casefold():
            raise AgentError('Fallback target must name a local Ollama model.')
        if row['from_provider'] == row['to_provider']:
            raise AgentError('Provider fallback must switch between Windows and SSH-tunneled Ollama.')
        if type(row['max_context']) is not int or not 1 <= row['max_context'] <= 131072:
            raise AgentError('Fallback max_context is outside the supported bound.')
        if type(row['operator_approved']) is not bool or type(row['data_sharing_authorized']) is not bool:
            raise AgentError('Fallback authorization fields must be boolean.')
        if mode == 'local_only' and row['to_provider'] != 'ollama':
            raise AgentError('local_only substitutions may target Windows Ollama only.')
        if mode == 'ask' and (row['operator_approved'] or row['data_sharing_authorized']):
            raise AgentError('ask substitutions must await per-run operator approval.')
        if mode == 'approved' and not (row['operator_approved'] and row['data_sharing_authorized']):
            raise AgentError('approved substitutions require explicit operator and data-sharing authorization.')
        key = (row['from_provider'], row['from_model'], row['to_provider'], row['to_model'])
        if key in seen:
            raise AgentError('Duplicate fallback substitution.')
        seen.add(key)
        normalized.append(dict(row))
    if mode == 'disabled' and normalized:
        raise AgentError('disabled fallback mode cannot contain substitutions.')
    return {'mode': mode, 'substitutions': normalized}


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'version', 'projects', 'inference'}:
        raise AgentError('Agent config must contain exactly version, projects, and inference.')
    if type(value['version']) is not int or value['version'] not in (
            CONFIG_VERSION, REMOTE_CONFIG_VERSION, MANAGED_TUNNEL_CONFIG_VERSION,
            FALLBACK_CONFIG_VERSION):
        raise AgentError('Unsupported agent config version.')
    if not isinstance(value['projects'], dict) or not value['projects']:
        raise AgentError('Agent config requires at least one registered project.')
    projects = {}
    names = set()
    for project_id, record in value['projects'].items():
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise AgentError('Configured project IDs must be stable lowercase identifiers.')
        if not isinstance(record, dict) or set(record) != {'name', 'root'}:
            raise AgentError('Each project entry must contain exactly name and root.')
        name, root = record['name'], record['root']
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or name.strip() in names):
            raise AgentError('Configured project names must be unique and nonempty.')
        if not isinstance(root, str) or not root.strip() or '\x00' in root:
            raise AgentError('Configured project root is invalid.')
        names.add(name.strip())
        projects[project_id] = {'name': name.strip(), 'root': root}
    inference = value['inference']
    version = value['version']
    fallback = None
    if not isinstance(inference, dict):
        raise AgentError('Inference config must be an object.')
    if version == 1:
        if set(inference) != {'provider', 'model', 'endpoint'} or inference['provider'] != 'ollama':
            raise AgentError('Version 1 inference config supports only local Ollama.')
        provider, endpoint = 'ollama', validate_endpoint(inference['endpoint'])
    elif version in (REMOTE_CONFIG_VERSION, MANAGED_TUNNEL_CONFIG_VERSION, FALLBACK_CONFIG_VERSION):
        expected_keys = {'provider', 'model', 'endpoint', 'remote'}
        if version == FALLBACK_CONFIG_VERSION:
            expected_keys.add('fallback')
            fallback = _validate_fallback_policy(inference.get('fallback'))
        if set(inference) != expected_keys:
            raise AgentError('Remote inference config must contain provider, model, endpoint, and remote.')
        provider, endpoint = inference['provider'], inference['endpoint']
        remote = inference['remote']
        legacy_fields = {'enabled', 'profile_id', 'port', 'ssh_host_alias',
                         'ssh_host_key_sha256', 'credential_ref'}
        managed_fields = legacy_fields | {'known_hosts_file', 'ssh_config_file'}
        required = (legacy_fields if version == REMOTE_CONFIG_VERSION else managed_fields)
        if not isinstance(remote, dict) or set(remote) != required:
            raise AgentError('Remote inference profile has missing or unknown fields.')
        if type(remote['enabled']) is not bool:
            raise AgentError('Remote inference enabled must be boolean.')
        profile_id = remote['profile_id']
        if not isinstance(profile_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{2,63}', profile_id):
            raise AgentError('Remote profile ID is invalid.')
        alias = remote['ssh_host_alias']
        if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', alias):
            raise AgentError('SSH host alias must be a simple configured alias.')
        fingerprint = remote['ssh_host_key_sha256']
        if not isinstance(fingerprint, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint):
            raise AgentError('SSH host key identity must be a SHA-256 fingerprint.')
        if remote['credential_ref'] != 'ssh-agent':
            raise AgentError('Remote credentials must use the SSH agent; inline credentials are forbidden.')
        if type(remote['port']) is not int or not 20000 <= remote['port'] <= 29999:
            raise AgentError('SSH forwarded endpoint port must be in the reserved 20000-29999 range.')
        if version == MANAGED_TUNNEL_CONFIG_VERSION:
            for field in ('known_hosts_file', 'ssh_config_file'):
                file_path = remote[field]
                if (not isinstance(file_path, str) or not file_path.strip()
                        or '\x00' in file_path or not Path(file_path).is_absolute()):
                    raise AgentError(f'Remote profile {field} must be an absolute path.')
        if provider == 'ollama':
            endpoint = validate_endpoint(endpoint)
            remote_fallback_configured = bool(fallback and any(
                row['to_provider'] == 'ssh_tunnel_ollama'
                or row['from_provider'] == 'ssh_tunnel_ollama'
                for row in fallback['substitutions']))
            if remote['enabled'] and not (version == FALLBACK_CONFIG_VERSION and remote_fallback_configured):
                raise AgentError('Enable a remote profile only when selecting its SSH-tunnel provider.')
        elif provider == 'ssh_tunnel_ollama':
            if version not in (MANAGED_TUNNEL_CONFIG_VERSION, FALLBACK_CONFIG_VERSION):
                raise AgentError('Managed SSH tunnel configuration version 3 is required or newer for remote inference.')
            if remote['enabled'] is not True:
                raise AgentError('Remote inference is disabled; enable the profile explicitly before selecting it.')
            endpoint = validate_forwarded_endpoint(endpoint, remote['port'])
        else:
            raise AgentError('Configured inference provider is not authorized.')
    model = inference['model']
    if not isinstance(model, str) or not model.strip() or len(model) > 160 or 'cloud' in model.casefold():
        raise AgentError('Configured model must identify an installed local model.')
    result = {'version': version, 'projects': projects,
              'inference': {'provider': provider, 'model': model.strip(),
                            'endpoint': endpoint}}
    if version in (REMOTE_CONFIG_VERSION, MANAGED_TUNNEL_CONFIG_VERSION, FALLBACK_CONFIG_VERSION):
        result['inference']['remote'] = dict(inference['remote'])
    if version == FALLBACK_CONFIG_VERSION:
        for row in fallback['substitutions']:
            if row['from_provider'] == 'ssh_tunnel_ollama' or row['to_provider'] == 'ssh_tunnel_ollama':
                if inference['remote']['enabled'] is not True:
                    raise AgentError('Remote fallback substitutions require an explicitly enabled SSH profile.')
        result['inference']['fallback'] = fallback
    return result

def load_config(path, legacy_projects):
    """Load private host configuration; adapt established defaults if absent."""
    path = Path(path)
    if not path.exists():
        projects = {}
        for name, root in legacy_projects.items():
            project_id = re.sub(r'[^a-z0-9_-]+', '-', name.casefold()).strip('-')
            projects[project_id] = {'name': name, 'root': str(root)}
        return {'version': 1, 'projects': projects,
                'inference': {'provider': 'ollama', 'model': DEFAULT_MODEL,
                              'endpoint': DEFAULT_ENDPOINT}}
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError('Host-local agent config cannot be read: ' + str(exc)[:200]) from exc
    return validate_config(value)


def legacy_name_map(config):
    return {record['name']: record['root'] for record in config['projects'].values()}


def selector_map(config):
    """Accept stable IDs and display names, preserving historical CLI names."""
    selectors = {}
    for project_id, record in config['projects'].items():
        selectors[record['name']] = project_id
        selectors[project_id] = project_id
    return selectors


def logical_id_for_name(config, name):
    for project_id, record in config['projects'].items():
        if record['name'] == name:
            return project_id
    raise AgentError('Selected project has no stable configured identity.')
