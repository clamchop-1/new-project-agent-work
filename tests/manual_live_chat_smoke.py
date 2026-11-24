"""Manually exercise real Mac chat, persistence, and cancellation.

Run from the repository root with the verified virtual environment. The script
captures the one-run token only in memory and never writes it to its evidence.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / '.local' / 'tests' / ('phasea-chat-live-' + uuid.uuid4().hex[:12])


def read_startup(process, output_queue):
    for line in process.stdout:
        output_queue.put(line.rstrip('\r\n'))
    output_queue.put(None)


def launch(port):
    process = subprocess.Popen([
        sys.executable, '-u', '-B', str(ROOT / 'agent.py'), 'local-chat',
        '--provider', 'ssh_tunnel_ollama', '--dashboard-port', str(port),
        '--no-browser-open',
    ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
       text=True, encoding='utf-8', errors='replace', bufsize=1,
       creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    lines = queue.Queue()
    threading.Thread(target=read_startup, args=(process, lines), daemon=True).start()
    output, token = [], None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and token is None:
        try:
            line = lines.get(timeout=.5)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        if line.startswith('One-run dashboard bearer token: '):
            token = line.split(': ', 1)[1]
        else:
            output.append(line)
    if token is None:
        stop(process)
        raise RuntimeError('Local chat did not start. Startup output: ' + ' | '.join(output[-8:]))
    base = f'http://127.0.0.1:{port}'
    return process, base, token, output


def stop(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def request(base, token, path, *, method='GET', body=None, stream=False):
    headers = {'Authorization': 'Bearer ' + token}
    raw = None
    if method == 'POST':
        headers['Content-Type'] = 'application/json'
        headers['Origin'] = base
        raw = json.dumps(body or {}).encode('utf-8')
    req = urllib.request.Request(base + path, data=raw, headers=headers, method=method)
    response = urllib.request.urlopen(req, timeout=240)
    if stream:
        return response
    with response:
        return json.loads(response.read())


def send_message(base, token, conversation_id, model, text):
    started = time.monotonic()
    response = request(base, token, f'/api/chat/conversations/{conversation_id}/messages',
        method='POST', body={'message': text, 'model': model}, stream=True)
    events, chunks = [], []
    with response:
        for line in response:
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event['type'])
            if event['type'] == 'token':
                chunks.append(event.get('text', ''))
            if event['type'] == 'error':
                raise RuntimeError(event.get('message', 'Chat failed.'))
    return {'prompt': text, 'response': ''.join(chunks), 'events': events,
            'elapsed_seconds': round(time.monotonic() - started, 3)}


def cancel_message(base, token, conversation_id, model):
    events = queue.Queue()
    errors = []
    def reader():
        try:
            response = request(base, token, f'/api/chat/conversations/{conversation_id}/messages',
                method='POST', body={'message': 'Give me a long explanation of recursion.',
                                     'model': model}, stream=True)
            with response:
                for line in response:
                    if line.strip():
                        events.put(json.loads(line))
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            events.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    saw_status = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=.25)
        except queue.Empty:
            continue
        if event is None:
            break
        if event.get('type') == 'status' and event.get('text') == 'Writing your response…':
            saw_status = True
            break
    if not saw_status:
        raise RuntimeError('Chat did not reach its streaming state before cancellation.')
    result = request(base, token, f'/api/chat/conversations/{conversation_id}/stop',
                     method='POST', body={})
    thread.join(timeout=20)
    if thread.is_alive():
        raise RuntimeError('Cancelled chat stream did not close.')
    return {'stop_accepted': result.get('cancellation_requested') is True,
            'reader_errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=52719)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    process = None
    result = {'format': 'localcodingagent-live-chat-smoke-v1', 'port': args.port,
              'model': 'qwen3-coder:30b', 'verified': False,
              'evidence_dir': str(args.output)}
    try:
        process, base, token, startup = launch(args.port)
        result['startup'] = [line for line in startup if 'token' not in line.casefold()]
        models = request(base, token, '/api/chat/models')
        result['provider_connection'] = models.get('connection')
        result['installed_models'] = models.get('installed', [])
        conversation = request(base, token, '/api/chat/conversations', method='POST', body={})
        conversation_id = conversation['id']
        result['conversation_id'] = conversation_id
        result['turns'] = [
            send_message(base, token, conversation_id, result['model'],
                         'Hello, what can you help me with?'),
            send_message(base, token, conversation_id, result['model'],
                         'Write me a Python function that finds duplicate filenames.'),
            send_message(base, token, conversation_id, result['model'],
                         'Can you modify that function to ignore file extensions?'),
        ]
        result['cancel_check'] = cancel_message(base, token, conversation_id, result['model'])
        expected_messages = request(base, token,
            f'/api/chat/conversations/{conversation_id}')['messages']
        result['messages_before_restart'] = len(expected_messages)
        stop(process); process = None
        process, base, token, restart_output = launch(args.port)
        result['restart_provider_connection'] = next((line for line in restart_output
            if line.startswith('Inference provider check:')), 'not reported')
        rows = request(base, token, '/api/chat/conversations')
        restored = next((row for row in rows['conversations'] if row['id'] == conversation_id), None)
        if restored is None:
            raise RuntimeError('Conversation was not restored after dashboard restart.')
        restored_row = request(base, token, f'/api/chat/conversations/{conversation_id}')
        result['messages_after_restart'] = len(restored_row['messages'])
        result['conversation_restored'] = result['messages_after_restart'] == result['messages_before_restart']
        result['verified'] = (result['provider_connection'] == 'ready'
            and result['model'] in result['installed_models']
            and all(turn['response'] for turn in result['turns'])
            and result['conversation_restored']
            and result['cancel_check']['stop_accepted'])
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)[:300]
    finally:
        if process is not None:
            stop(process)
    evidence = args.output / 'result.json'
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'turns'},
                     ensure_ascii=False, indent=2))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
