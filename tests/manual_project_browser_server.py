"""Start an isolated authenticated dashboard for disposable project browser acceptance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import PROJECTS, provider_specs
from agent_config import DEFAULT_ENDPOINT, config_path, load_config
from agent_core import Project, exclusive
from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from knowledge_documents import DocumentKnowledgeLibrary
from local_agent import HUB, create_inference_provider
from model_catalog import discover_model_catalog
from project_intelligence import ProjectIntelligence


def git(root: Path, *args: str) -> str:
    result = None
    for attempt in range(2):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                               text=True, timeout=20)
        if result.returncode == 0:
            break
        if attempt == 0:
            time.sleep(0.2)
    if result is None or result.returncode:
        raise RuntimeError('Disposable browser fixture Git operation failed: '
                           + (result.stderr.strip()[:240] if result else 'no result'))
    return result.stdout


def create_repo(root: Path, title: str, source: str, readme: str) -> None:
    if (root / '.git').is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'browser-fixture@example.invalid')
    git(root, 'config', 'user.name', 'LocalCodingAgent Browser Fixture')
    (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
    (root / 'README.md').write_text(readme, encoding='utf-8')
    (root / 'pipeline.py').write_text(source, encoding='utf-8')
    (root / 'test_pipeline.py').write_text(
        'def test_fixture_pipeline_has_expected_entrypoint():\n'
        '    assert callable(__import__("pipeline").run_pipeline)\n', encoding='utf-8')
    git(root, 'add', '.gitignore', 'README.md', 'pipeline.py', 'test_pipeline.py')
    git(root, 'commit', '-qm', title + ' initial fixture')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', required=True, type=Path)
    args = parser.parse_args()
    state = args.state_root.resolve()
    app_root = state / 'app'
    app_root.mkdir(parents=True, exist_ok=True)
    if not (app_root / '.git').is_dir():
        git(app_root, 'init', '-q')
    git(app_root, 'config', 'user.email', 'browser-fixture@example.invalid')
    git(app_root, 'config', 'user.name', 'LocalCodingAgent Browser Fixture')
    if not (app_root / '.gitignore').is_file():
        (app_root / '.gitignore').write_text('.local/\n', encoding='utf-8')
    git(app_root, 'add', '.gitignore')
    has_head = subprocess.run(['git','-C',str(app_root),'rev-parse','--verify','HEAD'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20).returncode == 0
    if not has_head:
        git(app_root, 'commit', '-qm', 'isolated test app')
    alpha = state / 'fixture-alpha'
    beta = state / 'fixture-beta'
    create_repo(alpha, 'FixtureAlpha',
        'def normalize_capture(samples):\n'
        '    """Scale PCM samples before audio feature analysis."""\n'
        '    return [sample / 100.0 for sample in samples]\n'
        '\n'
        'def run_pipeline(samples):\n'
        '    """Normalize the recording, then analyze its waveform."""\n'
        '    return analyze_waveform(normalize_capture(samples))\n',
        '# Aurora Audio Toolkit\n\n'
        'This package normalizes microphone recordings and analyzes waveform features.\n')
    create_repo(beta, 'FixtureBeta',
        'def run_pipeline(paths):\n'
        '    """Index image files and calculate content hashes."""\n'
        '    return [hash_photo(path) for path in paths]\n'
        '\n'
        'def classify_collection(hashes):\n'
        '    """Classify collections using BLUEBIRD_PRIVATE_TAG prototypes."""\n'
        '    return "BLUEBIRD_PRIVATE_TAG"\n',
        '# Bluebird Photo Index\n\n'
        'This package indexes photo files and classifies collections by image hashes.\n')

    config = load_config(config_path(HUB), PROJECTS)
    specs = provider_specs(config, DEFAULT_ENDPOINT, 'remote_allowed')
    host_ids = {'ollama': 'windows', 'ssh_tunnel_ollama':
        config.get('inference', {}).get('remote', {}).get('profile_id', 'mac_studio')}
    catalog = discover_model_catalog(specs, host_ids=host_ids)
    authorized = [row for row in catalog['models'] if row.get('authorized') is True
        and row.get('installed') is True and row.get('compatible') is True
        and row.get('available') is True
        and row.get('capabilities', {}).get('conversational') is not False]
    mac = next((row for row in authorized if row['provider_id'] == 'ssh_tunnel_ollama'
                and row['model_id'] == 'qwen3-coder:30b'), None)
    windows = next((row for row in authorized if row['provider_id'] == 'ollama'
                    and row['model_id'] == 'qwen3:8b'), None)
    if not mac or not windows:
        raise RuntimeError('Both required authorized browser acceptance models must be available.')
    store = ConversationStore(app_root)
    knowledge = DocumentKnowledgeLibrary(Project(app_root))
    projects = ProjectIntelligence(Project(app_root))
    original_register=projects.register
    original_git=Project.git
    def traced_git(project, *git_args):
        started=time.monotonic()
        try:
            return original_git(project,*git_args)
        except Exception as exc:
            with (state / 'project-registration-events.jsonl').open('a',encoding='utf-8') as log:
                log.write(json.dumps({'event':'git_failed','args':list(git_args),
                    'elapsed_seconds':round(time.monotonic()-started,3),
                    'type':type(exc).__name__})+'\n')
            raise
    Project.git=traced_git
    def traced_register(root, *, confirmed=False):
        log_path=state / 'project-registration-events.jsonl'
        with log_path.open('a',encoding='utf-8') as log:
            log.write(json.dumps({'event':'started','at':time.time()})+'\n')
        try:
            outcome=original_register(root,confirmed=confirmed)
        except Exception as exc:
            with log_path.open('a',encoding='utf-8') as log:
                log.write(json.dumps({'event':'failed','type':type(exc).__name__})+'\n')
            raise
        with log_path.open('a',encoding='utf-8') as log:
            log.write(json.dumps({'event':'completed','at':time.time()})+'\n')
        return outcome
    projects.register=traced_register

    def runner(messages, model, cancel_event, on_token):
        if not isinstance(model, dict) or model.get('authorized') is not True:
            raise RuntimeError('Selected browser model is not authorized.')
        spec = specs.get(model.get('provider_id'))
        if not spec:
            raise RuntimeError('Selected browser model provider is not configured.')
        try:
            lock_path=HUB / '.local' / 'inference.lock'
            deadline=time.monotonic()+45
            while lock_path.exists() and time.monotonic()<deadline and not cancel_event.is_set():
                time.sleep(0.2)
            if lock_path.exists():
                raise RuntimeError('The existing inference lock remained busy for this bounded test request.')
            with exclusive(lock_path, 'project-browser-acceptance'):
                provider = create_inference_provider(model['provider_id'], spec['endpoint'], spec.get('profile'))
                try:
                    provider.model(model['model_id'])
                    provider.chat_stream(model['model_id'], messages, 8192,
                        {'temperature': 0.1}, on_token, cancel_event=cancel_event, timeout=150)
                finally:
                    provider.close()
        except Exception as exc:
            message = re.sub(r'(?i)(authorization|token|password|api[_-]?key)(\s*[:=]\s*)\S+',
                              r'\1\2<redacted>', str(exc))[:300]
            with (state / 'inference-diagnostics.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'host_id': model.get('host_id'),
                    'model_id': model.get('model_id'), 'exception': type(exc).__name__,
                    'message': message}) + '\n')
            raise

    selected = [mac, windows]
    dashboard = CodingDashboard((), allowed_models=('qwen3-coder:30b', 'qwen3:8b'),
        chat_store=store, chat_runner=runner, chat_models=selected,
        chat_default_model=mac['model_key'], knowledge_store=knowledge,
        project_intelligence=projects,
        provider_status={'providers': catalog['providers'], 'discovery_errors': catalog['errors'],
                         'catalog_observed_at': catalog['observed_at']})
    server = dashboard.start()
    print(json.dumps({'ready': True, 'url': f'http://127.0.0.1:{server.server_port}/',
        'token': dashboard.token, 'state_root': str(state), 'alpha': str(alpha), 'beta': str(beta),
        'models': [{'provider_id': row['provider_id'], 'host_id': row['host_id'],
                    'model_id': row['model_id'], 'model_key': row['model_key']}
                   for row in selected]}, ensure_ascii=False), flush=True)
    def stop_on_stdin():
        try:
            sys.stdin.readline()
            server.shutdown()
        except (OSError, RuntimeError):
            pass
    threading.Thread(target=stop_on_stdin, name='test-dashboard-stop', daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        knowledge.close()


if __name__ == '__main__':
    main()
