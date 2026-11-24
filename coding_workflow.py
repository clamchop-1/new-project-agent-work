"""Patch-bound coding workflow layered over the existing Ollama and Broker.

This adapter grants no model tools. It accepts only fresh disposable Milestone 3
repositories, fixed test profile IDs, and one-use approval for exact patch bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import uuid

from agent_core import AgentError, Project, atomic_write, digest, encoded, file_hash
from coding_harness import ExecutionBudgets, HarnessTaskRequest
from execution_recovery import RecoveryStore
from local_agent import Broker, HUB
from orchestrator import ROLE_INSTRUCTIONS, ROLE_TOOLS
from patch_validation import (PatchValidationError, build_unified_diff_from_proposal,
                              validate_unified_diff)


TEST_PROFILE_CONFIGS = {
    'clamp_percent_v1': {
        'test_file': 'tests/test_progress.py',
        'test_sha256': '989f840b6db6d9f65823a7fa0725867b0abd8bc28554746d8b020bdc6c8a2af7',
        'modules': ['src.progress'],
    },
    'stats_multi_module_v1': {
        'test_file': 'tests/test_stats.py',
        'test_sha256': '6a9f34017eb9574fc9f415368eb9c845bb4643f9633ccc9e98de7b7bc98d3181',
        'modules': ['calculator', 'stats'],
    },
}
TEST_PROFILES = set(TEST_PROFILE_CONFIGS)
TEST_COMMANDS = {'clamp_percent_v1': 'Broker.execute("tests") → python -B -I pure_tests.py'}
TEST_COMMANDS['clamp_percent_v1'] = 'Broker.execute("tests") -> python -B -I pure_tests.py'
TEST_COMMANDS['stats_multi_module_v1'] = TEST_COMMANDS['clamp_percent_v1']
STATES = {'created', 'planning', 'generating', 'validating', 'reviewing', 'awaiting_approval', 'approved',
          'applying', 'testing', 'completed', 'failed', 'rejected', 'cancelled',
          'timed_out', 'interrupted'}
_TRANSITIONS = {
    'created': {'planning', 'generating', 'cancelled'},
    'planning': {'generating', 'failed', 'cancelled', 'timed_out'},
    'generating': {'validating', 'failed', 'cancelled', 'timed_out'},
    'validating': {'awaiting_approval', 'reviewing', 'generating', 'failed', 'cancelled'},
    'reviewing': {'awaiting_approval', 'failed', 'cancelled', 'timed_out'},
    'awaiting_approval': {'approved', 'rejected', 'cancelled'},
    'approved': {'applying', 'rejected'}, 'applying': {'testing', 'interrupted'},
    'testing': {'completed', 'failed', 'interrupted'},
}


class CodingWorkflowError(AgentError):
    pass


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class CodingWorkflow:
    """A persisted state machine; callers supply only existing trusted host services."""

    def __init__(self, project: Project, task: str, allowed_paths: tuple[str, ...],
                 *, test_profile: str = 'clamp_percent_v1', run_id: str | None = None):
        if not isinstance(project, Project) or not isinstance(task, str) or not task.strip() or len(task.encode()) > 12000:
            raise CodingWorkflowError('Workflow needs a validated project and bounded task text.')
        if test_profile not in TEST_PROFILES:
            raise CodingWorkflowError('Test profile is not predefined and trusted.')
        expected_parent = (HUB / '.local' / 'tests').resolve()
        if (project.root.name != 'repo' or project.root.parent.parent.resolve() != expected_parent
                or not project.root.parent.name.startswith(('milestone3-', 'milestone4-'))):
            raise CodingWorkflowError('Coding patch application is limited to a fresh Milestone 3/4 disposable repository.')
        paths = tuple(allowed_paths)
        if (not paths or len(paths) != len(set(paths)) or len(paths) > 8
                or any(not isinstance(p, str) or not p or PurePosixPath(p).is_absolute()
                       or '\\' in p or any(x in ('', '.', '..') for x in p.split('/'))
                       for p in paths)):
            raise CodingWorkflowError('Authorized file allowlist is malformed.')
        if project.state()['status'].strip():
            raise CodingWorkflowError('Disposable repository must start clean.')
        self.project, self.task, self.allowed_paths = project, task.strip(), paths
        self.test_profile = test_profile
        self.run_id = run_id or uuid.uuid4().hex
        if not re.fullmatch('[a-f0-9]{32}', self.run_id):
            raise CodingWorkflowError('Workflow identifier is invalid.')
        self.run_dir = project.root / '.local' / 'coding-workflows' / self.run_id
        if self.run_dir.exists():
            raise CodingWorkflowError('Workflow evidence directory already exists.')
        self.run_dir.mkdir(parents=True)
        self.state_path = self.run_dir / 'state.json'
        self.baseline = project.state()
        self.profile_path = project.root / '.local' / 'pinned-tests.json'
        if not self.profile_path.is_file():
            raise CodingWorkflowError('Disposable repository lacks the predefined pinned test configuration.')
        try:
            profile_config = json.loads(self.profile_path.read_bytes().decode('utf-8-sig'))
        except (OSError, ValueError, UnicodeError) as exc:
            raise CodingWorkflowError('Disposable pinned test configuration is unreadable.') from exc
        if profile_config != TEST_PROFILE_CONFIGS[test_profile]:
            raise CodingWorkflowError('Pinned test configuration does not match the selected trusted profile.')
        self.test_config_hash = file_hash(self.profile_path)
        self.row = {'schema_version': 1, 'run_id': self.run_id, 'state': 'created',
                    'project_id': project.identity, 'repository': str(project.root),
                    'baseline': self.baseline, 'task_sha256': _hash(self.task.encode()),
                    'allowed_paths': list(paths), 'test_profile': test_profile,
                    'test_command': TEST_COMMANDS[test_profile], 'test_config_sha256': self.test_config_hash,
                    'attempts': 0, 'approval': None, 'patch_sha256': None,
                    'expected_post_images': {}, 'created_at': time.time(),
                    'updated_at': time.time(), 'task': self.task, 'failure': None}
        self._save()

    def _save(self):
        if self.row['state'] not in STATES:
            raise CodingWorkflowError('Invalid workflow state.')
        self.row['updated_at'] = time.time()
        atomic_write(self.state_path, encoded(self.row))

    @classmethod
    def load(cls, project: Project, run_id: str, *, recover_ambiguous: bool = True) -> 'CodingWorkflow':
        """Reopen a recorded workflow without replaying consumed operations."""
        if not re.fullmatch('[a-f0-9]{32}', run_id):
            raise CodingWorkflowError('Workflow identifier is invalid.')
        root = project.root / '.local' / 'coding-workflows' / run_id
        state_path = root / 'state.json'
        patch_path = root / 'approved.patch'
        if (root.is_symlink() or state_path.is_symlink() or not state_path.is_file()):
            raise CodingWorkflowError('Workflow evidence is missing or linked.')
        try:
            row = json.loads(state_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise CodingWorkflowError('Workflow state is unreadable; preserve it for review.') from exc
        if (row.get('schema_version') != 1 or row.get('run_id') != run_id
                or row.get('project_id') != project.identity
                or row.get('repository') != str(project.root)
                or row.get('state') not in STATES):
            raise CodingWorkflowError('Workflow identity or state is invalid.')
        instance = cls.__new__(cls)
        instance.project = project
        instance.task = ''
        instance.allowed_paths = tuple(row['allowed_paths'])
        instance.test_profile = row['test_profile']
        instance.run_id = run_id
        instance.run_dir = root
        instance.state_path = state_path
        instance.profile_path = project.root / '.local' / 'pinned-tests.json'
        instance.test_config_hash = row['test_config_sha256']
        instance.baseline = row['baseline']
        instance.row = row
        if recover_ambiguous and row['state'] in {'applying', 'testing'}:
            row['state'] = 'interrupted'
            row['failure'] = 'Recovered during an ambiguous apply/test phase; operator review required, no replay allowed.'
            instance._save()
        return instance

    def _transition(self, state):
        if state not in _TRANSITIONS.get(self.row['state'], set()):
            raise CodingWorkflowError(f'Illegal workflow transition {self.row["state"]} -> {state}.')
        self.row['state'] = state
        self._save()

    def _verify_baseline(self):
        current = self.project.state()
        if current != self.baseline:
            raise CodingWorkflowError('Repository identity, commit, status, or file hashes changed since proposal.')
        return current

    def _authorized_context(self) -> str:
        chunks = []
        remaining = 16_384
        for relative in self.allowed_paths:
            path = self.project.path(relative)
            if not path.is_file():
                continue
            if path.stat().st_size > min(8192, remaining):
                chunks.append(f'--- {relative}: omitted (authorized context size limit) ---')
                continue
            raw = path.read_bytes()
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError:
                chunks.append(f'--- {relative}: omitted (not UTF-8 text) ---')
                continue
            chunks.append(f'--- {relative} ---\n{content}')
            remaining -= len(raw)
            if remaining <= 0:
                break
        return '\n\n'.join(chunks) if chunks else '(No existing allowlisted source files.)'

    def _proposal_file_context(self) -> list[dict]:
        """Return bounded source hashes and UTF-8 text for structured coder output."""
        records = []
        remaining = 16_384
        for relative in self.allowed_paths:
            path = self.project.path(relative)
            if not path.is_file():
                records.append({'path': relative, 'exists': False, 'sha256': None, 'content': None})
                continue
            raw = path.read_bytes()
            if len(raw) > min(8192, remaining):
                records.append({'path': relative, 'exists': True,
                                'sha256': _hash(raw), 'content': None,
                                'omitted_reason': 'authorized context size limit'})
                continue
            try:
                content = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                records.append({'path': relative, 'exists': True,
                                'sha256': _hash(raw), 'content': None,
                                'omitted_reason': 'not UTF-8 text'})
                continue
            records.append({'path': relative, 'exists': True,
                            'sha256': _hash(raw), 'content': content})
            remaining -= len(raw)
            if remaining <= 0:
                break
        return records

    def _structured_role_call(self, provider, *, role: str, model: str, prompt: str,
                               timeout: float, cancel_event, provider_id: str,
                               response_name: str, schema_validator) -> dict:
        if role not in ('architect', 'reviewer'):
            raise CodingWorkflowError('Unsupported read-only collaboration role.')
        if role not in ROLE_TOOLS or role not in ROLE_INSTRUCTIONS:
            raise CodingWorkflowError('Role metadata is missing from the established orchestration policy.')
        if cancel_event is not None and cancel_event.is_set():
            self.row['state'] = 'cancelled'; self._save()
            raise CodingWorkflowError(f'{role} inference cancelled.')
        task_request = HarnessTaskRequest(
            task_id='planner_plan' if role == 'architect' else 'reviewer_review',
            run_id=self.run_id, project_id=self.project.identity, objective=prompt[:12_000],
            model=model, provider=provider_id,
            budgets=ExecutionBudgets(max_turns=1, max_tool_calls=0,
                                     wall_seconds=timeout, cancellation_grace_seconds=1.0),
        )
        started = time.monotonic()
        messages = [
            {'role': 'system', 'content': ROLE_INSTRUCTIONS[role] +
             '\nReturn exactly one JSON object matching the requested schema. No Markdown, prose, tools, or commands.'},
            {'role': 'user', 'content': prompt},
        ]
        request_bytes = encoded({'role': role, 'model': model, 'messages': messages, 'tools': []})
        previous_cancel = getattr(provider, 'cancel_event', None)
        if hasattr(provider, 'cancel_event'):
            provider.cancel_event = cancel_event
        try:
            result = provider.chat(model, messages, [], 8192,
                                   {'temperature': 0, 'num_predict': 4096}, timeout=timeout)
        except Exception as exc:
            self.row['failure'] = f'{role} inference {type(exc).__name__}: {str(exc)[:300]}'
            code = getattr(exc, 'code', '')
            self.row['state'] = 'cancelled' if code == 'cancelled' or (cancel_event and cancel_event.is_set()) else ('timed_out' if code == 'timeout' else 'failed')
            self._save()
            raise CodingWorkflowError(f'{role} inference failed; evidence is preserved.') from exc
        finally:
            if hasattr(provider, 'cancel_event'):
                provider.cancel_event = previous_cancel
        message = result.get('message') if isinstance(result, dict) else None
        if (not isinstance(message, dict) or message.get('role') != 'assistant'
                or message.get('tool_calls') or not isinstance(message.get('content'), str)
                or len(message['content'].encode('utf-8')) > 16_384
                or isinstance(result, dict) and (result.get('done') is False
                    or result.get('done_reason') in {'length', 'max_tokens'})):
            self.row['state'] = 'failed'
            self.row['failure'] = f'{role} response structure invalid or oversized.'
            self._save()
            raise CodingWorkflowError(f'{role} response structure is invalid or exceeds its bound.')
        try:
            raw = message['content'].encode('utf-8')
        except UnicodeError as exc:
            self.row['state'] = 'failed'; self.row['failure'] = f'{role} response is not valid UTF-8 text.'
            self._save()
            raise CodingWorkflowError(f'{role} response is not valid UTF-8 text.') from exc
        atomic_write(self.run_dir / response_name, raw)
        if cancel_event is not None and cancel_event.is_set():
            self.row['state'] = 'cancelled'; self.row['failure'] = f'{role} inference cancelled by operator.'
            self._save()
            raise CodingWorkflowError(f'{role} inference cancelled.')
        try:
            parsed = json.loads(raw.decode('utf-8'))
            validated = schema_validator(parsed)
        except (UnicodeError, ValueError, TypeError, CodingWorkflowError) as exc:
            self.row['state'] = 'failed'
            self.row['failure'] = f'{role} response validation failed: {str(exc)[:300]}'
            self._save()
            if isinstance(exc, CodingWorkflowError):
                raise
            raise CodingWorkflowError(f'{role} response must be strict JSON.') from exc
        calls = self.row.setdefault('role_calls', [])
        calls.append({'role': role, 'provider_id': provider_id, 'model': model,
                      'tools': [], 'request_sha256': _hash(request_bytes),
                      'response_sha256': _hash(raw), 'response_artifact': response_name,
                      'task_request': task_request.to_dict(),
                      'seconds': round(time.monotonic() - started, 4), 'status': 'completed',
                      'prompt_tokens': result.get('prompt_eval_count')
                          if type(result.get('prompt_eval_count')) is int and result['prompt_eval_count'] >= 0 else None,
                      'completion_tokens': result.get('eval_count')
                          if type(result.get('eval_count')) is int and result['eval_count'] >= 0 else None,
                      'done_reason': result.get('done_reason')})
        self._save()
        return validated

    @staticmethod
    def _validate_plan(value: object, allowed_paths: tuple[str, ...]) -> dict:
        if not isinstance(value, dict) or set(value) != {
                'summary', 'files', 'acceptance_criteria', 'test_requirements', 'steps'}:
            raise CodingWorkflowError('Planner JSON schema is invalid.')
        for key, maximum in (('summary', 3000),):
            if not isinstance(value[key], str) or not value[key].strip() or len(value[key].encode()) > maximum:
                raise CodingWorkflowError(f'Planner {key} is invalid or oversized.')
        files = value['files']
        if (not isinstance(files, list) or not files
                or any(not isinstance(path, str) for path in files) or len(files) > 8
                or len(files) != len(set(files)) or set(files) - set(allowed_paths)):
            raise CodingWorkflowError('Planner file list must be a unique subset of the authorized allowlist.')
        for key, limit, item_bytes in (('acceptance_criteria', 12, 500),
                                        ('test_requirements', 12, 500), ('steps', 8, 500)):
            rows = value[key]
            if (not isinstance(rows, list) or not 1 <= len(rows) <= limit
                    or any(not isinstance(item, str) or not item.strip() or len(item.encode()) > item_bytes
                           for item in rows)):
                raise CodingWorkflowError(f'Planner {key} are invalid or exceed their bounds.')
        return value

    def generate_collaborative(self, provider, *, role_models: dict[str, str],
                               timeout: float = 120.0, cancel_event=None,
                               max_attempts: int = 2, provider_id: str = 'ollama') -> dict:
        """Run read-only planner, bounded coder, and advisory read-only reviewer sequentially."""
        if set(role_models) != {'planner', 'coder', 'reviewer'}:
            raise CodingWorkflowError('Exactly planner, coder, and reviewer model assignments are required.')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise CodingWorkflowError('Collaborative coder attempts must be between one and three.')
        if any(not isinstance(model, str) or not model.strip() or len(model) > 160 for model in role_models.values()):
            raise CodingWorkflowError('Role model assignments are malformed.')
        if self.row['state'] != 'created':
            raise CodingWorkflowError('Only a created workflow may start collaboration.')
        self.row['multi_agent'] = True
        self.row['role_models'] = dict(role_models)
        self.row['max_role_calls'] = 1 + max_attempts + 1
        self.row['coder_max_attempts'] = max_attempts
        self._transition('planning')
        files_context = self._authorized_context()
        planner_prompt = json.dumps({
            'task': self.task, 'authorized_files': list(self.allowed_paths),
            'repository_context': files_context,
            'required_output_schema': {
                'summary': 'string', 'files': ['authorized relative path'],
                'acceptance_criteria': 'array of 1 to 12 strings',
                'test_requirements': 'array of 1 to 12 strings',
                'steps': 'array of 1 to 8 strings'},
            'constraints': ['Plan only; do not edit or run anything.',
                            'Every file must be in authorized_files.',
                            'Test requirements may only describe predefined local verification.'],
        }, ensure_ascii=False)
        plan = self._structured_role_call(provider, role='architect', model=role_models['planner'],
            prompt=planner_prompt, timeout=timeout, cancel_event=cancel_event,
            provider_id=provider_id, response_name='planner.json',
            schema_validator=lambda value: self._validate_plan(value, self.allowed_paths))
        self.row['planner_output'] = plan
        self._save()
        proposal = self.generate(provider, role_models['coder'], timeout=timeout,
            cancel_event=cancel_event, max_attempts=max_attempts, provider_id=provider_id,
            defer_approval=True)
        reviewer_prompt = json.dumps({
            'task': self.task, 'plan': plan,
            'acceptance_criteria': plan['acceptance_criteria'],
            'test_requirements': plan['test_requirements'],
            'authorized_source_context': self._proposal_file_context(),
            'authorized_files': list(self.allowed_paths), 'patch_sha256': proposal['patch_sha256'],
            'diff': proposal['diff'], 'validation_report': proposal['validation_report'],
            'simulation_tests': self.row.get('simulation_test_result'),
            'output_schema': {
                'summary': 'string', 'recommendation': 'no_blocking_findings or changes_requested',
                'findings': [{'severity': 'high|medium|low', 'path': 'authorized path or null',
                              'line': 'positive integer or null', 'message': 'string'}]},
            'constraints': ['Review only; do not edit, execute, approve, or authorize.',
                            'Recommendations are advisory; the deterministic validator and operator decide.',
                            'Findings must reference only authorized_files or null.'],
        }, ensure_ascii=False)
        review = self._structured_role_call(provider, role='reviewer', model=role_models['reviewer'],
            prompt=reviewer_prompt, timeout=timeout, cancel_event=cancel_event,
            provider_id=provider_id, response_name='reviewer.json',
            schema_validator=lambda value: self._validate_review(value, self.allowed_paths))
        self.row['review_output'] = review
        self.row['review_advisory_only'] = True
        self.row['reviewed_patch_sha256'] = proposal['patch_sha256']
        self._save()
        self._record_preapproval_candidate()
        self._transition('awaiting_approval')
        proposal.update({'state': self.row['state'], 'planner': plan, 'review': review,
                         'role_models': dict(role_models), 'review_advisory_only': True})
        return proposal

    @staticmethod
    def _validate_review(value: object, allowed_paths: tuple[str, ...]) -> dict:
        if not isinstance(value, dict) or set(value) != {'summary', 'recommendation', 'findings'}:
            raise CodingWorkflowError('Reviewer JSON schema is invalid.')
        if (not isinstance(value['summary'], str) or not value['summary'].strip()
                or len(value['summary'].encode()) > 3000
                or value['recommendation'] not in {'no_blocking_findings', 'changes_requested'}):
            raise CodingWorkflowError('Reviewer summary or recommendation is invalid.')
        rows = value['findings']
        if not isinstance(rows, list) or len(rows) > 20:
            raise CodingWorkflowError('Reviewer findings must be a bounded array.')
        for item in rows:
            if (not isinstance(item, dict) or set(item) != {'severity', 'path', 'line', 'message'}
                    or item['severity'] not in {'high', 'medium', 'low'}
                    or item['path'] is not None and (not isinstance(item['path'], str)
                                                       or item['path'] not in allowed_paths)
                    or item['line'] is not None and (type(item['line']) is not int or item['line'] < 1)
                    or not isinstance(item['message'], str) or not item['message'].strip()
                    or len(item['message'].encode()) > 1000):
                raise CodingWorkflowError('Reviewer finding is malformed or references an unauthorized path.')
        return value

    def _record_preapproval_candidate(self):
        """Capture a reference-only lesson from a validated, simulated proposal."""
        from skill_library import SkillLibrary
        run_root = self.project.root / '.local' / 'orchestration' / 'runs' / self.run_id
        output = run_root / 'outputs' / 'coding'
        output.mkdir(parents=True, exist_ok=True)
        (run_root / 'worktrees').mkdir(exist_ok=True)
        state = {'schema_version': 1,
                 'graph': {'run_id': self.run_id, 'project_id': self.project.identity},
                 'project_baseline': {'project_id': self.project.identity},
                 'tasks': {'coding_task': {'role': 'developer', 'status': 'agent_completed'}}}
        atomic_write(run_root / 'state.json', encoded(state))
        evidence_files = []
        for source, name in ((self.run_dir / 'approved.patch', 'validated-proposal.patch'),
                             (self.run_dir / 'simulation-tests.txt', 'simulation-tests.txt'),
                             (self.state_path, 'workflow-state.json')):
            if not source.is_file():
                raise CodingWorkflowError('Proposal candidate evidence is incomplete.')
            destination = output / name
            atomic_write(destination, source.read_bytes())
            evidence_files.append(destination)
        references = [{'kind': 'artifact', 'uri': str(path),
                       'sha256': file_hash(path), 'run_id': self.run_id}
                      for path in evidence_files]
        SkillLibrary(self.project).propose(
            category='coding',
            problem='Model-authored multi-file unified diffs can contain incorrect hunk counts or mismatched line endings.',
            procedure=('Request complete structured file intents bound to exact source hashes, construct unified diffs locally, '
                       'preserve UTF-8 BOM and LF/CRLF bytes, strictly reconstruct every hunk, simulate exact post-images, '
                       'and run the fixed pinned tests before asking the operator to approve the exact patch.'),
            source_refs=[references[-1]], evidence_refs=references,
            run_id=self.run_id, task_id='coding_task', source_agent='developer',
            source_event_id='structured-patch-workflow-' + self.run_id)

    def generate(self, provider, model: str, *, timeout: float = 120.0,
                 cancel_event=None, max_attempts: int = 3,
                 provider_id: str = 'ollama', defer_approval: bool = False) -> dict:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise CodingWorkflowError('Proposal attempts must be between one and three.')
        if self.row['state'] not in {'created', 'planning'}:
            raise CodingWorkflowError('Only a created workflow may generate a proposal.')
        self._verify_baseline()
        request = HarnessTaskRequest(
            task_id='coding_task', run_id=self.run_id, project_id=self.project.identity,
            objective=self.task, model=model, provider=provider_id,
            budgets=ExecutionBudgets(max_turns=max_attempts, max_tool_calls=0,
                                     wall_seconds=timeout, cancellation_grace_seconds=1.0),
        )
        self.row['harness_task_request'] = request.to_dict()
        self._save()
        self._transition('generating')
        feedback = None
        for attempt in range(1, max_attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                self.row['state'] = 'cancelled'; self._save()
                raise CodingWorkflowError('Proposal cancelled by operator.')
            self.row['attempts'] = attempt; self._save()
            self.row['patch_sha256'] = None
            self.row['expected_post_images'] = {}
            self.row.pop('simulation_test_result', None)
            planned_paths = (set(self.row['planner_output']['files'])
                             if self.row.get('planner_output') else set(self.allowed_paths))
            prompt_data = {
                'task': self.task,
                'planner_plan': self.row.get('planner_output'),
                'authorized_files': list(self.allowed_paths),
                'planned_files': sorted(planned_paths),
                'source_context': self._proposal_file_context(),
                'required_response_schema': {
                    'files': [{'path': 'one planned authorized relative path',
                               'base_sha256': 'exact source_context sha256, or null for a missing file',
                               'content': 'complete intended UTF-8 file text using consistent LF or CRLF line endings'}]},
                'constraints': [
                    'Return one JSON object only; no Markdown, unified diff, prose, commands, or tools.',
                    'For every changed file return its complete intended text and exactly echo its base_sha256, not a partial edit.',
                    'Return only files that need changes and only paths in planned_files.',
                    'Do not modify tests, .local data, or test configuration.',
                    'Use one consistent LF or CRLF convention in each content string; local code preserves the target newline convention and BOM.',
                    'Preserve whether the file ends with a final newline.',
                    'Do not invent or paraphrase omitted source; if necessary context is missing, explain via a schema failure.'
                ],
                'correction_feedback': feedback,
            }
            prompt = json.dumps(prompt_data, ensure_ascii=False)
            messages = [
                {'role': 'system', 'content': ROLE_INSTRUCTIONS['developer'] +
                 '\nReturn only strict JSON matching required_response_schema. Do not use tools or commands.'},
                {'role': 'user', 'content': prompt},
            ]
            began = time.monotonic()
            try:
                previous_cancel = getattr(provider, 'cancel_event', None)
                if hasattr(provider, 'cancel_event'):
                    provider.cancel_event = cancel_event
                try:
                    response = provider.chat(model, messages, [],
                                             16384, {'temperature': 0, 'num_predict': 4096}, timeout=timeout)
                finally:
                    if hasattr(provider, 'cancel_event'):
                        provider.cancel_event = previous_cancel
            except Exception as exc:
                self.row['failure'] = type(exc).__name__ + ': ' + str(exc)[:400]
                code = getattr(exc, 'code', '')
                self.row['state'] = 'cancelled' if code == 'cancelled' else ('timed_out' if code == 'timeout' else 'failed')
                self._save()
                raise CodingWorkflowError('Inference did not produce a proposal; see workflow evidence.') from exc
            if cancel_event is not None and cancel_event.is_set():
                self.row['state'] = 'cancelled'; self.row['failure'] = 'Coder inference cancelled by operator.'
                self._save()
                raise CodingWorkflowError('Coder inference cancelled.')
            message = response.get('message') if isinstance(response, dict) else None
            if not isinstance(message, dict) or message.get('tool_calls'):
                failed_text = message.get('content') if isinstance(message, dict) else None
                failed_raw = failed_text.encode('utf-8') if isinstance(failed_text, str) else b''
                response_artifact = f'response-{attempt}.txt'
                atomic_write(self.run_dir / response_artifact, failed_raw)
                self.row.setdefault('coder_attempts', []).append({
                    'role': 'developer', 'provider_id': provider_id, 'model': model,
                    'tools': [], 'request_sha256': _hash(encoded(messages)),
                    'response_sha256': _hash(failed_raw), 'response_artifact': response_artifact,
                    'attempt': attempt, 'status': 'rejected_structure',
                    'seconds': round(time.monotonic() - began, 4),
                    'prompt_tokens': response.get('prompt_eval_count') if isinstance(response, dict) and type(response.get('prompt_eval_count')) is int else None,
                    'completion_tokens': response.get('eval_count') if isinstance(response, dict) and type(response.get('eval_count')) is int else None,
                    'done_reason': response.get('done_reason') if isinstance(response, dict) else None,
                })
                feedback = {'code': 'invalid_response_structure',
                            'message': 'Assistant response must contain text and no tool calls.',
                            'instruction': 'Return the required JSON object only.'}
                self.row.setdefault('validation_failures', []).append(
                    {'attempt': attempt, 'stage': 'response_structure', **feedback})
                self._save()
                continue
            content = message.get('content')
            if not isinstance(content, str) or not content:
                atomic_write(self.run_dir / f'response-{attempt}.txt', b'')
                self.row.setdefault('coder_attempts', []).append({
                    'role': 'developer', 'provider_id': provider_id, 'model': model,
                    'tools': [], 'request_sha256': _hash(encoded(messages)),
                    'response_sha256': _hash(b''), 'response_artifact': f'response-{attempt}.txt',
                    'attempt': attempt, 'status': 'rejected_structure',
                    'seconds': round(time.monotonic() - began, 4),
                })
                feedback = {'code': 'empty_response',
                            'message': 'Response content was empty or not text.',
                            'instruction': 'Return the required JSON object only.'}
                self.row.setdefault('validation_failures', []).append(
                    {'attempt': attempt, 'stage': 'response_structure', **feedback})
                self._save()
                continue
            raw = content.encode('utf-8')
            atomic_write(self.run_dir / f'response-{attempt}.txt', raw)
            self.row['model_response_sha256'] = _hash(raw)
            self.row.setdefault('coder_attempts', []).append({
                'role': 'developer', 'provider_id': provider_id, 'model': model,
                'tools': [], 'request_sha256': _hash(encoded(messages)),
                'response_sha256': _hash(raw),
                'response_artifact': f'response-{attempt}.txt', 'attempt': attempt,
                'status': 'received',
                'seconds': round(time.monotonic() - began, 4),
                'prompt_tokens': response.get('prompt_eval_count') if type(response.get('prompt_eval_count')) is int else None,
                'completion_tokens': response.get('eval_count') if type(response.get('eval_count')) is int else None,
                'done_reason': response.get('done_reason'),
            })
            self._save()
            self._transition('validating')
            try:
                if len(raw) > 16_384:
                    raise PatchValidationError('Structured model response exceeds 16 KiB and may be truncated.')
                if response.get('done') is False or response.get('done_reason') in {'length', 'max_tokens'}:
                    raise PatchValidationError('Model response was truncated before a complete proposal.')
                validated = build_unified_diff_from_proposal(
                    raw, root=self.project.root, allowed_paths=set(self.allowed_paths),
                    planned_paths=planned_paths,
                    protected_paths={TEST_PROFILE_CONFIGS[self.test_profile]['test_file'],
                                     '.local/pinned-tests.json'})
                self.row['patch_sha256'] = validated.sha256
                self.row['expected_post_images'] = {f.path: _hash(f.new_bytes) for f in validated.files}
                atomic_write(self.run_dir / f'proposal-attempt-{attempt}.patch', validated.raw)
                self._save()
                self._simulate(validated.raw)
            except Exception as exc:
                error_code = ('proposal_truncated' if 'truncated' in str(exc).casefold()
                              else 'strict_proposal_validation_failed')
                feedback = {'code': error_code, 'message': str(exc)[:500],
                            'attempt': attempt,
                            'instruction': 'Correct only the listed JSON/schema or source-content problem. '
                                           'Do not change allowed paths, tests, permissions, or commands.'}
                self.row.setdefault('validation_failures', []).append(
                    {'attempt': attempt, 'stage': 'strict_validation', **feedback})
                self.row['failure'] = str(exc)[:500]; self._save()
                if attempt == max_attempts:
                    self.row['state'] = 'failed'; self._save()
                    if self.row.get('simulation_test_result', {}).get('exit_code') not in (None, 0):
                        output_path = self.run_dir / 'simulation-tests.txt'
                        result = {'exit_code': self.row['simulation_test_result']['exit_code'],
                                  'output': output_path.read_text(encoding='utf-8') if output_path.is_file() else feedback}
                        self._record_candidate(result, success=False)
                    raise CodingWorkflowError('Patch proposal failed strict validation.') from exc
                self._transition('generating')
                continue
            self.row['failure'] = None
            atomic_write(self.run_dir / 'approved.patch', validated.raw)
            self.row['patch_sha256'] = validated.sha256
            self.row['expected_post_images'] = {f.path: _hash(f.new_bytes) for f in validated.files}
            self.row['files'] = [f.path for f in validated.files]
            validation_report = {'response_structure': 'passed',
                'strict_unified_diff': 'passed', 'hunk_counts_and_consumption': 'passed',
                'authorized_paths': 'passed', 'git_apply_check_simulation': 'passed',
                'simulated_post_images': 'byte_for_byte_match',
                'patch_sha256': validated.sha256}
            self.row['validation_report'] = validation_report
            simulation_tests = self.row.get('simulation_test_result', {})
            if simulation_tests.get('exit_code') != 0:
                raise CodingWorkflowError('Isolated pinned acceptance tests did not pass.')
            validation_report['fixed_acceptance_tests'] = 'passed'
            validation_report['test_count'] = simulation_tests.get('test_count')
            self.row['generation_seconds'] = time.monotonic() - began
            self._transition('reviewing' if defer_approval else 'awaiting_approval')
            return {'run_id': self.run_id, 'state': self.row['state'],
                    'repository': str(self.project.root), 'files': self.row['files'],
                    'patch_sha256': self.row['patch_sha256'],
                    'expected_post_images': self.row['expected_post_images'],
                    'validation_report': validation_report,
                    'diff': validated.raw.decode('utf-8'),
                    'test_command': self.row['test_command']}
        self.row['state'] = 'failed'
        self.row['failure'] = (json.dumps(feedback, ensure_ascii=False)[:500]
                               if isinstance(feedback, dict) else str(feedback)[:500])
        self._save()
        raise CodingWorkflowError('Proposal attempts exhausted.')

    def _simulate(self, raw: bytes):
        self._verify_baseline()
        tests_root = HUB / '.local' / 'tests'
        tests_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='milestone3-sim-', dir=tests_root) as temp:
            target = Path(temp) / 'repo'
            clone = subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', str(self.project.root), str(target)],
                                   capture_output=True, timeout=30, check=False)
            if clone.returncode:
                raise CodingWorkflowError('Could not create isolated patch simulation.')
            checked = subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(target), 'apply', '--check'], input=raw,
                                     capture_output=True, timeout=20, check=False)
            if checked.returncode:
                raise CodingWorkflowError('git apply --check rejected the strictly parsed patch.')
            applied = subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(target), 'apply'], input=raw,
                                     capture_output=True, timeout=20, check=False)
            if applied.returncode:
                raise CodingWorkflowError('Isolated patch simulation failed after --check.')
            proposed = validate_unified_diff(raw, root=self.project.root,
                                              allowed_paths=set(self.allowed_paths))
            for file in proposed.files:
                actual = target.joinpath(*PurePosixPath(file.path).parts).read_bytes()
                if actual != file.new_bytes:
                    raise CodingWorkflowError('Git simulation bytes differ from parser post-image.')
            # Restore the exact operator-pinned test bytes into the isolated
            # simulation, then use the existing Broker's fixed test capability.
            test_profile = TEST_PROFILE_CONFIGS[self.test_profile]
            simulated_test = target / test_profile['test_file']
            trusted_test = self.project.root / test_profile['test_file']
            if file_hash(trusted_test) != test_profile['test_sha256']:
                raise CodingWorkflowError('Operator-pinned acceptance test changed.')
            simulated_test.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(trusted_test, simulated_test)
            profile_dir = target / '.local'
            profile_dir.mkdir(exist_ok=True)
            simulated_config = profile_dir / 'pinned-tests.json'
            shutil.copyfile(self.profile_path, simulated_config)
            test_output = Path(temp) / 'broker-tests'
            test_output.mkdir()
            test_broker = Broker(Project(target), test_output, test_config=simulated_config)
            test_result = test_broker.execute('tests')
            atomic_write(self.run_dir / 'simulation-tests.txt',
                         test_result['output'].encode('utf-8'))
            run_match = re.search(r'Ran ([0-9]+) tests?', test_result['output'])
            failure_match = re.search(r'FAILED \(failures=([0-9]+), errors=([0-9]+)\)',
                                      test_result['output'])
            self.row['simulation_test_result'] = {
                'exit_code': test_result['exit_code'],
                'output_sha256': _hash(test_result['output'].encode('utf-8')),
                'test_command': TEST_COMMANDS[self.test_profile],
                'test_count': int(run_match.group(1)) if run_match else 0,
                'failures': int(failure_match.group(1)) if failure_match else 0,
                'errors': int(failure_match.group(2)) if failure_match else 0,
            }
            self._save()
            if test_result['exit_code'] != 0:
                raise CodingWorkflowError('Pre-approval fixed acceptance tests failed: '
                                          + test_result['output'][-1200:])

    def approve(self, *, patch_sha256: str, repository: str, operator: str,
                test_command: str) -> dict:
        if self.row['state'] != 'awaiting_approval':
            raise CodingWorkflowError('Workflow is not awaiting approval.')
        if self.row.get('simulation_test_result', {}).get('exit_code') != 0:
            raise CodingWorkflowError('Proposal has not passed the predefined pre-approval simulation tests.')
        if self.row.get('multi_agent') and (
                not isinstance(self.row.get('planner_output'), dict)
                or not isinstance(self.row.get('review_output'), dict)
                or self.row.get('reviewed_patch_sha256') != self.row.get('patch_sha256')
                or not self.row.get('review_advisory_only')):
            raise CodingWorkflowError('Collaborative plan/review is missing or not bound to this patch.')
        self._verify_baseline()
        normalize_command = lambda value: value.replace('→', '->') if isinstance(value, str) else value
        if (patch_sha256 != self.row['patch_sha256'] or repository != str(self.project.root)
                or normalize_command(test_command) != normalize_command(self.row['test_command'])
                or not operator.strip()):
            raise CodingWorkflowError('Approval does not exactly match patch, destination, tests, and operator.')
        approval = {'approval_id': uuid.uuid4().hex, 'operator': operator.strip()[:128],
                    'project_id': self.project.identity, 'repository': str(self.project.root),
                    'baseline': self.baseline, 'patch_sha256': self.row['patch_sha256'],
                    'expected_post_images': self.row['expected_post_images'],
                    'allowed_paths': self.row['allowed_paths'], 'test_profile': self.test_profile,
                    'test_config_sha256': self.test_config_hash,
                    'test_command': self.row['test_command'], 'used': False}
        if self.row.get('multi_agent'):
            approval['planner_response_sha256'] = next(
                call['response_sha256'] for call in self.row['role_calls'] if call['role'] == 'architect')
            approval['reviewer_response_sha256'] = next(
                call['response_sha256'] for call in self.row['role_calls'] if call['role'] == 'reviewer')
            approval['reviewed_patch_sha256'] = self.row['reviewed_patch_sha256']
        approval['binding_sha256'] = digest(encoded(approval))
        self.row['approval'] = approval
        self._transition('approved')
        return dict(approval)

    def reject(self, *, patch_sha256: str):
        if self.row['state'] != 'awaiting_approval' or patch_sha256 != self.row['patch_sha256']:
            raise CodingWorkflowError('Rejection does not match the pending proposal.')
        self._transition('rejected')

    def cancel(self):
        if self.row['state'] not in {'created', 'planning', 'generating', 'validating',
                                     'reviewing', 'awaiting_approval'}:
            raise CodingWorkflowError('Workflow cannot be cancelled after approval consumption.')
        self._transition('cancelled')

    def apply_and_test(self, *, operator: str):
        if self.row['state'] != 'approved' or not isinstance(self.row.get('approval'), dict):
            raise CodingWorkflowError('A matching explicit approval is required.')
        approval = self.row['approval']
        if approval['used'] or approval['operator'] != operator:
            raise CodingWorkflowError('Approval is reused or operator identity mismatches.')
        binding = {key: value for key, value in approval.items() if key != 'binding_sha256'}
        if approval.get('binding_sha256') != digest(encoded(binding)):
            raise CodingWorkflowError('Approval binding was modified after operator approval.')
        self._verify_baseline()
        raw = (self.run_dir / 'approved.patch').read_bytes()
        if _hash(raw) != approval['patch_sha256'] or _hash(raw) != self.row['patch_sha256']:
            raise CodingWorkflowError('Approved patch bytes changed.')
        parsed = validate_unified_diff(raw, root=self.project.root, allowed_paths=set(approval['allowed_paths']))
        if {f.path: _hash(f.new_bytes) for f in parsed.files} != approval['expected_post_images']:
            raise CodingWorkflowError('Expected post-images differ from approval.')
        if file_hash(self.profile_path) != approval['test_config_sha256']:
            raise CodingWorkflowError('Pinned test configuration changed after approval.')
        recovery = RecoveryStore(self.project)
        with recovery.locked():
            recovery.assert_clear()
            self._verify_baseline()
            current_patch = (self.run_dir / 'approved.patch').read_bytes()
            if current_patch != raw or _hash(current_patch) != approval['patch_sha256']:
                raise CodingWorkflowError('Patch changed before the locked application checkpoint.')
            approval['used'] = True
            approval['consumed_at'] = time.time()
            self.row['state'] = 'applying'; self._save()  # Consume durably before the first write.
            return self._apply_locked(recovery, approval, parsed)

    def _apply_locked(self, recovery, approval, parsed):
        output = (self.project.root / '.local' / 'orchestration' / 'runs'
                  / self.run_id / 'outputs' / 'coding' / 'broker')
        output.mkdir(parents=True)
        if not output.resolve().is_relative_to(self.project.root) or output.is_symlink():
            raise CodingWorkflowError('Broker evidence path is linked or outside the disposable repository.')
        journal = recovery.start(output, tuple(approval['allowed_paths']), None, (), (),
                                 self.profile_path, (), None)
        broker = Broker(self.project, output, writes=tuple(approval['allowed_paths']),
                         test_config=self.profile_path)
        broker.journal = journal
        try:
            for item in parsed.files:
                current = self.project.path(item.path)
                before = file_hash(current) if current.is_file() else None
                broker.write(item.path, item.new_bytes.decode('utf-8'), expected=before)
            for path, expected in approval['expected_post_images'].items():
                target = self.project.path(path)
                if not target.is_file() or file_hash(target) != expected:
                    self.row['state'] = 'interrupted'; self.row['failure'] = 'post-image mismatch'; self._save()
                    raise CodingWorkflowError('Applied bytes differ from approved post-image; no retry attempted.')
            self._transition('testing')
            result = broker.execute('tests')
            self.row['test_result'] = result
            if result['exit_code'] != 0:
                self.row['state'] = 'failed'; self.row['failure'] = 'trusted tests failed'; self._save()
                journal.close('failed')
                self._record_candidate(result, success=False)
                return result
            self._transition('completed')
            journal.close('complete')
            self._record_candidate(result, success=True)
            return result
        except BaseException as exc:
            if self.row['state'] in {'applying', 'testing'}:
                self.row['state'] = 'interrupted'; self.row['failure'] = type(exc).__name__ + ': ' + str(exc)[:400]
                self._save()
            elif self.row['state'] == 'completed':
                self.row['state'] = 'failed'
                self.row['failure'] = 'Candidate/evidence persistence failed after code tests completed: ' + str(exc)[:400]
                self._save()
            if (not journal.broken
                    and journal.row.get('status') not in {'complete', 'failed', 'aborted'}):
                journal.close('interrupted')
            raise

    def _record_candidate(self, test_result, *, success: bool):
        from skill_library import SkillLibrary
        run_root = self.project.root / '.local' / 'orchestration' / 'runs' / self.run_id
        output_dir = run_root / 'outputs' / 'coding'
        output_dir.mkdir(parents=True, exist_ok=True)
        (run_root / 'worktrees').mkdir(exist_ok=True)
        orchestration_state = {'schema_version': 1,
            'graph': {'run_id': self.run_id, 'project_id': self.project.identity},
            'project_baseline': {'project_id': self.project.identity},
            'tasks': {'coding_task': {'role': 'developer', 'status': 'agent_completed'}}}
        atomic_write(run_root / 'state.json', encoded(orchestration_state))
        lesson_path = output_dir / 'candidate-lesson.md'
        if success:
            lesson = ('# Candidate: exact integer validation\n\n'
                      '`bool` subclasses `int`, so `isinstance(True, int)` is true. '
                      'Use `type(value) is int` when a contract requires an exact built-in integer. '
                      'Keep tests for ordinary integers and both boolean values.\n\n'
                      f'Patch SHA-256: {self.row["patch_sha256"]}\n'
                      f'Pinned test configuration SHA-256: {self.test_config_hash}\n'
                      'Verification: Broker fixed test command passed.\n')
            problem = 'An exact-integer requirement can admit booleans through Python subclass checks.'
            procedure = 'Use type(value) is int only when the contract requires the exact built-in integer type; test True and False as rejected inputs.'
        else:
            lesson = ('# Candidate: preserve failed coding verification\n\n'
                      'The fixed Broker test command failed. Preserve its host receipt and output, '
                      'classify the result as unverified, and diagnose before proposing another patch. '
                      'Do not auto-edit or replay an ambiguous write.\n\n'
                      f'Patch SHA-256: {self.row["patch_sha256"]}\n'
                      f'Exit code: {test_result.get("exit_code")}\n'
                      f'Test output:\n{str(test_result.get("output", ""))[:12000]}\n')
            problem = 'A coding proposal failed its predefined local verification command.'
            procedure = 'Preserve the test receipt and output, report failure as unverified, diagnose the evidence, and require a separately reviewed proposal before any further write.'
        atomic_write(lesson_path, lesson.encode())
        ref = {'kind': 'artifact', 'uri': str(lesson_path), 'sha256': file_hash(lesson_path),
               'run_id': self.run_id}
        evidence_refs = [ref]
        if success:
            receipt_path = self.project.root / '.local' / 'orchestration' / 'runs' / self.run_id / 'outputs' / 'coding' / 'broker' / 'command-0.json'
            if not receipt_path.is_file():
                raise CodingWorkflowError('Successful test receipt is missing; candidate was not recorded.')
            evidence_refs.append({'kind': 'command_receipt', 'uri': str(receipt_path),
                                  'sha256': file_hash(receipt_path), 'run_id': self.run_id})
        SkillLibrary(self.project).propose(category='coding',
            problem=problem, procedure=procedure,
            source_refs=[ref], evidence_refs=evidence_refs, run_id=self.run_id,
            task_id='coding_task', source_agent='developer',
            source_event_id='coding-workflow-' + self.run_id)

