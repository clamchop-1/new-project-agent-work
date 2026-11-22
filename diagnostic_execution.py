"""Portable diagnostic worker contracts and nonprivileged test doubles.

The backend protocol describes effects but implements none. The only backend
here is an in-memory fake; real platform adapters require separate approval.
Authorization is supplied as a consumed grant for each mutating operation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import threading
from types import MappingProxyType
from typing import Protocol
import uuid

from diagnostic_trust import ConsumedApproval, TrustContractError, is_consumed_approval


class ExecutionContractError(ValueError):
    """A worker request or backend interaction violates the portable contract."""


@dataclass(frozen=True)
class ResourceLimits:
    cpu_count: int
    memory_bytes: int
    disk_bytes: int
    timeout_seconds: int
    process_count: int
    output_bytes: int

    def as_dict(self) -> dict[str, int]:
        return {
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "disk_bytes": self.disk_bytes,
            "timeout_seconds": self.timeout_seconds,
            "process_count": self.process_count,
            "output_bytes": self.output_bytes,
        }

    def __post_init__(self) -> None:
        values = self.as_dict()
        if any(type(value) is not int or value < 1 for value in values.values()):
            raise ExecutionContractError("Worker resource limits must be positive integers.")


@dataclass(frozen=True)
class ExecutionPlan:
    project_id: str
    project_instance_id: str
    run_id: str
    run_nonce: str
    source_sha256: str
    backend_id: str
    backend_version: str
    vm_identity: str
    architecture: str
    operation_id: str
    launcher_sha256: str
    media_hashes: dict[str, str]
    resource_limits: ResourceLimits

    def __post_init__(self) -> None:
        for label, value in (("run ID", self.run_id), ("run nonce", self.run_nonce),
                             ("project instance ID", self.project_instance_id)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ExecutionContractError(f"Execution plan {label} is malformed.")
        for label, value in (("source", self.source_sha256), ("launcher", self.launcher_sha256)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ExecutionContractError(f"Execution plan {label} digest is malformed.")
        copied_media = dict(self.media_hashes)
        if not copied_media or any(not re.fullmatch(r"[0-9a-f]{64}", digest)
                                   for digest in copied_media.values()):
            raise ExecutionContractError("Execution plan media hashes are malformed.")
        object.__setattr__(self, "media_hashes", MappingProxyType(copied_media))


@dataclass(frozen=True)
class BackendCapabilities:
    backend_id: str
    backend_version: str
    architectures: frozenset[str]
    supports_offline_media: bool
    max_workers: int
    maximum_limits: ResourceLimits


@dataclass(frozen=True)
class PreflightReport:
    backend_id: str
    vm_identity: str
    architecture: str
    isolated: bool
    networking_disabled: bool
    host_sharing_disabled: bool
    observed_at: str
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedMedia:
    input_media_id: str
    input_sha256: str
    output_media_id: str
    output_capacity_bytes: int


@dataclass(frozen=True)
class WorkerHandle:
    worker_id: str
    vm_identity: str
    run_id: str


@dataclass(frozen=True)
class CollectedOutput:
    run_id: str
    staging_path: Path
    media_id: str
    read_only: bool


class ExecutionBackend(Protocol):
    """Platform-neutral contract. Implementations must remain policy-blind."""

    def capabilities(self) -> BackendCapabilities: ...

    def preflight(self, plan: ExecutionPlan) -> PreflightReport: ...

    def prepare_media(self, plan: ExecutionPlan,
                      grant: ConsumedApproval) -> PreparedMedia: ...

    def attach(self, plan: ExecutionPlan, media: PreparedMedia,
               grant: ConsumedApproval) -> None: ...

    def start(self, plan: ExecutionPlan, media: PreparedMedia,
              grant: ConsumedApproval) -> WorkerHandle: ...

    def invoke_explicit_operation(self, plan: ExecutionPlan, worker: WorkerHandle,
                                  grant: ConsumedApproval) -> None: ...

    def stop(self, plan: ExecutionPlan, worker: WorkerHandle,
             grant: ConsumedApproval) -> None: ...

    def detach(self, plan: ExecutionPlan, media: PreparedMedia,
               grant: ConsumedApproval) -> None: ...

    def collect_read_only(self, plan: ExecutionPlan, media: PreparedMedia,
                          destination: Path,
                          grant: ConsumedApproval) -> CollectedOutput: ...

    def cleanup(self, plan: ExecutionPlan, media: PreparedMedia,
                grant: ConsumedApproval) -> None: ...


@dataclass(frozen=True)
class WorkerRequest:
    worker_id: str
    run_id: str
    scratch_path: Path
    writable_media_ids: tuple[str, ...]
    limits: ResourceLimits


@dataclass(frozen=True)
class WorkerLease:
    lease_id: str
    request: WorkerRequest


class WorkerAdmissionController:
    """Thread-safe admission accounting for isolated, non-shared workers."""

    def __init__(self, *, max_workers: int, aggregate_limits: ResourceLimits):
        if type(max_workers) is not int or max_workers < 1:
            raise ExecutionContractError("Worker capacity must be positive.")
        self.max_workers = max_workers
        self.aggregate_limits = aggregate_limits
        self._leases: dict[str, WorkerLease] = {}
        self._lock = threading.Lock()

    def admit(self, request: WorkerRequest) -> WorkerLease:
        self._validate_request(request)
        with self._lock:
            active = tuple(self._leases.values())
            if len(active) >= self.max_workers:
                raise ExecutionContractError("Worker capacity is exhausted.")
            if any(lease.request.worker_id == request.worker_id
                   or lease.request.run_id == request.run_id for lease in active):
                raise ExecutionContractError("Worker or run identity is already active.")
            scratch_key = _canonical_path_key(request.scratch_path)
            if any(_paths_overlap(_canonical_path_key(item.request.scratch_path), scratch_key)
                   for item in active):
                raise ExecutionContractError("Active workers cannot share a scratch directory.")
            requested_media = set(request.writable_media_ids)
            if any(requested_media.intersection(lease.request.writable_media_ids)
                   for lease in active):
                raise ExecutionContractError("Active workers cannot share writable media.")
            self._require_capacity((*active, WorkerLease("candidate", request)))
            lease = WorkerLease(uuid.uuid4().hex, request)
            self._leases[lease.lease_id] = lease
            return lease

    def release(self, lease: WorkerLease) -> None:
        with self._lock:
            if self._leases.get(lease.lease_id) != lease:
                raise ExecutionContractError("Worker lease is absent, stale, or belongs elsewhere.")
            del self._leases[lease.lease_id]

    def _validate_request(self, request: WorkerRequest) -> None:
        if not isinstance(request, WorkerRequest):
            raise ExecutionContractError("Worker request has an invalid type.")
        for name, value in (("worker ID", request.worker_id), ("run ID", request.run_id)):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ExecutionContractError(f"{name} is invalid.")
        if not isinstance(request.scratch_path, Path) or not request.scratch_path.is_absolute():
            raise ExecutionContractError("Worker scratch path must be absolute.")
        if (not isinstance(request.writable_media_ids, tuple)
                or not request.writable_media_ids
                or any(not isinstance(item, str) or not item or len(item) > 128
                       for item in request.writable_media_ids)
                or len(set(request.writable_media_ids)) != len(request.writable_media_ids)):
            raise ExecutionContractError("Workers require unique, run-specific writable media IDs.")

    def _require_capacity(self, leases: tuple[WorkerLease, ...]) -> None:
        used = {name: sum(getattr(lease.request.limits, name) for lease in leases)
                for name in self.aggregate_limits.as_dict()}
        available = self.aggregate_limits.as_dict()
        if any(used[name] > available[name] for name in used):
            raise ExecutionContractError("Aggregate worker resource limits would be exceeded.")


class FakeExecutionBackend:
    """In-memory fake that records calls and performs no host/guest effects."""

    def __init__(self, capabilities: BackendCapabilities,
                 preflight_report: PreflightReport):
        self._capabilities = capabilities
        self._preflight_report = preflight_report
        self.calls: list[str] = []
        self._attached: set[str] = set()
        self._running: set[str] = set()

    def capabilities(self) -> BackendCapabilities:
        self.calls.append("capabilities")
        return self._capabilities

    def preflight(self, plan: ExecutionPlan) -> PreflightReport:
        self.calls.append("preflight")
        self._match_plan(plan)
        return self._preflight_report

    def prepare_media(self, plan: ExecutionPlan, grant: ConsumedApproval) -> PreparedMedia:
        self._require_action(grant, plan, "prepare_media")
        self.calls.append("prepare_media")
        return PreparedMedia("fake-input-" + plan.run_id, plan.media_hashes["input_iso"],
                             "fake-output-" + plan.run_id, plan.resource_limits.disk_bytes)

    def attach(self, plan: ExecutionPlan, media: PreparedMedia,
               grant: ConsumedApproval) -> None:
        self._require_action(grant, plan, "attach_media")
        if plan.run_id in self._attached:
            raise ExecutionContractError("Run media is already attached in the fake backend.")
        self._attached.add(plan.run_id)
        self.calls.append("attach")

    def start(self, plan: ExecutionPlan, media: PreparedMedia,
              grant: ConsumedApproval) -> WorkerHandle:
        self._require_action(grant, plan, "start_worker")
        if plan.run_id not in self._attached or plan.run_id in self._running:
            raise ExecutionContractError("Fake worker is not attached or is already running.")
        self._running.add(plan.run_id)
        self.calls.append("start")
        return WorkerHandle("fake-worker-" + plan.run_id, plan.vm_identity, plan.run_id)

    def invoke_explicit_operation(self, plan: ExecutionPlan, worker: WorkerHandle,
                                  grant: ConsumedApproval) -> None:
        self._require_action(grant, plan, "invoke_operation")
        if worker.run_id not in self._running:
            raise ExecutionContractError("Fake worker is not running.")
        self.calls.append("invoke_explicit_operation")

    def stop(self, plan: ExecutionPlan, worker: WorkerHandle,
             grant: ConsumedApproval) -> None:
        self._require_action(grant, plan, "stop_worker")
        if worker.run_id not in self._running:
            raise ExecutionContractError("Fake worker is not running.")
        self._running.remove(worker.run_id)
        self.calls.append("stop")

    def detach(self, plan: ExecutionPlan, media: PreparedMedia,
               grant: ConsumedApproval) -> None:
        self._require_action(grant, plan, "detach_media")
        if plan.run_id in self._running:
            raise ExecutionContractError("Fake worker must be stopped before detach.")
        self._attached.discard(plan.run_id)
        self.calls.append("detach")

    def collect_read_only(self, plan: ExecutionPlan, media: PreparedMedia,
                          destination: Path, grant: ConsumedApproval) -> CollectedOutput:
        self._require_action(grant, plan, "collect_output")
        if plan.run_id in self._attached or plan.run_id in self._running:
            raise ExecutionContractError("Fake output collection requires detached media.")
        self.calls.append("collect_read_only")
        return CollectedOutput(plan.run_id, destination, media.output_media_id, True)

    def cleanup(self, plan: ExecutionPlan, media: PreparedMedia,
                grant: ConsumedApproval) -> None:
        self._require_action(grant, plan, "cleanup")
        if plan.run_id in self._attached or plan.run_id in self._running:
            raise ExecutionContractError("Fake cleanup requires a stopped and detached worker.")
        self.calls.append("cleanup")

    def _match_plan(self, plan: ExecutionPlan) -> None:
        if (plan.backend_id != self._capabilities.backend_id
                or plan.backend_version != self._capabilities.backend_version
                or plan.architecture not in self._capabilities.architectures):
            raise ExecutionContractError("Execution plan does not match fake backend capabilities.")

    @staticmethod
    def _require_action(grant: ConsumedApproval, plan: ExecutionPlan, action: str) -> None:
        if not is_consumed_approval(grant) or grant.action != action:
            raise TrustContractError("A consumed single-action approval is required.")
        payload = grant.approval.payload
        for field, expected in (("project_id", plan.project_id),
                                ("project_instance_id", plan.project_instance_id),
                                ("run_id", plan.run_id), ("run_nonce", plan.run_nonce),
                                ("source_sha256", plan.source_sha256),
                                ("backend_id", plan.backend_id),
                                ("backend_version", plan.backend_version),
                                ("vm_identity", plan.vm_identity),
                                ("architecture", plan.architecture),
                                ("operation_id", plan.operation_id),
                                ("launcher_sha256", plan.launcher_sha256),
                                ("media_hashes", plan.media_hashes),
                                ("resource_limits", plan.resource_limits.as_dict())):
            if payload[field] != expected:
                raise TrustContractError(f"Consumed approval does not bind the backend {field}.")


def _canonical_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path)).casefold()


def _paths_overlap(left: str, right: str) -> bool:
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False
    return common in {left, right}
