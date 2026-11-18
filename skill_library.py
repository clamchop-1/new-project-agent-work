"""Project-scoped, evidence-bound reusable skill candidates and approvals.

Skill text is always reference data. This module never grants tools, changes
project memory, executes a procedure, or changes model weights.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from agent_core import AgentError, Project, atomic_write, digest, encoded, exclusive, file_hash, no_secrets


SCHEMA_VERSION = 1
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_SKILLS = 1000
MAX_VERSIONS = 64
MAX_EVIDENCE = 32
MAX_USAGE = 5000
MAX_SKILL_TEXT = 4000
CATEGORIES = frozenset({'coding', 'debugging', 'qa', 'testing', 'architecture',
                        'review', 'research', 'operations', 'general'})
TRUST_CLASSES = frozenset({'reference', 'procedure', 'authoritative', 'executable'})
STATUSES = frozenset({'candidate', 'validated', 'approved', 'rejected', 'superseded', 'revoked'})
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, label: str, limit: int = MAX_SKILL_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
        raise AgentError(f'{label} must be nonempty and within its {limit}-byte limit.')
    no_secrets(value)
    return value.strip()


def _pid_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class SkillLibrary:
    """Atomic local skill registry bound to one exact Git worktree."""

    def __init__(self, project: Project):
        if not isinstance(project, Project):
            raise AgentError('Skill registry requires a validated Project root.')
        self.project = project
        self.root = project.root
        self.local_root = self.root / '.local'
        if self.local_root.is_symlink() or not self.local_root.resolve().is_relative_to(self.root):
            raise AgentError('Project private state root is linked or outside the project.')
        ignored = project.git('check-ignore', '--', '.local/skills/registry.json').strip()
        if not ignored:
            raise AgentError('Project .local/skills registry is not Git-ignored; private state would be exposed.')
        self.store = self.local_root / 'skills'
        self.registry_path = self.store / 'registry.json'
        self.pending_path = self.store / 'pending.json'
        self.lock_path = self.store / 'registry.lock'
        self.recovery_dir = self.store / 'recovery'
        self._thread_lock = self._thread_lock_for(self.lock_path)

    @staticmethod
    def _thread_lock_for(path: Path) -> threading.RLock:
        key = str(path).casefold()
        with _THREAD_LOCKS_GUARD:
            return _THREAD_LOCKS.setdefault(key, threading.RLock())

    def _empty_registry(self) -> dict:
        return {'schema_version': SCHEMA_VERSION, 'project_id': self.project.identity,
                'skills': {}, 'updated_at': _now()}

    def _check_private_paths(self) -> None:
        if self.store.is_symlink() or self.store.resolve().parent != self.local_root.resolve():
            raise AgentError('Skill registry directory is linked or outside project-private storage.')
        if (self.recovery_dir.is_symlink()
                or self.recovery_dir.resolve().parent != self.store.resolve()):
            raise AgentError('Skill registry recovery directory is linked or outside private storage.')
        for path in (self.registry_path, self.pending_path, self.lock_path):
            if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
                raise AgentError('Linked or hard-linked skill registry files are rejected.')

    def _load_registry(self, *, permit_lock: bool = False, permit_pending: bool = False) -> dict:
        self._check_private_paths()
        if not permit_lock and self.lock_path.exists():
            raise AgentError('Skill registry has an active or unreconciled writer lock.')
        if not permit_pending and self.pending_path.exists():
            raise AgentError('Skill registry has an unfinished transaction; inspect and recover it first.')
        if not self.registry_path.exists():
            return self._empty_registry()
        raw = self.registry_path.read_bytes()
        if len(raw) > MAX_REGISTRY_BYTES:
            raise AgentError('Skill registry exceeds its bounded storage limit.')
        try:
            no_secrets(raw.decode('utf-8'))
            registry = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AgentError('Skill registry is malformed; preserve it for manual recovery.') from exc
        if (not isinstance(registry, dict) or registry.get('schema_version') != SCHEMA_VERSION
                or registry.get('project_id') != self.project.identity
                or not isinstance(registry.get('skills'), dict)
                or len(registry['skills']) > MAX_SKILLS):
            raise AgentError('Skill registry identity or schema is invalid; preserve it for review.')
        for skill_id, record in registry['skills'].items():
            if (not isinstance(skill_id, str) or not isinstance(record, dict)
                    or record.get('skill_id') != skill_id
                    or record.get('project_id') != self.project.identity
                    or not isinstance(record.get('versions'), list)
                    or not 1 <= len(record['versions']) <= MAX_VERSIONS
                    or not isinstance(record.get('usage'), list)
                    or len(record['usage']) > MAX_USAGE):
                raise AgentError('Skill registry contains malformed project or version data.')
            for version in record['versions']:
                if (not isinstance(version, dict) or version.get('status') not in STATUSES
                        or version.get('category') not in CATEGORIES
                        or type(version.get('version')) is not int
                        or not isinstance(version.get('problem'), str)
                        or not isinstance(version.get('procedure'), str)):
                    raise AgentError('Skill registry contains a malformed skill version.')
                no_secrets(version['problem'])
                no_secrets(version['procedure'])
        return registry

    @contextmanager
    def _writer_lock(self, owner: str):
        self.store.mkdir(parents=True, exist_ok=True)
        self._check_private_paths()
        with self._thread_lock:
            lease = None
            for attempt in range(41):
                try:
                    lease = exclusive(self.lock_path, owner)
                    lease.__enter__()
                    break
                except AgentError:
                    try:
                        raw = self.lock_path.read_bytes()
                        record = json.loads(raw)
                    except (OSError, ValueError, TypeError) as exc:
                        raise AgentError('Skill registry lock is malformed; preserve it for review.') from exc
                    if (not isinstance(record, dict) or type(record.get('pid')) is not int
                            or record['pid'] <= 0 or not isinstance(record.get('owner'), str)
                            or not isinstance(record.get('nonce'), str)):
                        raise AgentError('Skill registry lock is malformed; preserve it for review.')
                    if not _pid_alive(record.get('pid')):
                        self.recovery_dir.mkdir(exist_ok=True)
                        backup = self.recovery_dir / ('registry-lock-' + uuid.uuid4().hex + '.json')
                        atomic_write(backup, raw)
                        if self.lock_path.read_bytes() != raw:
                            raise AgentError('Skill registry lock changed; preserved backup remains.')
                        self.lock_path.unlink()
                        continue
                    if attempt == 40:
                        raise AgentError('Skill registry is busy; retry after the active writer finishes.')
                    time.sleep(0.05)
            try:
                yield
            finally:
                if lease is not None:
                    lease.__exit__(None, None, None)

    def _write_transaction(self, registry: dict, event_id: str) -> None:
        raw_after = encoded(registry)
        if len(raw_after) > MAX_REGISTRY_BYTES:
            raise AgentError('Skill registry exceeds its bounded storage limit.')
        before_hash = file_hash(self.registry_path) if self.registry_path.exists() else None
        pending = {'schema_version': SCHEMA_VERSION, 'transaction_id': event_id,
                   'project_id': self.project.identity, 'before_sha256': before_hash,
                   'after_sha256': digest(raw_after),
                   'after_registry_b64': base64.b64encode(raw_after).decode('ascii'),
                   'created_at': _now()}
        atomic_write(self.pending_path, encoded(pending))
        current_hash = file_hash(self.registry_path) if self.registry_path.exists() else None
        if current_hash != before_hash:
            raise AgentError('Concurrent skill registry edit detected; transaction retained for recovery.')
        atomic_write(self.registry_path, raw_after)
        if file_hash(self.registry_path) != pending['after_sha256']:
            raise AgentError('Skill registry post-write hash mismatch; transaction retained for recovery.')
        self.pending_path.unlink()

    def _mutate(self, owner: str, callback: Callable[[dict], tuple[Any, bool]]) -> Any:
        with self._writer_lock(owner):
            registry = self._load_registry(permit_lock=True)
            result, changed = callback(registry)
            if changed:
                registry['updated_at'] = _now()
                self._write_transaction(registry, uuid.uuid4().hex)
            return result

    def _run_root(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id):
            raise AgentError('Evidence run identity is invalid.')
        orchestration = self.local_root / 'orchestration'
        runs = orchestration / 'runs'
        root = runs / run_id
        if any(path.is_symlink() for path in (self.local_root, orchestration, runs, root)):
            raise AgentError('Evidence run root is linked or outside this project.')
        local_resolved = self.local_root.resolve()
        orchestration_resolved = orchestration.resolve()
        runs_resolved = runs.resolve()
        root_resolved = root.resolve()
        if (not orchestration_resolved.is_relative_to(local_resolved)
                or not runs_resolved.is_relative_to(orchestration_resolved)
                or not root_resolved.is_relative_to(runs_resolved)):
            raise AgentError('Evidence run root is linked or outside this project.')
        return root_resolved

    def _verify_source_task(self, run_id: str, task_id: str, source_agent: str) -> None:
        run_root = self._run_root(run_id)
        state_path = run_root / 'state.json'
        if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 5 * 1024 * 1024:
            raise AgentError('Candidate source task state is missing or outside its bounded scope.')
        try:
            state = json.loads(state_path.read_bytes())
            task = state['tasks'][task_id]
            valid = (state.get('schema_version') == 1
                and state.get('graph', {}).get('run_id') == run_id
                and state.get('graph', {}).get('project_id') == self.project.identity
                and state.get('project_baseline', {}).get('project_id') == self.project.identity
                and task.get('role') == source_agent and task.get('status') == 'agent_completed')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AgentError('Candidate provenance is malformed or unavailable.') from exc
        if not valid:
            raise AgentError('Candidate must reference a completed task from this project and agent role.')

    def _verify_reference(self, reference: Mapping[str, Any], run_id: str) -> dict:
        if not isinstance(reference, Mapping):
            raise AgentError('Skill evidence reference must be an object.')
        kind, uri, expected = reference.get('kind'), reference.get('uri'), reference.get('sha256')
        if (not isinstance(kind, str) or kind not in {'source_file', 'artifact', 'command_receipt', 'harness_event_log'}
                or not isinstance(uri, str) or len(uri) > 1024 or not isinstance(expected, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise AgentError('Skill evidence reference has an unsupported type or hash.')
        original = Path(uri)
        if not original.is_absolute() or original.is_symlink():
            raise AgentError('Skill evidence must use an absolute, non-linked artifact path.')
        run_root = self._run_root(run_id)
        lexical = original.absolute()
        if not lexical.is_relative_to(run_root):
            raise AgentError('Skill evidence is outside its source run.')
        for ancestor in (lexical, *lexical.parents):
            if ancestor.is_symlink() or (ancestor.exists()
                    and getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400):
                raise AgentError('Linked or reparse evidence paths are rejected.')
            if ancestor == run_root:
                break
        path = original.resolve(strict=True)
        if (not path.is_relative_to(run_root) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024
                or file_hash(path) != expected):
            raise AgentError('Skill evidence is missing, stale, or outside its source run.')
        verified = {'kind': kind, 'uri': str(path), 'sha256': expected,
                    'evidence_id': str(reference.get('evidence_id', ''))[:128],
                    'run_id': run_id, 'verified_at': _now()}
        if kind == 'command_receipt':
            output_dir = (run_root / 'outputs').resolve()
            if not path.is_relative_to(output_dir):
                raise AgentError('Command receipt is outside this run output directory.')
            try:
                receipt = json.loads(path.read_bytes())
                if (receipt.get('format') != 'local-agent-command-receipt'
                        or receipt.get('project_id') not in self._run_project_ids(run_root)
                        or receipt.get('command') != 'tests' or receipt.get('exit_code') != 0):
                    raise AgentError('Command receipt does not establish a successful test run.')
                output_original = Path(receipt.get('output_path', ''))
                if output_original.is_symlink():
                    raise AgentError('Test output artifact is linked.')
                output_path = output_original.resolve(strict=True)
                if (not output_path.is_relative_to(output_dir) or not output_path.is_file()
                        or file_hash(output_path) != receipt.get('output_sha256')):
                    raise AgentError('Test receipt output hash or location is invalid.')
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise AgentError('Test receipt is unreadable or forged.') from exc
            verified['test_verified'] = True
        else:
            verified['test_verified'] = False
        return verified

    def _run_project_ids(self, run_root: Path) -> set[str]:
        """Return base/worktree identities recorded by this run, never arbitrary IDs."""
        identities = {self.project.identity}
        state_path = run_root / 'state.json'
        try:
            if state_path.is_symlink() or not state_path.is_file() or state_path.stat().st_size > 5 * 1024 * 1024:
                return identities
            state = json.loads(state_path.read_bytes())
            tasks = state.get('tasks', {})
            if not isinstance(tasks, dict):
                return identities
            worktree_root = (run_root / 'worktrees').resolve()
            for row in tasks.values():
                candidate = row.get('worktree') if isinstance(row, dict) else None
                if not isinstance(candidate, str):
                    continue
                path = Path(candidate)
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=True)
                if resolved.is_relative_to(worktree_root) and resolved.is_dir():
                    identities.add(Project(resolved).identity)
        except (OSError, ValueError, TypeError, AttributeError, AgentError):
            return identities
        return identities

    def _verify_evidence(self, references: list[Mapping[str, Any]]) -> list[dict]:
        if not isinstance(references, list) or not 1 <= len(references) <= MAX_EVIDENCE:
            raise AgentError('A candidate requires one to 32 supporting evidence references.')
        checked, seen = [], set()
        for reference in references:
            run_id = reference.get('run_id') if isinstance(reference, Mapping) else None
            if run_id is None:
                uri = Path(str(reference.get('uri', ''))) if isinstance(reference, Mapping) else Path()
                try:
                    relative = uri.resolve(strict=True).relative_to((self.local_root / 'orchestration' / 'runs').resolve())
                except (OSError, ValueError) as exc:
                    raise AgentError('Evidence is not contained in this project orchestration history.') from exc
                if not relative.parts:
                    raise AgentError('Evidence run identity is missing.')
                run_id = relative.parts[0]
            verified = self._verify_reference(reference, run_id)
            key = verified['uri'].casefold()
            if key in seen:
                continue
            seen.add(key)
            checked.append(verified)
        if not checked:
            raise AgentError('Candidate has no distinct verified evidence.')
        return checked

    @staticmethod
    def _current(record: dict) -> dict:
        versions = record.get('versions')
        if not isinstance(versions, list) or not versions:
            raise AgentError('Skill version history is malformed.')
        return versions[-1]

    @staticmethod
    def _version(record: dict, changes: Mapping[str, Any], actor: str, action: str) -> dict:
        old = SkillLibrary._current(record)
        if len(record['versions']) >= MAX_VERSIONS:
            raise AgentError('Skill version history reached its bounded limit.')
        current = {**old, **dict(changes), 'version': old['version'] + 1,
                   'updated_at': _now(), 'actor': actor, 'change': action}
        record['versions'].append(current)
        return current

    def propose(self, *, category: str, problem: str, procedure: str,
                source_refs: list[Mapping[str, Any]], evidence_refs: list[Mapping[str, Any]],
                run_id: str, task_id: str, source_agent: str,
                source_event_id: str | None = None) -> dict:
        if category not in CATEGORIES:
            raise AgentError('Skill category is unsupported.')
        problem = _bounded_text(problem, 'Skill problem')
        procedure = _bounded_text(procedure, 'Skill procedure')
        if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task_id):
            raise AgentError('Skill source task id is invalid.')
        if not isinstance(source_agent, str) or source_agent not in {
                'architect', 'developer', 'qa', 'reviewer', 'researcher', 'memory_analyst'}:
            raise AgentError('Skill source agent role is unsupported.')
        self._verify_source_task(run_id, task_id, source_agent)
        verified = self._verify_evidence(evidence_refs)
        if any(ref['run_id'] != run_id for ref in verified):
            raise AgentError('Candidate evidence must belong to the stated source run.')
        normalized = ' '.join((category + ' ' + problem + ' ' + procedure).casefold().split())
        skill_id = 'skill-' + digest((self.project.identity + '\0' + normalized).encode())[:24]
        source_rows = self._verify_evidence(source_refs) if source_refs else []
        if any(ref['run_id'] != run_id for ref in source_rows):
            raise AgentError('Candidate source references must belong to the stated source run.')
        semantic_sha = digest(normalized.encode())
        candidate = {'version': 1, 'status': 'candidate', 'validation_state': 'evidence_valid',
                     'category': category, 'problem': problem, 'procedure': procedure,
                     'trust_class': 'reference', 'semantic_sha256': semantic_sha,
                     'source_refs': source_rows, 'evidence_refs': verified,
                     'source': {'project_id': self.project.identity, 'run_id': run_id,
                                'task_id': task_id, 'agent': source_agent,
                                'event_id': source_event_id},
                     'confidence': round(min(0.8, 0.45 + (0.25 if any(
                         item.get('test_verified') for item in verified) else 0.0)
                         + (0.1 if source_rows else 0.0)), 2),
                     'validation': None, 'created_at': _now(), 'updated_at': _now(),
                     'actor': 'system', 'change': 'candidate_proposed'}

        def change(registry):
            record = registry['skills'].get(skill_id)
            if record is None:
                if len(registry['skills']) >= MAX_SKILLS:
                    raise AgentError('Project skill registry reached its bounded capacity.')
                record = {'skill_id': skill_id, 'project_id': self.project.identity,
                          'versions': [candidate], 'usage': []}
                registry['skills'][skill_id] = record
                return {'skill_id': skill_id, 'version': 1, 'deduplicated': False}, True
            if len(record['versions']) >= MAX_VERSIONS:
                raise AgentError('Skill version history reached its bounded limit.')
            if any(version.get('source', {}).get('event_id') == source_event_id
                   and source_event_id for version in record['versions']):
                current = self._current(record)
                return {'skill_id': skill_id, 'version': current['version'], 'deduplicated': True}, False
            old = self._current(record)
            merged = {**candidate, 'version': old['version'] + 1,
                      'evidence_refs': self._merge_refs(old.get('evidence_refs', []), verified),
                      'source_refs': self._merge_refs(old.get('source_refs', []), source_rows),
                      'created_at': old['created_at'], 'change': 'duplicate_evidence_added'}
            record['versions'].append(merged)
            return {'skill_id': skill_id, 'version': merged['version'], 'deduplicated': True}, True
        return self._mutate('propose-skill:' + skill_id, change)

    @staticmethod
    def _merge_refs(existing: list[dict], new: list[dict]) -> list[dict]:
        merged = {item.get('uri', item.get('sha256')): item for item in existing}
        for item in new:
            merged[item.get('uri', item.get('sha256'))] = item
        if len(merged) > MAX_EVIDENCE:
            raise AgentError('Merged skill evidence exceeds its bound.')
        return list(merged.values())

    def list_skills(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise AgentError('Skill status filter is unsupported.')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AgentError('Skill list limit must be between one and 200.')
        registry = self._load_registry()
        rows = []
        for record in registry['skills'].values():
            current = self._current(record)
            if status is None or current['status'] == status:
                rows.append({'skill_id': record['skill_id'], 'project_id': record['project_id'],
                             'version': current['version'], 'status': current['status'],
                             'validation_state': current['validation_state'],
                             'category': current['category'], 'problem': current['problem'],
                             'source': current['source']})
        return sorted(rows, key=lambda row: (row['category'], row['skill_id']))[:limit]

    def inspect(self, skill_id: str) -> dict:
        registry = self._load_registry()
        try:
            record = registry['skills'][skill_id]
        except KeyError as exc:
            raise AgentError('Skill candidate does not exist in this project.') from exc
        return json.loads(encoded(record))

    def history(self, skill_id: str) -> list[dict]:
        return self.inspect(skill_id)['versions']

    def _fresh_evidence(self, current: dict) -> list[dict]:
        refs = current.get('evidence_refs', [])
        if not refs:
            raise AgentError('Skill has no project-local evidence attached.')
        return self._verify_evidence(refs)

    def attach_evidence(self, skill_id: str, evidence_refs: list[Mapping[str, Any]],
                        *, operator: str) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        verified = self._verify_evidence(evidence_refs)
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if old['status'] in {'revoked', 'rejected', 'superseded'}:
                raise AgentError('Evidence cannot be attached to a closed skill record.')
            current = self._version(record, {
                'status': 'candidate', 'validation_state': 'evidence_valid',
                'evidence_refs': self._merge_refs(old.get('evidence_refs', []), verified),
                'validation': None,
            }, operator, 'evidence_attached')
            return {'skill_id': skill_id, 'version': current['version'], 'status': current['status']}, True
        return self._mutate('attach-evidence:' + skill_id, change)

    def validate(self, skill_id: str, *, operator: str, scope: str, note: str) -> dict:
        operator = _bounded_text(operator, 'Independent reviewer', 128)
        note = _bounded_text(note, 'Validation note', 2000)
        if scope not in {'tests', 'procedure'}:
            raise AgentError('Validation scope must be tests or procedure.')
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if old['status'] not in {'candidate', 'validated'}:
                raise AgentError('Only a candidate can be validated.')
            if operator.casefold() == str(old['source'].get('agent', '')).casefold():
                raise AgentError('The proposing agent cannot validate its own candidate.')
            fresh = self._fresh_evidence(old)
            if scope == 'tests' and not any(ref.get('test_verified') for ref in fresh):
                raise AgentError('Test-scoped validation requires a fresh host-verified passing test receipt.')
            if scope == 'procedure' and len(note) < 40:
                raise AgentError('Procedure validation requires a detailed independent review note.')
            current = self._version(record, {
                'status': 'validated', 'validation_state': 'validated', 'evidence_refs': fresh,
                'validation': {'scope': scope, 'reviewer': operator, 'note': note,
                               'validated_at': _now(),
                               'evidence_sha256': [ref['sha256'] for ref in fresh]},
            }, operator, 'candidate_validated')
            return {'skill_id': skill_id, 'version': current['version'], 'status': current['status']}, True
        return self._mutate('validate-skill:' + skill_id, change)

    def approve(self, skill_id: str, *, operator: str, trust_class: str,
                confirm_sensitive: bool = False) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        if trust_class not in TRUST_CLASSES:
            raise AgentError('Skill trust class is unsupported.')
        if trust_class in {'authoritative', 'executable'} and confirm_sensitive is not True:
            raise AgentError('Authoritative or executable skills require explicit sensitive approval.')
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if old['status'] != 'validated' or old.get('validation_state') != 'validated':
                raise AgentError('Only a separately validated candidate can be approved.')
            if operator.casefold() == str(old['source'].get('agent', '')).casefold():
                raise AgentError('The proposing agent cannot approve its own candidate.')
            fresh = self._fresh_evidence(old)
            current = self._version(record, {
                'status': 'approved', 'trust_class': trust_class,
                'validation_state': 'validated', 'evidence_refs': fresh,
                'approved_by': operator, 'approved_at': _now(),
            }, operator, 'candidate_approved')
            return {'skill_id': skill_id, 'version': current['version'], 'status': current['status'],
                    'trust_class': trust_class}, True
        return self._mutate('approve-skill:' + skill_id, change)

    def reject(self, skill_id: str, *, operator: str, reason: str) -> dict:
        return self._close(skill_id, operator, reason, 'rejected')

    def revoke(self, skill_id: str, *, operator: str, reason: str) -> dict:
        return self._close(skill_id, operator, reason, 'revoked')

    def _close(self, skill_id: str, operator: str, reason: str, status: str) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        reason = _bounded_text(reason, 'Review reason', 1000)
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if status == 'rejected' and old['status'] == 'approved':
                raise AgentError('Approved skills must be revoked, not rejected.')
            current = self._version(record, {'status': status, 'validation_state': status,
                                             'review_note': reason, 'validation': None},
                                    operator, 'candidate_' + status)
            return {'skill_id': skill_id, 'version': current['version'], 'status': status}, True
        return self._mutate(status + '-skill:' + skill_id, change)

    def correct(self, skill_id: str, *, operator: str, problem: str,
                procedure: str, category: str | None = None) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        problem = _bounded_text(problem, 'Corrected problem')
        procedure = _bounded_text(procedure, 'Corrected procedure')
        if category is not None and category not in CATEGORIES:
            raise AgentError('Skill category is unsupported.')
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if old['status'] in {'revoked', 'rejected', 'superseded'}:
                raise AgentError('A closed skill must be re-proposed as a new candidate.')
            selected_category = category or old['category']
            semantic = digest(' '.join((selected_category + ' ' + problem + ' ' + procedure).casefold().split()).encode())
            current = self._version(record, {'status': 'candidate', 'validation_state': 'unvalidated',
                'category': selected_category, 'problem': problem, 'procedure': procedure,
                'semantic_sha256': semantic, 'validation': None, 'approved_by': None,
                'approved_at': None, 'change_reason': 'operator correction'}, operator, 'corrected')
            return {'skill_id': skill_id, 'version': current['version'], 'status': current['status']}, True
        return self._mutate('correct-skill:' + skill_id, change)

    def supersede(self, skill_id: str, replacement_id: str, *, operator: str, reason: str) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        reason = _bounded_text(reason, 'Supersession reason', 1000)
        def change(registry):
            record = self._record(registry, skill_id)
            replacement = self._record(registry, replacement_id)
            if skill_id == replacement_id or self._current(replacement)['status'] != 'approved':
                raise AgentError('Replacement must be a different approved skill in this project.')
            current = self._version(record, {'status': 'superseded', 'validation_state': 'superseded',
                'replaced_by': replacement_id, 'review_note': reason}, operator, 'superseded')
            return {'skill_id': skill_id, 'version': current['version'], 'status': current['status']}, True
        return self._mutate('supersede-skill:' + skill_id, change)

    def revalidate(self, skill_id: str, *, operator: str, scope: str, note: str) -> dict:
        operator = _bounded_text(operator, 'Independent reviewer', 128)
        note = _bounded_text(note, 'Revalidation note', 2000)
        if scope not in {'tests', 'procedure'}:
            raise AgentError('Validation scope must be tests or procedure.')
        if scope == 'procedure' and len(note) < 40:
            raise AgentError('Procedure revalidation requires a detailed independent review note.')
        def change(registry):
            record = self._record(registry, skill_id)
            old = self._current(record)
            if old['status'] != 'approved':
                raise AgentError('Only an approved skill can be revalidated.')
            if operator.casefold() == str(old['source'].get('agent', '')).casefold():
                raise AgentError('The proposing agent cannot revalidate its own skill.')
            try:
                fresh = self._fresh_evidence(old)
                if scope == 'tests' and not any(ref.get('test_verified') for ref in fresh):
                    raise AgentError('Fresh test receipt required for test revalidation.')
            except AgentError:
                current = self._version(record, {'status': 'revoked', 'validation_state': 'stale',
                    'review_note': 'Evidence became unavailable or stale.', 'validation': None},
                    operator, 'evidence_stale_revoked')
                return {'skill_id': skill_id, 'version': current['version'], 'status': 'revoked',
                        'reason': 'evidence_stale'}, True
            current = self._version(record, {'validation_state': 'validated', 'evidence_refs': fresh,
                'validation': {'scope': scope, 'reviewer': operator, 'note': note,
                               'validated_at': _now(), 'evidence_sha256': [ref['sha256'] for ref in fresh]}},
                operator, 'evidence_revalidated')
            return {'skill_id': skill_id, 'version': current['version'], 'status': 'approved'}, True
        return self._mutate('revalidate-skill:' + skill_id, change)

    def retrieve(self, *, category: str, problem: str, limit: int = 3,
                 run_id: str | None = None, task_id: str | None = None,
                 attempt: int = 1) -> list[dict]:
        if category not in CATEGORIES:
            raise AgentError('Skill retrieval category is unsupported.')
        problem = _bounded_text(problem, 'Skill retrieval query', 1000)
        if type(limit) is not int or not 1 <= limit <= 5:
            raise AgentError('Skill retrieval limit must be between one and five.')
        if type(attempt) is not int or not 1 <= attempt <= 100:
            raise AgentError('Skill retrieval attempt must be between one and 100.')
        query_tokens = set(re.findall(r'[a-z0-9_]{3,}', (category + ' ' + problem).casefold()))
        def change(registry):
            scored, changed = [], False
            for record in registry['skills'].values():
                current = self._current(record)
                if (record.get('project_id') != self.project.identity or current['status'] != 'approved'
                        or current.get('validation_state') != 'validated'):
                    continue
                try:
                    fresh = self._fresh_evidence(current)
                except AgentError:
                    self._version(record, {'status': 'revoked', 'validation_state': 'stale',
                        'review_note': 'Evidence became unavailable or stale.', 'validation': None},
                        'system', 'evidence_stale_revoked')
                    changed = True
                    continue
                text = (current['category'] + ' ' + current['problem'] + ' ' + current['procedure']).casefold()
                tokens = set(re.findall(r'[a-z0-9_]{3,}', text))
                overlap = len(query_tokens & tokens)
                if current['category'] not in {category, 'general'} and overlap == 0:
                    continue
                score = overlap / max(1, len(query_tokens))
                if current['category'] == category:
                    score += 0.25
                if any(ref.get('test_verified') for ref in fresh):
                    score += 0.1
                scored.append((score, record, current, fresh))
            scored.sort(key=lambda row: (-row[0], row[2]['category'], row[1]['skill_id']))
            results = []
            for score, record, current, fresh in scored[:limit]:
                if run_id and task_id:
                    if len(record['usage']) >= MAX_USAGE:
                        raise AgentError('Skill reuse history reached its bounded limit.')
                    if not any(item.get('run_id') == run_id and item.get('task_id') == task_id
                               and item.get('attempt') == attempt for item in record['usage']):
                        record['usage'].append({'run_id': run_id, 'task_id': task_id,
                            'attempt': attempt, 'version': current['version'],
                            'used_at': _now(), 'outcome': 'pending'})
                        changed = True
                results.append({'skill_id': record['skill_id'], 'version': current['version'],
                    'category': current['category'], 'problem': current['problem'],
                    'procedure': current['procedure'], 'trust_class': current['trust_class'],
                    'validation': current['validation'], 'evidence_sha256': [ref['sha256'] for ref in fresh],
                    'relevance': round(score, 3)})
            return results, changed
        return self._mutate('retrieve-skills', change)

    def record_outcome(self, *, run_id: str, task_id: str, task_state: str,
                       test_receipts_verified: bool, evidence_refs: list[Mapping[str, Any]],
                       attempt: int = 1) -> int:
        verified = []
        try:
            if evidence_refs:
                verified = self._verify_evidence(evidence_refs)
        except AgentError:
            verified = []
        success = task_state == 'agent_completed' and test_receipts_verified is True and any(
            ref.get('test_verified') for ref in verified)
        def change(registry):
            updates = 0
            for record in registry['skills'].values():
                for row in record['usage']:
                    if (row.get('run_id') == run_id and row.get('task_id') == task_id
                            and row.get('attempt') == attempt and row.get('outcome') == 'pending'):
                        row.update(outcome='supported' if success else 'unverified',
                                   outcome_recorded_at=_now(),
                                   host_task_state=task_state,
                                   test_receipt_verified=success,
                                   evidence_sha256=[ref['sha256'] for ref in verified])
                        updates += 1
            return updates, updates > 0
        return self._mutate('record-skill-outcome:' + run_id + ':' + task_id, change)

    def export_package(self, skill_id: str) -> dict:
        current = self._current(self._record(self._load_registry(), skill_id))
        if current['status'] != 'approved' or current.get('validation_state') != 'validated':
            raise AgentError('Only a currently approved skill can be explicitly exported.')
        payload = {'schema_version': SCHEMA_VERSION, 'source_project_id': self.project.identity,
            'skill_id': skill_id, 'category': current['category'], 'problem': current['problem'],
            'procedure': current['procedure'], 'trust_class': current['trust_class'],
            'source': current['source'],
            'evidence': [{'kind': ref['kind'], 'sha256': ref['sha256']} for ref in current['evidence_refs']],
            'exported_at': _now()}
        return {'payload': payload, 'sha256': digest(encoded(payload))}

    def import_package(self, package: Mapping[str, Any], *, operator: str) -> dict:
        operator = _bounded_text(operator, 'Operator identity', 128)
        if (not isinstance(package, Mapping) or set(package) != {'payload', 'sha256'}
                or not isinstance(package['payload'], Mapping)
                or not isinstance(package['sha256'], str)
                or digest(encoded(dict(package['payload']))) != package['sha256']):
            raise AgentError('Cross-project skill package integrity check failed.')
        payload = dict(package['payload'])
        if (payload.get('schema_version') != SCHEMA_VERSION
                or payload.get('source_project_id') == self.project.identity
                or payload.get('trust_class') not in TRUST_CLASSES
                or not isinstance(payload.get('source'), Mapping)
                or not isinstance(payload.get('evidence'), list)):
            raise AgentError('Cross-project package identity or schema is invalid.')
        category = payload.get('category')
        if category not in CATEGORIES:
            raise AgentError('Cross-project skill category is unsupported.')
        problem = _bounded_text(payload.get('problem'), 'Imported problem')
        procedure = _bounded_text(payload.get('procedure'), 'Imported procedure')
        skill_id = 'skill-' + digest((self.project.identity + '\0' + category + problem + procedure).encode())[:24]
        candidate = {'version': 1, 'status': 'candidate', 'validation_state': 'local_evidence_required',
            'category': category, 'problem': problem, 'procedure': procedure,
            'trust_class': 'reference', 'semantic_sha256': digest(encoded(payload)),
            'source_refs': [], 'evidence_refs': [], 'imported_evidence': payload.get('evidence', []),
            'source': {'project_id': self.project.identity, 'source_project_id': payload['source_project_id'],
                       'task_id': str(payload.get('source', {}).get('task_id', 'imported'))[:80],
                       'agent': 'operator_import', 'event_id': None},
            'confidence': 0.0, 'validation': None, 'created_at': _now(), 'updated_at': _now(),
            'actor': operator, 'change': 'cross_project_import_requires_local_validation'}
        def change(registry):
            if skill_id in registry['skills']:
                return {'skill_id': skill_id, 'version': self._current(registry['skills'][skill_id])['version'],
                        'deduplicated': True}, False
            if len(registry['skills']) >= MAX_SKILLS:
                raise AgentError('Project skill registry reached its bounded capacity.')
            registry['skills'][skill_id] = {'skill_id': skill_id, 'project_id': self.project.identity,
                                            'versions': [candidate], 'usage': []}
            return {'skill_id': skill_id, 'version': 1, 'deduplicated': False}, True
        return self._mutate('import-skill:' + skill_id, change)

    def recovery_status(self) -> dict:
        self._check_private_paths()
        if not self.pending_path.exists():
            return {'pending': False}
        try:
            pending = json.loads(self.pending_path.read_bytes())
            raw_after = base64.b64decode(pending['after_registry_b64'], validate=True)
            if (pending.get('schema_version') != SCHEMA_VERSION
                    or pending.get('project_id') != self.project.identity
                    or digest(raw_after) != pending.get('after_sha256')):
                raise ValueError('integrity mismatch')
            current = file_hash(self.registry_path) if self.registry_path.exists() else None
            classification = 'before' if current == pending.get('before_sha256') else 'after' if current == pending['after_sha256'] else 'conflict'
            return {'pending': True, 'transaction_id': pending.get('transaction_id'),
                    'classification': classification, 'before_sha256': pending.get('before_sha256'),
                    'after_sha256': pending['after_sha256'], 'actual_sha256': current}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError('Skill transaction journal is malformed; preserve it for manual review.') from exc

    def recover(self, *, action: str) -> dict:
        if action not in {'finish', 'discard'}:
            raise AgentError('Skill recovery action must be finish or discard.')
        with self._writer_lock('recover-skill-registry'):
            status = self.recovery_status()
            if not status['pending']:
                return status
            if status['classification'] == 'conflict':
                raise AgentError('Skill registry differs from both transaction hashes; preserve for manual recovery.')
            pending = json.loads(self.pending_path.read_bytes())
            raw_after = base64.b64decode(pending['after_registry_b64'], validate=True)
            if status['classification'] == 'before':
                if action == 'discard':
                    current = file_hash(self.registry_path) if self.registry_path.exists() else None
                    if current != pending.get('before_sha256'):
                        raise AgentError('Skill registry changed during recovery; journal retained.')
                    self.pending_path.unlink()
                    return {'pending': False, 'action': 'discarded_unapplied'}
                current = file_hash(self.registry_path) if self.registry_path.exists() else None
                if current != pending.get('before_sha256'):
                    raise AgentError('Skill registry changed during recovery; journal retained.')
                atomic_write(self.registry_path, raw_after)
                if file_hash(self.registry_path) != pending['after_sha256']:
                    raise AgentError('Recovered skill registry hash mismatch; journal retained.')
            elif action == 'discard':
                raise AgentError('Transaction already reached the after state; it cannot be discarded safely.')
            self.pending_path.unlink()
            return {'pending': False, 'action': 'finished', 'registry_sha256': pending['after_sha256']}

    @staticmethod
    def _record(registry: dict, skill_id: str) -> dict:
        skills = registry.get('skills', {}) if isinstance(registry, dict) else {}
        if not isinstance(skill_id, str) or skill_id not in skills:
            raise AgentError('Skill does not exist in this project.')
        return skills[skill_id]
