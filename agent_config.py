"""Validated, host-local configuration for project roots and Ollama defaults."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from agent_core import AgentError

CONFIG_VERSION = 1
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


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {'version', 'projects', 'inference'}:
        raise AgentError('Agent config must contain exactly version, projects, and inference.')
    if type(value['version']) is not int or value['version'] != CONFIG_VERSION:
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
    if not isinstance(inference, dict) or set(inference) != {'provider', 'model', 'endpoint'}:
        raise AgentError('Inference config must contain exactly provider, model, and endpoint.')
    if inference['provider'] != 'ollama':
        raise AgentError('Only Ollama is supported by this configuration version.')
    model = inference['model']
    if not isinstance(model, str) or not model.strip() or len(model) > 160 or 'cloud' in model.casefold():
        raise AgentError('Configured model must identify an installed local model.')
    endpoint = validate_endpoint(inference['endpoint'])
    return {'version': CONFIG_VERSION, 'projects': projects,
            'inference': {'provider': 'ollama', 'model': model.strip(),
                          'endpoint': endpoint}}

def load_config(path, legacy_projects):
    """Load private host configuration; adapt established defaults if absent."""
    path = Path(path)
    if not path.exists():
        projects = {}
        for name, root in legacy_projects.items():
            project_id = re.sub(r'[^a-z0-9_-]+', '-', name.casefold()).strip('-')
            projects[project_id] = {'name': name, 'root': str(root)}
        return {'version': CONFIG_VERSION, 'projects': projects,
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
