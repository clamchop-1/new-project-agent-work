"""Bounded live verification for discovered host-specific Ollama chat models.

The script uses the ordinary local-chat launcher, persists only redacted result
metadata, and never stores the one-run dashboard token or full responses.
"""
from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_OUTPUT = ROOT / '.local' / 'tests' / ('model-catalog-live-' + uuid.uuid4().hex[:12])


def _read_startup(process, output):
    for line in process.stdout:
        output.put(line.rstrip('\r\n'))
    output.put(None)


def launch(port):
    process = subprocess.Popen([
        sys.executable, '-u', '-B', str(ROOT / 'agent.py'), 'local-chat',
        '--dashboard-port', str(port), '--no-browser-open',
    ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
       text=True, encoding='utf-8', errors='replace', bufsize=1,
       creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    output = queue.Queue()
    threading.Thread(target=_read_startup, args=(process, output), daemon=True).start()
    startup, token = [], None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and token is None:
        try:
            line = output.get(timeout=.5)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        if line.startswith('One-run dashboard bearer token: '):
            token = line.split(': ', 1)[1]
        elif 'token' not in line.casefold():
            startup.append(line)
    if token is None:
        stop(process)
        raise RuntimeError('Local chat did not start: ' + ' | '.join(startup[-8:]))
    return process, f'http://127.0.0.1:{port}', token, startup


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


def send_message(base, token, conversation_id, model, prompt):
    started = time.monotonic()
    response = request(base, token,
        f'/api/chat/conversations/{conversation_id}/messages', method='POST',
        body={'message': prompt, 'model': model['model_key']}, stream=True)
    events, chunks, identity = [], [], None
    with response:
        for line in response:
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event['type'])
            if event['type'] == 'model':
                identity = {key: event.get(key) for key in
                            ('provider_id', 'host_id', 'model_id')}
            elif event['type'] == 'token':
                chunks.append(event.get('text', ''))
            elif event['type'] == 'error':
                raise RuntimeError(event.get('message', 'Chat failed.'))
    text = ''.join(chunks)
    if not text or identity != {key: model[key] for key in
                                ('provider_id', 'host_id', 'model_id')}:
        raise RuntimeError('Stream did not confirm the selected host/model and return text.')
    return {'selected_model_key': model['model_key'], 'identity': identity,
            'events': events, 'response_chars': len(text),
            'response_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'elapsed_seconds': round(time.monotonic() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=52720)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    process = None
    result = {'format': 'localcodingagent-model-catalog-live-v1',
              'port': args.port, 'verified': False,
              'evidence_dir': str(args.output)}
    try:
        process, base, token, startup = launch(args.port)
        result['startup'] = startup
        inventory = request(base, token, '/api/chat/models')
        result['connection'] = inventory.get('connection')
        result['models_discovered'] = len(inventory.get('models', []))
        result['installed_keys'] = len(inventory.get('installed', []))
        refresh = request(base, token, '/api/chat/models/refresh', method='POST', body={})
        rows = refresh.get('models', [])
        result['refresh_models_discovered'] = len(rows)
        mac_model = next(row for row in rows if row.get('provider_id') == 'ssh_tunnel_ollama'
                         and row.get('model_id') == 'qwen3-coder:30b'
                         and row.get('authorized') is True and row.get('compatible') is True)
        windows_model = next(row for row in rows if row.get('provider_id') == 'ollama'
                             and row.get('model_id') == 'qwen3:4b'
                             and row.get('authorized') is True and row.get('compatible') is True)
        conversation = request(base, token, '/api/chat/conversations', method='POST', body={})
        conversation_id = conversation['id']
        result['turns'] = [
            send_message(base, token, conversation_id, mac_model,
                         'Give a short greeting in one sentence.'),
            send_message(base, token, conversation_id, windows_model,
                         'Reply with one concise sentence confirming this follow-up.'),
        ]
        saved = request(base, token, f'/api/chat/conversations/{conversation_id}')
        result['conversation_messages'] = len(saved.get('messages', []))
        result['catalog_persisted'] = (ROOT / '.local' / 'model-catalog.json').is_file()
        result['verified'] = (
            inventory.get('connection') == 'ready'
            and result['models_discovered'] >= 2
            and result['refresh_models_discovered'] >= 2
            and len(result['turns']) == 2
            and all('token' in turn['events'] and 'model' in turn['events']
                    for turn in result['turns'])
            and result['conversation_messages'] == 4
            and result['catalog_persisted'])
    except Exception as exc:
        result['failure'] = type(exc).__name__ + ': ' + str(exc)[:300]
    finally:
        if process is not None:
            stop(process)
    (args.output / 'result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
