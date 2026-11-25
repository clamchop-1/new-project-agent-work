"""Kill an isolated dashboard after its first real multi-file Broker write.

This Windows-only harness distinguishes a real process termination from the
deterministic exception-based recovery tests. It uses a disposable repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manual_broker_dashboard_hard_kill import (
    READY, _fixture, _read_lines, _remove_fixture,
)
PATCH_WRITE = 'PATCH_FIRST_WRITE '


def _worker(application):
    from agent_core import Project
    from chat_conversations import ConversationStore
    from coding_dashboard import CodingDashboard
    from local_agent import Broker
    from project_intelligence import ProjectIntelligence

    app = Project(application)
    chat = ConversationStore(application)
    projects = ProjectIntelligence(app)
    dashboard = CodingDashboard((), allowed_models=('qwen3:8b',), chat_store=chat,
        chat_runner=lambda *_args: None, project_intelligence=projects)
    original_write = Broker.write

    def gated_write(broker, path, *args, **kwargs):
        receipt = original_write(broker, path, *args, **kwargs)
        if path == 'calculator.py':
            print(PATCH_WRITE + json.dumps({'path': path,
                'after': receipt.get('after'), 'pid': os.getpid()}), flush=True)
            threading.Event().wait()
        return receipt

    Broker.write = gated_write
    server = dashboard.start(port=0)
    print(READY + json.dumps({'pid': os.getpid(), 'token': dashboard.token,
        'port': server.server_port}), flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


def _request(base, token, conversation, operation):
    from urllib.request import Request, urlopen
    data = json.dumps({'decision': 'approve'}).encode()
    request = Request(base + f'/api/chat/conversations/{conversation}/operations/{operation}/decision',
        data=data, method='POST', headers={'Authorization': 'Bearer ' + token,
            'Origin': base, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _run(output):
    if os.name != 'nt':
        raise RuntimeError('Actual dashboard termination acceptance requires Windows.')
    from agent_core import AgentError, Project
    from chat_conversations import ConversationStore
    from conversational_capabilities import CapabilityDispatcher
    from conversational_execution import ConversationalExecution
    from project_intelligence import ProjectIntelligence

    output.mkdir(parents=True, exist_ok=False)
    app_root, repo = _fixture(output)
    child = None
    try:
        app = Project(app_root)
        chat = ConversationStore(app_root)
        projects = ProjectIntelligence(app)
        registered = projects.register(str(repo), confirmed=True)
        conversation = chat.create()['id']
        chat.set_project(conversation, registered['project_id'])
        service = ConversationalExecution(app, projects, chat)
        service.audit_dispatcher = CapabilityDispatcher(projects, chat)
        project = projects._record(registered['project_id'])[1]
        before = {name: project.path(name).read_text(encoding='utf-8')
                  for name in ('calculator.py', 'stats.py')}
        after = {
            'calculator.py': ('from stats import validate_values\n\n'
                'def average(values):\n    validate_values(values)\n'
                '    return sum(values) / len(values)\n'),
            'stats.py': ('def validate_values(values):\n'
                '    if not values:\n        raise ValueError("empty")\n'
                '    for value in values:\n'
                '        if not isinstance(value, (int, float)):\n'
                '            raise ValueError("numeric values required")\n\n'
                + before['stats.py']),
        }

        def runner(messages, _model, _cancel, on_token):
            request = json.loads(messages[-1]['content'])
            source = {item['path']: item for item in request['source_context']}
            result = {'files': [{'path': name, 'base_sha256': source[name]['sha256'],
                                'content': content} for name, content in after.items()]}
            on_token(json.dumps(result))

        proposal = service.propose_patch(conversation, registered['project_id'],
            ['calculator.py', 'stats.py'],
            'Update calculator.py and stats.py with shared numeric input validation.',
            {'provider_id': 'ollama', 'model_id': 'qwen3:8b'}, runner)
        if proposal.get('kind') != 'multi_patch':
            raise RuntimeError('The existing conversational workflow did not issue a multi-file proposal.')

        worker_python = getattr(sys, '_base_executable', sys.executable)
        child = subprocess.Popen([worker_python, '-u', '-B', str(Path(__file__).resolve()),
            '--worker', '--application', str(app_root)], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
        lines = queue.Queue()
        threading.Thread(target=_read_lines, args=(child, lines), daemon=True).start()
        ready = None
        logs = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ready is None:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                if child.poll() is not None:
                    break
                continue
            if line and line.startswith(READY):
                ready = json.loads(line[len(READY):])
            elif line:
                logs.append(line[-400:])
        if ready is None or ready['pid'] != child.pid:
            raise RuntimeError('Isolated dashboard process did not start: ' + repr(logs[-5:]))

        decision_error = []
        def approve():
            try:
                _request(f"http://127.0.0.1:{ready['port']}", ready['token'], conversation,
                         proposal['operation_id'])
            except Exception as exc:
                decision_error.append(type(exc).__name__ + ': ' + str(exc)[:160])
        requester = threading.Thread(target=approve, daemon=True)
        requester.start()
        write_event = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and write_event is None:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                if child.poll() is not None:
                    break
                continue
            if line and line.startswith(PATCH_WRITE):
                write_event = json.loads(line[len(PATCH_WRITE):])
            elif line:
                logs.append(line[-400:])
        if write_event is None or write_event['path'] != 'calculator.py':
            raise RuntimeError('Dashboard did not reach its first real Broker write: ' + repr(logs[-5:]))
        child.kill()
        child.wait(timeout=10)
        requester.join(timeout=5)

        restarted = ConversationalExecution(app, projects, chat)
        restarted.audit_dispatcher = service.audit_dispatcher
        state = restarted.inspect(conversation, proposal['operation_id'])
        classes = {row['path']: row['classification']
                   for row in state.get('recovery', {}).get('file_classifications', [])}
        current = {name: hashlib.sha256(project.path(name).read_bytes()).hexdigest()
                   for name in before}
        if (state.get('state') != 'interrupted'
                or classes.get('calculator.py') != 'applied'
                or classes.get('stats.py') != 'unchanged'
                or state.get('result', {}).get('receipt_verified') is not False):
            raise RuntimeError('Restart did not conservatively classify the actual partial patch: '
                               + json.dumps(state, default=str)[:1200])
        try:
            restarted.propose_tests(conversation, registered['project_id'], 'auto')
        except AgentError:
            tests_blocked = True
        else:
            tests_blocked = False
        retry_rejected = False
        try:
            restarted.decide(conversation, proposal['operation_id'], 'approve')
        except AgentError:
            retry_rejected = True
        if not tests_blocked or not retry_rejected:
            raise RuntimeError('Partial patch recovery allowed dependent tests or approval replay.')

        from execution_recovery import RecoveryStore
        recovery_store = RecoveryStore(project)
        journal_source = recovery_store.path(proposal['workflow_id'])
        evidence_source = Path(state['recovery']['evidence_directory'])
        journal_copy = output / 'broker-recovery-journal.json'
        shutil.copyfile(journal_source, journal_copy)
        evidence_copy = output / 'broker-recovery-evidence'
        shutil.copytree(evidence_source, evidence_copy)
        journal_hash = hashlib.sha256(journal_copy.read_bytes()).hexdigest()
        evidence_files = {item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                          for item in evidence_copy.iterdir() if item.is_file()}

        evidence = {'format': 'localcodingagent-multi-patch-dashboard-hard-kill-v1',
            'verified': True, 'dashboard_pid': child.pid, 'operation_id': proposal['operation_id'],
            'dashboard_terminated_after_actual_broker_write': write_event,
            'dashboard_exit_code': child.returncode, 'decision_request_error': decision_error,
            'restarted_state': state['state'], 'file_classifications': classes,
            'actual_file_sha256': current, 'tests_blocked': tests_blocked,
            'approval_replay_rejected': retry_rejected,
            'recovery_evidence': str(evidence_copy), 'recovery_journal': str(journal_copy),
            'recovery_journal_sha256': journal_hash,
            'recovery_evidence_sha256': evidence_files,
            'application': str(app_root), 'repository': str(repo)}
        (output / 'result.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))
    finally:
        if child is not None and child.poll() is None:
            child.kill(); child.wait(timeout=10)
        _remove_fixture(repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.local' / 'tests' /
                        ('increment4-multi-patch-hard-kill-' + uuid.uuid4().hex[:12]))
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--application')
    args = parser.parse_args()
    if args.worker:
        _worker(args.application)
    else:
        _run(args.output.resolve())


if __name__ == '__main__':
    main()
