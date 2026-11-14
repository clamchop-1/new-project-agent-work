"""Bounded repository and authoritative-memory operations. No model imports."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


class AgentError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _retry_windows_sharing(operation, attempts=6, delay=0.1):
    for attempt in range(attempts):
        try:
            return operation()
        except OSError as exc:
            if (
                os.name != 'nt'
                or getattr(exc, 'winerror', None) not in (32, 33)
                or attempt == attempts - 1
            ):
                raise
            time.sleep(delay * (attempt + 1))


def atomic_write(path, raw):
    path = Path(path)
    temporary = path.with_name(path.name + '.new-' + uuid.uuid4().hex)
    try:
        def create_and_write():
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())

        _retry_windows_sharing(create_and_write)
        _retry_windows_sharing(lambda: os.replace(temporary, path))
    finally:
        if temporary.exists():
            try:
                _retry_windows_sharing(temporary.unlink)
            except OSError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) not in (32, 33):
                    raise
                # Preserve the original failure; a locked temporary file
                # can be reviewed and cleaned up separately.


SECRET = re.compile(r'(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----|[\"\']?(?:api[_-]?key|password|access[_-]?token)[\"\']?\s*[:=]\s*[\"\']?[A-Za-z0-9_+/=-]{16,}|Bearer\s+[A-Za-z0-9._~-]{16,})', re.I)


def no_secrets(text):
    if SECRET.search(text):
        raise AgentError('Possible credential material rejected; value withheld.')


def section(path, index=0, history_authorized=False):
    """Read complete H2 sections with sequential 4/8/16KiB budgets and no reread."""
    if type(index) is not int or index < 0 or index > 100:
        raise AgentError('Invalid bounded section index.')
    if index and not history_authorized:
        raise AgentError('Historical retrieval requires explicit authorization.')
    path = Path(path)
    output = bytearray()
    pending = bytearray()
    section_number = -1
    section_lines = 0
    line_number = 0
    first_line = None
    budget = 4096
    scanned = 0
    fence = None
    started = False
    with path.open('rb') as stream:
        while True:
            byte = stream.read(1)
            if byte:
                scanned += 1
                if scanned > 2 * 1024 * 1024:
                    raise AgentError('Historical scan exceeds bounded 2MiB budget.')
                pending.extend(byte)
                if section_number == index and output and fence is None:
                    try:
                        boundary = pending.decode('utf-8')
                    except UnicodeError:
                        boundary = ''
                    if re.match(r'^ {0,3}#{1,2}\s+\S', boundary):
                        return section_record(path, output, first_line, line_number)
                if section_number == index and len(output) + len(pending) > budget:
                    if budget == 16384:
                        # Permit bounded lookahead for a possible next heading.
                        possible_heading = (
                            fence is None
                            and bool(output)
                            and len(pending) <= 256
                            and re.fullmatch(
                                rb' {0,3}(?:#{0,2}[ \t]*|##?[ \t]+[^\r\n]*)?',
                                bytes(pending),
                            ) is not None
                        )
                        if not possible_heading:
                            raise AgentError(
                                'Current section exceeds 16KiB; no partial section returned.'
                            )
                    else:
                        budget *= 2
                if byte != b'\n':
                    continue
            elif not pending:
                if not started or section_number != index or fence is not None:
                    raise AgentError('Missing selected section or invalid fenced EOF boundary.')
                return section_record(path, output, first_line, line_number)
            line_number += 1
            raw = bytes(pending)
            pending.clear()
            try:
                text = raw.decode('utf-8-sig' if line_number == 1 else 'utf-8').rstrip('\r\n')
            except UnicodeError as exc:
                raise AgentError('Memory must contain valid UTF-8.') from exc
            heading = re.match(r'^ {0,3}(#{1,2})\s+\S', text) if fence is None else None
            if not started and text.strip():
                if not heading or len(heading.group(1)) != 2:
                    raise AgentError('First nonblank memory line must be an H2 heading.')
                started = True
            if heading:
                if section_number == index:
                    return section_record(path, output, first_line, line_number - 1)
                if len(heading.group(1)) != 2:
                    raise AgentError('Unexpected shallower heading before selected memory section.')
                section_number += 1
                if section_number == index:
                    first_line = line_number
            token = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', text)
            if token:
                mark, suffix = token.groups()
                if fence is None:
                    fence = mark
                elif mark[0] == fence[0] and len(mark) >= len(fence) and not suffix.strip():
                    fence = None
            if section_number == index:
                output.extend(raw)
                section_lines += 1
                if section_lines > 256:
                    raise AgentError('Selected memory section exceeds 256 lines.')
            if not byte:
                if section_number != index or fence is not None:
                    raise AgentError('Invalid selected section EOF.')
                return section_record(path, output, first_line, line_number)


def section_record(path, raw, first, last):
    text = bytes(raw).decode('utf-8-sig')
    no_secrets(text)
    return {'path': str(path.resolve()), 'first_line': first, 'last_line': last,
            'sha256': digest(bytes(raw)), 'text': text}


@contextmanager
def exclusive(path, owner):
    """Never steal another process's lock, including after a crash."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentError('Project has an active or unreconciled agent lock.') from exc
    token = encoded({'owner': owner, 'pid': os.getpid(), 'nonce': uuid.uuid4().hex})
    try:
        os.write(fd, token)
        os.close(fd)
        yield
    finally:
        if path.exists() and path.read_bytes() == token:
            path.unlink()


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        top = self.git('rev-parse', '--show-toplevel').strip()
        if Path(top).resolve() != self.root:
            raise AgentError('Project must be the exact registered Git worktree root.')
        self.identity = digest(str(self.root).casefold().encode())

    def git(self, *args):
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        result = subprocess.run(['git', '-c', 'safe.directory=' + str(self.root), '-C', str(self.root), *args],
                                env=env, capture_output=True, timeout=20)
        if result.returncode:
            raise AgentError('Git inspection failed: ' + result.stderr.decode(errors='replace')[:300])
        return result.stdout.decode('utf-8', errors='strict')

    def state(self):
        status = self.git('status', '--porcelain=v1', '--untracked-files=all')
        changed = self.git('diff', 'HEAD', '--binary') if self.git('rev-parse', '--verify', 'HEAD').strip() else ''
        # State includes untracked/dirty bytes, not just status letters.
        names = self.git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')
        files = {}
        for name in names:
            if name:
                path = self.path(name, allow_instructions=True)
                if path.is_file():
                    files[name] = file_hash(path)
        return {'project_id': self.identity, 'root': str(self.root),
                'branch': self.git('branch', '--show-current').strip(),
                'commit': self.git('rev-parse', 'HEAD').strip(), 'status': status,
                'diff_sha256': digest(changed.encode()), 'files': files}

    def path(self, name, allow_instructions=False):
        if not isinstance(name, str) or not name or name.startswith(('\\\\', '//')):
            raise AgentError('Invalid project path.')
        relative = Path(name.replace('\\', '/'))
        if '..' in relative.parts or (relative.drive and not relative.is_absolute()):
            raise AgentError('Path traversal or drive-relative path rejected.')
        if relative.is_absolute():
            try:
                relative = relative.relative_to(self.root)
            except ValueError as exc:
                raise AgentError('Absolute path is outside the approved project.') from exc
        if any(':' in part for part in relative.parts):
            raise AgentError('Alternate data streams are forbidden.')
        forbidden = {'.git', '.local', '.codex', '.claude', '.ssh', '.aws', '.venv', 'node_modules', '__pycache__'}
        if any(part.casefold() in forbidden or part.casefold().startswith('.env') for part in relative.parts):
            raise AgentError('Private, generated or credential path rejected.')
        if relative.name.casefold() in ('auth.json', 'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519') or relative.suffix.casefold() in ('.pem', '.p12', '.pfx'):
            raise AgentError('Credential artifact rejected.')
        if (self.root.name.casefold() == 'localagentsandbox'
                and tuple(part.casefold() for part in relative.parts)
                == ('tests', 'test_verified_calculator.py')
                and not allow_instructions):
            raise AgentError('Trusted verification fixture is protected.')
        target = self.root / relative
        current = target
        while current != self.root:
            if current.exists() and (current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
                raise AgentError('Linked/reparse project paths are not permitted.')
            current = current.parent
        if not target.resolve().is_relative_to(self.root):
            raise AgentError('Resolved path escapes approved project.')
        if target.is_file() and target.stat().st_nlink > 1:
            raise AgentError('Hard-linked source artifacts cannot change the approved boundary.')
        if not allow_instructions and target.name.casefold() in ('agents.md', 'agents.override.md', 'claude.md'):
            raise AgentError('Agent instruction writes are protected.')
        return target

    def instructions(self):
        result = []
        # Exact ancestry only; no unrelated project discovery.
        for directory in [*reversed(self.root.parents), self.root]:
            for name in ('AGENTS.md', 'AGENTS.override.md'):
                path = directory / name
                if path.is_file():
                    raw = path.read_bytes()
                    if len(raw) > 16384:
                        raise AgentError('Instruction file exceeds startup limit.')
                    text = raw.decode('utf-8-sig');no_secrets(text)
                    result.append({'path': str(path), 'sha256': digest(raw), 'text': text})
        return result

    def memory(self, historical_index=0, history_authorized=False):
        local = self.root / '.local'
        if local.resolve() != local or local.is_symlink():
            raise AgentError('Private memory root must not alias another location.')
        if (local / 'memory-adapter/pending.json').exists() or (local / '.agent-memory.lock').exists():
            raise AgentError('Memory has an active/unreconciled transaction; do not read a mixed pair.')
        records = []
        for name in ('PROJECT_CONTEXT.md', 'DURABLE_MEMORY.md'):
            path = local / name
            if not path.is_file():
                records.append({'sources': [str(path)], 'status': 'missing'})
                continue
            if path.is_symlink() or path.resolve().parent != local or path.stat().st_nlink > 1:
                raise AgentError('Memory file links cannot change project authority.')
            record = section(path, historical_index, history_authorized)
            same = next((r for r in records if r.get('sha256') == record['sha256']), None)
            if same:
                same['sources'].append(record['path'])
            else:
                records.append({**record, 'sources': [record['path']]})
        return records

    def takeover(self):
        instructions = self.instructions()
        memory = self.memory()
        state = self.state()
        raw = encoded({'instructions': instructions, 'memory': memory})
        if len(raw) > 26000:
            raise AgentError('Bounded startup packet exceeds small-model budget; explicit scoped retrieval needed.')
        return {'format': 'shared-project-takeover', 'version': 1, 'state': state,
                'instructions': instructions, 'memory': memory,
                'memory_authority': 'existing-project-files', 'write_authorized': False,
                'hold_in_current_heading': any(re.search(r'\bHOLD\b|\bHELD\b',row.get('text','').split('\n',1)[0],re.I) for row in memory),
                'notice': 'Newest memory supersedes retained older statements. Recall never resumes held work.'}

    def memory_snapshot(self):
        return {name: file_hash(self.root / '.local' / name)
                for name in ('PROJECT_CONTEXT.md', 'DURABLE_MEMORY.md')}


def validate_handoff(record):
    required = {'event_id', 'agent', 'objective', 'completed', 'unfinished', 'decisions',
                'assumptions', 'next_actions', 'constraints', 'test_receipts'}
    if not isinstance(record, dict) or set(record) != required:
        raise AgentError('Handoff schema must separate facts, assumptions, tasks and evidence.')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', record['event_id']):
        raise AgentError('Invalid handoff event identity.')
    if record['agent'] not in ('codex', 'claude', 'qwen'):
        raise AgentError('Explicit originating agent is required.')
    if not isinstance(record['objective'], str) or not record['objective'].strip():
        raise AgentError('A concrete objective is required.')
    for key in required - {'event_id', 'agent', 'objective', 'test_receipts'}:
        if not isinstance(record[key], list) or any(not isinstance(v, str) for v in record[key]):
            raise AgentError('Handoff sections must contain text lists.')
    if not isinstance(record['test_receipts'], list):
        raise AgentError('Test evidence must be explicit, never inferred from prose.')
    raw = encoded(record)
    if len(raw) > 6000:
        raise AgentError('Handoff is too large for bounded current memory.')
    no_secrets(raw.decode())


def save_handoff(project, record, expected_state, expected_memory, authorized=False):
    """CAS-protected prepend preserving both original histories, not a new memory store."""
    if not authorized:
        raise AgentError('Saving authoritative project memory needs explicit authorization.')
    validate_handoff(record)
    local = project.root / '.local'
    if not local.is_dir() or local.is_symlink() or local.resolve() != local:
        raise AgentError('Existing private project memory directory required.')
    memory_names = ['.local/PROJECT_CONTEXT.md', '.local/DURABLE_MEMORY.md']
    if project.git('ls-files', '--', *memory_names).strip():
        raise AgentError('Authoritative memory must remain untracked and private.')
    project.git('check-ignore', '--', *memory_names)
    control = local / 'memory-adapter'
    if control.exists() and (control.is_symlink() or control.resolve().parent != local):
        raise AgentError('Memory control path escapes the project.')
    control.mkdir(exist_ok=True)
    verified_receipts = []
    for reference in record['test_receipts']:
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise AgentError('Test receipts must be exact artifact references, not model claims.')
        receipt_path = Path(reference['path']).resolve()
        allowed = [local, Path(__file__).resolve().parent/'.local']
        if not any(receipt_path.is_relative_to(root) for root in allowed) or file_hash(receipt_path) != reference['sha256']:
            raise AgentError('Test receipt location or hash differs.')
        receipt = json.loads(receipt_path.read_bytes())
        output = Path(receipt.get('output_path', '')).resolve()
        if (receipt.get('format') != 'local-agent-command-receipt' or receipt.get('project_id') != project.identity
                or type(receipt.get('exit_code')) is not int or output.parent != receipt_path.parent
                or file_hash(output) != receipt.get('output_sha256')):
            raise AgentError('Test receipt does not bind the actual project/command output.')
        verified_receipts.append({**reference, 'command': receipt['command'], 'exit_code': receipt['exit_code'],
                                  'seconds': receipt['seconds'], 'scope': 'recorded execution, not inferred from agent text'})
    with exclusive(local / '.agent-memory.lock', record['event_id']):
        pending = control / 'pending.json'
        if pending.exists():
            raise AgentError('An earlier memory transaction needs manual reconciliation.')
        if project.state() != expected_state or project.memory_snapshot() != expected_memory:
            raise AgentError('Stale Git/worktree/memory snapshot; refresh before saving.')
        paths = [local / name for name in expected_memory]
        old = {path.name: path.read_bytes() for path in paths}
        marker = ('Handoff-ID: ' + record['event_id']).encode()
        if any(marker in raw for raw in old.values()):
            raise AgentError('Duplicate handoff event rejected.')
        semantic = digest(encoded({k: v for k, v in record.items() if k not in ('event_id', 'agent')}))
        if any(('Handoff-Content: ' + semantic).encode() in raw for raw in old.values()):
            raise AgentError('Duplicate semantic handoff rejected.')
        stamp = datetime.now(timezone.utc).isoformat()
        body = [f'## AGENT HANDOFF - {stamp}', '', 'Handoff-ID: ' + record['event_id'],
                'Handoff-Content: ' + semantic, 'Originating agent: ' + record['agent'],
                'Project: ' + str(project.root), 'Branch: ' + expected_state['branch'],
                'Commit: ' + expected_state['commit'], 'Objective: ' + record['objective'], '',
                'Git/worktree snapshot verified by the adapter at save time.',
                'Completed statements below are agent-reported unless backed by test receipts.', '']
        for key in ('completed', 'unfinished', 'decisions', 'assumptions', 'next_actions', 'constraints', 'test_receipts'):
            body += ['### ' + key.replace('_', ' ').title(), '']
            values = verified_receipts if key == 'test_receipts' else record[key]
            body += ['- ' + json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else '- ' + value
                     for value in values] or ['- None recorded.']
            body.append('')
        body += ['### Changed file identities at save time', '',
                 'Worktree status: ' + json.dumps(expected_state['status']),
                 'Worktree content SHA-256: ' + digest(encoded(expected_state['files'])), '',
                 '### Retained current project constraints and state', '']
        prefix = ('\n'.join(body) + '\n').encode('utf-8')
        transaction = control / record['event_id']
        transaction.mkdir(exist_ok=False)
        candidates = {}
        for path in paths:
            current = section(path)['text'].split('\n', 1)[1]
            candidate = prefix + current.rstrip().encode('utf-8') + b'\n\n'
            # Current limits apply to the entire retained summary, not just new text.
            if len(candidate) > 15000 or len(candidate.splitlines()) > 240:
                raise AgentError('Reviewed compaction required; retained constraints cannot be silently dropped.')
            (transaction / (path.name + '.before')).write_bytes(old[path.name])
            (transaction / (path.name + '.new')).write_bytes(candidate + old[path.name])
            candidates[path.name] = candidate + old[path.name]
        journal = {'event_id': record['event_id'], 'project_id': project.identity,
                   'before': expected_memory, 'after': {k: digest(v) for k, v in candidates.items()}}
        atomic_write(pending, encoded(journal))
        # Check both immediately before the first replacement; recheck each again
        # before its replacement. Non-cooperating editors still need coordination.
        for path in paths:
            if file_hash(path) != expected_memory[path.name]:
                raise AgentError('Concurrent external memory edit detected; transaction retained for recovery.')
        for path in paths:
            if file_hash(path) != expected_memory[path.name]:
                raise AgentError('Concurrent external memory edit detected; transaction retained for recovery.')
            atomic_write(path, candidates[path.name])
        for path in paths:
            if file_hash(path) != journal['after'][path.name]:
                raise AgentError('Post-write memory mismatch; transaction retained for recovery.')
        atomic_write(transaction / 'receipt.json', encoded({**journal, 'state': expected_state, 'record': record}))
        pending.unlink()
        return journal
