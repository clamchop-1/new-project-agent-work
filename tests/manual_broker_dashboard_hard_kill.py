"""Prove dashboard death with a live Broker test child using a disposable repo.

Run with the project venv from the repository root. This Windows-only harness
uses the real chat routes and Broker. It suspends only the newly created pinned
test child long enough to terminate its direct Python dashboard owner, then
terminates the child and exercises operator-guided recovery. It never targets a
user project or an already-running dashboard.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
READY = 'HARNESS_READY '
CHILD = 'BROKER_CHILD '


def _windows_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL('ntdll')
    ntdll.NtSuspendProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtSuspendProcess.restype = wintypes.LONG
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = wintypes.LONG
    return kernel, ntdll


def _process_handle(pid, access):
    kernel, _ = _windows_api()
    handle = kernel.OpenProcess(access, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'OpenProcess failed for owned harness PID')
    return kernel, handle


def _suspend(pid):
    kernel, ntdll = _windows_api()
    handle = kernel.OpenProcess(0x0800 | 0x1000, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Could not open the new Broker child for suspension')
    try:
        status = ntdll.NtSuspendProcess(handle)
        if status != 0:
            raise OSError('NtSuspendProcess returned NTSTATUS ' + hex(status & 0xffffffff))
    finally:
        kernel.CloseHandle(handle)


def _resume(pid):
    kernel, ntdll = _windows_api()
    handle = kernel.OpenProcess(0x0800 | 0x1000, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Could not reopen the owned Broker child')
    try:
        status = ntdll.NtResumeProcess(handle)
        if status != 0:
            raise OSError('NtResumeProcess returned NTSTATUS ' + hex(status & 0xffffffff))
    finally:
        kernel.CloseHandle(handle)


def _terminate_owned(pid, timeout_ms=10000):
    kernel, handle = _process_handle(pid, 0x0001 | 0x00100000 | 0x1000)
    try:
        kernel.TerminateProcess(handle, 97)
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise TimeoutError('Owned Broker test child did not stop after termination.')
    finally:
        kernel.CloseHandle(handle)


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Git fixture setup failed: ' + result.stderr[-500:])
    return result.stdout.strip()


def _remove_fixture(path):
    if path is None or not Path(path).exists():
        return
    target = Path(path).resolve(strict=True)
    allowed = (ROOT / 'tests').resolve(strict=True)
    if (not target.is_relative_to(allowed)
            or not target.name.startswith('_increment2-hard-kill-')
            or (target / '.git').is_symlink()):
        raise RuntimeError('Refusing to remove a path outside this harness-owned fixture.')
    last_error = None
    for attempt in range(4):
        errors = []
        def retry_readonly(function, name, exc_info):
            try:
                os.chmod(name, stat.S_IWRITE)
                function(name)
            except OSError as exc:
                errors.append((name, str(exc)))
        shutil.rmtree(target, onerror=retry_readonly)
        if not target.exists():
            return
        last_error = errors[-1] if errors else 'directory remains after cleanup attempt'
        time.sleep(.1 * (attempt + 1))
    raise RuntimeError('Could not remove owned disposable fixture: ' + str(last_error))


def _fixture(base):
    from agent_core import Project, atomic_write, encoded
    from coding_workflow import TEST_PROFILE_CONFIGS

    app = base / 'application'
    # Registration intentionally rejects any target whose path contains
    # ``.local``. Keep the disposable Git worktree in the workspace's ignored
    # temporary test area and keep application evidence under .local.
    repo = ROOT / 'tests' / ('_increment2-hard-kill-' + base.name)
    app.mkdir(parents=True)
    repo.mkdir(parents=True, exist_ok=False)
    for root in (app, repo):
        (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
        _git(root, 'init', '-q')
        _git(root, 'config', 'user.email', 'hardkill-fixture@example.invalid')
        _git(root, 'config', 'user.name', 'Hard Kill Fixture')
    (repo / 'tests').mkdir()
    (repo / 'calculator.py').write_text(
        'def average(values):\n    if not values:\n        raise ValueError("empty")\n'
        '    return sum(values) / len(values)\n', encoding='utf-8')
    (repo / 'stats.py').write_text(
        'def clamp(value, low, high):\n'
        '    if value < low:\n        return low\n'
        '    if value > high:\n        return high\n'
        '    return value\n', encoding='utf-8')
    source = ROOT / 'tests' / 'fixtures' / 'stats_multi_module' / 'test_stats.py'
    expected = TEST_PROFILE_CONFIGS['stats_multi_module_v1']['test_sha256']
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Pinned acceptance fixture source does not match the trusted profile.')
    shutil.copyfile(source, repo / 'tests' / 'test_stats.py')
    (repo / '.local').mkdir()
    atomic_write(repo / '.local' / 'pinned-tests.json', encoded(
        TEST_PROFILE_CONFIGS['stats_multi_module_v1']))
    for root in (app, repo):
        _git(root, 'add', '.')
        _git(root, 'commit', '-qm', 'isolated hard-kill acceptance fixture')
    return app, repo


def _worker(args):
    from agent_core import Project
    from chat_conversations import ConversationStore
    from coding_dashboard import CodingDashboard
    from project_intelligence import ProjectIntelligence

    app_root = Path(args.application).resolve()
    chat = ConversationStore(app_root)
    projects = ProjectIntelligence(Project(app_root))
    dashboard = CodingDashboard((), allowed_models=('qwen3:8b',), chat_store=chat,
        chat_runner=lambda _messages, _model, _cancel, on_token: on_token(
            'Disposable acceptance harness response; no model inference was invoked.'),
        project_intelligence=projects)
    server = dashboard.start(port=args.port)
    conversation = chat.create()['id']
    print(READY + json.dumps({'pid': os.getpid(), 'token': dashboard.token,
        'conversation_id': conversation, 'port': server.server_port}), flush=True)

    import subprocess as subprocess_module
    original_popen = subprocess_module.Popen

    def gated_popen(*popen_args, **popen_kwargs):
        child = original_popen(*popen_args, **popen_kwargs)
        command = popen_args[0] if popen_args else popen_kwargs.get('args')
        parts = command if isinstance(command, (tuple, list)) else [command]
        if any('pure_tests.py' in str(part) for part in parts):
            _suspend(child.pid)
            print(CHILD + json.dumps({'pid': child.pid, 'owner_pid': os.getpid()}), flush=True)
            # The controller terminates this direct dashboard process. Holding
            # here guarantees the Broker-created child is still live at that point.
            threading.Event().wait()
        return child

    subprocess_module.Popen = gated_popen
    threading.Thread(target=server.serve_forever, name='isolated-dashboard', daemon=True).start()
    threading.Event().wait()


def _read_lines(process, q):
    for line in process.stdout:
        q.put(line.rstrip('\r\n'))
    q.put(None)


def _request(base, token, route, *, method='GET', body=None, timeout=30):
    headers = {'Authorization': 'Bearer ' + token}
    data = None
    if method == 'POST':
        headers.update({'Content-Type': 'application/json', 'Origin': base})
        data = json.dumps(body or {}).encode()
    request = urllib.request.Request(base + route, headers=headers, data=data, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _message(base, token, conversation_id, message):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    body = json.dumps({'message': message, 'model': 'qwen3:8b'}).encode()
    request = Request(base + f'/api/chat/conversations/{conversation_id}/messages', data=body,
        method='POST', headers={'Authorization': 'Bearer ' + token,
            'Origin': base, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=30) as response:
            events = [json.loads(line) for line in response.read().splitlines() if line]
    except HTTPError as exc:
        detail = exc.read(512).decode('utf-8', errors='replace')
        raise RuntimeError('Authenticated chat request failed: ' + str(exc.code) + ' ' + detail) from exc
    error = next((event for event in events if event.get('type') == 'error'), None)
    if error is not None:
        raise RuntimeError('Chat request failed: ' + repr(error)[:500])
    return events


def _proposal(events):
    rows = [event['proposal'] for event in events if event.get('type') == 'operation_proposal']
    if len(rows) != 1:
        raise RuntimeError('Expected one trusted operation proposal, got ' + str(len(rows)))
    return rows[0]


def _decision(base, token, conversation_id, proposal, decision='approve'):
    return _request(base, token,
        f"/api/chat/conversations/{conversation_id}/operations/{proposal['operation_id']}/decision",
        method='POST', body={'decision': decision}, timeout=120)


def _main(args):
    if os.name != 'nt':
        raise RuntimeError('The real process termination acceptance is Windows-only.')
    from agent_core import AgentError, Project
    from chat_conversations import ConversationStore
    from conversational_capabilities import CapabilityDispatcher
    from conversational_execution import ConversationalExecution
    from execution_recovery import RecoveryStore
    from project_intelligence import ProjectIntelligence

    base_dir = args.output.resolve()
    base_dir.mkdir(parents=True, exist_ok=False)
    app, repo = _fixture(base_dir)
    port = args.port
    # Windows virtualenv python.exe is a redirector. Launch its base binary so
    # Popen.pid identifies the actual dashboard owner that will be terminated.
    worker_python = getattr(sys, '_base_executable', sys.executable)
    proc = subprocess.Popen([worker_python, '-u', '-B', str(Path(__file__).resolve()),
        '--worker', '--application', str(app), '--port', str(port),
        ], cwd=ROOT, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
    lines = queue.Queue()
    threading.Thread(target=_read_lines, args=(proc, lines), daemon=True).start()
    ready = child = None
    logs = []
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ready is None:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            if line.startswith(READY):
                ready = json.loads(line[len(READY):])
            else:
                logs.append(line[-500:])
        if ready is None:
            raise RuntimeError('Isolated dashboard did not start: ' + repr(logs[-8:]))
        if ready['pid'] != proc.pid:
            raise RuntimeError(f"Direct process identity mismatch: Popen={proc.pid}, worker={ready['pid']}")
        conv = ready['conversation_id']
        base = f"http://127.0.0.1:{ready['port']}"
        registration = _proposal(_message(base, ready['token'], conv,
            'Register the repository at ' + str(repo)))
        if registration.get('kind') != 'register':
            raise RuntimeError('Registration request produced the wrong operation kind.')
        approved_registration = _decision(base, ready['token'], conv, registration)
        if approved_registration.get('status') != 'completed':
            raise RuntimeError('Disposable repository registration failed: ' + repr(approved_registration))
        selected = _message(base, ready['token'], conv, 'Select project ' + repo.name)
        conversation_state = _request(base, ready['token'], f'/api/chat/conversations/{conv}')
        if conversation_state.get('project_id') != registration.get('project_id'):
            raise RuntimeError('Natural-language project selection was not persisted.')
        proposal = _proposal(_message(base, ready['token'], conv,
            'Run the approved tests for this project'))
        if proposal.get('kind') != 'tests' or proposal.get('host') != 'Windows Broker':
            raise RuntimeError('Pinned test request did not propose Windows Broker execution.')

        decision_result, decision_error = [], []
        def approve_tests():
            try:
                decision_result.append(_decision(base, ready['token'], conv, proposal))
            except Exception as exc:
                decision_error.append(type(exc).__name__ + ': ' + str(exc)[:180])
        requester = threading.Thread(target=approve_tests, daemon=True)
        requester.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and child is None:
            try:
                line = lines.get(timeout=.25)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            if line.startswith(CHILD):
                child = json.loads(line[len(CHILD):])
            else:
                logs.append(line[-500:])
        if child is None:
            raise RuntimeError('Broker did not create the expected real pinned-test child: ' + repr(logs[-8:]))
        if child['owner_pid'] != proc.pid:
            raise RuntimeError('Broker child owner does not match direct dashboard process.')
        kernel, child_handle = _process_handle(child['pid'], 0x00100000 | 0x1000 | 0x0001)
        if kernel.WaitForSingleObject(child_handle, 0) != 258:
            kernel.CloseHandle(child_handle)
            raise RuntimeError('Broker child was not alive when dashboard termination began.')
        proc.kill()
        proc.wait(timeout=10)
        if kernel.WaitForSingleObject(child_handle, 0) != 258:
            kernel.CloseHandle(child_handle)
            raise RuntimeError('Broker child did not survive dashboard process termination.')
        _resume(child['pid'])
        # The test child is now unowned by the dead dashboard and cannot create
        # a trusted receipt; terminate only this exact harness-created child.
        _terminate_owned(child['pid'])
        kernel.CloseHandle(child_handle)
        requester.join(timeout=5)
        if requester.is_alive():
            decision_error.append('approval HTTP request remained open after dashboard death')

        operation_path = app / '.local' / 'chat' / 'conversations' / 'operations' / (proposal['operation_id'] + '.json')
        operation = json.loads(operation_path.read_bytes())
        if operation.get('owner_pid') != proc.pid or operation.get('state') not in {
                'executing', 'validating', 'interrupted'}:
            raise RuntimeError('Operation state did not retain the killed dashboard owner and in-flight status.')
        recovery_store = RecoveryStore(Project(repo))
        report = recovery_store.inspect(proposal['operation_id'])
        if report['pending_outcome'] != 'command_liveness_unknown' or report['safe_to_reconcile']:
            raise RuntimeError('Unrecorded child PID was not conservatively detected: ' + repr(report))
        confirmed = recovery_store.reconcile(proposal['operation_id'], report['review_sha256'],
                                               'confirm-command-stopped')
        report = recovery_store.inspect(proposal['operation_id'])
        if (confirmed.get('status') != 'command_stop_confirmed_for_review'
                or report['pending_outcome'] != 'unverified_command'
                or not report['safe_to_reconcile']
                or not {'accept', 'abort'} <= set(report['options'])):
            raise RuntimeError('Operator-guided stopped-command reconciliation did not unlock safely.')
        aborted = recovery_store.reconcile(proposal['operation_id'], report['review_sha256'], 'abort')
        restarted_chat = ConversationStore(app)
        restarted_projects = ProjectIntelligence(Project(app))
        restarted_execution = ConversationalExecution(app, restarted_projects, restarted_chat)
        restarted_execution.audit_dispatcher = CapabilityDispatcher(restarted_projects, restarted_chat)
        chat_recovery = restarted_execution.inspect(conv, proposal['operation_id'])
        if (chat_recovery.get('state') != 'interrupted'
                or chat_recovery.get('result', {}).get('reconciliation_required') is not True
                or chat_recovery.get('result', {}).get('receipt_verified') is not False
                or chat_recovery.get('recovery', {}).get('recovery_state') != 'aborted'):
            raise RuntimeError('Restarted chat did not preserve the unverified interrupted outcome.')
        try:
            restarted_execution.decide(conv, proposal['operation_id'], 'approve')
        except AgentError:
            retry_rejected = True
        else:
            raise RuntimeError('Consumed test approval was accepted again after recovery.')
        evidence = {'format': 'localcodingagent-live-dashboard-hard-kill-v1',
            'verified': True, 'dashboard_pid_matches_direct_child': proc.pid == ready['pid'],
            'owner_pid': proc.pid, 'broker_test_child_pid': child['pid'],
            'broker_child_alive_at_dashboard_termination': True,
            'dashboard_process_exit_code': proc.returncode,
            'post_kill_request_error': decision_error,
            'registration_approved': approved_registration.get('status') == 'completed',
            'project_selected_in_conversation': True,
            'selection_response_events': [event.get('type') for event in selected],
            'test_operation_id': proposal['operation_id'],
            'pending_outcome_before_operator_review': 'command_liveness_unknown',
            'manual_stop_confirmation': confirmed, 'recovery_options_after_confirmation': report['options'],
            'operator_reconciliation': aborted,
            'restarted_chat_state': chat_recovery['state'],
            'restarted_chat_receipt_verified': chat_recovery['result']['receipt_verified'],
            'reapproval_rejected': retry_rejected,
            'broker_recovery_journal': str(recovery_store.path(proposal['operation_id'])),
            'operation_record': str(operation_path), 'fixture_application': str(app),
            'fixture_repository': str(repo)}
        (base_dir / 'result.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        _remove_fixture(repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.local' / 'tests' /
                        ('increment2-hard-kill-' + uuid.uuid4().hex[:12]))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--application')
    args = parser.parse_args()
    if args.worker:
        _worker(args)
    else:
        _main(args)


if __name__ == '__main__':
    main()
