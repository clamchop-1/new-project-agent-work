"""Bounded, read-only access to the directory from which the assistant was launched."""

import os
from pathlib import Path

from agent_core import AgentError, atomic_write, file_hash, no_secrets


TEXT_EXTENSIONS = frozenset({
    '.c', '.cc', '.cfg', '.cpp', '.cs', '.css', '.csv', '.go', '.h', '.hpp',
    '.htm', '.html', '.ini', '.java', '.js', '.json', '.jsx', '.log', '.md',
    '.ps1', '.py', '.rb', '.rs', '.sh', '.sql', '.toml', '.ts', '.tsx', '.txt',
    '.xml', '.yaml', '.yml',
})
BLOCKED_DIRECTORY_NAMES = frozenset({
    '$recycle.bin', '.aws', '.claude', '.codex', '.git', '.local', '.ollama',
    '.ssh', '.venv', 'appdata', 'node_modules', 'program files',
    'program files (x86)', 'programdata', 'system volume information', 'windows',
    '__pycache__',
})
BLOCKED_FILE_NAMES = frozenset({
    'auth.json', 'credentials.json', 'id_ed25519', 'id_rsa', 'secrets.json',
})
BLOCKED_EXTENSIONS = frozenset({'.key', '.kdbx', '.p12', '.pfx', '.pem'})
MAX_FILE_BYTES = 1_000_000
MAX_PDF_BYTES = 20_000_000
MAX_PDF_PAGES = 100
MAX_PDF_PAGE_COUNT = 10
MAX_PDF_TEXT_CHARS = 30_000
MAX_DIRECTORY_ITEMS = 120
MAX_SEARCH_FILES = 2000
MAX_SEARCH_BYTES = 20_000_000
MAX_DEPTH = 16


class AssistantFiles:
    """Read-only file tools bounded to one operator-selected absolute directory."""

    def __init__(self, root):
        raw_root = Path(root)
        if not raw_root.is_absolute() or str(raw_root).startswith(('\\\\', '//')):
            raise AgentError('Assistant file root must be a local absolute path.')
        current = Path(raw_root.anchor)
        for part in raw_root.parts[1:]:
            current = current / part
            if current.is_symlink() or _is_reparse_point(current):
                raise AgentError('Assistant file root cannot pass through a symbolic link or reparse point.')
            if _is_blocked_name(part, True):
                raise AgentError('Private or system directories cannot be selected as assistant file roots.')
        try:
            self.root = raw_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AgentError('Assistant file root is unavailable.') from exc
        if not self.root.is_dir():
            raise AgentError('Assistant file root must be a directory.')

    def _path(self, relative):
        if not isinstance(relative, str) or len(relative) > 2048 or '\x00' in relative:
            raise AgentError('Invalid assistant file path.')
        requested = Path(relative.replace('\\', '/'))
        if requested.is_absolute() or requested.drive or '..' in requested.parts:
            raise AgentError('Assistant file path must stay relative to its selected root.')
        if any(':' in part for part in requested.parts):
            raise AgentError('Alternate data streams are not allowed.')

        target = self.root / requested
        current = self.root
        for part in requested.parts:
            current = current / part
            if current.is_symlink() or _is_reparse_point(current):
                raise AgentError('Linked paths are excluded from assistant file access.')
            if _is_blocked_name(part, current.is_dir() if current.exists() else False):
                raise AgentError('Private or system paths are excluded from assistant file access.')
        try:
            if not target.resolve().is_relative_to(self.root):
                raise AgentError('Resolved assistant path escapes its selected root.')
        except (OSError, RuntimeError) as exc:
            raise AgentError('Assistant path could not be resolved safely.') from exc
        if target.is_file() and target.stat().st_nlink > 1:
            raise AgentError('Hard-linked files are excluded from assistant file access.')
        return target

    def canonical_path(self, relative):
        return self._path(relative).relative_to(self.root).as_posix()

    def list_directory(self, path=''):
        directory = self._path(path)
        if not directory.is_dir():
            raise AgentError('Selected assistant path is not a directory.')
        rows = []
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda row: row.name.casefold()):
                entry_path = Path(entry.path)
                if _is_blocked_name(entry.name, entry.is_dir(follow_symlinks=False)):
                    continue
                if entry.is_symlink() or _is_reparse_point(entry_path):
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    size = 0 if is_directory else entry.stat(follow_symlinks=False).st_size
                    links = 1 if is_directory else entry.stat(follow_symlinks=False).st_nlink
                except OSError:
                    continue
                if links > 1:
                    continue
                rows.append({'name': entry.name, 'kind': 'directory' if is_directory else 'file',
                             'size_bytes': size})
                if len(rows) == MAX_DIRECTORY_ITEMS:
                    return {'root': str(self.root), 'path': path, 'entries': rows, 'truncated': True}
        return {'root': str(self.root), 'path': path, 'entries': rows, 'truncated': False}

    def find_files(self, query, limit=50):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise AgentError('File-name search needs a nonempty query of at most 200 characters.')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentError('File-name search limit must be from 1 to 100.')
        matches = []
        visited = 0
        truncated = False
        for relative, item in self._walk_files():
            visited += 1
            if query.casefold() in item.name.casefold():
                try:
                    size = item.stat().st_size
                except OSError:
                    continue
                matches.append({'path': relative, 'kind': 'file', 'size_bytes': size})
                if len(matches) == limit:
                    truncated = True
                    break
            if visited >= MAX_SEARCH_FILES:
                truncated = True
                break
        return {'root': str(self.root), 'matches': matches, 'files_checked': visited,
                'truncated': truncated}

    def search_text(self, query, limit=30):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise AgentError('Text search needs a nonempty query of at most 200 characters.')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentError('Text search limit must be from 1 to 100.')
        matches = []
        scanned = 0
        bytes_read = 0
        excluded = 0
        truncated = False
        for relative, item in self._walk_files():
            try:
                size = item.stat().st_size
            except OSError:
                excluded += 1
                continue
            if item.suffix.casefold() not in TEXT_EXTENSIONS or size > MAX_FILE_BYTES:
                continue
            if bytes_read + size > MAX_SEARCH_BYTES:
                truncated = True
                break
            scanned += 1
            bytes_read += size
            try:
                text = item.read_text(encoding='utf-8-sig')
                no_secrets(text)
            except (OSError, UnicodeError, AgentError):
                excluded += 1
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    matches.append({'path': relative, 'line': number, 'text': line[:400]})
                    if len(matches) >= limit:
                        truncated = True
                        break
            if truncated or scanned >= MAX_SEARCH_FILES:
                truncated = True
                break
        return {'root': str(self.root), 'matches': matches, 'files_checked': scanned,
                'bytes_read': bytes_read,
                'sensitive_or_unreadable_files_skipped': excluded, 'truncated': truncated}

    def read_text(self, path, start_line=1, line_count=100):
        if type(start_line) is not int or type(line_count) is not int or not 1 <= start_line <= 100000 or not 1 <= line_count <= 120:
            raise AgentError('Read requires bounded positive line numbers/count.')
        target = self._path(path)
        if target.suffix.casefold() == '.pdf':
            raise AgentError('Use the PDF reader for this file; ordinary UTF-8 text reading is not supported.')
        if target.suffix.casefold() not in TEXT_EXTENSIONS:
            raise AgentError('This file type is not supported by the text reader.')
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            raise AgentError('File is missing, not a regular file, or exceeds the 1 MB read limit.')
        try:
            text = target.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise AgentError('Text file could not be read as UTF-8.') from exc
        no_secrets(text)
        lines = text.splitlines()
        selected = '\n'.join(lines[start_line - 1:start_line - 1 + line_count])
        if len(selected.encode('utf-8')) > 16000:
            raise AgentError('Selected text passage exceeds 16 KiB.')
        return {'root': str(self.root), 'path': path, 'sha256': _sha256(target),
                'first_line': start_line, 'total_lines': len(lines), 'text': selected}

    def read_pdf(self, path, start_page=1, page_count=5):
        if type(start_page) is not int or type(page_count) is not int or start_page < 1 or not 1 <= page_count <= MAX_PDF_PAGE_COUNT:
            raise AgentError('PDF read requires a positive start page and 1 to 10 pages per request.')
        target = self._path(path)
        if target.suffix.casefold() != '.pdf' or not target.is_file():
            raise AgentError('Selected path is not a PDF file.')
        if target.stat().st_size > MAX_PDF_BYTES:
            raise AgentError('PDF exceeds the 20 MB local extraction limit.')
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise AgentError('PDF text extraction needs the local pypdf package; no package was installed.') from exc
        try:
            reader = PdfReader(str(target), strict=True)
            if reader.is_encrypted:
                raise AgentError('Encrypted PDFs are not opened by the assistant.')
            total_pages = len(reader.pages)
            if total_pages > MAX_PDF_PAGES:
                raise AgentError('PDF exceeds the 100-page local extraction limit.')
            if start_page > total_pages:
                raise AgentError('Requested start page is past the end of the PDF.')
            end_page = min(total_pages, start_page + page_count - 1)
            pages = []
            chars = 0
            for page_number in range(start_page, end_page + 1):
                text = reader.pages[page_number - 1].extract_text() or ''
                chars += len(text)
                if chars > MAX_PDF_TEXT_CHARS:
                    raise AgentError('Extracted PDF passage exceeds the 30,000-character limit.')
                pages.append({'page': page_number, 'text': text})
            no_secrets('\n'.join(row['text'] for row in pages))
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError('PDF could not be safely parsed as a text document.') from exc
        return {'root': str(self.root), 'path': path, 'sha256': _sha256(target),
                'total_pages': total_pages, 'first_page': start_page, 'last_page': end_page,
                'pages': pages, 'embedded_text_found': any(row['text'].strip() for row in pages),
                'ocr_available': False}

    def write_text(self, path, content, expected_sha256):
        target = self._path(path)
        if target.name.casefold() in {'agents.md', 'agents.override.md', 'claude.md'}:
            raise AgentError('Agent instruction files are protected from assistant writes.')
        if target.suffix.casefold() not in TEXT_EXTENSIONS:
            raise AgentError('Only supported UTF-8 text files can be edited.')
        if not isinstance(content, str) or len(content.encode('utf-8')) > 64000:
            raise AgentError('Edited content must be text and at most 64 KB.')
        if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
            raise AgentError('Existing file is missing, not regular, or exceeds the 1 MB edit limit.')
        if not isinstance(expected_sha256, str) or file_hash(target) != expected_sha256:
            raise AgentError('File changed since inspection; edit refused.')
        no_secrets(content)
        previous = expected_sha256
        atomic_write(target, content.encode('utf-8'))
        return {'path': Path(path.replace('\\', '/')).as_posix(),
                'before_sha256': previous, 'after_sha256': file_hash(target)}

    def create_text(self, path, content):
        target = self._path(path)
        if target.name.casefold() in {'agents.md', 'agents.override.md', 'claude.md'}:
            raise AgentError('Agent instruction files are protected from assistant writes.')
        if target.suffix.casefold() not in TEXT_EXTENSIONS:
            raise AgentError('Only supported UTF-8 text files can be created.')
        if not isinstance(content, str) or len(content.encode('utf-8')) > 64000:
            raise AgentError('New content must be text and at most 64 KB.')
        if target.exists():
            raise AgentError('Existing file preserved; create cannot overwrite.')
        no_secrets(content)
        try:
            with target.open('x', encoding='utf-8', newline='') as stream:
                stream.write(content)
        except FileExistsError as exc:
            raise AgentError('File appeared during creation; existing data was preserved.') from exc
        return {'path': Path(path.replace('\\', '/')).as_posix(),
                'before_sha256': None, 'after_sha256': file_hash(target)}

    def _walk_files(self):
        """Yield bounded root-relative files without following links or private trees."""
        pending = [(self.root, 0)]
        emitted = 0
        while pending and emitted < MAX_SEARCH_FILES:
            directory, depth = pending.pop()
            if depth > MAX_DEPTH:
                continue
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda row: row.name.casefold(), reverse=True)
            except OSError:
                continue
            for entry in entries:
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if _is_blocked_name(entry.name, is_directory):
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_symlink() or _is_reparse_point(entry_path):
                        continue
                    if is_directory:
                        if depth < MAX_DEPTH:
                            pending.append((entry_path, depth + 1))
                        continue
                    if entry.is_file(follow_symlinks=False):
                        if entry.stat(follow_symlinks=False).st_nlink > 1:
                            continue
                        relative = entry_path.relative_to(self.root).as_posix()
                        yield relative, entry_path
                        emitted += 1
                        if emitted >= MAX_SEARCH_FILES:
                            return
                except OSError:
                    continue


def _is_reparse_point(path):
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def _is_blocked_name(name, is_directory):
    folded = name.casefold()
    if folded.startswith('.') or folded in BLOCKED_FILE_NAMES or folded.startswith('.env'):
        return True
    if is_directory and folded in BLOCKED_DIRECTORY_NAMES:
        return True
    if not is_directory and Path(folded).suffix in BLOCKED_EXTENSIONS:
        return True
    return False


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()
