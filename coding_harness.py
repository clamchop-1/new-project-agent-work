"""Versioned, host-owned contract for coding harness implementations.

Requests describe work; they do not grant authority. A host-only execution
context supplies the existing Broker-backed runner and its approved scope.
Harness output is untrusted until independently checked by the host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable
import uuid


SCHEMA_VERSION = 1
EVENT_TYPES = frozenset({
    'run.started', 'assistant.turn.completed', 'tool.proposed',
    'tool.completed', 'approval.requested', 'artifact.reported',
    'run.cancel_requested', 'run.completed', 'run.failed',
    'run.incomplete', 'run.cancelled', 'run.timed_out', 'skill.candidate.proposed',
})
TERMINAL_EVENTS = frozenset({'run.completed', 'run.incomplete', 'run.failed', 'run.cancelled', 'run.timed_out'})


class HarnessContractError(ValueError):
    """Invalid or unsupported versioned harness data."""


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise HarnessContractError(f'{label} must be a JSON object.')
    return value


def _required(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    missing = keys - value.keys()
    if missing:
        raise HarnessContractError(f'{label} missing fields: {", ".join(sorted(missing))}.')


def _version(value: Mapping[str, Any], label: str) -> None:
    if type(value.get('schema_version')) is not int or value['schema_version'] != SCHEMA_VERSION:
        raise HarnessContractError(f'Unsupported {label} schema version.')


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessContractError(f'{label} must be a nonempty string.')
    return value


@dataclass(frozen=True)
class EvidenceReference:
    kind: str
    uri: str
    sha256: str
    evidence_id: str = ''

    def __post_init__(self):
        _nonempty(self.kind, 'Evidence kind')
        _nonempty(self.uri, 'Evidence URI')
        if len(self.sha256) != 64 or any(c not in '0123456789abcdef' for c in self.sha256):
            raise HarnessContractError('Evidence reference requires a lowercase SHA-256 hash.')

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'uri': self.uri, 'sha256': self.sha256,
                'evidence_id': self.evidence_id}

    @classmethod
    def from_dict(cls, value: Any) -> 'EvidenceReference':
        row = _object(value, 'Evidence reference')
        _required(row, {'kind', 'uri', 'sha256'}, 'Evidence reference')
        return cls(row['kind'], row['uri'], row['sha256'], row.get('evidence_id', ''))


@dataclass(frozen=True)
class SessionReference:
    harness_id: str
    session_id: str
    resumable: bool

    def to_dict(self) -> dict:
        return {'harness_id': self.harness_id, 'session_id': self.session_id,
                'resumable': self.resumable}

    @classmethod
    def from_dict(cls, value: Any) -> 'SessionReference':
        row = _object(value, 'Session reference')
        _required(row, {'harness_id', 'session_id', 'resumable'}, 'Session reference')
        if type(row['resumable']) is not bool:
            raise HarnessContractError('Session resumable must be boolean.')
        return cls(_nonempty(row['harness_id'], 'Session harness id'),
                   _nonempty(row['session_id'], 'Session id'), row['resumable'])


@dataclass(frozen=True)
class ExecutionBudgets:
    max_turns: int = 16
    max_tool_calls: int = 64
    wall_seconds: float = 300.0
    cancellation_grace_seconds: float = 20.0

    def __post_init__(self):
        if type(self.max_turns) is not int or self.max_turns < 1:
            raise HarnessContractError('max_turns must be a positive integer.')
        if type(self.max_tool_calls) is not int or self.max_tool_calls < 0:
            raise HarnessContractError('max_tool_calls must be a nonnegative integer.')
        for name in ('wall_seconds', 'cancellation_grace_seconds'):
            value = getattr(self, name)
            if type(value) not in (float, int) or value <= 0:
                raise HarnessContractError(f'{name} must be positive.')

    def to_dict(self) -> dict:
        return {'max_turns': self.max_turns, 'max_tool_calls': self.max_tool_calls,
                'wall_seconds': self.wall_seconds,
                'cancellation_grace_seconds': self.cancellation_grace_seconds}

    @classmethod
    def from_dict(cls, value: Any) -> 'ExecutionBudgets':
        row = _object(value, 'Execution budgets')
        _required(row, {'max_turns', 'max_tool_calls', 'wall_seconds',
                        'cancellation_grace_seconds'}, 'Execution budgets')
        return cls(**row)


@dataclass(frozen=True)
class HarnessTaskRequest:
    task_id: str
    run_id: str
    project_id: str
    objective: str
    model: str
    provider: str
    budgets: ExecutionBudgets = field(default_factory=ExecutionBudgets)
    instruction_refs: tuple[EvidenceReference, ...] = ()
    memory_refs: tuple[EvidenceReference, ...] = ()
    session_ref: SessionReference | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version({'schema_version': self.schema_version}, 'task request')
        for name in ('task_id', 'run_id', 'project_id', 'objective', 'model', 'provider'):
            _nonempty(getattr(self, name), name)
        if not isinstance(self.budgets, ExecutionBudgets):
            raise HarnessContractError('Task request budgets are invalid.')

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version, 'task_id': self.task_id,
                'run_id': self.run_id, 'project_id': self.project_id,
                'objective': self.objective, 'model': self.model,
                'provider': self.provider, 'budgets': self.budgets.to_dict(),
                'instruction_refs': [r.to_dict() for r in self.instruction_refs],
                'memory_refs': [r.to_dict() for r in self.memory_refs],
                'session_ref': self.session_ref.to_dict() if self.session_ref else None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Any) -> 'HarnessTaskRequest':
        row = _object(value, 'Task request')
        _version(row, 'task request')
        _required(row, {'task_id', 'run_id', 'project_id', 'objective', 'model',
                        'provider', 'budgets'}, 'Task request')
        allowed = {'schema_version', 'task_id', 'run_id', 'project_id', 'objective',
                   'model', 'provider', 'budgets', 'instruction_refs', 'memory_refs',
                   'session_ref'}
        if row.keys() - allowed:
            raise HarnessContractError('Unknown task request field.')
        for name in ('instruction_refs', 'memory_refs'):
            if not isinstance(row.get(name, []), list):
                raise HarnessContractError(f'{name} must be a list.')
        session = row.get('session_ref')
        return cls(
            schema_version=row['schema_version'], task_id=row['task_id'], run_id=row['run_id'],
            project_id=row['project_id'], objective=row['objective'], model=row['model'],
            provider=row['provider'], budgets=ExecutionBudgets.from_dict(row['budgets']),
            instruction_refs=tuple(EvidenceReference.from_dict(v) for v in row.get('instruction_refs', [])),
            memory_refs=tuple(EvidenceReference.from_dict(v) for v in row.get('memory_refs', [])),
            session_ref=SessionReference.from_dict(session) if session is not None else None,
        )

    @classmethod
    def from_json(cls, raw: str) -> 'HarnessTaskRequest':
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HarnessContractError('Task request is not valid JSON.') from exc


@dataclass(frozen=True)
class HarnessEvent:
    run_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    harness_id: str = 'unknown'
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        _version({'schema_version': self.schema_version}, 'event')
        _nonempty(self.run_id, 'Event run id')
        _nonempty(self.event_id, 'Event id')
        if type(self.sequence) is not int or self.sequence < 1:
            raise HarnessContractError('Event sequence must be a positive integer.')
        if self.event_type not in EVENT_TYPES:
            raise HarnessContractError(f'Unknown harness event type: {self.event_type}.')
        if not isinstance(self.payload, Mapping):
            raise HarnessContractError('Event payload must be an object.')
        try:
            json.dumps(dict(self.payload), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise HarnessContractError('Event payload must contain JSON values.') from exc
        if self.event_type == 'skill.candidate.proposed':
            _required(self.payload, {'candidate_lesson_ref', 'source_refs', 'evidence_refs',
                                     'validation_status', 'source_hashes'}, 'Skill candidate event')
            if not isinstance(self.payload['source_refs'], list) or not isinstance(self.payload['evidence_refs'], list):
                raise HarnessContractError('Candidate source and evidence references must be lists.')
            if not isinstance(self.payload['source_hashes'], list):
                raise HarnessContractError('Candidate source hashes must be a list.')
            if (not isinstance(self.payload['candidate_lesson_ref'], str)
                    or not self.payload['candidate_lesson_ref']
                    or len(self.payload['candidate_lesson_ref']) > 256
                    or len(self.payload['source_refs']) > 32
                    or len(self.payload['evidence_refs']) > 32
                    or len(self.payload['source_hashes']) > 32):
                raise HarnessContractError('Candidate event references exceed their bounds.')
            for digest in self.payload['source_hashes']:
                if not isinstance(digest, str) or len(digest) != 64 or any(
                        char not in '0123456789abcdef' for char in digest):
                    raise HarnessContractError('Candidate source hashes must be lowercase SHA-256 values.')
            if self.payload['validation_status'] not in {'unvalidated', 'validated', 'rejected'}:
                raise HarnessContractError('Unknown skill-candidate validation status.')

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version, 'run_id': self.run_id,
                'sequence': self.sequence, 'event_id': self.event_id,
                'occurred_at': self.occurred_at, 'harness_id': self.harness_id,
                'event_type': self.event_type, 'payload': dict(self.payload)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Any) -> 'HarnessEvent':
        row = _object(value, 'Harness event')
        _version(row, 'event')
        _required(row, {'run_id', 'sequence', 'event_id', 'occurred_at',
                        'harness_id', 'event_type', 'payload'}, 'Harness event')
        allowed = {'schema_version', 'run_id', 'sequence', 'event_id', 'occurred_at',
                   'harness_id', 'event_type', 'payload'}
        if row.keys() - allowed:
            raise HarnessContractError('Unknown harness event field.')
        return cls(schema_version=row['schema_version'], run_id=row['run_id'],
                   sequence=row['sequence'], event_id=row['event_id'],
                   occurred_at=row['occurred_at'], harness_id=row['harness_id'],
                   event_type=row['event_type'], payload=row['payload'])


@dataclass(frozen=True)
class HarnessCapabilities:
    native_resume: bool
    max_turns: bool
    max_tool_calls: bool
    wall_time: bool
    cooperative_cancel: bool
    broker_only_tools: bool = True


@dataclass(frozen=True)
class HarnessResult:
    schema_version: int
    run_id: str
    harness_id: str
    harness_version: str
    status: str
    raw_status: str
    summary: Mapping[str, Any]
    session_ref: SessionReference | None
    evidence_refs: tuple[EvidenceReference, ...]
    receipt_refs: tuple[EvidenceReference, ...]
    verification_authority: str = 'host_pending'

    def __post_init__(self):
        _version({'schema_version': self.schema_version}, 'result')
        if self.verification_authority != 'host_pending':
            raise HarnessContractError('Harness cannot claim independent verification.')
        if self.status not in {'completed', 'incomplete', 'failed', 'cancelled', 'timed_out'}:
            raise HarnessContractError('Unknown terminal harness result status.')

    def to_dict(self) -> dict:
        return {'schema_version': self.schema_version, 'run_id': self.run_id,
                'harness_id': self.harness_id, 'harness_version': self.harness_version,
                'status': self.status, 'raw_status': self.raw_status,
                'summary': dict(self.summary),
                'session_ref': self.session_ref.to_dict() if self.session_ref else None,
                'evidence_refs': [v.to_dict() for v in self.evidence_refs],
                'receipt_refs': [v.to_dict() for v in self.receipt_refs],
                'verification_authority': self.verification_authority}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Any) -> 'HarnessResult':
        row = _object(value, 'Harness result')
        _version(row, 'result')
        required = {'run_id', 'harness_id', 'harness_version', 'status', 'raw_status',
                    'summary', 'evidence_refs', 'receipt_refs', 'verification_authority'}
        _required(row, required, 'Harness result')
        allowed = required | {'schema_version', 'session_ref'}
        if row.keys() - allowed:
            raise HarnessContractError('Unknown harness result field.')
        if (not isinstance(row['summary'], dict) or not isinstance(row['evidence_refs'], list)
                or not isinstance(row['receipt_refs'], list)):
            raise HarnessContractError('Malformed harness result collections.')
        session = row.get('session_ref')
        return cls(row['schema_version'], row['run_id'], row['harness_id'],
                   row['harness_version'], row['status'], row['raw_status'], row['summary'],
                   SessionReference.from_dict(session) if session is not None else None,
                   tuple(EvidenceReference.from_dict(v) for v in row['evidence_refs']),
                   tuple(EvidenceReference.from_dict(v) for v in row['receipt_refs']),
                   row['verification_authority'])

    @classmethod
    def from_json(cls, raw: str) -> 'HarnessResult':
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HarnessContractError('Harness result is not valid JSON.') from exc


@dataclass(frozen=True)
class LegacyExecutionContext:
    """Host-only arguments. Never serialize or accept these from a model."""
    project: Any
    output_dir: Path
    endpoint: str
    approved_write_paths: tuple[str, ...] = ()
    test_config: Path | None = None
    provider_name: str = 'ollama'
    context_tokens: int = 16384
    conversation: tuple[Mapping[str, Any], ...] = ()
    assistant_root: Path | None = None
    assistant_write_paths: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    role_instructions: str = ''
    provider_profile: Mapping[str, Any] | None = None
    remote_profile: Mapping[str, Any] | None = None
    fallback_policy: Mapping[str, Any] | None = None
    fallback_approval: Any = None
    routing_decision: Mapping[str, Any] | None = None

    @property
    def project_id(self) -> str:
        return self.project.identity


@runtime_checkable
class HarnessExecutionContext(Protocol):
    """Host-provided execution context; concrete grants remain host-owned."""
    @property
    def project_id(self) -> str: ...


class HarnessRunHandle(Protocol):
    def events(self) -> Iterator[HarnessEvent]: ...
    def wait(self, timeout: float | None = None) -> HarnessResult | None: ...
    def cancel(self, reason: str = 'operator') -> bool: ...
    def close(self, timeout: float | None = None) -> bool: ...


class HarnessRun:
    """One supervised run; close waits for the owned worker and preserves evidence."""
    def __init__(self, adapter: 'LegacyCustomHarness', request: HarnessTaskRequest,
                 context: LegacyExecutionContext):
        self.adapter, self.request, self.context = adapter, request, context
        self.state = 'running'
        self.result: HarnessResult | None = None
        self.error: str | None = None
        self._queue: queue.Queue[HarnessEvent | None] = queue.Queue()
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._timed_out = False
        self._sequence = 0
        self._emit('run.started', {'task_id': request.task_id})
        self._timer = threading.Timer(float(request.budgets.wall_seconds), self._expire)
        self._timer.daemon = True
        self._timer.start()
        self._worker = threading.Thread(target=self._execute, name='lca-harness-'+request.run_id, daemon=True)
        self._worker.start()

    def _emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            self._sequence += 1
            self._queue.put(HarnessEvent(self.request.run_id, self._sequence, event_type,
                                         payload or {}, harness_id=self.adapter.harness_id))

    def _expire(self):
        with self._lock:
            if self.state in {'running', 'cancel_requested'}:
                self._timed_out = True
        self.cancel(reason='wall_time_budget')

    def cancel(self, reason: str = 'operator') -> bool:
        with self._lock:
            if self.state not in {'running', 'cancel_requested'}:
                return False
            if self.state == 'running':
                self.state = 'cancel_requested'
                self._cancel.set()
                self._emit('run.cancel_requested', {'reason': str(reason)[:120]})
            return True

    def _execute(self):
        raw_result: Mapping[str, Any] = {}
        try:
            if not self._cancel.is_set():
                ctx = self.context
                raw_result = self.adapter.runner(
                    ctx.project, self.request.model, self.request.objective,
                    ctx.output_dir, ctx.endpoint,
                    writes=ctx.approved_write_paths,
                    test_config=ctx.test_config,
                    max_steps=self.request.budgets.max_turns,
                    context=ctx.context_tokens,
                    conversation=ctx.conversation,
                    provider_name=ctx.provider_name,
                    assistant_root=ctx.assistant_root,
                    assistant_write_paths=ctx.assistant_write_paths,
                    max_tool_calls=self.request.budgets.max_tool_calls,
                    cancel_event=self._cancel,
                    event_callback=self._event_callback,
                    allowed_tools=ctx.allowed_tools,
                    role_instructions=ctx.role_instructions,
                    provider_profile=ctx.provider_profile,
                    remote_profile=ctx.remote_profile,
                    fallback_policy=ctx.fallback_policy,
                    fallback_approval=ctx.fallback_approval,
                    routing_decision=ctx.routing_decision,
                )
        except BaseException as exc:  # Preserve harness failures as a terminal record.
            self.error = str(exc)[:1000]
            try:
                raw_path = self.context.output_dir / 'result.json'
                raw_result = json.loads(raw_path.read_text(encoding='utf-8')) if raw_path.is_file() else {}
            except (OSError, ValueError):
                raw_result = {}
        finally:
            self._timer.cancel()
            self._finish(raw_result)

    def _event_callback(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self._emit(event_type, payload)

    def _finish(self, raw: Mapping[str, Any]):
        raw_status = str(raw.get('status', 'failed'))
        if self._timed_out:
            status, event_type = 'timed_out', 'run.timed_out'
        elif self._cancel.is_set():
            status, event_type = 'cancelled', 'run.cancelled'
        elif raw_status == 'complete':
            status, event_type = 'completed', 'run.completed'
        elif raw_status in {'running', 'failed'} or self.error:
            status, event_type = 'failed', 'run.failed'
        else:
            status, event_type = 'incomplete', 'run.incomplete'

        evidence: list[EvidenceReference] = []
        receipts: list[EvidenceReference] = []
        for name, kind, target in (('result.json', 'harness_result', evidence),
                                   ('events.jsonl', 'harness_event_log', evidence)):
            path = self.context.output_dir / name
            if path.is_file():
                target.append(EvidenceReference(kind, str(path.resolve()),
                                                hashlib.sha256(path.read_bytes()).hexdigest()))
        for row in raw.get('commands', []) if isinstance(raw.get('commands', []), list) else []:
            if isinstance(row, dict) and isinstance(row.get('path'), str):
                path = Path(row['path'])
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row.get('sha256'):
                    receipts.append(EvidenceReference('command_receipt', str(path.resolve()),
                                                      row['sha256']))
        summary = dict(raw.get('summary')) if isinstance(raw.get('summary'), Mapping) else {}
        # Host-produced fields remain separate from the model's own finish claim.
        for key in ('verified_test_status', 'receipt_integrity',
                    'completion_classification', 'model_metrics', 'writes',
                    'provider', 'model', 'model_digest', 'routing_decision',
                    'error', 'provider_error'):
            if key in raw:
                summary['host_' + key] = raw[key]
        result = HarnessResult(
            SCHEMA_VERSION, self.request.run_id, self.adapter.harness_id,
            self.adapter.version, status, raw_status,
            summary,
            None, tuple(evidence), tuple(receipts),
        )
        with self._lock:
            self.result = result
            self.state = 'terminal'
            self._emit(event_type, {'status': status, 'raw_status': raw_status,
                                    'error': self.error, 'evidence_refs': [v.to_dict() for v in evidence],
                                    'receipt_refs': [v.to_dict() for v in receipts]})
            self._queue.put(None)

    def events(self) -> Iterator[HarnessEvent]:
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item

    def wait(self, timeout: float | None = None) -> HarnessResult | None:
        self._worker.join(timeout)
        return self.result if not self._worker.is_alive() else None

    def close(self, timeout: float | None = None) -> bool:
        if self._worker.is_alive():
            self.cancel('close')
        self._worker.join(timeout if timeout is not None else
                          float(self.request.budgets.cancellation_grace_seconds))
        if self._worker.is_alive():
            return False
        with self._lock:
            self.state = 'closed'
        return True


@runtime_checkable
class CodingHarness(Protocol):
    harness_id: str
    version: str
    capabilities: HarnessCapabilities

    def start(self, request: HarnessTaskRequest,
              context: HarnessExecutionContext) -> HarnessRunHandle: ...

    def resume(self, request: HarnessTaskRequest, session: SessionReference,
               context: HarnessExecutionContext) -> HarnessRunHandle: ...


class LegacyCustomHarness:
    """Adapter for the established local_agent.run_agent custom loop."""
    harness_id = 'legacy_custom'
    version = '1'
    capabilities = HarnessCapabilities(
        native_resume=False, max_turns=True, max_tool_calls=True,
        wall_time=True, cooperative_cancel=True, broker_only_tools=True,
    )

    def __init__(self, runner=None):
        if runner is None:
            from local_agent import run_agent
            runner = run_agent
        self.runner = runner
        self._runs: dict[str, HarnessRun] = {}
        self._lock = threading.Lock()

    def start(self, request: HarnessTaskRequest,
              context: HarnessExecutionContext) -> HarnessRun:
        if not isinstance(context, LegacyExecutionContext):
            raise HarnessContractError('LegacyCustomHarness requires its host-owned legacy execution context.')
        if request.session_ref is not None:
            raise HarnessContractError('LegacyCustomHarness does not support resumable sessions.')
        if request.project_id != context.project_id:
            raise HarnessContractError('Task project identity does not match host execution context.')
        with self._lock:
            if request.run_id in self._runs:
                raise HarnessContractError('Run id is already active or retained by this harness.')
            handle = HarnessRun(self, request, context)
            self._runs[request.run_id] = handle
        return handle

    def resume(self, request: HarnessTaskRequest, session: SessionReference,
               context: HarnessExecutionContext) -> HarnessRun:
        raise HarnessContractError('LegacyCustomHarness has no native session-resume capability.')

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            handle = self._runs.get(run_id)
        return handle.cancel() if handle else False

    def close(self, run_id: str, timeout: float | None = None) -> bool:
        with self._lock:
            handle = self._runs.get(run_id)
        closed = handle.close(timeout) if handle else True
        if closed:
            with self._lock:
                self._runs.pop(run_id, None)
        return closed
