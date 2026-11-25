"""Bounded live inference smoke for project-aware retrieval on authorized Ollama hosts."""
from __future__ import annotations

from datetime import datetime, timezone
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
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            timeout=20)
    if result.returncode:
        raise RuntimeError('Disposable fixture Git initialization failed.')
    return result.stdout


def main():
    run_id = uuid.uuid4().hex
    evidence_root = HUB / '.local' / 'tests' / ('project-live-' + run_id)
    app_root = evidence_root / 'app'
    repo = evidence_root / 'fixture-repo'
    app_root.mkdir(parents=True)
    repo.mkdir()
    for root in (app_root, repo):
        git(root, 'init', '-q')
        git(root, 'config', 'user.email', 'local-agent@example.invalid')
        git(root, 'config', 'user.name', 'LocalCodingAgent Fixture')
        (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        git(root, 'add', '.gitignore'); git(root, 'commit', '-qm', 'fixture baseline')
    (repo / 'pipeline.py').write_text(
        'def analyze_recording(samples):\n'
        '    """Process normalized PCM samples into audio features."""\n'
        '    return extract_audio_features(samples)\n', encoding='utf-8')
    (repo / 'README.md').write_text(
        '# Audio pipeline\nAfter amplitude normalization, the recording flow invokes analyze_recording.\n',
        encoding='utf-8')
    git(repo, 'add', 'pipeline.py', 'README.md'); git(repo, 'commit', '-qm', 'add recording analysis')

    config = load_config(config_path(HUB), PROJECTS)
    specs = provider_specs(config, DEFAULT_ENDPOINT, 'remote_allowed')
    host_ids = {'ollama': 'windows', 'ssh_tunnel_ollama':
        config.get('inference', {}).get('remote', {}).get('profile_id', 'mac_studio')}
    catalog = discover_model_catalog(specs, host_ids=host_ids)
    allowed = [row for row in catalog['models'] if row.get('authorized') is True
        and row.get('installed') is True and row.get('compatible') is True and row.get('available') is True
        and row.get('capabilities', {}).get('conversational') is not False]
    mac = next((row for row in allowed if row['provider_id'] == 'ssh_tunnel_ollama'
        and row['model_id'] == 'qwen3-coder:30b'), None)
    windows = next((row for preferred in ('qwen3:8b', 'qwen3:4b', 'deepseek-r1:8b')
        for row in allowed if row['provider_id'] == 'ollama' and row['model_id'] == preferred), None)
    if mac is None:
        raise RuntimeError('Authorized Mac Studio qwen3-coder:30b was not available for this live check.')
    selected = [mac] + ([windows] if windows else [])

    store = ConversationStore(app_root)
    service = ProjectIntelligence(Project(app_root))
    registration = service.register(str(repo), confirmed=True)
    project_id = registration['project_id']
    index_result = service.refresh(project_id)

    def runner(messages, model, cancel_event, on_token):
        if not isinstance(model, dict) or model.get('authorized') is not True:
            raise RuntimeError('Chat model selection failed authorization.')
        spec = specs.get(model.get('provider_id'))
        if spec is None:
            raise RuntimeError('Selected inference provider is outside the trusted host configuration.')
        with exclusive(HUB / '.local' / 'inference.lock', 'project-intelligence-live-smoke'):
            provider = create_inference_provider(model['provider_id'], spec['endpoint'], spec.get('profile'))
            try:
                provider.model(model['model_id'])
                provider.chat_stream(model['model_id'], messages, 8192, {'temperature': 0.1},
                    on_token, cancel_event=cancel_event, timeout=150)
            finally:
                provider.close()

    dashboard = CodingDashboard((), allowed_models=('qwen3-coder:30b', 'qwen3:8b'),
        chat_store=store, chat_runner=runner, chat_models=selected,
        chat_default_model=mac['model_key'], project_intelligence=service,
        provider_status={'providers': catalog['providers'], 'discovery_errors': catalog['errors'],
            'catalog_observed_at': catalog['observed_at']})
    server = dashboard.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = {'schema_version': 1, 'run_id': run_id,
        'observed_at': datetime.now(timezone.utc).isoformat(), 'project_index': index_result['index'],
        'models': [], 'browser_verified': False,
        'browser_blocker': 'Computer-use browser inventory reported browser access host unavailable.'}
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        for model in selected:
            row = store.create()
            store.set_project(row['id'], project_id)
            body = json.dumps({'message': 'Which function processes the normalized recording samples? '
                'Answer from the selected project and name its source file.',
                'model': model['model_key'], 'research_mode': 'local'}).encode('utf-8')
            request = urllib.request.Request(base + '/api/chat/conversations/' + row['id'] + '/messages',
                data=body, headers={'Authorization': 'Bearer ' + dashboard.token,
                    'Origin': base, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'},
                method='POST')
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=180) as response:
                events = [json.loads(line) for line in response.read().splitlines() if line]
            saved = store.load(row['id'])
            sources = [event for event in events if event.get('type') == 'project_sources']
            output['models'].append({'provider_id': model['provider_id'], 'host_id': model['host_id'],
                'model_id': model['model_id'], 'authorized': model['authorized'],
                'response_characters': len(saved['messages'][-1]['content']),
                'project_citations': sources[0]['sources'] if sources else [],
                'completed': any(event.get('type') == 'done' for event in events),
                'elapsed_seconds': round(time.monotonic() - started, 3)})
            if not sources or not any(item.get('path') == 'pipeline.py'
                                      and item.get('validated') for item in sources[0]['sources']):
                raise RuntimeError('Live project-aware answer did not produce a validated source citation.')
        output['status'] = 'passed'
    except Exception as exc:
        output['status'] = 'failed'
        output['error'] = type(exc).__name__ + ': ' + str(exc)[:240]
        raise
    finally:
        try:
            service.remove_registration(project_id)
        except Exception:
            pass
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        evidence_root.mkdir(parents=True, exist_ok=True)
        (evidence_root / 'result.json').write_text(json.dumps(output, ensure_ascii=False,
            indent=2) + '\n', encoding='utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
