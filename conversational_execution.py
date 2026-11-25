"""Conversation-bound, one-use approvals for repository registration and pinned tests.

This module owns proposals and presentation data only. Test processes are always
started by the existing Windows Broker; persisted proposals never dispatch work.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from agent_core import AgentError, Project, atomic_write, encoded, exclusive, file_hash, no_secrets
from coding_workflow import TEST_PROFILE_CONFIGS
from execution_recovery import RecoveryStore, _owner_exited
from local_agent import Broker, HUB, verify_test_receipts
from project_intelligence import _regular_project_root


_LOCK = threading.RLock()
_SENSITIVE = {'.local', '.ssh', '.aws', '.azure', '.gnupg', '.codex', '.config',
              'appdata', 'programdata', 'windows', 'system32', 'secrets',
              'credentials', 'models', 'weights', 'recordings'}
_ID = re.compile(r'^[a-f0-9]{32}$')
_REGISTRATION = re.compile(r'^register (?:the )?(?:project|repository) (?:at )?(.+)$', re.I)
_EDIT_TARGET = re.compile(r'\b(?:in|to|for)\s+["`]?([A-Za-z0-9_./-]+\.(?:py|js|jsx|ts|tsx|cs|java|go|rs|c|h|cpp|hpp|swift|kt|rb|php))["`]?\b', re.I)
_EDIT_FILES = re.compile(r'(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+\.(?:py|js|jsx|ts|tsx|cs|java|go|rs|c|h|cpp|hpp|swift|kt|rb|php))(?![A-Za-z0-9_./-])', re.I)


class _ApprovedTargetChanged(AgentError):
    """The approved repository no longer matches the proposal fingerprint."""


def _test_summary(output):
    total = re.search(r'^Ran (\d+) tests?\b', output, re.M)
    failures = re.search(r'^FAILED(?: \(([^)]*)\))?\r?$', output, re.M)
    failed = 0
    if failures and failures.group(1):
        failed = sum(int(count) for count in re.findall(r'(?:failures|errors)=(\d+)', failures.group(1)))
    failed_tests = re.findall(r'^([A-Za-z_][\w.]*) \([^\n]*\) \.\.\. (?:FAIL|ERROR)\r?$', output, re.M)
    count = int(total.group(1)) if total else None
    return {'test_count': count, 'passed_count': max(0, count - failed) if count is not None else None,
            'failed_count': failed, 'failed_tests': failed_tests}


def _now():
    return datetime.now(timezone.utc).timestamp()


def _fingerprint(project):
    state = project.state()
    return hashlib.sha256(encoded({'id': project.identity, 'root': str(project.root),
                                   'head': state['commit'], 'status': state['status'],
                                   'files': state['files']})).hexdigest()


def _local_python_dependencies(project, paths, authorized_paths):
    """Read a small, source-hash-bound set of local Python imports as untrusted context."""
    import ast
    references = []
    seen = set(authorized_paths)
    remaining = 8192
    for relative in paths:
        try:
            source = project.path(relative).read_text(encoding='utf-8-sig')
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeError, SyntaxError):
            continue
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        for module in modules:
            parts = module.split('.')
            candidates = [('/'.join(parts) + '.py'), ('/'.join(parts) + '/__init__.py')]
            for candidate in candidates:
                if candidate in seen or any(part.casefold() in {
                        '.local', 'vendor', 'generated', 'fixtures', '.git'} for part in Path(candidate).parts):
                    continue
                try:
                    target = project.path(candidate)
                    if not target.is_file() or target.stat().st_size > min(4096, remaining):
                        continue
                    raw = target.read_bytes()
                    text = raw.decode('utf-8-sig')
                    no_secrets(text)
                except (AgentError, OSError, UnicodeError):
                    continue
                references.append({'path': candidate, 'sha256': file_hash(target), 'content': text})
                seen.add(candidate)
                remaining -= len(raw)
                break
            if len(references) >= 6 or remaining <= 0:
                return references
    return references


class ConversationalExecution:
    """Persist approval state under the application's private chat directory."""

    def __init__(self, application, project_intelligence, chat_store):
        self.application = Path(application.root)
        self.projects = project_intelligence
        self.chat = chat_store
        self.root = chat_store.root / 'operations'
        self.profile_root = HUB / '.local' / 'chat' / 'test-profiles'
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def intent(text, selected_project):
        if not isinstance(text, str):
            return None
        value = text.strip()
        match = _REGISTRATION.fullmatch(value)
        if match:
            return {'kind': 'register', 'root': match.group(1).strip().strip('"\'')}
        folded = value.casefold().rstrip('.!?')
        if selected_project and folded in {
                'show me the results of the previous test run',
                'which tests failed', 'tell me which tests failed',
                'explain why the tests failed', 'why did the tests fail'}:
            return {'kind': 'previous'}
        if selected_project and folded in {
                'run the approved tests for this project', 'run approved tests',
                'run the pinned tests', 'run tests for this project'}:
            return {'kind': 'tests', 'profile': 'auto'}
        if selected_project and re.search(r'\b(fix|change|modify|add|implement|correct|update)\b', folded):
            files = list(dict.fromkeys(match.group(1).replace('\\', '/') for match in _EDIT_FILES.finditer(value)))
            if 1 <= len(files) <= 3:
                return ({'kind': 'multi_patch', 'paths': files, 'task': value}
                        if len(files) >= 2 else {'kind': 'patch', 'path': files[0], 'task': value})
        return None

    def propose_patch(self, conversation_id, project_id, relative_path, task,
                      selected_model, chat_runner, cancel_event=None):
        """Generate a strict single or coordinated multi-file conversational proposal."""
        from coding_workflow import CodingWorkflow, TEST_PROFILE_CONFIGS
        from project_intelligence import ProjectIntelligence
        row, project = self.projects._record(project_id)
        paths = ((relative_path,) if isinstance(relative_path, str)
                 else tuple(relative_path) if isinstance(relative_path, (list, tuple)) else ())
        if not 1 <= len(paths) <= 3 or len({path.casefold() for path in paths}) != len(paths):
            raise AgentError('Choose one to three distinct existing project source files.')
        names = {name.replace('\\', '/') for name in ProjectIntelligence._file_inventory(project)}
        if any(path not in names for path in paths):
            raise AgentError('Every target must be an existing file in the registered project inventory.')
        profile = next((name for name, config in TEST_PROFILE_CONFIGS.items()
                        if (project.root / config['test_file']).is_file()
                        and file_hash(project.root / config['test_file']) == config['test_sha256']), None)
        if profile is None:
            raise AgentError('No trusted pinned test profile matches this project; no patch was generated.')
        source_hashes = {}
        for relative in paths:
            target = project.path(relative)
            parts = {part.casefold() for part in Path(relative).parts}
            is_test = any(part in {'tests', 'test'} for part in parts)
            if (not target.is_file() or target.stat().st_size > 8192
                    or any(part in {'fixtures', 'vendor', 'generated', '.local'} for part in parts)
                    or (is_test and (len(paths) == 1
                                     or relative == TEST_PROFILE_CONFIGS[profile]['test_file']))):
                raise AgentError('Targets must be small existing source files; the trusted pinned test file is immutable.')
            try:
                source = target.read_bytes().decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise AgentError('A target is not supported UTF-8 source.') from exc
            if not source or '\x00' in source:
                raise AgentError('Empty or binary source files cannot be modified conversationally.')
            no_secrets(source)
            source_hashes[relative] = file_hash(target)
        profile_path = self.profile_root / project_id / (profile + '.json')
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        config = TEST_PROFILE_CONFIGS[profile]
        if profile_path.exists():
            if json.loads(profile_path.read_bytes()) != config:
                raise AgentError('Application pinned test profile changed; proposal refused.')
        else:
            atomic_write(profile_path, encoded(config))
        dependencies = _local_python_dependencies(project, paths, paths)
        workflow = CodingWorkflow(project, task, paths, test_profile=profile,
                                  conversational=True, profile_path=profile_path,
                                  minimum_files=2 if len(paths) > 1 else 1,
                                  allow_test_files=len(paths) > 1,
                                  reference_context=dependencies)
        model_name = selected_model.get('model_id') if isinstance(selected_model, dict) else str(selected_model)
        provider_id = selected_model.get('provider_id', 'ollama') if isinstance(selected_model, dict) else 'ollama'

        class ChatProvider:
            def chat(self, model, messages, tools, context, options, timeout=120):
                pieces = []
                chat_runner(messages, selected_model, cancel_event, pieces.append)
                return {'message': {'content': ''.join(pieces)}, 'done': True}

        proposal = workflow.generate(ChatProvider(), model_name, timeout=180,
            cancel_event=cancel_event, max_attempts=2, provider_id=provider_id,
            defer_approval=False, run_simulation_tests=False)
        complete_diff = proposal['diff']
        if len(complete_diff.encode('utf-8')) > 24_000:
            raise AgentError('Validated change-set diff exceeds the chat review limit; narrow the requested files.')
        changed_source_hashes = {name: source_hashes[name] for name in proposal['files']}
        change_set_sha = hashlib.sha256(encoded({
            'project_id': project_id, 'root': str(project.root),
            'fingerprint': _fingerprint(project), 'authorized_files': list(paths),
            'files': proposal['files'],
            'source_hashes': source_hashes, 'changed_source_hashes': changed_source_hashes,
            'patch_sha256': proposal['patch_sha256'],
            'expected_post_images': proposal['expected_post_images'], 'task': task[:2000],
            'profile': profile, 'test_config_sha256': workflow.test_config_hash,
            'dependencies': [{'path': item['path'], 'sha256': item['sha256']}
                             for item in dependencies]})).hexdigest()
        target_data = {'kind': 'patch' if len(paths) == 1 else 'multi_patch',
            'project_id': project_id,
            'root': str(project.root), 'fingerprint': _fingerprint(project),
            'display_name': row['display_name'], 'workflow_id': workflow.run_id,
            'path': proposal['files'][0] if len(paths) == 1 else None,
            'paths': proposal['files'], 'files': proposal['files'],
            'authorized_files': list(paths), 'source_hashes': source_hashes,
            'changed_source_hashes': changed_source_hashes,
            'dependencies': [{'path': item['path'], 'sha256': item['sha256']}
                             for item in dependencies],
            'change_set_sha256': change_set_sha,
            'test_config_sha256': workflow.test_config_hash,
            'task': task[:2000], 'patch_sha256': proposal['patch_sha256'],
            'expected_post_images': proposal['expected_post_images'],
            'diff': complete_diff, 'validation': proposal['validation_report'],
            'profile': profile, 'host': 'Windows Broker',
            'working_directory': str(project.root),
            'side_effects': ('approval writes only the displayed existing files and private Broker recovery evidence; '
                             'tests need separate approval'),
            'limits': 'at most three existing UTF-8 files; strict complete-set parser and per-file hashes; fixed pinned tests after separate approval'}
        return self._create(conversation_id, target_data)

    def _apply_patch(self, path, row, cancel_event):
        from coding_workflow import CodingWorkflow
        target = row['target']
        is_multi = target.get('kind') == 'multi_patch'
        files = target.get('files') if is_multi else [target.get('path')]
        project = None
        try:
            _, project = self.projects._record(target['project_id'])
            if str(project.root) != target['root'] or _fingerprint(project) != target['fingerprint']:
                raise _ApprovedTargetChanged('Project changed after proposal; request a fresh source review.')
            calculated_set = hashlib.sha256(encoded({
                'project_id': target['project_id'], 'root': target['root'],
                'fingerprint': target['fingerprint'],
                'authorized_files': target.get('authorized_files', files), 'files': files,
                'source_hashes': target['source_hashes'],
                'changed_source_hashes': target['changed_source_hashes'],
                'patch_sha256': target['patch_sha256'],
                'expected_post_images': target['expected_post_images'],
                'task': target['task'], 'profile': target['profile'],
                'test_config_sha256': target['test_config_sha256'],
                'dependencies': target['dependencies']})).hexdigest()
            if (calculated_set != target.get('change_set_sha256')
                    or not 1 <= len(target.get('authorized_files', files)) <= 3
                    or len({name.casefold() for name in target.get('authorized_files', files)})
                       != len(target.get('authorized_files', files))
                    or not set(files) <= set(target.get('authorized_files', files))
                    or set(target['source_hashes']) != set(target.get('authorized_files', files))
                    or set(target['changed_source_hashes']) != set(files)
                    or set(target['expected_post_images']) != set(files)):
                raise AgentError('Approved change-set integrity or exact file inventory is invalid.')
            if cancel_event is not None and cancel_event.is_set():
                raise AgentError('Patch application cancelled before the write checkpoint.')
            workflow = CodingWorkflow.load(project, target['workflow_id'])
            if (workflow.row.get('patch_sha256') != target['patch_sha256']
                    or workflow.row.get('files') != files
                    or workflow.row.get('allowed_paths') != target.get('authorized_files', files)
                    or workflow.row.get('test_config_sha256') != target.get('test_config_sha256')
                    or workflow.row.get('expected_post_images') != target['expected_post_images']
                    or {name: workflow.baseline['files'].get(name)
                        for name in target.get('authorized_files', files)}
                       != target['source_hashes']):
                raise AgentError('Stored patch or source hashes no longer match the approved change set.')
            workflow.approve(patch_sha256=target['patch_sha256'], repository=str(project.root),
                operator='LocalCodingAgent chat operator', test_command=workflow.row['test_command'])
            result = (workflow.apply_change_set(operator='LocalCodingAgent chat operator',
                                                cancel_event=cancel_event)
                      if is_multi else workflow.apply_only(operator='LocalCodingAgent chat operator'))
            actual_hashes = {name: file_hash(project.path(name)) for name in files
                             if project.path(name).is_file()}
            if actual_hashes != target['expected_post_images']:
                raise AgentError('Applied change set differs from approved post-images; preserve evidence and reconcile.')
            journal = RecoveryStore(project)
            report = journal.inspect(target['workflow_id'])
            recovery_row, _ = journal.load(target['workflow_id'])
            writes = [item for item in recovery_row.get('operations', [])
                      if item.get('kind') == 'project_write' and item.get('state') == 'recorded']
            write_checks = {item.get('path'): item for item in report.get('writes', [])}
            if (recovery_row.get('status') != 'complete' or report.get('status') != 'complete'
                    or len(writes) != len(files) or set(write_checks) != set(files)
                    or any(write_checks[name].get('actual') != target['expected_post_images'][name]
                           or write_checks[name].get('checkpoint_sha256') != target['expected_post_images'][name]
                           for name in files)):
                raise AgentError('Broker write receipt or recovery journal did not verify; reconciliation required.')
            result.update(path=target.get('path'), paths=files, receipt_verified=True,
                post_image_sha256=actual_hashes.get(target.get('path')) if not is_multi else None,
                post_image_sha256_by_path=actual_hashes, test_profile=target['profile'],
                test_authorization_required=True,
                write_receipt={'run_id': target['workflow_id'], 'operations': writes},
                recovery_status='complete')
            self._finish(path, row, 'modified', result)
            self._audit(row, 'modified', {'paths': files,
                'post_image_sha256_by_path': actual_hashes,
                'patch_sha256': target['patch_sha256'],
                'change_set_sha256': target['change_set_sha256'], 'receipt_verified': True})
            return {'status': 'modified', 'operation_id': row['id'], **result}
        except Exception as exc:
            reconciliation = isinstance(exc, OSError) or any(word in str(exc).casefold()
                for word in ('reconcil', 'post-image', 'receipt', 'interrupted'))
            try:
                if project is None:
                    raise AgentError('Registered project is unavailable for workflow reconciliation.')
                workflow = CodingWorkflow.load(project, target['workflow_id'])
                reconciliation = reconciliation or workflow.row.get('state') in {'applying', 'interrupted'}
            except (AgentError, OSError, ValueError, TypeError, KeyError):
                pass
            state = 'interrupted' if reconciliation else 'failed'
            result = {'error': str(exc)[:240], 'receipt_verified': False,
                      'reconciliation_required': reconciliation,
                      'target_changed': isinstance(exc, _ApprovedTargetChanged)}
            self._finish(path, row, state, result)
            self._audit(row, state, result)
            return {'status': state, 'operation_id': row['id'], **result}

    def _path(self, op_id):
        if not isinstance(op_id, str) or not _ID.fullmatch(op_id):
            raise AgentError('Operation identifier is invalid.')
        return self.root / (op_id + '.json')

    def propose_registration(self, conversation_id, root):
        # Project constructor establishes the Git boundary; validation rejects
        # traversal/reparse ancestors before canonicalization.
        project = _regular_project_root(root)
        if any(part.casefold() in _SENSITIVE for part in project.root.parts):
            raise AgentError('Repositories inside sensitive or application-managed directories cannot be registered.')
        repo = project.git('rev-parse', '--show-toplevel').strip()
        if Path(repo).resolve() != project.root:
            raise AgentError('Choose the Git repository root, not a nested folder.')
        return self._create(conversation_id, {'kind': 'register', 'root': str(project.root),
            'project_id': project.identity, 'fingerprint': _fingerprint(project),
            'display_name': project.root.name, 'scope': 'local read-only indexing'})

    def propose_tests(self, conversation_id, project_id, profile='stats_multi_module_v1'):
        if profile == 'auto':
            _, project = self.projects._record(project_id)
            profile = next((name for name, config in TEST_PROFILE_CONFIGS.items()
                            if (project.root / config['test_file']).is_file()
                            and file_hash(project.root / config['test_file']) == config['test_sha256']), None)
        if profile not in TEST_PROFILE_CONFIGS:
            raise AgentError('The requested test profile is not trusted.')
        row, project = self.projects._record(project_id)
        self._assert_no_unresolved_patch(project_id)
        return self._create(conversation_id, {'kind': 'tests', 'root': str(project.root),
            'project_id': project.identity, 'fingerprint': _fingerprint(project),
            'display_name': row['display_name'], 'profile': profile,
            'coding_provenance': self._latest_patch_provenance(conversation_id, project.identity),
            'test_file': TEST_PROFILE_CONFIGS[profile]['test_file'],
            'host': 'Windows Broker', 'working_directory': str(project.root),
            'side_effects': ('read project source; write private application receipt/output and Broker recovery evidence; '
                'after a verified pass, add ignored project-local provenance and an unpromoted SkillLibrary candidate'),
            'limits': 'fixed pinned pure-Python suite; 60 second command timeout; 32 KiB output cap'})

    def _assert_no_unresolved_patch(self, project_id):
        for operation_path in self.root.glob('*.json'):
            try:
                row = json.loads(operation_path.read_bytes())
            except (OSError, ValueError):
                continue
            target, result = row.get('target', {}), row.get('result', {})
            if (target.get('project_id') == project_id
                    and target.get('kind') in {'patch', 'multi_patch'}
                    and (row.get('state') in {'consumed_approve', 'executing', 'validating', 'interrupted'}
                         or isinstance(result, dict) and result.get('reconciliation_required') is True)):
                raise AgentError('A source change operation needs recovery review before tests can be proposed: '
                                 + str(row.get('id', 'unknown')))

    def _latest_patch_provenance(self, conversation_id, project_id):
        rows = []
        for path in self.root.glob('*.json'):
            try:
                row = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            target, result = row.get('target', {}), row.get('result', {})
            if (row.get('conversation_id') == conversation_id and row.get('state') == 'modified'
                    and target.get('kind') in {'patch', 'multi_patch'} and target.get('project_id') == project_id
                    and result.get('receipt_verified') is True):
                rows.append((row.get('updated', row.get('created', 0)), row, target, result))
        if not rows:
            return None
        _, row, target, result = max(rows, key=lambda item: item[0])
        return {key: target.get(key) for key in (
            'workflow_id', 'path', 'paths', 'authorized_files', 'source_hashes',
            'changed_source_hashes', 'patch_sha256', 'change_set_sha256',
            'expected_post_images', 'dependencies')} | {
            'operation_id': row.get('id'),
            'post_image_sha256': result.get('post_image_sha256')}

    def _create(self, conversation_id, target):
        if not _ID.fullmatch(conversation_id):
            raise AgentError('Conversation identifier is invalid.')
        self.chat.load(conversation_id)
        op_id = secrets.token_hex(16)
        record = {'schema': 1, 'id': op_id, 'conversation_id': conversation_id,
                  'state': 'awaiting_approval', 'created': _now(), 'expires': _now() + 300,
                  'target': target}
        with _LOCK:
            self._audit(record, 'awaiting_approval')
            atomic_write(self._path(op_id), encoded(record))
        return {'operation_id': op_id, 'expires_in_seconds': 300, **target}

    def _load(self, conversation_id, op_id):
        path = self._path(op_id)
        try:
            row = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise AgentError('Approval proposal is unavailable or already consumed.') from exc
        if row.get('conversation_id') != conversation_id:
            raise AgentError('Approval belongs to another conversation.')
        if row.get('state') != 'awaiting_approval' or _now() >= row.get('expires', 0):
            if row.get('state') == 'awaiting_approval':
                row['state'] = 'expired'
                atomic_write(path, encoded(row))
            raise AgentError('Approval has expired or was already consumed.')
        return path, row

    def decide(self, conversation_id, op_id, decision, cancel_event=None):
        if decision not in {'approve', 'deny'}:
            raise AgentError('Choose approve or deny.')
        with _LOCK, exclusive(self.root / (op_id + '.decision.lock'), 'chat-operation-decision'):
            path, row = self._load(conversation_id, op_id)
            self._audit(row, 'approval_' + decision)
            row['state'] = 'consumed_' + decision
            row['consumed'] = _now()
            if decision == 'approve':
                row['owner_pid'] = os.getpid()
            atomic_write(path, encoded(row))  # consume before any side effect
        target = row['target']
        if decision == 'deny':
            self._finish(path, row, 'denied', {'approved': False})
            self._audit(row, 'denied')
            return {'status': 'denied', 'operation_id': op_id}
        if target['kind'] == 'register':
            try:
                project = _regular_project_root(target['root'])
                if (str(project.root) != target['root'] or project.identity != target['project_id']
                        or _fingerprint(project) != target['fingerprint']):
                    raise _ApprovedTargetChanged(
                        'Repository changed after the proposal; request a fresh confirmation.')
            except (AgentError, OSError, ValueError) as exc:
                error = (exc if isinstance(exc, _ApprovedTargetChanged) else
                         _ApprovedTargetChanged(
                             'Repository path or contents changed after the proposal; request a fresh confirmation.'))
                self._finish(path, row, 'failed', {'error': str(error),
                    'target_changed': True, 'receipt_verified': False,
                    'reconciliation_required': False})
                self._audit(row, 'failed', {'target_changed': True,
                    'reconciliation_required': False})
                return {'status': 'failed', 'operation_id': op_id,
                    'error': str(error), 'target_changed': True,
                    'reconciliation_required': False}
            result = self.projects.register(target['root'], confirmed=True)
            try:
                indexed = self.projects.refresh(result['project_id'])
            except Exception as exc:
                self._finish(path, row, 'partial', {'registration': result,
                    'error': str(exc)[:200], 'reconciliation_required': True})
                self._audit(row, 'partial', {'project_id': result['project_id'],
                                             'error': str(exc)[:200]})
                return {'status': 'partial', 'operation_id': op_id,
                        'project_id': result['project_id'], 'error': str(exc)[:200]}
            self._finish(path, row, 'completed', {'registration': result, 'index': indexed})
            self._audit(row, 'completed', {'project_id': result['project_id'],
                                           'indexed_files': indexed.get('indexed_files', 0)})
            return {'status': 'completed', 'operation_id': op_id,
                    'project_id': result['project_id'], 'display_name': result['display_name'],
                    'indexed_files': indexed.get('indexed_files', 0)}
        if target['kind'] in {'patch', 'multi_patch'}:
            return self._apply_patch(path, row, cancel_event)
        try:
            return self._run_tests(path, row, cancel_event)
        except _ApprovedTargetChanged as exc:
            result = {'error': str(exc), 'target_changed': True,
                      'receipt_verified': False, 'reconciliation_required': False}
            self._finish(path, row, 'failed', result)
            self._audit(row, 'failed', {'target_changed': True,
                                        'reconciliation_required': False})
            return {'status': 'failed', 'operation_id': op_id, **result}
        except Exception as exc:
            # A consumed approval is terminal. The Broker recovery journal, if
            # present, is authoritative about whether execution needs review.
            if row.get('state') == 'consumed_approve':
                self._finish(path, row, 'failed', {'error': str(exc)[:240],
                    'reconciliation_required': True})
            raise

    def _audit(self, row, state, result=None):
        dispatcher = getattr(self, 'audit_dispatcher', None)
        if dispatcher is None:
            raise AgentError('Capability audit storage is unavailable; operation was not run.')
        target = row['target']
        capability = ('project.register' if target['kind'] == 'register' else
                      'coding.patch' if target['kind'] in {'patch', 'multi_patch'} else 'tests.run')
        dispatcher._write_audit(row['id'], state, row['conversation_id'], capability,
            target.get('project_id'), target,
            encoded(result) if result is not None else None)

    def _run_tests(self, path, row, cancel_event):
        target = row['target']
        project_id = target['project_id']
        try:
            _, project = self.projects._record(project_id)
        except (AgentError, OSError, ValueError) as exc:
            raise _ApprovedTargetChanged(
                'Project registration changed after the proposal; request a fresh test confirmation.') from exc
        store = RecoveryStore(project)
        with store.locked():
            store.assert_clear()
            self._assert_no_unresolved_patch(project_id)
            return self._run_tests_locked(path, row, cancel_event, store)

    def _run_tests_locked(self, path, row, cancel_event, store):
        from coding_workflow import TEST_PROFILE_CONFIGS
        target = row['target']
        project_id = target['project_id']
        try:
            _, project = self.projects._record(project_id)
            current_fingerprint = _fingerprint(project)
        except (AgentError, OSError, ValueError) as exc:
            raise _ApprovedTargetChanged(
                'Project path or contents changed after the proposal; request a fresh test confirmation.') from exc
        if str(project.root) != target['root'] or current_fingerprint != target['fingerprint']:
            raise _ApprovedTargetChanged(
                'Project changed after the proposal; request a fresh test confirmation.')
        profile = target['profile']
        config = TEST_PROFILE_CONFIGS[profile]
        profile_dir = self.profile_root / project_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        config_path = profile_dir / (profile + '.json')
        if config_path.exists():
            if json.loads(config_path.read_bytes()) != config:
                raise AgentError('Application pinned profile changed; execution refused.')
        else:
            atomic_write(config_path, encoded(config))
        run_id = row['id']
        output = self.root / (run_id + '-evidence')
        output.mkdir(parents=True, exist_ok=False)
        journal = store.start(output, (), None, (), ['tests'], config_path,
                              ['command'], run_id=row['id'])
        journal.row['status'] = 'executing'
        journal.save()
        broker = Broker(project, output, test_config=config_path, allowed_tools={'tests'})
        broker.journal = journal
        broker.cancel_event = cancel_event
        began = time.monotonic()
        self._set_state(path, row, 'executing')
        try:
            result = broker.execute('tests')
            self._set_state(path, row, 'validating')
            verified_status, integrity = verify_test_receipts(broker.commands, project, output)
            if integrity.get('valid') is not True:
                raise AgentError('Broker receipt did not verify; preserve this run for reconciliation.')
            # A nonzero test exit is a verified completed execution. Keep Broker
            # recovery distinct from the test outcome so the project isn't locked
            # behind a false interrupted-operation report.
            journal.close('complete')
            report = {'status': 'passed' if result['exit_code'] == 0 else 'failed',
                      'test_status': verified_status, 'exit_code': result['exit_code'],
                      **_test_summary(result['output']),
                      'duration_seconds': round(time.monotonic() - began, 3),
                      'output': result['output'], 'receipt': result,
                      'receipt_reference': broker.commands[0],
                      'receipt_verified': True, 'execution_host': 'Windows Broker',
                      'profile': profile, 'project_id': project_id,
                      'output_is_untrusted_evidence': True}
            if report['status'] == 'passed':
                try:
                    report['learning_candidate'] = self._capture_candidate(
                        project, profile, run_id, broker.commands[0],
                        coding_provenance=target.get('coding_provenance'))
                except Exception as exc:
                    # Learning is an evidence sidecar. It cannot change the
                    # already verified execution outcome or auto-promote a skill.
                    report['learning_candidate'] = {'status': 'not_recorded',
                                                     'error': str(exc)[:180]}
            self._finish(path, row, report['status'], report)
            self._audit(row, report['status'], {'exit_code': report['exit_code'],
                'receipt_verified': True, 'receipt_reference': broker.commands[0]})
            return {'status': report['status'], 'operation_id': row['id'],
                    'exit_code': report['exit_code'], 'duration_seconds': report['duration_seconds'],
                    'test_count': report['test_count'], 'passed_count': report['passed_count'],
                    'failed_count': report['failed_count'], 'failed_tests': report['failed_tests'],
                    'receipt_verified': True, 'receipt_reference': broker.commands[0],
                    'learning_candidate': report.get('learning_candidate'),
                    'output': result['output']}
        except Exception as exc:
            cancelled = bool(cancel_event and cancel_event.is_set())
            timed_out = 'timed out' in str(exc).casefold()
            reconciliation = journal.pending is not None or journal.row.get('status') == 'interrupted'
            state = 'cancelled' if cancelled else 'timed_out' if timed_out else 'failed'
            if journal.row['status'] in {'authorized', 'executing'} and journal.pending is None:
                journal.close('complete' if cancelled else 'failed')
            self._finish(path, row, state,
                         {'error': str(exc)[:240], 'receipt_verified': False,
                          'timed_out': timed_out,
                          'reconciliation_required': reconciliation})
            self._audit(row, state, {'timed_out': timed_out,
                                     'receipt_verified': False,
                                     'reconciliation_required': reconciliation})
            return {'status': state, 'operation_id': row['id'],
                    'error': str(exc)[:240], 'timed_out': timed_out,
                    'receipt_verified': False,
                    'reconciliation_required': reconciliation}

    @staticmethod
    def _capture_candidate(project, profile, run_id, receipt_reference, *, coding_provenance=None):
        """Stage verified test evidence into the existing project SkillLibrary."""
        from skill_library import SkillLibrary

        library = SkillLibrary(project)  # refuses to write unless .local is ignored
        root = project.root / '.local' / 'orchestration' / 'runs' / run_id
        output_root = root / 'outputs'
        for path in (project.root / '.local', project.root / '.local' / 'orchestration',
                     project.root / '.local' / 'orchestration' / 'runs', root):
            if path.is_symlink() or (path.exists()
                    and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
                raise AgentError('Candidate evidence path contains a link or reparse point.')
        root.mkdir(parents=True, exist_ok=False)
        output_root.mkdir()
        source_receipt = Path(receipt_reference['path'])
        if source_receipt.is_symlink() or file_hash(source_receipt) != receipt_reference['sha256']:
            raise AgentError('Verified Broker receipt changed before candidate capture.')
        receipt = json.loads(source_receipt.read_bytes())
        source_output = Path(receipt['output_path'])
        if source_output.is_symlink() or file_hash(source_output) != receipt.get('output_sha256'):
            raise AgentError('Verified Broker output changed before candidate capture.')
        output_path = output_root / 'command-0.txt'
        atomic_write(output_path, source_output.read_bytes())
        receipt['output_path'] = str(output_path)
        staged_receipt = output_root / 'command-0.json'
        atomic_write(staged_receipt, encoded(receipt))
        profile_path = output_root / 'pinned-profile.json'
        atomic_write(profile_path, encoded(TEST_PROFILE_CONFIGS[profile]))
        provenance_path = output_root / 'broker-source.json'
        atomic_write(provenance_path, encoded({
            'operation_id': run_id, 'project_id': project.identity,
            'profile': profile, 'source_receipt': str(source_receipt),
            'source_receipt_sha256': receipt_reference['sha256'],
            'source_output_sha256': receipt.get('output_sha256')}))
        state = {'schema_version': 1,
                 'graph': {'run_id': run_id, 'project_id': project.identity},
                 'project_baseline': {'project_id': project.identity},
                 'tasks': {'conversational_test': {'role': 'qa', 'status': 'agent_completed'}}}
        atomic_write(root / 'state.json', encoded(state))
        refs = [
            {'kind': 'command_receipt', 'uri': str(staged_receipt),
             'sha256': file_hash(staged_receipt), 'run_id': run_id},
            {'kind': 'artifact', 'uri': str(output_path),
             'sha256': file_hash(output_path), 'run_id': run_id},
            {'kind': 'artifact', 'uri': str(provenance_path),
             'sha256': file_hash(provenance_path), 'run_id': run_id},
        ]
        source_refs = [{'kind': 'artifact', 'uri': str(profile_path),
                        'sha256': file_hash(profile_path), 'run_id': run_id}]
        if isinstance(coding_provenance, dict):
            workflow_id = coding_provenance.get('workflow_id')
            if not isinstance(workflow_id, str) or not _ID.fullmatch(workflow_id):
                raise AgentError('Coding learning provenance identifier is invalid.')
            patch_path = project.root / '.local' / 'coding-workflows' / workflow_id / 'approved.patch'
            if (patch_path.is_symlink() or not patch_path.is_file()
                    or file_hash(patch_path) != coding_provenance.get('patch_sha256')):
                raise AgentError('Coding patch evidence changed before candidate capture.')
            patch_artifact = output_root / 'source-patch.diff'
            atomic_write(patch_artifact, patch_path.read_bytes())
            source_refs.append({'kind': 'artifact', 'uri': str(patch_artifact),
                'sha256': file_hash(patch_artifact), 'run_id': run_id})
            provenance_path = output_root / 'coding-provenance.json'
            atomic_write(provenance_path, encoded(coding_provenance))
            refs.append({'kind': 'artifact', 'uri': str(provenance_path),
                'sha256': file_hash(provenance_path), 'run_id': run_id})
        candidate = library.propose(
            category='testing',
            problem='A fixed pinned test profile completed successfully for this registered project.',
            procedure=(f'After future changes, rerun the same pinned profile ({profile}) through the Windows Broker. '
                       'Inspect the bounded test output and verified Broker receipt before relying on the result.'),
            source_refs=source_refs, evidence_refs=refs, run_id=run_id,
            task_id='conversational_test', source_agent='qa',
            source_event_id='conversational-test-' + run_id)
        return {'status': 'candidate', **candidate, 'promoted': False}

    def _set_state(self, path, row, state):
        row['state'] = state
        row['updated'] = _now()
        atomic_write(path, encoded(row))

    def _finish(self, path, row, state, result):
        row.update(state=state, result=result, updated=_now())
        atomic_write(path, encoded(row))

    def previous_run(self, conversation_id, project_id):
        rows = []
        for path in self.root.glob('*.json'):
            try:
                row = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if (row.get('conversation_id') == conversation_id
                    and row.get('target', {}).get('project_id') == project_id
                    and row.get('target', {}).get('kind') == 'tests'
                    and isinstance(row.get('result'), dict)):
                rows.append(row)
        if not rows:
            return {'status': 'none'}
        latest = max(rows, key=lambda item: item.get('updated', item.get('created', 0)))
        result = self._verified_saved_result(latest)
        return {'status': latest['state'], 'operation_id': latest['id'],
                'result': result}

    def _verified_saved_result(self, row):
        result = dict(row.get('result', {}))
        reference = result.get('receipt_reference')
        if result.get('receipt_verified') is not True or not isinstance(reference, dict):
            return result
        try:
            _, project = self.projects._record(row['target']['project_id'])
            output = Path(reference['path']).parent.resolve(strict=True)
            if not output.is_relative_to(self.root.resolve(strict=True)):
                raise AgentError('Stored Broker receipt is outside private operation evidence.')
            status, integrity = verify_test_receipts([reference], project, output)
            if integrity.get('valid') is not True:
                return {'status': 'receipt_invalid', 'receipt_verified': False,
                        'evidence_errors': integrity.get('errors', [])}
            result['receipt_verified'] = True
            result['historical_test_status'] = status
            return result
        except (AgentError, OSError, ValueError, TypeError, KeyError):
            return {'status': 'receipt_invalid', 'receipt_verified': False}

    def inspect(self, conversation_id, op_id):
        path = self._path(op_id)
        try:
            row = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise AgentError('Operation record is unavailable.') from exc
        if row.get('conversation_id') != conversation_id:
            raise AgentError('Operation belongs to another conversation.')
        if row.get('state') == 'awaiting_approval' and _now() >= row.get('expires', 0):
            row['state'] = 'expired'
            atomic_write(path, encoded(row))
        recovery = None
        active_states = {'consumed_approve', 'executing', 'validating', 'interrupted'}
        needs_recovery = (row.get('state') in active_states
            or isinstance(row.get('result'), dict)
            and row['result'].get('reconciliation_required') is True)
        if row.get('target', {}).get('kind') in {'patch', 'multi_patch'} and needs_recovery:
            from coding_workflow import CodingWorkflow
            target = row['target']
            owner_pid = row.get('owner_pid')
            if row.get('state') in {'consumed_approve', 'executing', 'validating'} \
                    and owner_pid and not _owner_exited(owner_pid):
                return {'operation_id': op_id, 'state': row.get('state'), 'kind': 'patch',
                        'result': None, 'recovery': None}
            try:
                _, project = self.projects._record(target['project_id'])
                workflow = CodingWorkflow.load(project, target['workflow_id'])
                is_multi = target.get('kind') == 'multi_patch'
                files = target.get('files') if is_multi else [target.get('path')]
                store = RecoveryStore(project)
                report = store.inspect(target['workflow_id'])
                journal, _ = store.load(target['workflow_id'])
                actual_hashes = {name: file_hash(project.path(name))
                                 for name in files if project.path(name).is_file()}
                exact = actual_hashes == target.get('expected_post_images')
                write_checks = {item.get('path'): item for item in report.get('writes', [])}
                accepted_recovery = (journal.get('status') == 'resumable'
                    and isinstance(journal.get('reconciliation'), dict)
                    and journal['reconciliation'].get('action') == 'accept')
                if (workflow.row.get('state') in {'modified', 'interrupted'} and exact
                        and (journal.get('status') == 'complete' and report.get('status') == 'complete'
                             or accepted_recovery and report.get('safe_to_reconcile') is True)
                        and len(write_checks) == len(files) and set(write_checks) == set(files)
                        and all(write_checks[name].get('actual') == target['expected_post_images'][name]
                                and write_checks[name].get('checkpoint_sha256')
                                == target['expected_post_images'][name] for name in files)):
                    result = {'status': 'modified', 'path': target.get('path'), 'paths': files,
                        'post_image_sha256': actual_hashes.get(target.get('path')),
                        'post_image_sha256_by_path': actual_hashes, 'receipt_verified': True,
                        'recovered': True, 'test_authorization_required': True,
                        'recovery_status': 'complete',
                        'learning_candidate': {'status': 'not_recorded',
                            'reason': 'Recovery reports verified write evidence without replaying learning.'}}
                    row.update(state='modified', result=result, updated=_now())
                    self._audit(row, 'recovered_modified', {'receipt_verified': True,
                        'post_image_sha256_by_path': actual_hashes})
                    atomic_write(path, encoded(row))
                else:
                    result = {'status': 'interrupted', 'receipt_verified': False,
                        'reconciliation_required': True, 'workflow_state': workflow.row.get('state'),
                        'post_image_matches': exact, 'recovery_state': report.get('recovery_state'),
                        'file_classifications': report.get('file_classifications', []),
                        'pending_outcome': report.get('pending_outcome'),
                        'reconciliation_options': report.get('options')}
                    recovery = {'recovery_run_id': target['workflow_id'],
                        'recovery_state': report.get('recovery_state'),
                        'pending_outcome': report.get('pending_outcome'),
                        'safe_to_reconcile': report.get('safe_to_reconcile'),
                        'options': report.get('options'),
                        'review_sha256': report.get('review_sha256'),
                        'file_classifications': report.get('file_classifications', []),
                        'evidence_directory': journal.get('output')}
                    row.update(state='interrupted', result=result, updated=_now())
                    atomic_write(path, encoded(row))
            except (AgentError, OSError, ValueError, TypeError, KeyError) as exc:
                result = {'status': 'interrupted', 'receipt_verified': False,
                    'reconciliation_required': True, 'recovery_state': 'journal_unavailable',
                    'recovery_error': str(exc)[:200]}
                recovery = {'recovery_run_id': target.get('workflow_id'),
                    'recovery_state': 'journal_unavailable', 'safe_to_reconcile': False,
                    'options': ['operator must inspect Broker journal and source files'],
                    'error': str(exc)[:200]}
                row.update(state='interrupted', result=result, updated=_now())
                atomic_write(path, encoded(row))
        if row.get('target', {}).get('kind') == 'tests' and needs_recovery:
            owner_pid = row.get('owner_pid')
            if row.get('state') in active_states and owner_pid and not _owner_exited(owner_pid):
                return {'operation_id': op_id, 'state': row.get('state'), 'kind': 'tests',
                        'result': None, 'recovery': None}
            try:
                _, project = self.projects._record(row['target']['project_id'])
                store = RecoveryStore(project)
                report = store.inspect(op_id)
                journal, _ = store.load(op_id)
                recovery = {'recovery_run_id': op_id, 'recovery_state': report['recovery_state'],
                    'pending_outcome': report['pending_outcome'],
                    'safe_to_reconcile': report['safe_to_reconcile'],
                    'options': report['options'], 'review_sha256': report['review_sha256'],
                    'evidence_directory': journal['output'],
                    'receipt_integrity': report['receipt_integrity']}
            except (AgentError, OSError, ValueError, TypeError, KeyError):
                project = None
                journal = None
                recovery = {'recovery_run_id': op_id, 'recovery_state': 'journal_unavailable',
                            'pending_outcome': 'unknown', 'safe_to_reconcile': False,
                            'options': ['operator inspection required']}
            # A fully closed Broker journal with an intact test receipt is
            # sufficient to recover the chat result. This path never reruns the
            # command or stages a learning candidate after restart.
            if (row.get('state') in active_states
                    and project is not None and journal is not None
                    and journal.get('status') == 'complete'
                    and not any(item.get('state') == 'executing'
                                for item in journal.get('operations', []))):
                refs = [item.get('evidence') for item in journal.get('operations', [])
                        if item.get('kind') == 'command' and item.get('state') == 'recorded'
                        and isinstance(item.get('evidence'), dict)]
                verified_status, integrity = verify_test_receipts(
                    refs, project, journal.get('output', ''))
                test_refs = []
                for reference in refs:
                    try:
                        receipt = json.loads(Path(reference['path']).read_bytes())
                        if receipt.get('command') == 'tests':
                            test_refs.append((reference, receipt))
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                if integrity.get('valid') is True and len(test_refs) == 1:
                    reference, receipt = test_refs[0]
                    try:
                        output_path = Path(receipt['output_path']).resolve(strict=True)
                        output = output_path.read_text(encoding='utf-8')
                        exit_code = receipt['exit_code']
                        status = 'passed' if type(exit_code) is int and exit_code == 0 else 'failed'
                        result = {'status': status, 'test_status': verified_status,
                            'exit_code': exit_code, **_test_summary(output),
                            'duration_seconds': round(float(receipt.get('seconds', 0)), 3),
                            'output': output, 'receipt': receipt,
                            'receipt_reference': reference, 'receipt_verified': True,
                            'execution_host': 'Windows Broker', 'profile': row['target']['profile'],
                            'project_id': row['target']['project_id'],
                            'output_is_untrusted_evidence': True, 'recovered': True,
                            'learning_candidate': {'status': 'not_recorded',
                                'reason': 'Result recovered after restart; learning side effects were not replayed.'}}
                        row.update(state=status, result=result, updated=_now())
                        self._audit(row, 'recovered_' + status, {
                            'exit_code': exit_code, 'receipt_verified': True,
                            'receipt_reference': reference})
                        atomic_write(path, encoded(row))
                        recovery = {**recovery, 'recovery_state': 'complete',
                                    'safe_to_reconcile': True, 'auto_finalized': True}
                    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                        pass
            active_operation = row.get('state') in active_states
            if not recovery.get('auto_finalized'):
                result = (dict(row.get('result', {})) if not active_operation
                          and isinstance(row.get('result'), dict) else {})
                result.update(status='interrupted' if active_operation else row.get('state'),
                              receipt_verified=False, reconciliation_required=True,
                              recovery=recovery)
                if active_operation and row.get('state') != 'interrupted':
                    row.update(state='interrupted', result=result, updated=_now())
                    self._audit(row, 'interrupted', {'recovery_state': recovery['recovery_state'],
                        'pending_outcome': recovery['pending_outcome'],
                        'safe_to_reconcile': recovery['safe_to_reconcile']})
                    atomic_write(path, encoded(row))
                else:
                    row.update(result=result)
                    atomic_write(path, encoded(row))
        result = self._verified_saved_result(row) if isinstance(row.get('result'), dict) else None
        return {'operation_id': op_id, 'state': row.get('state'),
                'kind': row.get('target', {}).get('kind'), 'result': result,
                'recovery': recovery}

    def reconcile_patch(self, conversation_id, op_id, action, review_sha256):
        if action not in {'accept', 'abort'} or not isinstance(review_sha256, str):
            raise AgentError('Choose a verified accept or abort recovery action.')
        path = self._path(op_id)
        with _LOCK, exclusive(self.root / (op_id + '.reconcile.lock'), 'chat-operation-reconcile'):
            inspected = self.inspect(conversation_id, op_id)
            if inspected.get('kind') not in {'patch', 'multi_patch'} or inspected.get('state') != 'interrupted':
                raise AgentError('Only an interrupted conversational source change can be reconciled.')
            recovery = inspected.get('recovery') or {}
            if (recovery.get('review_sha256') != review_sha256
                    or action not in recovery.get('options', [])):
                raise AgentError('Recovery evidence changed or does not permit that action.')
            row = json.loads(path.read_bytes())
            target = row['target']
            _, project = self.projects._record(target['project_id'])
            store = RecoveryStore(project)
            pre_report = store.inspect(target['workflow_id'])
            files = target.get('files') if target['kind'] == 'multi_patch' else [target['path']]
            expected_hashes = (target.get('expected_post_images', {}) if action == 'accept'
                               else target.get('source_hashes', {}))
            current_hashes = {name: file_hash(project.path(name))
                              for name in expected_hashes if project.path(name).is_file()}
            if (pre_report.get('safe_to_reconcile') is not True
                    or current_hashes != expected_hashes):
                raise AgentError('Current files do not match every hash required for that recovery action.')
            store.reconcile(target['workflow_id'], review_sha256, action)
            report = store.inspect(target['workflow_id'])
            journal, _ = store.load(target['workflow_id'])
            actual_hashes = {name: file_hash(project.path(name))
                             for name in files if project.path(name).is_file()}
            if action == 'accept':
                if (report.get('safe_to_reconcile') is not True
                        or actual_hashes != target.get('expected_post_images')
                        or len([item for item in journal['operations']
                                if item['kind'] == 'project_write'
                                and item['state'] in {'recorded', 'reconciled'}]) != len(files)):
                    raise AgentError('Recovered change set no longer matches every approved post-image and receipt.')
                row.update(state='modified', updated=_now(), result={
                    'status': 'modified', 'paths': files,
                    'post_image_sha256_by_path': actual_hashes,
                    'patch_sha256': target['patch_sha256'],
                    'change_set_sha256': target.get('change_set_sha256'),
                    'receipt_verified': True, 'recovered': True,
                    'write_receipts': [item for item in journal['operations']
                        if item['kind'] == 'project_write'
                        and item['state'] in {'recorded', 'reconciled'}],
                    'test_authorization_required': True,
                    'recovery_status': journal['status'],
                    'learning_candidate': {'status': 'not_recorded',
                        'reason': 'Recovery reconciliation does not replay learning; run separately approved tests.'}})
                self._audit(row, 'reconciled_modified', {
                    'paths': files, 'post_image_sha256_by_path': actual_hashes,
                    'recovery_review_sha256': review_sha256, 'receipt_verified': True})
            else:
                row.update(state='aborted', updated=_now(), result={
                    'status': 'aborted', 'receipt_verified': False,
                    'reconciliation_required': False,
                    'message': 'The operator verified the original source hashes; no patch was retried.'})
                self._audit(row, 'reconciled_aborted', {'recovery_review_sha256': review_sha256})
            atomic_write(path, encoded(row))
            return {'operation_id': op_id, 'state': row['state'], **row['result']}
