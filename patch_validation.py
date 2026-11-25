"""Strict, bounded unified-diff validation for operator-reviewed coding patches."""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


class PatchValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PatchFile:
    path: str
    old_path: str | None
    new_bytes: bytes


@dataclass(frozen=True)
class ValidatedPatch:
    sha256: str
    files: tuple[PatchFile, ...]
    raw: bytes


_HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?$")


def normalize_model_diff_response(raw: bytes, *, allowed_paths: set[str]) -> bytes:
    """Normalize exactly one optional diff fence and one standard unified file.

    No prose or multiple loose file sections are accepted. The normalized bytes,
    not the model transport wrapper, are the patch artifact bound to approval.
    """
    if not isinstance(raw, bytes) or len(raw) > 256_000:
        raise PatchValidationError('Model diff response is empty or exceeds its bound.')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PatchValidationError('Model diff response must be UTF-8.') from exc
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == '```diff':
        if len(lines) < 4 or lines[-1].strip() != '```':
            raise PatchValidationError('Diff code fence is incomplete or contains trailing prose.')
        lines = lines[1:-1]
        if any(line.strip().startswith('```') for line in lines):
            raise PatchValidationError('Only one complete diff code fence may wrap a proposal.')
        raw = ''.join(lines).encode('utf-8')
    elif any(line.strip().startswith('```') for line in lines):
        raise PatchValidationError('Only one complete diff code fence may wrap a proposal.')
    if not raw.endswith(b'\n'):
        raw += b'\n'
    rows = raw.splitlines(keepends=True)
    if rows and rows[0].startswith(b'--- ') and len(rows) >= 3 and rows[1].startswith(b'+++ '):
        old_header = rows[0][4:].rstrip(b'\r\n')
        new_header = rows[1][4:].rstrip(b'\r\n')
        if old_header == b'/dev/null':
            new_path = _safe_path(new_header, b'b/')
            if new_path not in allowed_paths:
                raise PatchValidationError('Standalone unified diff path is unauthorized.')
            boundary = (f'diff --git a/{new_path} b/{new_path}\nnew file mode 100644\n').encode()
        else:
            old_path = _safe_path(old_header, b'a/')
            new_path = _safe_path(new_header, b'b/')
            if old_path != new_path or new_path not in allowed_paths:
                raise PatchValidationError('Standalone unified diff paths are ambiguous or unauthorized.')
            boundary = f'diff --git a/{new_path} b/{new_path}\n'.encode()
        raw = boundary + raw
    return raw


def _safe_path(raw: bytes, prefix: bytes) -> str:
    if not raw.startswith(prefix):
        raise PatchValidationError('Malformed unified-diff file header.')
    value = raw[len(prefix):].rstrip(b'\r\n')
    if any(byte in value for byte in (b'\t', b'\\', b'"', b'\x00', b' ')):
        raise PatchValidationError('Quoted or ambiguous patch paths are unsupported.')
    try:
        name = value.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PatchValidationError('Patch paths must be UTF-8.') from exc
    path = PurePosixPath(name)
    if (not name or name.startswith('/') or name.startswith('../') or
            any(part in ('', '.', '..') for part in name.split('/')) or
            ':' in name or path.is_absolute()):
        raise PatchValidationError('Absolute or traversing patch path rejected.')
    return name


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(metadata, 'st_file_attributes', 0) & 0x400)


def _target_path(root: Path, relative: str, *, allow_missing: bool) -> Path:
    """Resolve one already-allowlisted path without crossing links/junctions."""
    target = root.joinpath(*PurePosixPath(relative).parts)
    for component in (target, *target.parents):
        if component == root:
            break
        if _is_reparse_point(component):
            raise PatchValidationError('Proposal path crosses a symlink, junction, or reparse point.')
    try:
        resolved = target.resolve(strict=not allow_missing)
    except (OSError, RuntimeError) as exc:
        raise PatchValidationError('Proposal path cannot be resolved safely.') from exc
    if not resolved.is_relative_to(root):
        raise PatchValidationError('Proposal path escapes the repository.')
    if target.exists():
        if not resolved.is_file() or resolved.stat().st_nlink != 1:
            raise PatchValidationError('Proposal target is not a regular single-link file.')
    elif not allow_missing or not target.parent.is_dir():
        raise PatchValidationError('New files require an existing safe parent and an unused path.')
    return target


def _post_image(source: bytes, content: str) -> bytes:
    """Encode model text while retaining a target's UTF-8 BOM and newline style."""
    if not isinstance(content, str) or '\x00' in content:
        raise PatchValidationError('Structured file content must be NUL-free UTF-8 text.')
    try:
        content_bytes = content.encode('utf-8')
        source.decode('utf-8')
    except (UnicodeError, AttributeError) as exc:
        raise PatchValidationError('Binary files and non-UTF-8 encodings are unsupported.') from exc
    if content.startswith('\ufeff'):
        raise PatchValidationError('Model content must not provide or alter the UTF-8 BOM.')
    bom = b'\xef\xbb\xbf' if source.startswith(b'\xef\xbb\xbf') else b''
    body = source[len(bom):]
    content_crlf = b'\r\n' in content_bytes
    content_bare_lf = re.search(rb'(?<!\r)\n', content_bytes) is not None
    content_bare_cr = re.search(rb'\r(?!\n)', content_bytes) is not None
    if content_bare_cr or (content_crlf and content_bare_lf):
        raise PatchValidationError('Mixed or unsupported proposal line endings are ambiguous.')
    crlf = b'\r\n' in body
    bare_lf = re.search(rb'(?<!\r)\n', body) is not None
    bare_cr = re.search(rb'\r(?!\n)', body) is not None
    if bare_cr or (crlf and bare_lf):
        raise PatchValidationError('Mixed or unsupported source line endings are ambiguous.')
    model_newline = b'\r\n' if crlf else b'\n'
    existing_newline = (b'\r\n' if b'\r\n' in body else
                        b'\n' if b'\n' in body else model_newline)
    logical_content = content_bytes.replace(b'\r\n', b'\n')
    return bom + logical_content.replace(b'\n', existing_newline)


def _hunk_range(start: int, count: int) -> bytes:
    return str(start if count == 0 else start + 1).encode('ascii') + b',' + str(count).encode('ascii')


def _file_hunks(old: bytes, new: bytes) -> bytes:
    """Construct complete unified hunks, including explicit EOF markers."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    chunks = []
    for group in matcher.get_grouped_opcodes(3):
        first, last = group[0], group[-1]
        old_start, old_stop = first[1], last[2]
        new_start, new_stop = first[3], last[4]
        old_count, new_count = old_stop - old_start, new_stop - new_start
        chunks.append(b'@@ -' + _hunk_range(old_start, old_count) + b' +' +
                      _hunk_range(new_start, new_count) + b' @@\n')
        for tag, i1, i2, j1, j2 in group:
            rows = []
            if tag in ('equal', 'delete', 'replace'):
                rows.extend((b' ' if tag == 'equal' else b'-', line)
                            for line in old_lines[i1:i2])
            if tag in ('insert', 'replace'):
                rows.extend((b'+', line) for line in new_lines[j1:j2])
            for prefix, line in rows:
                has_newline = line.endswith(b'\n')
                payload = line[:-1] if has_newline else line
                chunks.extend((prefix + payload + b'\n',))
                if not has_newline:
                    chunks.append(b'\\ No newline at end of file\n')
    return b''.join(chunks)


def build_unified_diff_from_proposal(raw: bytes, *, root: Path,
                                     allowed_paths: set[str],
                                     planned_paths: set[str] | None = None,
                                     protected_paths: set[str] | None = None,
                                     max_files: int = 8,
                                     allow_test_files: bool = False,
                                     max_bytes: int = 16_384) -> ValidatedPatch:
    """Turn strict JSON full-file intents into a locally constructed, validated diff.

    The model supplies no diff syntax. Its paths/content remain untrusted; source
    bytes, newline/BOM policy, path containment, hunk construction and final
    reconstruction are all enforced locally.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise PatchValidationError('Structured proposal is empty or exceeds its byte limit.')
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON field.')
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError) as exc:
        raise PatchValidationError('Coder proposal must be strict UTF-8 JSON.') from exc
    if not isinstance(value, dict) or set(value) != {'files'}:
        raise PatchValidationError('Structured proposal schema is invalid.')
    rows = value['files']
    if type(max_files) is not int or not 1 <= max_files <= 8:
        raise PatchValidationError('Change-set file limit is invalid.')
    if not isinstance(rows, list) or not 1 <= len(rows) <= max_files:
        raise PatchValidationError(f'Structured proposal must contain one to {max_files} files.')
    root = Path(root).resolve(strict=True)
    planned = allowed_paths if planned_paths is None else planned_paths
    protected = protected_paths or set()
    seen: set[str] = set()
    seen_folded: set[str] = set()
    hunks = []
    aggregate = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'base_sha256', 'content'}:
            raise PatchValidationError('Each structured file must contain exactly path, base_sha256, and content.')
        relative, content, base_sha = row['path'], row['content'], row['base_sha256']
        if (not isinstance(relative, str) or relative not in allowed_paths
                or relative not in planned or relative in protected or relative in seen
                or relative.startswith('.local/')
                or (relative.startswith('tests/') and not allow_test_files)):
            raise PatchValidationError('Structured proposal contains an unauthorized, unplanned, or duplicate path.')
        safe = _safe_path(('b/' + relative).encode('utf-8'), b'b/')
        if safe != relative:
            raise PatchValidationError('Structured proposal path is noncanonical.')
        if not isinstance(content, str):
            raise PatchValidationError('Structured file content must be text.')
        try:
            aggregate += len(content.encode('utf-8'))
        except UnicodeError as exc:
            raise PatchValidationError('Structured file content is not valid UTF-8 text.') from exc
        if aggregate > max_bytes:
            raise PatchValidationError('Structured file content exceeds its aggregate bound.')
        target = _target_path(root, relative, allow_missing=True)
        existed = target.exists()
        if existed:
            old = target.read_bytes()
            try:
                old.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise PatchValidationError('Binary targets and non-UTF-8 encodings are unsupported.') from exc
        else:
            old = b''
        expected_base = hashlib.sha256(old).hexdigest() if existed else None
        if relative.casefold() in seen_folded:
            raise PatchValidationError('Change set contains case-insensitive path collisions.')
        if base_sha != expected_base:
            raise PatchValidationError(f'Stale source hash for {relative}; refresh authorized context before retrying.')
        new = _post_image(old, content)
        if existed and old == new:
            raise PatchValidationError('Structured proposal contains an unchanged file; intent is ambiguous.')
        if not existed and not new:
            raise PatchValidationError('Empty new files are unsupported.')
        if existed:
            header = (f'diff --git a/{relative} b/{relative}\n'
                      f'--- a/{relative}\n+++ b/{relative}\n').encode('utf-8')
        else:
            header = (f'diff --git a/{relative} b/{relative}\nnew file mode 100644\n'
                      f'--- /dev/null\n+++ b/{relative}\n').encode('utf-8')
        body = _file_hunks(old, new)
        if not body:
            raise PatchValidationError('Structured proposal produced no changes.')
        hunks.extend((header, body))
        seen.add(relative)
        seen_folded.add(relative.casefold())
    diff = b''.join(hunks)
    return validate_unified_diff(diff, root=root, allowed_paths=allowed_paths,
                                 max_bytes=256_000)


def validate_unified_diff(raw: bytes, *, root: Path, allowed_paths: set[str],
                          max_bytes: int = 256_000) -> ValidatedPatch:
    """Accept only fully consumed text additions/edits for exact allowlisted paths.

    Reconstructs resulting bytes itself, so a permissive `git apply --check` can
    never truncate an under-counted hunk and silently authorize a partial patch.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise PatchValidationError('Patch is empty or exceeds its byte limit.')
    if b'\x00' in raw or raw.startswith(b'\xef\xbb\xbf'):
        raise PatchValidationError('Patch must be plain UTF-8 text without NUL or BOM.')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PatchValidationError('Patch is not valid UTF-8.') from exc
    if not raw.endswith(b'\n'):
        raise PatchValidationError('Patch must end with a newline; trailing content is ambiguous.')
    lines = raw.splitlines(keepends=True)
    i = 0
    result = []
    seen = set()
    root = Path(root).resolve(strict=True)
    while i < len(lines):
        if lines[i].startswith((b'GIT binary patch', b'diff --cc ', b'diff --combined ', b'old mode ', b'new mode ', b'rename ', b'copy ', b'Binary files ')):
            raise PatchValidationError('Binary, mode, rename, copy, and combined patches are unsupported.')
        if not lines[i].startswith(b'diff --git a/'):
            raise PatchValidationError('Expected an unambiguous git file boundary.')
        if b'\r' in lines[i]:
            raise PatchValidationError('Git file boundaries must use canonical LF separators.')
        boundary = lines[i].rstrip(b'\n').split(b' ')
        if len(boundary) != 4:
            raise PatchValidationError('Malformed git file boundary.')
        old_name = _safe_path(boundary[2], b'a/')
        new_name = _safe_path(boundary[3], b'b/')
        if old_name != new_name or new_name not in allowed_paths or new_name in seen:
            raise PatchValidationError('Patch modifies an unauthorized, renamed, or duplicate path.')
        seen.add(new_name)
        i += 1
        new_file_mode = False
        if i < len(lines) and lines[i].startswith(b'new file mode '):
            if lines[i] != b'new file mode 100644\n':
                raise PatchValidationError('Only regular text files with mode 100644 are supported.')
            new_file_mode = True
            i += 1
        if i < len(lines) and lines[i].startswith(b'index '):
            if not re.fullmatch(rb'index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}(?: [0-7]{6})?\n', lines[i]):
                raise PatchValidationError('Malformed Git index header.')
            i += 1
        if i + 1 >= len(lines) or not lines[i].startswith(b'--- ') or not lines[i+1].startswith(b'+++ '):
            raise PatchValidationError('Malformed or missing file headers.')
        if b'\r' in lines[i] or b'\r' in lines[i + 1]:
            raise PatchValidationError('File headers must use canonical LF separators.')
        header_old = lines[i][4:].rstrip(b'\r\n')
        header_new = lines[i+1][4:].rstrip(b'\r\n')
        if header_old == b'/dev/null' or header_new == b'/dev/null':
            if header_old != b'/dev/null' or header_new != b'b/' + new_name.encode():
                raise PatchValidationError('Only new-file additions and existing-file edits are supported.')
            if not new_file_mode:
                raise PatchValidationError('New-file patch must declare regular file mode 100644.')
            old_bytes = b''
            old_path = None
            target = root.joinpath(*PurePosixPath(new_name).parts)
            _target_path(root, new_name, allow_missing=True)
            if target.exists():
                raise PatchValidationError('New-file patch targets an existing path.')
        else:
            if new_file_mode:
                raise PatchValidationError('New-file mode metadata conflicts with existing-file headers.')
            if _safe_path(header_old, b'a/') != old_name or _safe_path(header_new, b'b/') != new_name:
                raise PatchValidationError('File headers disagree with the git boundary.')
            target = root.joinpath(*PurePosixPath(new_name).parts)
            _target_path(root, new_name, allow_missing=False)
            resolved = target.resolve(strict=True)
            if not resolved.is_file() or resolved.stat().st_nlink != 1:
                raise PatchValidationError('Patch target is not a regular single-link file.')
            old_bytes = resolved.read_bytes()
            old_path = new_name
        try:
            old_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PatchValidationError('Binary target files are unsupported.') from exc
        source = old_bytes.splitlines(keepends=True)
        output = []
        cursor = 0
        i += 2
        hunk_count = 0
        line_delta = 0
        while i < len(lines) and lines[i].startswith(b'@@ '):
            if b'\r' in lines[i]:
                raise PatchValidationError('Hunk headers must use canonical LF separators.')
            match = _HUNK.fullmatch(lines[i].rstrip(b'\r\n'))
            if not match:
                raise PatchValidationError('Malformed hunk header.')
            old_start, old_count, new_start, new_count = match.groups()
            old_start = int(old_start); old_count = int(old_count or b'1')
            new_start = int(new_start); new_count = int(new_count or b'1')
            hunk_position = old_start if old_count == 0 else old_start - 1
            expected_new_start = (0 if new_count == 0 else
                                  old_start + line_delta + (1 if old_count == 0 else 0))
            if (hunk_position < cursor or hunk_position > len(source)
                    or new_start != expected_new_start):
                raise PatchValidationError('Hunk ranges overlap, disagree, or exceed the source file.')
            output.extend(source[cursor:hunk_position]); cursor = hunk_position
            got_old = got_new = 0
            i += 1
            while i < len(lines) and lines[i][:1] in (b' ', b'+', b'-'):
                line = lines[i]
                if not line.endswith(b'\n'):
                    raise PatchValidationError('Hunk body line lacks a newline.')
                marker = line[:1]
                no_newline = i + 1 < len(lines) and lines[i + 1] == b'\\ No newline at end of file\n'
                payload = line[1:-1] if no_newline else line[1:]
                if no_newline and payload.endswith(b'\r'):
                    raise PatchValidationError('Bare carriage return in a no-final-newline hunk record.')
                if marker in (b' ', b'-'):
                    if cursor >= len(source) or source[cursor] != payload:
                        raise PatchValidationError(
                            'Hunk context does not match repository bytes: '
                            + repr(source[cursor] if cursor < len(source) else None)
                            + ' != ' + repr(payload)
                        )
                    if marker == b' ':
                        output.append(payload); got_new += 1
                    got_old += 1; cursor += 1
                else:
                    output.append(payload); got_new += 1
                i += 1
                if no_newline:
                    i += 1
            if i < len(lines) and lines[i] == b'\\ No newline at end of file\n':
                raise PatchValidationError('Orphan no-final-newline marker.')
            if got_old != old_count or got_new != new_count:
                raise PatchValidationError(
                    f'Hunk count mismatch: declared -{old_count}/+{new_count}, consumed -{got_old}/+{got_new}.'
                )
            hunk_count += 1
            line_delta += got_new - got_old
        if not hunk_count:
            raise PatchValidationError('File patch contains no hunks.')
        output.extend(source[cursor:])
        result.append(PatchFile(new_name, old_path, b''.join(output)))
        if i < len(lines) and not lines[i].startswith(b'diff --git '):
            raise PatchValidationError('Unconsumed or unsupported trailing patch content.')
    if not result:
        raise PatchValidationError('Patch contains no authorized changes.')
    return ValidatedPatch(hashlib.sha256(raw).hexdigest(), tuple(result), raw)
