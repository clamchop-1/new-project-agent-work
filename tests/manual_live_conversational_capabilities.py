"""Bounded live API acceptance for conversational read-only project actions.

Uses only already-authorized Ollama providers and a disposable repository. It
does not modify production projects, install packages, or run project scripts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import PROJECTS, provider_specs
from agent_config import DEFAULT_ENDPOINT, config_path, load_config
from agent_core import Project, exclusive
from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from local_agent import HUB, create_inference_provider
from model_catalog import discover_model_catalog
from project_intelligence import ProjectIntelligence


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Disposable fixture Git operation failed.')
    return result.stdout.strip()


def send(base, token, conversation_id, model_key, text):
    payload = json.dumps({'message': text, 'model': model_key,
                          'research_mode': 'local'}).encode('utf-8')
    request = urllib.request.Request(base + f'/api/chat/conversations/{conversation_id}/messages',
        data=payload, headers={'Authorization': 'Bearer ' + token, 'Origin': base,
            'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=190) as response:
        return [json.loads(line) for line in response.read().splitlines() if line]


def main():
    run_id = uuid.uuid4().hex
    evidence = HUB / '.local' / 'tests' / ('conversational-capabilities-live-' + run_id)
    app_root, repository = evidence / 'app', evidence / 'assistant-action-fixture'
    app_root.mkdir(parents=True); repository.mkdir()
    for root in (app_root, repository):
        git(root, 'init', '-q')
        git(root, 'config', 'user.email', 'local-agent@example.invalid')
        git(root, 'config', 'user.name', 'LocalCodingAgent Fixture')
        (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        git(root, 'add', '.gitignore'); git(root, 'commit', '-qm', 'fixture baseline')
    source = ('def analyze_recording(samples):\n'
              '    """Return the normalized recording feature envelope."""\n'
              '    return calculate_feature_envelope(samples)\n')
    (repository / 'pipeline.py').write_text(source, encoding='utf-8')
    (repository / 'README.md').write_text(
        '# Disposable audio fixture\nThe project calculates a feature envelope.\n', encoding='utf-8')
    git(repository, 'add', 'pipeline.py', 'README.md')
    git(repository, 'commit', '-qm', 'add audio pipeline')
    expected_hash = hashlib.sha256((repository / 'pipeline.py').read_bytes()).hexdigest()

    config = load_config(config_path(HUB), PROJECTS)
    specs = provider_specs(config, DEFAULT_ENDPOINT, 'remote_allowed')
    remote_id = config.get('inference', {}).get('remote', {}).get('profile_id', 'mac_studio')
    catalog = discover_model_catalog(specs, host_ids={'ollama': 'windows',
        'ssh_tunnel_ollama': remote_id})
    available = [row for row in catalog.get('models', []) if row.get('authorized') is True
        and row.get('available') is True and row.get('installed') is True
        and row.get('compatible') is True and row.get('capabilities', {}).get('conversational') is not False]
    selected = []
    for host, provider, model_id in (('mac', 'ssh_tunnel_ollama', 'qwen3-coder:30b'),
                                     ('windows', 'ollama', 'qwen3:8b')):
        row = next((item for item in available if item.get('provider_id') == provider
                    and item.get('model_id') == model_id), None)
        if row:
            selected.append(row)

    store = ConversationStore(app_root)
    projects = ProjectIntelligence(Project(app_root))
    registered = projects.register(str(repository), confirmed=True)
    projects.refresh(registered['project_id'])

    def runner(messages, model, cancel, on_token):
        spec = specs.get(model.get('provider_id')) if isinstance(model, dict) else None
        if model.get('authorized') is not True or spec is None:
            raise RuntimeError('Live test model is outside the authorized catalog.')
        with exclusive(HUB / '.local' / 'inference.lock', 'conversational-capability-live'):
            provider = create_inference_provider(model['provider_id'], spec['endpoint'], spec.get('profile'))
            try:
                provider.model(model['model_id'])
                provider.chat_stream(model['model_id'], messages, 8192,
                    {'temperature': 0.1}, on_token, cancel_event=cancel, timeout=150)
            finally:
                provider.close()

    dashboard = CodingDashboard((), allowed_models=('qwen3-coder:30b', 'qwen3:8b'),
        chat_store=store, chat_runner=runner, chat_models=selected,
        chat_default_model=selected[0]['model_key'] if selected else None,
        project_intelligence=projects,
        provider_status={'providers': catalog.get('providers', []),
            'discovery_errors': catalog.get('errors', []),
            'catalog_observed_at': catalog.get('observed_at')})
    server = dashboard.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    output = {'schema_version': 1, 'run_id': run_id,
        'observed_at': datetime.now(timezone.utc).isoformat(), 'browser_verified': False,
        'browser_blocker': 'Browser access host unavailable; Edge accessibility state unavailable.',
        'models': [], 'fixture_source_sha256': expected_hash,
        'registered_project_id': registered['project_id'], 'catalog_errors': catalog.get('errors', [])}
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        for model in selected:
            item = {'provider_id': model['provider_id'], 'host_id': model['host_id'],
                    'model_id': model['model_id'], 'authorized': model['authorized']}
            try:
                conversation = store.create()
                start = time.monotonic()
                opened = send(base, dashboard.token, conversation['id'], model['model_key'],
                              'Open project assistant-action-fixture')
                item['natural_project_selection'] = {
                    'completed': any(event.get('type') == 'done' for event in opened),
                    'bound': store.load(conversation['id']).get('project_id') == registered['project_id'],
                    'elapsed_seconds': round(time.monotonic() - start, 3)}
                start = time.monotonic()
                events = send(base, dashboard.token, conversation['id'], model['model_key'],
                              'Read file pipeline.py')
                saved = store.load(conversation['id'])
                citations = [source for event in events if event.get('type') == 'project_sources'
                             for source in event.get('sources', [])]
                citation = next((source for source in citations if source.get('path') == 'pipeline.py'), None)
                item['natural_file_read'] = {
                    'completed': any(event.get('type') == 'done' for event in events),
                    'action_progress': any(event.get('type') == 'status'
                        and 'Reading an authorized project file' in event.get('text', '') for event in events),
                    'citation': citation,
                    'citation_matches_fixture_hash': bool(citation and
                        citation.get('document_sha256') == expected_hash and citation.get('validated') is True),
                    'response_characters': len(saved['messages'][-1]['content']),
                    'elapsed_seconds': round(time.monotonic() - start, 3)}
                item['status'] = ('passed' if item['natural_project_selection']['bound']
                    and item['natural_file_read']['completed']
                    and item['natural_file_read']['citation_matches_fixture_hash'] else 'failed')
            except Exception as exc:
                item['status'] = 'failed'
                item['error'] = type(exc).__name__ + ': ' + str(exc)[:220]
            output['models'].append(item)
        output['status'] = ('passed' if selected and all(item.get('status') == 'passed'
                                                         for item in output['models']) else 'partial_or_failed')
    finally:
        try:
            projects.remove_registration(registered['project_id'])
        except Exception:
            pass
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / 'result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n',
                                             encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
