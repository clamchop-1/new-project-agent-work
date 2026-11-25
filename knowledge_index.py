"""Project-local, provenance-bound technical knowledge index.

Documents are reference data. This module never grants tools, changes project
permissions, or promotes retrieved material into trusted skills.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import errno
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import time
from typing import Any, Mapping, Protocol, Sequence
import uuid

from agent_core import AgentError, Project, atomic_write, encoded, file_hash, no_secrets

SCHEMA_VERSION = 1
SUPPORTED_EXTENSIONS = frozenset({'.md', '.markdown', '.txt', '.py', '.js', '.jsx', '.ts',
    '.tsx', '.html', '.htm', '.css', '.json', '.toml', '.yaml', '.yml', '.ini', '.cfg',
    '.java', '.cs', '.go', '.rs', '.cpp', '.c', '.h', '.hpp', '.sql', '.xml', '.swift',
    '.rb', '.php', '.kt'})
BLOCKED_DIRS = frozenset({'.git', '.local', '.venv', 'venv', 'node_modules', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist', 'coverage', 'site-packages'})
BLOCKED_FILES = frozenset({'credentials.json', 'auth.json', 'secrets.json', 'id_rsa',
    'id_ed25519', 'known_hosts', 'authorized_keys'})
BLOCKED_SUFFIXES = frozenset({'.pem', '.key', '.p12', '.pfx', '.kdbx', '.sqlite', '.db', '.pyc'})
MAX_SOURCES = 2048
MAX_DOCUMENTS = 2000
MAX_DOCUMENT_BYTES = 1_000_000
MAX_TOTAL_BYTES = 50_000_000
MAX_CHUNKS = 12000
MAX_EMBEDDING_CHUNKS = 256
MAX_CHUNK_CHARS = 2800
CHUNK_OVERLAP_CHARS = 180
MAX_CANDIDATES = 40
MAX_RESULT_CHARS = 2400
MAX_CONTEXT_CHARS = 8000
RRF_K = 60
LANGUAGE_BY_SUFFIX = {'.md': 'markdown', '.markdown': 'markdown', '.txt': 'text',
    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.ts': 'typescript',
    '.tsx': 'typescript', '.html': 'html', '.htm': 'html', '.css': 'css', '.json': 'json',
    '.toml': 'toml', '.yaml': 'yaml', '.yml': 'yaml', '.ini': 'ini', '.cfg': 'ini',
    '.java': 'java', '.cs': 'csharp', '.go': 'go', '.rs': 'rust', '.cpp': 'cpp',
    '.c': 'c', '.h': 'c', '.hpp': 'cpp', '.sql': 'sql', '.xml': 'xml', '.swift': 'swift',
    '.rb': 'ruby', '.php': 'php', '.kt': 'kotlin'}


class EmbeddingProvider(Protocol):
    """A bounded provider returns one finite vector per input string."""
    def descriptor(self) -> Mapping[str, Any]: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[tuple[int, str]] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'svg', 'iframe'}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'svg', 'iframe'} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append((self.getpos()[0], data))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return False


def _vector(values: Any, expected_dim: int | None = None) -> list[float]:
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 8192
            or (expected_dim is not None and len(values) != expected_dim)):
        raise AgentError('Embedding vector dimensions are invalid or incompatible.')
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise AgentError('Embedding provider returned a non-finite vector value.')
        result.append(float(value))
    norm = math.sqrt(sum(value * value for value in result))
    if norm <= 1e-12:
        raise AgentError('Embedding provider returned a zero vector.')
    return [value / norm for value in result]


def _pack_vector(values: Sequence[float]) -> bytes:
    return struct.pack('<' + 'f' * len(values), *values)


def _unpack_vector(value: bytes, dimensions: int) -> list[float]:
    if not isinstance(value, bytes) or dimensions < 1 or len(value) != dimensions * 4:
        raise AgentError('Stored embedding vector is malformed.')
    return list(struct.unpack('<' + 'f' * dimensions, value))


def _process_alive(pid: int, expected_start: str | None = None) -> bool:
    """Fail closed on PID reuse/permission ambiguity; test the actual Windows process handle."""
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            wait_status = kernel.WaitForSingleObject(handle, 0)
            if wait_status == 0:
                return False
            if wait_status != 0x00000102:
                return True
            alive = True
            if alive and expected_start:
                observed = _process_start_token(pid)
                if observed is not None and observed != expected_start:
                    return False
            return alive
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        if expected_start:
            observed = _process_start_token(pid)
            if observed is not None and observed != expected_start:
                return False
        return True
    except PermissionError:
        return True
    except OSError as exc:
        if getattr(exc, 'errno', None) == errno.ESRCH:
            return False
        return True


def _process_start_token(pid: int) -> str | None:
    """Best-effort OS process creation identity, used to distinguish Windows PID reuse."""
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        class FILETIME(ctypes.Structure):
            _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        kernel.GetProcessTimes.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in
                    (created, exited, kernel_time, user_time)]):
                return None
            return f'{created.high:08x}{created.low:08x}'
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text(encoding='ascii')
        return stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


@contextmanager
def _writer_lock(path: Path, owner: str):
    """Atomic process-identity lock, left intact after abrupt process termination."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentError('Knowledge index has an active or unreconciled writer lock.') from exc
    token = encoded({'owner': owner, 'pid': os.getpid(), 'nonce': uuid.uuid4().hex,
                     'process_start': _process_start_token(os.getpid())})
    try:
        os.write(fd, token)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    try:
        yield
    finally:
        try:
            if path.exists() and path.read_bytes() == token:
                path.unlink()
        except OSError:
            pass


def _content_text(raw: bytes, suffix: str) -> str:
    try:
        text = raw.decode('utf-8-sig', errors='strict')
    except UnicodeDecodeError as exc:
        raise AgentError('Knowledge source is not valid UTF-8 text.') from exc
    if suffix in {'.html', '.htm'}:
        parser = _HTMLText()
        parser.feed(text)
        visible: dict[int, list[str]] = {}
        for first_line, content in parser.parts:
            for offset, line in enumerate(content.splitlines() or [content]):
                if line.strip():
                    visible.setdefault(first_line + offset, []).append(line.strip())
        source_lines = text.splitlines()
        text = '\n'.join(' '.join(visible.get(number, []))
                         for number in range(1, max(1, len(source_lines)) + 1))
    if suffix == '.json':
        # Validate that a JSON source is well formed, while preserving line references.
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentError('JSON knowledge source is malformed.') from exc
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip():
        raise AgentError('Knowledge source contains no indexable text.')
    no_secrets(text)
    return text


def _split_oversized(text: str, start_line: int, limit: int = MAX_CHUNK_CHARS) -> list[tuple[str, int, int]]:
    lines = text.splitlines() or [text]
    rows = []
    current: list[str] = []
    current_start = start_line
    current_size = 0
    line_no = start_line
    for line in lines:
        if len(line) > limit:
            if current:
                value = '\n'.join(current).strip()
                if value:
                    rows.append((value, current_start, line_no - 1))
                current, current_size = [], 0
            offset = 0
            while offset < len(line):
                piece = line[offset:offset + limit]
                rows.append((piece, line_no, line_no))
                offset += max(1, limit - CHUNK_OVERLAP_CHARS)
            current_start = line_no + 1
        elif current_size + len(line) + 1 > limit and current:
            value = '\n'.join(current).strip()
            if value:
                rows.append((value, current_start, line_no - 1))
            overlap = []
            size = 0
            for prior in reversed(current):
                if size + len(prior) + 1 > CHUNK_OVERLAP_CHARS:
                    break
                overlap.insert(0, prior)
                size += len(prior) + 1
            current = overlap
            current_start = max(start_line, line_no - len(overlap))
            current.append(line)
            current_size = size + len(line) + 1
        else:
            if not current:
                current_start = line_no
            current.append(line)
            current_size += len(line) + 1
        line_no += 1
    value = '\n'.join(current).strip()
    if value:
        rows.append((value, current_start, line_no - 1))
    return rows


def chunk_document(text: str, suffix: str) -> list[dict[str, Any]]:
    """Deterministic section/function-aware chunks with stable line references."""
    if not isinstance(text, str) or len(text) > MAX_DOCUMENT_BYTES:
        raise AgentError('Document text is absent or exceeds its size limit.')
    lines = text.splitlines()
    groups: list[tuple[str, int, int, str]] = []
    if suffix in {'.md', '.markdown'}:
        start, heading, section = 0, '', []
        for index, line in enumerate(lines):
            match = re.match(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$', line)
            if match and section:
                groups.append(('\n'.join(section), start + 1, index, heading))
                start, section = index, []
            if match:
                heading = match.group(2).strip()[:180]
            section.append(line)
        if section:
            groups.append(('\n'.join(section), start + 1, len(lines), heading))
    elif suffix == '.py':
        try:
            import ast
            tree = ast.parse(text)
            nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef,
                ast.AsyncFunctionDef, ast.ClassDef))]
            cursor = 1
            for node in nodes:
                first = max(cursor, getattr(node, 'lineno', cursor))
                if first > cursor:
                    groups.append(('\n'.join(lines[cursor - 1:first - 1]), cursor, first - 1, 'module'))
                end = getattr(node, 'end_lineno', node.lineno)
                label = getattr(node, 'name', 'module')
                groups.append(('\n'.join(lines[first - 1:end]), first, end, label))
                cursor = end + 1
            if cursor <= len(lines):
                groups.append(('\n'.join(lines[cursor - 1:]), cursor, len(lines), 'module'))
        except (SyntaxError, ValueError):
            groups = []
    elif suffix in {'.js', '.jsx', '.ts', '.tsx'}:
        boundaries = [i for i, line in enumerate(lines) if re.match(
            r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|let)\s+\w+', line)]
        if boundaries:
            if boundaries[0] > 0:
                groups.append(('\n'.join(lines[:boundaries[0]]), 1, boundaries[0], 'module'))
            for i, start in enumerate(boundaries):
                end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
                groups.append(('\n'.join(lines[start:end]), start + 1, end,
                               lines[start].strip()[:180]))
    if not groups:
        visible = [index for index, line in enumerate(lines, 1) if line.strip()]
        first = visible[0] if visible else 1
        last = visible[-1] if visible else max(1, len(lines))
        groups = [(text, first, last, '')]
    output = []
    for content, first, last, section in groups:
        if len(content) <= MAX_CHUNK_CHARS:
            if content.strip():
                output.append({'text': content.strip(), 'line_start': first,
                               'line_end': last, 'section': section})
            continue
        for piece, part_start, part_end in _split_oversized(content, first):
            output.append({'text': piece, 'line_start': part_start,
                           'line_end': part_end, 'section': section})
    if len(output) > MAX_CHUNKS:
        raise AgentError('Document creates more than the chunk limit.')
    return output


class KnowledgeIndex:
    """One exact Git project, one private SQLite index and explicit source allowlist."""
    def __init__(self, project: Project, *, storage_owner: Project | None = None,
                 storage_namespace: str | None = None):
        if not isinstance(project, Project):
            raise AgentError('Knowledge index requires a validated Project root.')
        if (storage_owner is None) != (storage_namespace is None):
            raise AgentError('External knowledge storage requires an owner and namespace.')
        if storage_owner is not None and (not isinstance(storage_owner, Project)
                or not isinstance(storage_namespace, str)
                or not re.fullmatch(r'[a-f0-9]{64}', storage_namespace)):
            raise AgentError('External knowledge storage identity is invalid.')
        self.project = project
        self.root = project.root
        self.storage_owner = storage_owner
        self.private = storage_owner.root / '.local' if storage_owner else self.root / '.local'
        self.directory = (self.private / 'project-intelligence' / 'indexes' / storage_namespace / 'knowledge'
                          if storage_owner else self.private / 'knowledge')
        self.database = self.directory / 'index.sqlite3'
        self.sources_file = self.directory / 'sources.json'
        self.lock = self.directory / 'index.lock'

    def _check_private(self, create: bool = False) -> None:
        owner_root = self.storage_owner.root if self.storage_owner else self.root
        if _is_reparse(self.private) or not self.private.resolve().is_relative_to(owner_root):
            raise AgentError('Knowledge state root is linked or outside the project.')
        if create:
            relative_directory = self.directory.relative_to(owner_root).as_posix()
            ignored = (bool(self.storage_owner.git('check-ignore', '--',
                        relative_directory + '/index.sqlite3').strip()) if self.storage_owner
                       else bool(self.project.git('check-ignore', '--', '.local/knowledge/index.sqlite3').strip()))
            if not ignored:
                raise AgentError('Knowledge index is not Git-ignored; private data would be exposed.')
            self.directory.mkdir(parents=True, exist_ok=True)
        current = self.private
        for part in self.directory.relative_to(self.private).parts:
            current = current / part
            if _is_reparse(current):
                raise AgentError('Knowledge index path contains a reparse point.')
        if self.directory.exists() and not self.directory.resolve().is_relative_to(self.private.resolve()):
            raise AgentError('Knowledge index directory is linked or outside private storage.')
        for path in (self.database, self.sources_file, self.lock):
            if _is_reparse(path) or (path.exists() and path.is_file() and path.stat().st_nlink > 1):
                raise AgentError('Linked or hard-linked knowledge state is rejected.')

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        db_path = path or self.database
        self._check_private(create=True)
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=5000')
        try:
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA temp_store=MEMORY')
            if not db_path.exists() or db_path.stat().st_size == 0:
                self._schema(connection)
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise AgentError('Knowledge index schema is invalid; preserve it and use rebuild after review.') from exc
        return connection

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript('''
            PRAGMA user_version=1;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL, status TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, indexed_at TEXT NOT NULL,
                duplicate_of TEXT, language TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, section TEXT NOT NULL, line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                embedding BLOB, dimensions INTEGER, embedding_model TEXT,
                UNIQUE(doc_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS chunks_doc_id ON chunks(doc_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                chunk_id UNINDEXED, content, tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS library_documents (
                doc_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('global','conversation')),
                conversation_id TEXT, title TEXT NOT NULL, original_name TEXT NOT NULL,
                extension TEXT NOT NULL, mime_type TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL,
                page_count INTEGER, status TEXT NOT NULL, duplicate_of TEXT,
                semantic_status TEXT NOT NULL DEFAULT 'requires_reindex',
                document_version INTEGER NOT NULL DEFAULT 1, version_of TEXT,
                created_at TEXT NOT NULL, original BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS library_documents_scope
                ON library_documents(project_id,scope,conversation_id,status);
            CREATE INDEX IF NOT EXISTS library_documents_hash
                ON library_documents(project_id,content_sha256);
            CREATE TABLE IF NOT EXISTS library_chunks (
                chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL
                    REFERENCES library_documents(doc_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, section TEXT NOT NULL,
                page_number INTEGER, line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL, content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, embedding BLOB, embedding_dimensions INTEGER,
                embedding_provider TEXT, embedding_model TEXT, embedding_version TEXT,
                embedding_digest TEXT, UNIQUE(doc_id,ordinal)
            );
            CREATE INDEX IF NOT EXISTS library_chunks_doc_id ON library_chunks(doc_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
                chunk_id UNINDEXED, content, tokenize='unicode61'
            );
            INSERT INTO metadata(key,value) VALUES('library_schema_version','3')
                ON CONFLICT(key) DO NOTHING;
        ''')

    def _sources(self) -> dict:
        self._check_private()
        if not self.sources_file.exists():
            return {'schema_version': SCHEMA_VERSION, 'project_id': self.project.identity, 'sources': []}
        if self.sources_file.stat().st_size > 1024 * 1024:
            raise AgentError('Knowledge source allowlist exceeds its size limit.')
        try:
            value = json.loads(self.sources_file.read_bytes())
        except (OSError, ValueError) as exc:
            raise AgentError('Knowledge source allowlist is malformed; preserve it for recovery.') from exc
        if (not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION
                or value.get('project_id') != self.project.identity
                or not isinstance(value.get('sources'), list) or len(value['sources']) > MAX_SOURCES):
            raise AgentError('Knowledge source allowlist schema or project identity is invalid.')
        seen = set()
        for row in value['sources']:
            if (not isinstance(row, dict) or set(row) != {'path', 'source_type', 'added_at'}
                    or not isinstance(row['path'], str)):
                raise AgentError('Knowledge source allowlist row is malformed.')
            self._source_kind(row['source_type'])
            canonical = self._safe_source(row['path'], require_exists=False).relative_to(self.root).as_posix()
            if canonical != row['path'] or canonical.casefold() in seen:
                raise AgentError('Knowledge source allowlist paths are noncanonical or duplicated.')
            seen.add(canonical.casefold())
        return value

    @staticmethod
    def _source_kind(value: str) -> str:
        if value not in {'project_documentation', 'curated_reference'}:
            raise AgentError('Source kind must be project_documentation or curated_reference.')
        return value

    def _safe_source(self, relative: str, *, require_exists: bool = True) -> Path:
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise AgentError('Knowledge source path is missing or too long.')
        target = self.project.path(relative, allow_instructions=True)
        relative_path = target.relative_to(self.root)
        for part in relative_path.parts:
            if part.casefold() in BLOCKED_DIRS or part.casefold().startswith('.env'):
                raise AgentError('Private, generated, temporary, or environment paths cannot be indexed.')
        current = self.root
        for part in relative_path.parts:
            current = current / part
            if _is_reparse(current):
                raise AgentError('Knowledge sources cannot contain symlinks or reparse points.')
        if require_exists and (not target.exists() or not (target.is_file() or target.is_dir())):
            raise AgentError('Knowledge source must be an existing file or directory under the project root.')
        if target.exists() and not (target.is_file() or target.is_dir()):
            raise AgentError('Knowledge source must be a regular file or directory.')
        if target.is_file() and target.stat().st_nlink > 1:
            raise AgentError('Hard-linked knowledge source files are rejected.')
        return target

    def add_source(self, relative_path: str, *, source_type: str = 'project_documentation') -> dict:
        source_type = self._source_kind(source_type)
        target = self._safe_source(relative_path)
        relative = target.relative_to(self.root).as_posix()
        self._check_private(create=True)
        with _writer_lock(self.lock, 'knowledge-source-add:' + uuid.uuid4().hex):
            value = self._sources()
            if any(row['path'].casefold() == relative.casefold() for row in value['sources']):
                raise AgentError('Knowledge source is already allowlisted.')
            if len(value['sources']) >= MAX_SOURCES:
                raise AgentError('Knowledge source allowlist reached its limit.')
            value['sources'].append({'path': relative, 'source_type': source_type,
                                     'added_at': _now()})
            atomic_write(self.sources_file, encoded(value))
        return {'added': True, 'path': relative, 'source_type': source_type}

    def replace_sources(self, relative_paths: Sequence[str], *,
                        source_type: str = 'project_documentation') -> dict:
        """Replace the bounded allowlist with validated regular files under this project."""
        source_type = self._source_kind(source_type)
        if (not isinstance(relative_paths, (list, tuple)) or len(relative_paths) > MAX_SOURCES
                or any(not isinstance(value, str) for value in relative_paths)):
            raise AgentError('Project source inventory exceeds its fixed bound.')
        normalized = []
        seen = set()
        for value in relative_paths:
            target = self._safe_source(value)
            if not target.is_file():
                raise AgentError('Project source inventory may contain files only.')
            relative = target.relative_to(self.root).as_posix()
            if relative.casefold() in seen:
                continue
            seen.add(relative.casefold())
            normalized.append({'path': relative, 'source_type': source_type, 'added_at': _now()})
        self._check_private(create=True)
        with _writer_lock(self.lock, 'knowledge-source-replace:' + uuid.uuid4().hex):
            atomic_write(self.sources_file, encoded({'schema_version': SCHEMA_VERSION,
                'project_id': self.project.identity, 'sources': normalized}))
        return {'sources': len(normalized)}

    def list_sources(self) -> list[dict]:
        return self._sources()['sources']

    def _walk(self, sources: list[dict]) -> tuple[list[dict], list[str]]:
        candidates: dict[str, str] = {}
        skipped = []
        for source in sources:
            try:
                base = self._safe_source(source['path'], require_exists=False)
            except AgentError:
                skipped.append(source['path'] + ':missing_or_unsafe')
                continue
            if not base.exists():
                skipped.append(source['path'] + ':missing')
                continue
            rows = [base] if base.is_file() else []
            if base.is_dir():
                stack = [base]
                while stack:
                    folder = stack.pop()
                    try:
                        entries = sorted(os.scandir(folder), key=lambda item: item.name.casefold(), reverse=True)
                    except OSError:
                        raise AgentError('An allowlisted knowledge directory cannot be read; index changes were not applied.')
                    for entry in entries:
                        item = Path(entry.path)
                        name = entry.name.casefold()
                        if entry.is_symlink() or _is_reparse(item):
                            skipped.append(item.relative_to(self.root).as_posix() + ':linked')
                        elif entry.is_dir(follow_symlinks=False):
                            if name not in BLOCKED_DIRS and not name.startswith('.env'):
                                stack.append(item)
                        elif entry.is_file(follow_symlinks=False):
                            rows.append(item)
            for path in rows:
                relative = path.relative_to(self.root).as_posix()
                suffix, name = path.suffix.casefold(), path.name.casefold()
                if (suffix not in SUPPORTED_EXTENSIONS or name in BLOCKED_FILES
                        or suffix in BLOCKED_SUFFIXES or name.startswith('.env')
                        or any(part.casefold() in BLOCKED_DIRS or part.casefold().startswith('.env')
                               for part in path.relative_to(self.root).parts[:-1])):
                    skipped.append(relative + ':excluded')
                    continue
                candidates[relative] = source['source_type']
        if len(candidates) > MAX_DOCUMENTS:
            raise AgentError('Allowlisted sources exceed the document-count limit.')
        return ([{'path': path, 'source_type': candidates[path]} for path in sorted(candidates,
            key=str.casefold)], skipped[:100])

    def _read_document(self, row: Mapping[str, str]) -> dict:
        path = self._safe_source(row['path'])
        if not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise AgentError('Knowledge document is not a regular supported-size file.')
        raw = path.read_bytes()
        text = _content_text(raw, path.suffix.casefold())
        return {'path': row['path'], 'source_type': row['source_type'], 'absolute_path': path,
                'raw': raw, 'text': text, 'sha256': hashlib.sha256(raw).hexdigest(),
                'byte_size': len(raw), 'modified_ns': path.stat().st_mtime_ns,
                'language': LANGUAGE_BY_SUFFIX[path.suffix.casefold()],
                'chunks': chunk_document(text, path.suffix.casefold())}

    def _connect_initialized(self) -> sqlite3.Connection | None:
        self._check_private()
        if not self.database.exists():
            return None
        try:
            connection = self._connect()
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            project_id = self._meta(connection, 'project_id')
            empty_unclaimed = (project_id is None and
                connection.execute('SELECT count(*) FROM documents').fetchone()[0] == 0 and
                connection.execute('SELECT count(*) FROM chunks').fetchone()[0] == 0)
            if version != SCHEMA_VERSION or (project_id != self.project.identity and not empty_unclaimed):
                raise AgentError('Knowledge index schema or project identity mismatch; preserve and rebuild.')
            return connection
        except sqlite3.DatabaseError as exc:
            if 'connection' in locals():
                connection.close()
            raise AgentError('Knowledge index is corrupt; preserve it and run an explicit rebuild.') from exc
        except BaseException:
            if 'connection' in locals():
                connection.close()
            raise

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute('INSERT INTO metadata(key,value) VALUES(?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    @staticmethod
    def _doc_id(project_id: str, relative_path: str) -> str:
        return hashlib.sha256((project_id + '\0' + relative_path.casefold()).encode()).hexdigest()

    def _embed_documents(self, documents: list[dict], provider: EmbeddingProvider | None,
                         expected_descriptor: Mapping[str, Any] | None = None) -> tuple[dict, int | None]:
        descriptor = dict(provider.descriptor()) if provider else {}
        comparable_expected = ({key: value for key, value in expected_descriptor.items()
                                if key not in {'dimensions', 'normalized'}}
                               if expected_descriptor is not None else None)
        if provider and comparable_expected is not None and comparable_expected != descriptor:
            raise AgentError('Embedding model identity differs from the index; rebuild with one compatible model.')
        dimension = None
        if provider:
            texts = [chunk['text'] for doc in documents for chunk in doc['chunks']]
            vectors: list[list[float]] = []
            for offset in range(0, len(texts), 8):
                batch = provider.embed(texts[offset:offset + 8])
                if not isinstance(batch, list) or len(batch) != len(texts[offset:offset + 8]):
                    raise AgentError('Embedding provider returned an incomplete batch.')
                for vector in batch:
                    normalized = _vector(vector, dimension)
                    dimension = dimension or len(normalized)
                    vectors.append(normalized)
            position = 0
            for doc in documents:
                doc['vectors'] = vectors[position:position + len(doc['chunks'])]
                position += len(doc['chunks'])
            descriptor['dimensions'] = dimension
            descriptor['normalized'] = True
        else:
            for doc in documents:
                doc['vectors'] = [None] * len(doc['chunks'])
        return descriptor, dimension

    def _write_document(self, connection: sqlite3.Connection, doc: dict,
                        project_id: str, descriptor: Mapping[str, Any]) -> None:
        doc_id = self._doc_id(project_id, doc['path'])
        connection.execute('DELETE FROM chunk_fts WHERE chunk_id IN '
                           '(SELECT chunk_id FROM chunks WHERE doc_id=?)', (doc_id,))
        connection.execute('DELETE FROM documents WHERE doc_id=?', (doc_id,))
        duplicate = connection.execute('SELECT relative_path FROM documents WHERE content_sha256=? '
            'AND relative_path<>? AND status="current" ORDER BY relative_path LIMIT 1',
            (doc['sha256'], doc['path'])).fetchone()
        connection.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (doc_id, project_id, doc['path'], doc['source_type'], 'current', doc['sha256'],
             doc['byte_size'], doc['modified_ns'], _now(), duplicate['relative_path'] if duplicate else None,
             doc['language']))
        for index, (chunk, vector) in enumerate(zip(doc['chunks'], doc['vectors'])):
            chunk_hash = hashlib.sha256(chunk['text'].encode('utf-8')).hexdigest()
            chunk_id = hashlib.sha256(f"{doc_id}\0{index}\0{chunk_hash}".encode()).hexdigest()
            embedding = _pack_vector(vector) if vector is not None else None
            dimensions = len(vector) if vector is not None else None
            model_id = descriptor.get('model_id') if vector is not None else None
            connection.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (chunk_id, doc_id, index, chunk['section'], chunk['line_start'], chunk['line_end'],
                 chunk['text'], chunk_hash, embedding, dimensions, model_id))
            connection.execute('INSERT INTO chunk_fts(chunk_id,content) VALUES(?,?)',
                               (chunk_id, chunk['text']))

    def _prepare_documents(self, rows: list[dict], *, existing: sqlite3.Connection | None = None,
                            force: bool = False) -> tuple[list[dict], list[str], int]:
        known = {}
        if existing:
            known = {row['relative_path']: row for row in existing.execute(
                'SELECT relative_path,content_sha256,source_type,status,modified_ns FROM documents')}
        docs, skipped, total, unchanged = [], [], 0, 0
        for row in rows:
            try:
                doc = self._read_document(row)
            except (OSError, AgentError) as exc:
                skipped.append(row['path'] + ':' + str(exc)[:120])
                continue
            total += doc['byte_size']
            if total > MAX_TOTAL_BYTES:
                raise AgentError('Allowlisted source content exceeds the aggregate byte limit.')
            previous = known.get(doc['path'])
            if (not force and previous and previous['status'] == 'current'
                    and previous['content_sha256'] == doc['sha256']
                    and previous['source_type'] == doc['source_type']
                    and previous['modified_ns'] == doc['modified_ns']):
                unchanged += 1
                continue
            docs.append(doc)
        return docs, skipped[:100], total, unchanged

    def index(self, *, provider: EmbeddingProvider | None = None, rebuild: bool = False) -> dict:
        self._check_private(create=True)
        sources = self._sources()
        rows, excluded = self._walk(sources['sources'])
        with _writer_lock(self.lock, 'knowledge-index:' + uuid.uuid4().hex):
            previous_index_corrupt = False
            try:
                current = self._connect_initialized()
            except AgentError as exc:
                if not rebuild or not str(exc).startswith('Knowledge index is corrupt;'):
                    raise
                # The active bytes are retained in recovery below. A corrupt DB
                # cannot safely supply its uploaded-document tables to staging.
                current = None
                previous_index_corrupt = True
            staging = None
            target = self.database
            if rebuild:
                staging = self.directory / ('index.rebuild.' + uuid.uuid4().hex + '.sqlite3')
                target = staging
                connection = self._connect(target)
                existing = None
                force = True
            else:
                connection = current or self._connect()
                existing = current
                force = False
                if existing is not None:
                    old_identity = self._meta(existing, 'embedding_identity')
                    if old_identity:
                        old = json.loads(old_identity)
                        if provider is None:
                            connection.close()
                            raise AgentError('This index uses embeddings; provide the same model or use rebuild.')
                        if dict(provider.descriptor()) != {key: value for key, value in old.items()
                                if key not in {'dimensions', 'normalized'}}:
                            connection.close()
                            raise AgentError('Embedding model identity differs; rebuild before changing models.')
                    elif provider and existing.execute('SELECT count(*) FROM documents').fetchone()[0]:
                        connection.close()
                        raise AgentError('Add embeddings with an explicit full rebuild to avoid mixed index vectors.')
            try:
                docs, skipped, total, unchanged = self._prepare_documents(rows, existing=existing, force=force)
                if provider and sum(len(doc['chunks']) for doc in docs) > MAX_EMBEDDING_CHUNKS:
                    raise AgentError(f'Embedding work exceeds the {MAX_EMBEDDING_CHUNKS}-chunk bound; use keyword-only indexing or a smaller explicit source set.')
                descriptor, dimension = self._embed_documents(docs, provider,
                    json.loads(self._meta(connection, 'embedding_identity'))
                    if not rebuild and self._meta(connection, 'embedding_identity') else None)
                previous_identity = (json.loads(self._meta(connection, 'embedding_identity'))
                                     if not rebuild and self._meta(connection, 'embedding_identity') else None)
                if provider and dimension is None and previous_identity:
                    descriptor = previous_identity
                    dimension = previous_identity.get('dimensions')
                connection.execute('BEGIN IMMEDIATE')
                if not self._meta(connection, 'project_id'):
                    self._set_meta(connection, 'project_id', self.project.identity)
                if provider and dimension is not None and not self._meta(connection, 'embedding_identity'):
                    self._set_meta(connection, 'embedding_identity', encoded(descriptor).decode())
                for doc in docs:
                    self._write_document(connection, doc, self.project.identity, descriptor)
                # Remove documents no longer present in any current allowlisted root, but
                # retain the source files themselves and count their IDs in the result.
                seen = {row['path'] for row in rows}
                removed = []
                for old in connection.execute('SELECT doc_id,relative_path FROM documents').fetchall():
                    if old['relative_path'] not in seen:
                        connection.execute('DELETE FROM chunk_fts WHERE chunk_id IN '
                                           '(SELECT chunk_id FROM chunks WHERE doc_id=?)', (old['doc_id'],))
                        connection.execute('DELETE FROM documents WHERE doc_id=?', (old['doc_id'],))
                        removed.append(old['relative_path'])
                connection.commit()
                if rebuild and current is not None:
                    connection.execute('ATTACH DATABASE ? AS preserved_library', (str(self.database),))
                    tables = {row['name'] for row in connection.execute(
                        "SELECT name FROM preserved_library.sqlite_master WHERE type IN ('table','view')").fetchall()}
                    if {'library_documents', 'library_chunks', 'library_fts'} <= tables:
                        for table in ('library_documents', 'library_chunks'):
                            target_columns = [row['name'] for row in connection.execute(
                                f'PRAGMA main.table_info({table})').fetchall()]
                            old_columns = {row['name'] for row in connection.execute(
                                f'PRAGMA preserved_library.table_info({table})').fetchall()}
                            columns = [name for name in target_columns if name in old_columns]
                            column_list = ','.join(columns)
                            connection.execute(f'INSERT INTO main.{table}({column_list}) '
                                f'SELECT {column_list} FROM preserved_library.{table}')
                        connection.execute('INSERT INTO main.library_fts SELECT * FROM preserved_library.library_fts')
                        connection.execute('INSERT INTO metadata(key,value) VALUES(?,?) '
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            ('library_schema_version', '3'))
                    connection.commit()
                    connection.execute('DETACH DATABASE preserved_library')
                    current.close()
                    current = None
                if rebuild:
                    check = connection.execute('PRAGMA integrity_check').fetchone()[0]
                    if check != 'ok':
                        raise AgentError(f'Rebuilt index failed integrity check: {check}')
                    connection.close()
                    connection = None
                    if self.database.exists():
                        backup_dir = self.directory / 'recovery'
                        backup_dir.mkdir(exist_ok=True)
                        backup = backup_dir / ('index-before-rebuild-' + uuid.uuid4().hex + '.sqlite3')
                        import shutil
                        shutil.copy2(self.database, backup)
                    os.replace(staging, self.database)
                    staging = None
                else:
                    connection.close()
                    connection = None
                return {'indexed': len(docs), 'unchanged': unchanged,
                    'removed_from_index': removed, 'excluded_or_skipped': excluded + skipped,
                    'documents_seen': len(rows), 'bytes_read': total,
                    'embedding': descriptor if dimension is not None else None,
                    'embedding_dimensions': dimension,
                    'mode': 'hybrid_ready' if provider and dimension is not None else 'keyword_only',
                    'previous_index_corrupt_preserved': previous_index_corrupt,
                    'rebuilt': rebuild}
            except BaseException:
                if connection is not None:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()
                if current is not None:
                    current.close()
                raise
            finally:
                if current is not None:
                    current.close()
                if staging is not None and staging.exists():
                    staging.unlink()

    def _verify_doc(self, row: sqlite3.Row) -> bool:
        try:
            path = self._safe_source(row['relative_path'])
            expected = (row['document_sha256'] if 'document_sha256' in row.keys()
                        else row['content_sha256'])
            return (path.is_file() and path.stat().st_size == row['byte_size']
                    and file_hash(path) == expected)
        except (OSError, AgentError):
            return False

    def _mark_stale(self, doc_ids: set[str]) -> None:
        if not doc_ids:
            return
        with _writer_lock(self.lock, 'knowledge-mark-stale:' + uuid.uuid4().hex):
            connection = self._connect_initialized()
            if connection is None:
                return
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.executemany('UPDATE documents SET status="stale" WHERE doc_id=?',
                                       [(doc_id,) for doc_id in doc_ids])
                connection.commit()
            finally:
                connection.close()

    def _query_vector(self, provider: EmbeddingProvider | None, identity: dict | None,
                      query: str) -> tuple[list[float] | None, str]:
        if identity is None:
            return None, 'not_configured'
        if provider is None:
            try:
                from knowledge_embeddings import OllamaEmbeddingProvider
                provider = OllamaEmbeddingProvider(identity['model_id'])
            except (AgentError, KeyError, ValueError):
                return None, 'unavailable_keyword_fallback'
        try:
            descriptor = dict(provider.descriptor())
            expected = {key: value for key, value in identity.items()
                        if key not in {'dimensions', 'normalized'}}
            if descriptor != expected:
                return None, 'incompatible_model_keyword_fallback'
            vectors = provider.embed([query])
            return _vector(vectors[0], identity['dimensions']), 'available'
        except (AgentError, OSError, ValueError, KeyError, IndexError, TypeError):
            # Embedding is optional; provider errors never change retrieval scope.
            return None, 'provider_failed_keyword_fallback'

    def search(self, query: str, *, limit: int = 8,
               provider: EmbeddingProvider | None = None,
               semantic_min_similarity: float = 0.0) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > 2000:
            raise AgentError('Knowledge query must contain 1 to 2000 UTF-8 bytes.')
        no_secrets(query)
        if type(limit) is not int or not 1 <= limit <= 12:
            raise AgentError('Knowledge result limit must be between 1 and 12.')
        if (isinstance(semantic_min_similarity, bool) or not isinstance(semantic_min_similarity, (int, float))
                or not math.isfinite(semantic_min_similarity) or not 0.0 <= semantic_min_similarity <= 1.0):
            raise AgentError('Semantic relevance threshold is invalid.')
        started = time.monotonic()
        connection = self._connect_initialized()
        if connection is None:
            return {'results': [], 'strategy': 'keyword', 'embedding_status': 'not_configured',
                    'latency_ms': round((time.monotonic() - started) * 1000, 3)}
        stale = set()
        try:
            identity_raw = self._meta(connection, 'embedding_identity')
            identity = json.loads(identity_raw) if identity_raw else None
            terms = list(dict.fromkeys(re.findall(r'[A-Za-z0-9_]{2,}', query.casefold())))[:24]
            keyword_rows = []
            if terms:
                expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
                try:
                    keyword_rows = connection.execute('''SELECT c.chunk_id,c.doc_id,c.ordinal,c.section,
                        c.line_start,c.line_end,c.content,c.content_sha256,d.relative_path,d.source_type,
                        d.status,d.content_sha256 AS document_sha256,d.modified_ns,d.byte_size,d.language,
                        bm25(chunk_fts) AS rank
                        FROM chunk_fts JOIN chunks c ON c.chunk_id=chunk_fts.chunk_id
                        JOIN documents d ON d.doc_id=c.doc_id
                        WHERE chunk_fts MATCH ? AND d.project_id=? AND d.status='current'
                        ORDER BY rank LIMIT ?''', (expression, self.project.identity, MAX_CANDIDATES)).fetchall()
                except sqlite3.DatabaseError as exc:
                    raise AgentError('Knowledge keyword index is corrupt; verify or rebuild it.') from exc
            query_vector, embed_status = self._query_vector(provider, identity, query)
            semantic_rows = []
            if query_vector is not None:
                stored = connection.execute('''SELECT c.chunk_id,c.doc_id,c.ordinal,c.section,
                    c.line_start,c.line_end,c.content,c.content_sha256,d.relative_path,d.source_type,
                    d.status,d.content_sha256 AS document_sha256,d.modified_ns,d.byte_size,d.language,
                    c.embedding,c.dimensions FROM chunks c JOIN documents d ON d.doc_id=c.doc_id
                    WHERE d.project_id=? AND d.status='current' AND c.embedding IS NOT NULL
                    AND c.embedding_model=? LIMIT ?''', (self.project.identity, identity['model_id'],
                    MAX_CHUNKS + 1)).fetchall()
                if len(stored) > MAX_CHUNKS:
                    raise AgentError('Semantic candidate set exceeds its configured bound.')
                scored = []
                for row in stored:
                    vector = _unpack_vector(row['embedding'], row['dimensions'])
                    if row['dimensions'] != len(query_vector):
                        raise AgentError('Stored vector dimensions differ from the active embedding model.')
                    score = sum(a * b for a, b in zip(query_vector, vector))
                    if score >= semantic_min_similarity:
                        scored.append((score, row))
                semantic_rows = [row for _, row in sorted(scored, key=lambda pair: (-pair[0], pair[1]['chunk_id']))[:MAX_CANDIDATES]]
            ranks: dict[str, tuple[sqlite3.Row, float]] = {}
            for rows in (keyword_rows, semantic_rows):
                for rank, row in enumerate(rows, 1):
                    old = ranks.get(row['chunk_id'], (row, 0.0))[1]
                    ranks[row['chunk_id']] = (row, old + 1.0 / (RRF_K + rank))
            ordered = sorted(ranks.values(), key=lambda item: (-item[1], item[0]['relative_path'],
                                                               item[0]['line_start']))
            results = []
            for row, score in ordered:
                if not self._verify_doc(row):
                    stale.add(row['doc_id'])
                    continue
                results.append({'chunk_id': row['chunk_id'], 'path': row['relative_path'],
                    'source_type': row['source_type'], 'trust': 'untrusted_reference_data',
                    'evidence_state': 'fresh_hash_verified', 'section': row['section'],
                    'line_start': row['line_start'], 'line_end': row['line_end'],
                    'language': row['language'], 'content_sha256': row['content_sha256'],
                    'document_sha256': row['document_sha256'], 'content': row['content'][:MAX_RESULT_CHARS],
                    'relevance': round(score, 6), 'rank_method': 'reciprocal_rank_fusion'})
                if len(results) >= limit:
                    break
        finally:
            connection.close()
        if stale:
            self._mark_stale(stale)
        return {'results': results, 'strategy': 'hybrid' if query_vector is not None else 'keyword',
                'embedding_status': embed_status,
                'latency_ms': round((time.monotonic() - started) * 1000, 3)}

    def status(self) -> dict:
        self._check_private()
        sources = self._sources()['sources']
        if not self.database.exists():
            return {'initialized': False, 'project_id': self.project.identity,
                    'sources': len(sources), 'documents': 0, 'chunks': 0,
                    'embedding': None, 'integrity': 'not_created',
                    'recovery': self.recovery_status()}
        connection = self._connect_initialized()
        try:
            check = connection.execute('PRAGMA integrity_check').fetchone()[0]
            return {'initialized': True, 'project_id': self.project.identity,
                'sources': len(sources), 'documents': connection.execute('SELECT count(*) FROM documents').fetchone()[0],
                'current_documents': connection.execute('SELECT count(*) FROM documents WHERE status="current"').fetchone()[0],
                'stale_documents': connection.execute('SELECT count(*) FROM documents WHERE status="stale"').fetchone()[0],
                'chunks': connection.execute('SELECT count(*) FROM chunks').fetchone()[0],
                'embedding': json.loads(self._meta(connection, 'embedding_identity'))
                    if self._meta(connection, 'embedding_identity') else None,
                'integrity': check, 'recovery': self.recovery_status()}
        except sqlite3.DatabaseError as exc:
            raise AgentError('Knowledge index is corrupt; preserve it and run knowledge rebuild.') from exc
        finally:
            connection.close()

    def inspect(self, chunk_id: str) -> dict:
        if not isinstance(chunk_id, str) or not re.fullmatch(r'[0-9a-f]{64}', chunk_id):
            raise AgentError('Knowledge inspect requires a chunk ID.')
        connection = self._connect_initialized()
        if connection is None:
            raise AgentError('Knowledge index has not been created.')
        try:
            row = connection.execute('''SELECT c.chunk_id,c.section,c.line_start,c.line_end,
                c.content,c.content_sha256,d.relative_path,d.source_type,d.content_sha256 AS document_sha256,
                d.status,d.language,d.byte_size FROM chunks c JOIN documents d ON d.doc_id=c.doc_id
                WHERE c.chunk_id=? AND d.project_id=?''', (chunk_id, self.project.identity)).fetchone()
            if row is None:
                raise AgentError('Knowledge chunk is not present in this project index.')
            fresh = row['status'] == 'current' and self._verify_doc(row)
            return {'chunk_id': row['chunk_id'], 'path': row['relative_path'],
                'source_type': row['source_type'], 'trust': 'untrusted_reference_data',
                'evidence_state': 'fresh_hash_verified' if fresh else 'stale_or_unavailable',
                'section': row['section'], 'line_start': row['line_start'], 'line_end': row['line_end'],
                'language': row['language'], 'content_sha256': row['content_sha256'],
                'document_sha256': row['document_sha256'], 'content': row['content']}
        finally:
            connection.close()

    def verify(self) -> dict:
        connection = self._connect_initialized()
        if connection is None:
            return {'valid': True, 'documents_checked': 0, 'stale': [], 'integrity': 'not_created'}
        stale = []
        try:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise AgentError('SQLite integrity check failed; preserve the index and rebuild explicitly.')
            rows = connection.execute('SELECT * FROM documents WHERE status="current" LIMIT ?',
                                      (MAX_DOCUMENTS + 1,)).fetchall()
            if len(rows) > MAX_DOCUMENTS:
                raise AgentError('Knowledge document verification exceeded its bound.')
            for row in rows:
                if not self._verify_doc(row):
                    stale.append(row['relative_path'])
            return {'valid': not stale, 'documents_checked': len(rows), 'stale': stale,
                    'integrity': integrity}
        finally:
            connection.close()
            if stale:
                self._mark_stale({self._doc_id(self.project.identity, path) for path in stale})

    def recovery_status(self) -> dict:
        """Report interrupted index locks and staging files without modifying them."""
        self._check_private()
        lock = None
        if self.lock.exists():
            if self.lock.stat().st_size > 4096 or self.lock.is_symlink():
                return {'pending': True, 'classification': 'malformed_lock',
                        'message': 'Preserve the lock and inspect it manually.'}
            try:
                raw = self.lock.read_bytes()
                row = json.loads(raw)
                if (not isinstance(row, dict) or set(row) not in (
                        {'owner', 'pid', 'nonce'}, {'owner', 'pid', 'nonce', 'process_start'})
                        or type(row['pid']) is not int or not 0 < row['pid'] <= 2_147_483_647
                        or not isinstance(row['owner'], str) or not isinstance(row['nonce'], str)
                        or ('process_start' in row and row['process_start'] is not None
                            and not isinstance(row['process_start'], str))):
                    raise ValueError('lock fields')
                alive = _process_alive(row['pid'], row.get('process_start'))
                lock = {'pending': True, 'classification': 'active' if alive else 'stale',
                        'owner': row['owner'][:200], 'pid': row['pid'],
                        'process_start': row.get('process_start'),
                        'lock_sha256': hashlib.sha256(raw).hexdigest()}
            except (OSError, ValueError, TypeError) as exc:
                return {'pending': True, 'classification': 'malformed_lock',
                        'message': 'Preserve the lock and inspect it manually.'}
        staging = sorted(path.name for path in self.directory.glob('index.rebuild.*.sqlite3'))[:20]
        return {'pending': bool(lock), 'lock': lock, 'staging_indexes': staging,
                'staging_preserved': True}

    def recover(self, *, operator: str, confirm_stale_lock: bool = False) -> dict:
        """Clear only a confirmed dead writer's exact lock after SQLite integrity check."""
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 128:
            raise AgentError('Knowledge recovery requires a bounded operator identity.')
        status = self.recovery_status()
        if not status.get('pending'):
            return {'recovered': False, 'status': status}
        if status.get('lock', {}).get('classification') != 'stale' or not confirm_stale_lock:
            raise AgentError('Recovery requires an explicitly confirmed stale writer lock; live or malformed locks remain untouched.')
        if self.database.exists():
            connection = self._connect_initialized()
            try:
                result = connection.execute('PRAGMA integrity_check').fetchone()[0]
                if result != 'ok':
                    raise AgentError('SQLite integrity check failed; lock retained for manual review.')
            except sqlite3.DatabaseError as exc:
                raise AgentError('SQLite recovery check failed; lock retained for manual review.') from exc
            finally:
                connection.close()
        observed = self.lock.read_bytes()
        if hashlib.sha256(observed).hexdigest() != status['lock']['lock_sha256']:
            raise AgentError('Knowledge lock changed during recovery; preserve it for review.')
        current = json.loads(observed)
        if _process_alive(current['pid'], current.get('process_start')):
            raise AgentError('The lock owner is still running; lock retained.')
        recovery_dir = self.directory / 'recovery'
        if recovery_dir.exists() and (_is_reparse(recovery_dir)
                or recovery_dir.resolve().parent != self.directory.resolve()):
            raise AgentError('Knowledge recovery directory is linked or outside private state.')
        recovery_dir.mkdir(exist_ok=True)
        record = {'schema_version': SCHEMA_VERSION, 'project_id': self.project.identity,
            'operator': operator.strip(), 'owner': current['owner'], 'pid': current['pid'],
            'lock_sha256': status['lock']['lock_sha256'], 'recovered_at': _now(),
            'database_integrity': 'ok' if self.database.exists() else 'not_created',
            'staging_preserved': status['staging_indexes']}
        receipt = recovery_dir / ('stale-lock-' + uuid.uuid4().hex + '.json')
        atomic_write(receipt, encoded(record))
        if self.lock.read_bytes() != observed:
            raise AgentError('Knowledge lock changed after recovery record; lock retained.')
        self.lock.unlink()
        return {'recovered': True, 'action': 'cleared_confirmed_stale_lock',
                'receipt': str(receipt), 'staging_preserved': status['staging_indexes']}

    def remove_source(self, relative_path: str) -> dict:
        target = self._safe_source(relative_path, require_exists=False)
        relative = target.relative_to(self.root).as_posix()
        self._check_private(create=True)
        with _writer_lock(self.lock, 'knowledge-source-remove:' + uuid.uuid4().hex):
            value = self._sources()
            matches = [row for row in value['sources'] if row['path'].casefold() == relative.casefold()]
            if not matches:
                raise AgentError('Knowledge source is not allowlisted.')
            connection = self._connect_initialized()
            removed = []
            if connection is not None:
                try:
                    prefix = relative.casefold().rstrip('/')
                    rows = connection.execute('SELECT doc_id,relative_path FROM documents').fetchall()
                    doc_ids = [row['doc_id'] for row in rows if row['relative_path'].casefold() == prefix
                               or row['relative_path'].casefold().startswith(prefix + '/')]
                    connection.execute('BEGIN IMMEDIATE')
                    for doc_id in doc_ids:
                        connection.execute('DELETE FROM chunk_fts WHERE chunk_id IN '
                                           '(SELECT chunk_id FROM chunks WHERE doc_id=?)', (doc_id,))
                        connection.execute('DELETE FROM documents WHERE doc_id=?', (doc_id,))
                    connection.commit()
                    removed = [row['relative_path'] for row in rows if row['doc_id'] in set(doc_ids)]
                finally:
                    connection.close()
            value['sources'] = [row for row in value['sources'] if row['path'].casefold() != relative.casefold()]
            atomic_write(self.sources_file, encoded(value))
        return {'source_removed': relative, 'documents_removed_from_index': removed,
                'original_files_deleted': False}
