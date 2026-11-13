"""Local-only Qwen tool loop with bounded, typed repository capabilities."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

from agent_core import AgentError, Project, atomic_write, digest, encoded, exclusive, no_secrets, file_hash

HUB = Path(__file__).resolve().parent


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AgentError('Model endpoint redirects are forbidden.')


class Ollama:
    def __init__(self, base='http://127.0.0.1:11434'):
        url = urlsplit(base)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise AgentError('Use an explicit IPv4 loopback Ollama endpoint or approved SSH tunnel.')
        self.base = base.rstrip('/')
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, timeout=180):
        if path not in ('/api/version', '/api/tags', '/api/ps', '/api/chat'):
            raise AgentError('Endpoint is not on the local inference allowlist.')
        request = Request(self.base + path, data=encoded(payload) if payload is not None else None,
                          headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise AgentError('Model response exceeds local limit.')
        return json.loads(raw)

    def model(self, name):
        if 'cloud' in name.casefold():
            raise AgentError('Cloud model tags are forbidden.')
        matches = [row for row in self.request('/api/tags').get('models', []) if row['name'] == name]
        if len(matches) != 1 or matches[0].get('remote_host') or matches[0].get('remote_model'):
            raise AgentError('An already installed, non-remote model is required; no download fallback.')
        if matches[0].get('details', {}).get('format') not in ('gguf', 'safetensors'):
            raise AgentError('Unverified local model format.')
        return matches[0]


def function(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties or {},
                       'required': required or [], 'additionalProperties': False}}}


TEXT = {'type': 'string'}
TOOLS = [
    function('list_files', 'List bounded source paths in the selected actual repository.'),
    function('read_file', 'Read source and applicable instructions. Use returned SHA for edits.',
             {'path': TEXT, 'start_line': {'type': 'integer'}, 'line_count': {'type': 'integer'}}, ['path']),
    function('search', 'Search source literally; no shell or regular-expression execution.', {'text': TEXT}, ['text']),
    function('git_status', 'Read actual branch, commit and dirty status.'),
    function('git_diff', 'Read the actual current source diff.'),
    function('replace_text', 'Authorized edit only: exact file SHA and one exact occurrence required.',
             {k: TEXT for k in ('path', 'expected_sha256', 'old', 'new')}, ['path', 'expected_sha256', 'old', 'new']),
    function('create_file', 'Create a specifically authorized new source file; never overwrite.',
             {'path': TEXT, 'content': TEXT}, ['path', 'content']),
    function('run_tests', 'Execute the operator-pinned restricted pure-Python fixture unittest suite. General shell/testing is unavailable.'),
    function('run_command', 'Only python_parse and powershell_file_info are available; no arbitrary terminal commands.',
             {'name': TEXT, 'path': TEXT}, ['name', 'path']),
    function('finish', 'Report actual work and next steps. Never invent test or command results.',
             {'summary': TEXT, 'unfinished': {'type': 'array', 'items': TEXT},
              'next_actions': {'type': 'array', 'items': TEXT}}, ['summary', 'unfinished', 'next_actions'])]


class Broker:
    def __init__(self, project, run, writes=(), test_config=None):
        self.project, self.run = project, Path(run)
        self.allowed_writes = set(writes)
        self.test_config = test_config
        self.commands, self.writes = [], []
        self.dirty = bool(project.state()['status'].strip())
        self.loaded_memory = project.memory()

    def source_paths(self):
        names = self.project.git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        result = []
        for name in names:
            if not name:
                continue
            try:
                path = self.project.path(name, allow_instructions=True)
            except AgentError:
                continue
            if path.is_file() and path.suffix.casefold() in ('.py', '.js', '.ts', '.md', '.html', '.css', '.json', '.toml', '.txt', '.ps1'):
                result.append(name)
        return result[:500]

    def read(self, path, start_line=1, line_count=80):
        if type(start_line) is not int or type(line_count) is not int or not 1 <= start_line <= 100000 or not 1 <= line_count <= 120:
            raise AgentError('Read requires bounded positive line numbers/count.')
        requested = Path(path.replace('\\','/'))
        if not requested.is_absolute():requested=self.project.root/requested
        authorities = [self.project.root/'.local'/name for name in ('PROJECT_CONTEXT.md','DURABLE_MEMORY.md')]
        if requested in authorities:
            if self.project.memory()!=self.loaded_memory:raise AgentError('Authoritative memory changed during the session; refresh takeover.')
            return {'already_loaded_at_startup':True,
                    'sections':[{k:v for k,v in row.items() if k!='text'} for row in self.loaded_memory],
                    'guidance':'Complete newest text is already in the initial packet. Historical content was not loaded.'}
        target = self.project.path(path, allow_instructions=True)
        if target.stat().st_size > 1024 * 1024:
            raise AgentError('Source file exceeds bounded read size.')
        raw = target.read_bytes()
        text = raw.decode('utf-8-sig');no_secrets(text)
        lines = text.splitlines()
        selected = '\n'.join(lines[start_line-1:start_line-1+line_count])
        if len(selected.encode()) > 16000:
            raise AgentError('Selected source passage exceeds 16KiB.')
        nested = []
        directory = self.project.root
        for part in target.parent.relative_to(self.project.root).parts:
            directory = directory/part
            instruction = directory/'AGENTS.md'
            if instruction.is_file():
                if instruction == target: continue
                raw_instruction = instruction.read_bytes()
                if len(raw_instruction) > 16000: raise AgentError('Nested instructions exceed the bounded budget.')
                instruction_text = raw_instruction.decode('utf-8-sig');no_secrets(instruction_text)
                nested.append({'path': str(instruction.relative_to(self.project.root)), 'sha256': digest(raw_instruction),
                               'text': instruction_text})
        return {'path': path, 'sha256': digest(raw), 'first_line': start_line,
                'total_lines': len(lines), 'text': selected, 'nested_instructions': nested}

    def write(self, path, content, expected=None):
        target = self.project.path(path)
        canonical = target.relative_to(self.project.root).as_posix()
        if canonical not in self.allowed_writes:
            raise AgentError('Write path was not specifically authorized by the operator.')
        if self.dirty:
            raise AgentError('Dirty starting worktree preserved; isolate or review existing changes first.')
        if target.suffix.casefold() in ('.pcg', '.kmp', '.ksc', '.wav', '.sqlite', '.db', '.exe'):
            raise AgentError('Instrument, binary and data artifacts are protected.')
        no_secrets(content)
        if len(content.encode()) > 64000:
            raise AgentError('Source write exceeds size budget.')
        if expected is None:
            if target.exists():
                raise AgentError('Existing file preserved; creation cannot overwrite.')
            before = None
            with target.open('xb') as stream:
                stream.write(content.encode())
        else:
            if not target.is_file() or file_hash(target) != expected:
                raise AgentError('File changed since inspection; edit refused.')
            before = expected
            atomic_write(target, content.encode())
        receipt = {'path': canonical, 'before': before, 'after': file_hash(target)}
        self.writes.append(receipt)
        return receipt

    def execute(self, name, path=None):
        extra_env = {}
        if name == 'tests':
            if self.test_config is None:
                raise AgentError('General project tests/shell need a qualified OS sandbox and separate approval; no automatic execution.')
            command = [sys.executable, '-B', '-I', str(HUB/'pure_tests.py'), str(self.project.root), str(self.test_config)]
        elif name == 'python_parse':
            target = self.project.path(path)
            command = [sys.executable, '-B', '-I', str(HUB/'inspect_source.py'), str(target)]
        elif name == 'powershell_file_info':
            target = self.project.path(path)
            # Fixed built-in read command; no script-policy change or model shell text.
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                       'Get-Item -LiteralPath $env:LOCAL_AGENT_READ_FILE | Select-Object Name,Length | ConvertTo-Json -Compress']
            extra_env['LOCAL_AGENT_READ_FILE'] = str(target)
        else:
            raise AgentError('Command is not on the fixed capability allowlist; no shell interpolation.')
        env = {k: v for k, v in os.environ.items() if not any(word in k.upper() for word in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD'))}
        env.update(extra_env)
        began = time.monotonic()
        child = subprocess.Popen(command, cwd=self.project.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = child.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill();output, _ = child.communicate()
            raise AgentError('Owned bounded command timed out; PID terminated.')
        except BaseException:
            if child.poll() is None:child.kill()
            child.communicate(timeout=10)
            raise
        if len(output) > 32000:
            raise AgentError('Command output exceeds limit.')
        text = output.decode('utf-8', errors='replace');no_secrets(text)
        index = len(self.commands)
        log = self.run/f'command-{index}.txt';log.write_bytes(output)
        receipt = {'format': 'local-agent-command-receipt', 'project_id': self.project.identity,
                   'command': name, 'exit_code': child.returncode, 'seconds': time.monotonic()-began,
                   'worktree_content_sha256': digest(encoded(self.project.state()['files'])),
                   'output_path': str(log), 'output_sha256': file_hash(log)}
        receipt_path = self.run/f'command-{index}.json';receipt_path.write_bytes(encoded(receipt))
        self.commands.append({'path': str(receipt_path), 'sha256': file_hash(receipt_path)})
        return {**receipt, 'output': text}

    def call(self, name, args):
        if name == 'list_files': return {'files': self.source_paths()}
        if name == 'read_file': return self.read(**args)
        if name == 'search':
            needle = args['text']
            if not isinstance(needle, str) or not 1 <= len(needle) <= 200:
                raise AgentError('Use a bounded literal search.')
            matches = [];sensitive_exclusions=0
            for path in self.source_paths():
                source = self.project.path(path, allow_instructions=True)
                if source.stat().st_size > 200000: continue
                text = source.read_text(encoding='utf-8-sig')
                try:no_secrets(text)
                except AgentError:
                    sensitive_exclusions+=1;continue
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line: matches.append({'path': path, 'line': number, 'text': line[:400]})
                    if len(matches) == 40: return {'matches': matches, 'limited': True,'sensitive_exclusions':sensitive_exclusions}
            return {'matches': matches,'sensitive_exclusions':sensitive_exclusions}
        if name == 'git_status':
            state = self.project.state();return {k: v for k, v in state.items() if k != 'files'}
        if name == 'git_diff':
            text = self.project.git('diff', 'HEAD', '--stat') + self.project.git('diff', 'HEAD', '--', *sorted(self.allowed_writes))
            no_secrets(text)
            return {'diff': text[:16000], 'limited': len(text) > 16000}
        if name == 'replace_text':
            target = self.project.path(args['path'])
            before = target.read_text(encoding='utf-8')
            if not args['old'] or before.count(args['old']) != 1:
                raise AgentError('Edit must match one exact nonempty occurrence.')
            return self.write(args['path'], before.replace(args['old'], args['new'], 1), args['expected_sha256'])
        if name == 'create_file': return self.write(args['path'], args['content'])
        if name == 'run_tests': return self.execute('tests')
        if name == 'run_command': return self.execute(args['name'], args['path'])
        if name == 'finish': return args
        raise AgentError('Unknown tool rejected.')


def run_agent(project, model, task, output, base='http://127.0.0.1:11434', writes=(), test_config=None, max_steps=16, context=16384):
    output = Path(output);output.mkdir(parents=True, exist_ok=False)
    client = Ollama(base);identity = client.model(model)
    packet = project.takeover()
    public_packet = {**packet, 'state': {k: v for k, v in packet['state'].items() if k != 'files'}}
    broker = Broker(project, output, writes, test_config)
    system = ('You are a local coding agent. Follow the supplied authoritative project instructions and newest memory. '
              'Read tools before claiming file facts. Git and actual tool receipts are authoritative. Never fabricate tool/test results. '
              'Recall does not resume holds. Only exact operator-authorized paths may be edited. No deletes, installs, hardware, secrets or network tools. '
              'Use tool errors/test failures to correct your implementation within the task. Finish with the finish tool. '
              'Prefer project-relative tool paths such as README.md. '
              'For read-only takeover, explain architecture, current hold/task and next action without changing files.')
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(public_packet, ensure_ascii=False) + '\nTASK:\n' + task}]
    log = output/'events.jsonl'
    result = {'status': 'running', 'model': model, 'model_digest': identity['digest'], 'project_id': project.identity,
              'endpoint': base, 'context': context, 'steps': 0, 'writes': [], 'commands': [], 'cloud_fallback': False,
              'operator_policy':{'write_paths':list(writes),'hold_in_current_heading':packet['hold_in_current_heading'],
                                 'verified_commit':packet['state']['commit'],'model_next_actions_are_proposals':True,
                                 'notice':'No project work is resumed by inspection. Existing holds require explicit task-specific authorization.'}}
    atomic_write(output/'result.json', encoded(result))
    started = time.monotonic()
    try:
        for step in range(max_steps):
            if (output/'STOP').exists(): raise AgentError('Operator emergency STOP requested.')
            if len(encoded(messages)) > 54000: raise AgentError('Context budget reached; save a bounded handoff and continue in a new session.')
            active_tools=TOOLS
            if len(encoded(messages)) > 47000:
                active_tools=[tool for tool in TOOLS if tool['function']['name']=='finish']
                messages.append({'role':'user','content':'The bounded read budget is nearly used. Finish now using only observed evidence and mark remaining unknowns; do not request more source.'})
            began = time.monotonic()
            response = client.request('/api/chat', {'model': model, 'messages': messages, 'tools': active_tools,
                'stream': False, 'think': False, 'keep_alive': '2m',
                'options': {'num_ctx': context, 'num_predict': 1600, 'temperature': 0, 'num_thread': 4}})
            message = response['message']
            no_secrets(encoded(message).decode())
            with log.open('ab') as stream:
                stream.write(encoded({'step': step, 'seconds': time.monotonic()-began, 'message': message,
                    'timings': {k: response.get(k) for k in ('load_duration','prompt_eval_count','prompt_eval_duration','eval_count','eval_duration')}}) + b'\n')
            messages.append(message)
            calls = message.get('tool_calls', [])
            if not calls:
                messages.append({'role': 'user', 'content': 'Use actual tools and finish with the finish tool; prose alone does not establish execution.'})
                continue
            for call in calls:
                function_call = call['function'];name = function_call['name'];args = function_call.get('arguments', {})
                try:
                    if isinstance(args, str): args = json.loads(args)
                    tool_result = broker.call(name, args)
                except (AgentError, OSError, ValueError, TypeError, KeyError) as exc:
                    tool_result = {'error': str(exc)[:500], 'operation_performed': False}
                with log.open('ab') as stream:
                    stream.write(encoded({'tool': name, 'arguments': args, 'result': tool_result}) + b'\n')
                messages.append({'role': 'tool', 'tool_name': name, 'tool_call_id': call.get('id', name),
                                 'content': json.dumps(tool_result, ensure_ascii=False)})
                if name == 'finish' and 'error' not in tool_result:
                    result.update(status='complete', summary=tool_result)
                    break
            result['steps'] = step + 1
            if result['status'] == 'complete': break
        if result['status'] != 'complete': result['status'] = 'step_limit'
    except BaseException as exc:
        result.update(status='failed', error=str(exc)[:1000])
        raise
    finally:
        tests = [json.loads(Path(ref['path']).read_bytes()) for ref in broker.commands
                 if json.loads(Path(ref['path']).read_bytes())['command'] == 'tests']
        actual_tests = 'not_run'
        if tests:
            latest = tests[-1]
            actual_tests = ('stale_after_edit' if latest['worktree_content_sha256'] != digest(encoded(project.state()['files']))
                            else 'passed' if latest['exit_code'] == 0 else 'failed')
        result.update(seconds=time.monotonic()-started, writes=broker.writes, commands=broker.commands,
                      verified_test_status=actual_tests)
        atomic_write(output/'result.json', encoded(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True);parser.add_argument('--task', required=True)
    parser.add_argument('--model', default='qwen3.5:4b');parser.add_argument('--output', required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:11434')
    args = parser.parse_args()
    result = run_agent(Project(args.project), args.model, args.task, args.output, args.endpoint)
    print(json.dumps(result, indent=2))
