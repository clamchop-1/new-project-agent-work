"""Bounded, read-only Git project registration and source retrieval."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

from agent_core import AgentError, Project, atomic_write, encoded, file_hash
from knowledge_index import KnowledgeIndex, SUPPORTED_EXTENSIONS

MAX_REGISTERED = 24
MAX_INVENTORY = 5000
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_INDEXED_FILES = 2000
MAX_STATUS_PATHS = 250
MAX_STATUS_BYTES = 2 * 1024 * 1024
EXCLUDED_NAMES = frozenset({'.env', '.env.local', '.env.production', 'credentials.json',
    'auth.json', 'secrets.json', 'id_rsa', 'id_ed25519', 'known_hosts', 'authorized_keys',
    'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'poetry.lock', 'uv.lock'})
EXCLUDED_DIRS = frozenset({'.git', '.local', '.venv', 'venv', 'node_modules', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist', 'coverage', 'site-packages',
    'vendor', 'target', 'models', 'weights', 'recordings', 'audio', 'media', 'generated'})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _regular_project_root(value: str) -> Project:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise AgentError('Choose an absolute path to a Git repository.')
    raw = Path(value)
    if not raw.is_absolute() or any(part == '..' for part in raw.parts):
        raise AgentError('Project registration requires an absolute path without traversal.')
    current = raw
    while current != current.parent:
        if current.exists() and (current.is_symlink()
                or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400):
            raise AgentError('Project roots cannot traverse symbolic links or reparse points.')
        current = current.parent
    return Project(raw.resolve(strict=True))


def _safe_remote(url: str) -> str | None:
    """Expose repository identity without userinfo, query, or fragment credentials."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    if re.match(r'^[^/@:]+@[^:]+:', url):
        host, path = url.split(':', 1)
        return host.split('@', 1)[1].casefold() + '/' + path.strip('/').removesuffix('.git')
    try:
        parts = urlsplit(url)
        if parts.scheme not in {'https', 'http', 'ssh'} or not parts.hostname:
            return None
        path = parts.path.strip('/')
        if parts.username or parts.password:
            pass
        if not path:
            return None
        return parts.hostname.casefold() + '/' + path.removesuffix('.git')
    except ValueError:
        return None


def _source_allowed(relative: str) -> bool:
    path = Path(relative.replace('\\', '/'))
    suffix = path.suffix.casefold()
    name = path.name.casefold()
    return (suffix in SUPPORTED_EXTENSIONS and name not in EXCLUDED_NAMES
            and not name.startswith('.env') and suffix not in {'.sqlite', '.db', '.pyc'}
            and not any(part.casefold() in EXCLUDED_DIRS or part.casefold().startswith('.env')
                        for part in path.parts[:-1]))


class ProjectIntelligence:
    """Explicitly registered projects; reads source bytes and Git metadata only."""
    def __init__(self, application: Project, *, embedding_provider=None):
        if not isinstance(application, Project):
            raise AgentError('Project intelligence requires the LocalCodingAgent application root.')
        self.application = application
        self.embedding_provider = embedding_provider
        self.directory = application.root / '.local' / 'project-intelligence'
        self.registry_path = self.directory / 'registry.json'

    def _prepare(self, create=False):
        root = self.application.root
        private = root / '.local'
        for path in (private, self.directory, self.registry_path):
            if path.exists() and (path.is_symlink()
                    or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
                raise AgentError('Project registry storage contains a linked path.')
        if create:
            if not self.application.git('check-ignore', '--', '.local/project-intelligence/registry.json').strip():
                raise AgentError('Project registry storage is not Git-ignored.')
            self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.exists() and not self.directory.resolve().is_relative_to(private.resolve()):
            raise AgentError('Project registry storage escapes the private application directory.')

    def _load(self):
        self._prepare()
        if not self.registry_path.exists():
            return {'schema_version': 1, 'projects': []}
        if self.registry_path.stat().st_size > 256 * 1024 or self.registry_path.stat().st_nlink > 1:
            raise AgentError('Project registry exceeds its private storage limits.')
        try:
            value = json.loads(self.registry_path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError('Project registry is invalid; preserve it for reconciliation.') from exc
        rows = value.get('projects') if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(rows, list) or len(rows) > MAX_REGISTERED:
            raise AgentError('Project registry schema or size is invalid.')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or row.get('project_id') in seen
                    or not re.fullmatch(r'[a-f0-9]{64}', str(row.get('project_id', '')))
                    or not isinstance(row.get('root'), str)
                    or row.get('authorization_scope') != 'explicit_local_read_only'):
                raise AgentError('Project registry entry is invalid.')
            seen.add(row['project_id'])
        return value

    def _save(self, value):
        self._prepare(create=True)
        atomic_write(self.registry_path, encoded(value))

    def _record(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r'[a-f0-9]{64}', project_id):
            raise AgentError('Select a registered project.')
        rows = self._load()['projects']
        row = next((item for item in rows if item['project_id'] == project_id), None)
        if row is None:
            raise AgentError('Project is not registered in this LocalCodingAgent instance.')
        project = _regular_project_root(row['root'])
        if project.identity != project_id:
            raise AgentError('Registered project identity changed; re-register it after review.')
        return row, project

    @staticmethod
    def _bounded_git_output(project, args, max_bytes):
        env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
        command = ['git', '-c', 'core.fsmonitor=false', '-c',
                   'safe.directory=' + str(project.root), '-C', str(project.root), *args]
        try:
            process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise AgentError('Bounded Git inspection could not start.') from exc
        timed_out = threading.Event()
        def stop_process():
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass
        timer = threading.Timer(20, stop_process)
        timer.daemon = True
        timer.start()
        output = bytearray()
        try:
            while True:
                chunk = process.stdout.read(min(64 * 1024, max_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    stop_process()
                    raise AgentError('Git inspection exceeded its fixed output budget.')
            status = process.wait(timeout=2)
            if timed_out.is_set():
                raise AgentError('Git inspection exceeded its 20 second time limit.')
            if status:
                raise AgentError('Git inspection failed.')
            return output.decode('utf-8', errors='strict')
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            stop_process()
            raise AgentError('Bounded Git inspection could not be read safely.') from exc
        finally:
            timer.cancel()
            if process.poll() is None:
                stop_process()
                process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()

    @classmethod
    def _git_status(cls, project):
        raw = cls._bounded_git_output(project,
            ('status', '--porcelain=v1', '-z', '--untracked-files=all'), MAX_STATUS_BYTES)
        staged = unstaged = untracked = 0
        paths = []
        entries = raw.split('\0')
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4 or entry[2] != ' ':
                continue
            x, y, name = entry[0], entry[1], entry[3:]
            if x == '?' and y == '?':
                untracked += 1
            else:
                staged += x != ' '
                unstaged += y != ' '
            if len(paths) < MAX_STATUS_PATHS:
                paths.append({'path': name.replace('\\', '/'), 'staged': x != ' ',
                              'unstaged': y != ' ', 'untracked': x == '?' and y == '?'})
            if x in {'R', 'C'} or y in {'R', 'C'}:
                index += 1
        return {'staged_count': staged, 'unstaged_count': unstaged,
                'untracked_count': untracked, 'paths': paths, 'truncated': len(entries) > MAX_STATUS_PATHS}

    @classmethod
    def snapshot(cls, project):
        head = project.git('rev-parse', 'HEAD').strip()
        branch = project.git('branch', '--show-current').strip() or '(detached HEAD)'
        status = cls._git_status(project)
        log = project.git('log', '-8', '--date=iso-strict', '--format=%H%x1f%ad%x1f%s')
        commits = []
        for line in log.splitlines():
            fields = line.split('\x1f', 2)
            if len(fields) == 3:
                commits.append({'commit': fields[0], 'date': fields[1], 'subject': fields[2][:240]})
        remotes = []
        remote_text = project.git('remote')
        for name in remote_text.splitlines()[:8]:
            if not re.fullmatch(r'(?!-)[A-Za-z0-9._-]{1,80}', name):
                continue
            urls = project.git('remote', 'get-url', '--all', name).splitlines()
            identities = list(dict.fromkeys(_safe_remote(url) for url in urls if _safe_remote(url)))
            remotes.append({'name': name[:80], 'identities': identities})
        refs_text = project.git('for-each-ref', '--count=100', '--format=%(refname:short)%00%(objectname)',
                                'refs/remotes')
        refs = []
        for line in refs_text.splitlines()[:100]:
            name, _, commit = line.partition('\0')
            refs.append({'name': name[:180], 'commit': commit[:64]})
        try:
            upstream = project.git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')
        except AgentError:
            upstream = ''
        relation = None
        if upstream.strip():
            counts = project.git('rev-list', '--left-right', '--count', 'HEAD...@{upstream}').split()
            if len(counts) == 2 and all(item.isdigit() for item in counts):
                relation = {'cached_upstream': upstream.strip(), 'ahead': int(counts[0]),
                            'behind': int(counts[1]), 'fresh_remote_fetch': False}
        files = cls._file_inventory(project)
        inventory_count = len([item for item in files if item])
        return {'project_id': project.identity, 'root': str(project.root), 'branch': branch,
                'commit': head, 'status': status, 'recent_commits': commits,
                'remotes': remotes, 'cached_remote_refs': refs,
                'remote_relationship': relation, 'inventory_count': inventory_count,
                'inventory_truncated': inventory_count > MAX_INVENTORY,
                'observed_at': _now(), 'source_state': 'local_worktree_and_cached_git_refs'}

    @staticmethod
    def _file_inventory(project):
        """Read Git's local file list with an output cap and timeout; never walk arbitrary paths."""
        raw = ProjectIntelligence._bounded_git_output(project,
            ('ls-files', '-z', '--cached', '--others', '--exclude-standard'), MAX_INVENTORY_BYTES)
        return raw.split('\0')

    def register(self, root: str, *, confirmed=False):
        if confirmed is not True:
            raise AgentError('Explicit confirmation is required to register a local repository.')
        project = _regular_project_root(root)
        existing = self._load()
        if any(row['project_id'] == project.identity for row in existing['projects']):
            raise AgentError('This repository is already registered.')
        if len(existing['projects']) >= MAX_REGISTERED:
            raise AgentError('Project registration limit reached.')
        snapshot = self.snapshot(project)
        row = {'project_id': project.identity, 'display_name': project.root.name,
               'root': str(project.root), 'remote_repositories': [remote for item in snapshot['remotes']
                   for remote in item['identities']], 'registered_at': _now(),
               'last_refreshed_at': snapshot['observed_at'],
               'authorization_scope': 'explicit_local_read_only', 'indexing_status': 'not_indexed'}
        existing['projects'].append(row)
        self._save(existing)
        return {**row, 'snapshot': snapshot}

    def list(self):
        rows = []
        for row in self._load()['projects']:
            try:
                current = self.snapshot(self._record(row['project_id'])[1])
                rows.append({**row, 'snapshot': current})
            except (AgentError, OSError, ValueError):
                rows.append({**row, 'snapshot': None, 'availability': 'unavailable'})
        return rows

    def remove_registration(self, project_id):
        value = self._load()
        before = len(value['projects'])
        value['projects'] = [row for row in value['projects'] if row['project_id'] != project_id]
        if before == len(value['projects']):
            raise AgentError('Project is not registered.')
        self._save(value)
        return {'project_id': project_id, 'unregistered': True, 'source_files_deleted': False}

    def _index(self, project_id):
        row, project = self._record(project_id)
        return row, project, KnowledgeIndex(project, storage_owner=self.application,
                                             storage_namespace=project_id)

    def refresh(self, project_id, *, embedding_provider=None):
        row, project, index = self._index(project_id)
        names = self._file_inventory(project)
        names = [name.replace('\\', '/') for name in names if name]
        if len(names) > MAX_INVENTORY:
            names = names[:MAX_INVENTORY]
        candidates = []
        excluded_sources = 0
        for name in names:
            if not _source_allowed(name):
                continue
            try:
                path = project.path(name, allow_instructions=True)
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    excluded_sources += 1
                    continue
            except (AgentError, OSError):
                excluded_sources += 1
                continue
            candidates.append(name)
            if len(candidates) >= MAX_INDEXED_FILES:
                break
        sources = index.replace_sources(candidates)
        status_before = index.status()
        indexing_provider = embedding_provider
        if status_before.get('documents', 0) and not status_before.get('embedding'):
            indexing_provider = None
        try:
            outcome = index.index(provider=indexing_provider)
        except AgentError as exc:
            if (indexing_provider is None or status_before.get('documents', 0)
                    or 'Embedding work exceeds' not in str(exc)):
                raise
            # An oversized first batch falls back to the independently useful keyword index.
            outcome = index.index(provider=None)
            outcome['semantic_note'] = 'Embedding work exceeded its safe batch limit; keyword retrieval is active.'
        outcome['excluded_unsafe_or_oversized'] = excluded_sources
        row['last_refreshed_at'] = _now()
        row['indexing_status'] = 'ready' if outcome.get('indexed', 0) or outcome.get('unchanged', 0) else 'empty'
        value = self._load()
        for existing in value['projects']:
            if existing['project_id'] == project_id:
                existing.update(row)
        self._save(value)
        return {'project_id': project_id, 'sources': sources['sources'], 'index': outcome,
                'index_status': index.status(), 'snapshot': self.snapshot(project)}

    def search(self, project_id, query, *, limit=4, embedding_provider=None):
        row, project, index = self._index(project_id)
        result = index.search(query, limit=limit, provider=embedding_provider,
                              semantic_min_similarity=0.28)
        commit = project.git('rev-parse', 'HEAD').strip()
        for item in result['results']:
            item['citation'] = {'project': row['display_name'], 'path': item['path'],
                'line_start': item['line_start'], 'line_end': item['line_end'],
                'section': item['section'], 'commit': commit,
                'document_sha256': item['document_sha256'], 'content_sha256': item['content_sha256'],
                'retrieved_at': _now(), 'validated': item['evidence_state'] == 'fresh_hash_verified'}
        result['project_id'] = project_id
        result['commit'] = commit
        return result

    def validate_results(self, project_id, results):
        """Revalidate each citation against the current source bytes before showing it."""
        _, project, index = self._index(project_id)
        if not isinstance(results, list) or len(results) > 8:
            raise AgentError('Project citation set exceeds its validation bound.')
        valid = []
        for result in results:
            try:
                stored = index.inspect(result['chunk_id'])
                path = project.path(stored['path'], allow_instructions=True)
                if (stored['evidence_state'] != 'fresh_hash_verified'
                        or stored['path'] != result['path']
                        or stored['document_sha256'] != result['document_sha256']
                        or stored['content_sha256'] != result['content_sha256']
                        or stored['line_start'] != result['line_start']
                        or stored['line_end'] != result['line_end']
                        or not path.is_file()
                        or file_hash(path) != result['document_sha256']):
                    continue
                valid.append({**result, 'citation': {**result['citation'],
                    'validated': True, 'retrieved_at': _now()}})
            except (AgentError, KeyError, OSError, TypeError, ValueError):
                continue
        return valid
