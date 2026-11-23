"""Append-only diagnostic operation evidence and fake-only coordination.

This module adds evidence records without changing the version-1 journal or
approval/receipt schemas. It does not implement a production platform backend,
collection adapter, validator, or signing authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import BinaryIO, Callable, Iterable, Mapping, Protocol
import uuid

from diagnostic_runs import (
    DiagnosticRunError,
    DiagnosticRunLedger,
    RunInterruptedError,
    atomic_create_new_file,
)
from diagnostic_trust import (
    ApprovalConsumptionStore,
    ConsumedApproval,
    TrustContractError,
    TrustPolicy,
    consume_approval,
    is_consumed_approval,
    verify_approval,
    canonical_json,
)


CONTRACT_ID = "localcodingagent.diagnostic.evidence"
CONTRACT_VERSION = 1
INTENT_SCHEMA = "localcodingagent.diagnostic.operation-intent"
RESULT_SCHEMA = "localcodingagent.diagnostic.operation-result"
CONSUMPTION_SCHEMA = "localcodingagent.diagnostic.approval-consumption"
COLLECTION_SCHEMA = "localcodingagent.diagnostic.collection"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ACTION_SET = frozenset({
    "prepare_media", "attach_media", "start_worker", "invoke_operation",
    "stop_worker", "detach_media", "collect_output", "cleanup",
})
RESULT_STATUSES = frozenset({"completed", "failed", "outcome_unknown",
                              "operator_intervention_required"})
SOURCE_CLASSES = frozenset({"direct_machine", "signed_platform_record", "operator_report",
                            "guest_report", "simulated", "unknown"})
_PROCESS_SESSION_ID = uuid.uuid4().hex
ACTION_STATE_TRANSITIONS = {
    "prepare_media": ("reserved", "preparing"),
    "attach_media": ("approved", "attached"),
    "start_worker": ("attached", "running"),
    "stop_worker": ("running", "stopped"),
    "detach_media": ("stopped", "detached"),
    "collect_output": ("detached", "collected"),
}


class EvidenceError(RuntimeError):
    """Evidence is malformed, inconsistent, incomplete, or unsafe to use."""


class OperationPendingError(EvidenceError):
    """An operation is ambiguous or has not reached a committed ledger event."""


class BackendActionFailure(EvidenceError):
    """A backend reports a failed or ambiguous action with explicit evidence."""

    def __init__(self, status: str, *, attempted: bool,
                 occurred_at: str | None, time_observation: dict,
                 observation: dict, message: str, source_class: str = "simulated"):
        if status not in RESULT_STATUSES - {"completed"}:
            raise ValueError("Backend failure status is invalid.")
        super().__init__(message)
        self.status = status
        self.attempted = attempted
        self.occurred_at = occurred_at
        self.time_observation = time_observation
        self.observation = observation
        self.source_class = source_class


@dataclass(frozen=True)
class EvidenceRef:
    record_type: str
    record_id: str
    relative_path: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "record_type": self.record_type,
            "record_id": self.record_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ActionObservation:
    occurred_at: str | None
    time_observation: dict
    observation: dict
    source_class: str = "simulated"


@dataclass(frozen=True)
class SourceIdentity:
    """Identity a future read-only collector must verify before reading."""

    source_id: str
    media_sha256: str
    capacity_bytes: int
    access_mode: str


@dataclass(frozen=True)
class SourceFileEntry:
    name: str
    size: int
    kind: str = "regular"
    link_count: int = 1
    reparse_point: bool = False
    alternate_streams: bool = False


class ReadOnlyCollectionSource(Protocol):
    """Interface only; no production source adapter is implemented here.

    Implementations must independently verify identity, open without write
    access, and expose only named regular files from the fixed allowlist.
    """

    def identify(self) -> SourceIdentity: ...

    def open_allowlisted(self, name: str) -> BinaryIO: ...

    def list_allowlisted(self) -> Iterable[SourceFileEntry]: ...


class FakeDirectoryCollectionSource:
    """Temporary-directory adapter for tests; not a production source adapter."""

    def __init__(self, root: Path, identity: SourceIdentity):
        self.root = Path(root)
        self._identity = identity
        self._require_root()

    def _require_root(self) -> None:
        info = self.root.lstat()
        attrs = getattr(info, "st_file_attributes", 0)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise EvidenceError("Fake collection source root is not a plain directory.")

    def identify(self) -> SourceIdentity:
        self._require_root()
        return self._identity

    def list_allowlisted(self) -> Iterable[SourceFileEntry]:
        self._require_root()
        rows = []
        for path in self.root.iterdir():
            info = path.lstat()
            attrs = getattr(info, "st_file_attributes", 0)
            reparse = bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            rows.append(SourceFileEntry(
                path.name, info.st_size,
                "regular" if stat.S_ISREG(info.st_mode) else "other",
                info.st_nlink, reparse or stat.S_ISLNK(info.st_mode), False))
        return tuple(rows)

    def open_allowlisted(self, name: str) -> BinaryIO:
        self._require_root()
        path = self.root / name
        before = path.lstat()
        attrs = getattr(before, "st_file_attributes", 0)
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
                or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise EvidenceError("Fake source file is not a plain, unlinked file.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            os.close(descriptor)
            raise EvidenceError("Fake source changed while opening.")
        return os.fdopen(descriptor, "rb")


def verify_readonly_source_inventory(source: ReadOnlyCollectionSource, *,
                                     expected_identity: SourceIdentity,
                                     allowlisted_names: frozenset[str],
                                     maximum_file_bytes: int,
                                     maximum_total_bytes: int) -> tuple[SourceFileEntry, ...]:
    """Preflight a source adapter without opening or copying any file content.

    A concrete adapter must implement ``open_allowlisted`` as a read-only
    handle and must re-check identity after reading. This function verifies
    the adapter's declared identity and exact, bounded root inventory only.
    """
    if not isinstance(expected_identity, SourceIdentity):
        raise EvidenceError("Expected source identity has an invalid type.")
    observed = source.identify()
    _validate_source_identity(observed)
    _validate_source_identity(expected_identity)
    if observed != expected_identity or observed.access_mode != "read_only":
        raise EvidenceError("Collection source identity or read-only mode does not match.")
    if (not allowlisted_names or any(not isinstance(name, str) or not name
                                     or "/" in name or "\\" in name
                                     or name in {".", ".."} for name in allowlisted_names)):
        raise EvidenceError("Collection allowlist is malformed.")
    entries = tuple(source.list_allowlisted())
    if (any(not isinstance(row, SourceFileEntry)
            or not isinstance(row.name, str) or not row.name or "\x00" in row.name
            or "/" in row.name or "\\" in row.name
            or type(row.size) is not int or row.size < 0
            or row.kind != "regular" or row.link_count != 1
            or row.reparse_point or row.alternate_streams
            for row in entries)
            or len({row.name.casefold() for row in entries}) != len(entries)
            or {row.name for row in entries} != set(allowlisted_names)
            or any(row.name.casefold() not in {name.casefold() for name in allowlisted_names}
                   for row in entries)):
        raise EvidenceError("Collection source inventory is missing, extra, or aliased.")
    if (type(maximum_file_bytes) is not int or maximum_file_bytes < 1
            or type(maximum_total_bytes) is not int or maximum_total_bytes < 1
            or any(row.size > maximum_file_bytes for row in entries)
            or sum(row.size for row in entries) > maximum_total_bytes):
        raise EvidenceError("Collection source inventory exceeds its configured size bounds.")
    if source.identify() != expected_identity:
        raise EvidenceError("Collection source identity changed during inventory inspection.")
    return tuple(sorted(entries, key=lambda row: row.name.casefold()))


class EvidenceStore:
    """Run-scoped immutable evidence files linked from the legacy journal."""

    def __init__(self, ledger: DiagnosticRunLedger, run_id: str):
        if not RUN_ID_RE.fullmatch(run_id):
            raise EvidenceError("Run ID is malformed.")
        self.ledger = ledger
        self.run_id = run_id
        self.run_directory = ledger.run_directory(run_id)
        run = ledger.get_run(run_id)
        self.run_nonce_sha256 = hashlib.sha256(run["run_nonce"].encode("ascii")).hexdigest()
        self.root = self.run_directory / "evidence"
        self._ensure_directory(self.root)
        for name in ("approvals", "consumptions", "intents", "results"):
            self._ensure_directory(self.root / name)
        if (self.root / "collections").exists() or (self.root / "collections").is_symlink():
            self._ensure_directory(self.root / "collections")

    def archive_approval(self, raw: bytes, verified) -> EvidenceRef:
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != verified.envelope_sha256:
            raise EvidenceError("Approval bytes do not match the verified envelope digest.")
        if (verified.payload.get("project_id") != self.ledger.project_id
                or verified.payload.get("run_id") != self.run_id
                or verified.payload.get("run_nonce") is None
                or hashlib.sha256(str(verified.payload["run_nonce"]).encode("ascii")).hexdigest()
                != self.run_nonce_sha256):
            raise EvidenceError("Approval envelope is not bound to this evidence run.")
        approval_id = verified.payload.get("approval_id")
        if not isinstance(approval_id, str) or not RUN_ID_RE.fullmatch(approval_id):
            raise EvidenceError("Approval ID is malformed.")
        relative = f"evidence/approvals/{approval_id}.signed.json"
        path = self._path(relative)
        atomic_create_new_file(path, raw)
        return EvidenceRef("approval_envelope", approval_id, relative,
                           hashlib.sha256(raw).hexdigest())

    def write_intent(self, *, operation_id: str, expected_state: str,
                     next_state: str, action: str, target: dict,
                     approval_ref: EvidenceRef, preconditions: list[str],
                     expected_postconditions: list[str]) -> EvidenceRef:
        self._validate_operation_id(operation_id)
        if action not in ACTION_SET:
            raise EvidenceError("Action is not allowlisted.")
        if (not isinstance(expected_state, str) or not expected_state
                or not isinstance(next_state, str) or not next_state):
            raise EvidenceError("Expected lifecycle states are required.")
        if ACTION_STATE_TRANSITIONS.get(action) != (expected_state, next_state):
            raise EvidenceError("Intent action does not match its permitted lifecycle transition.")
        target_raw = canonical_json(target)
        target_hash = hashlib.sha256(target_raw).hexdigest()
        record = {
            "schema": INTENT_SCHEMA,
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "project_id": self.ledger.project_id,
            "run_id": self.run_id,
            "run_nonce_sha256": self.run_nonce_sha256,
            "operation_id": operation_id,
            "expected_state": expected_state,
            "next_state": next_state,
            "action": action,
            "target": target,
            "target_sha256": target_hash,
            "approval_ref": approval_ref.as_dict(),
            "preconditions": self._string_list(preconditions, "preconditions"),
            "expected_postconditions": self._string_list(
                expected_postconditions, "expected postconditions"),
            "idempotency": "never_replay",
            "recovery_class": "side_effect_ambiguous",
            **_recording_clock(),
        }
        return self._write_record("intents", operation_id, record)

    def write_consumption(self, grant: ConsumedApproval, *,
                          operation_id: str) -> EvidenceRef:
        if not is_consumed_approval(grant):
            raise EvidenceError("A durably consumed approval is required.")
        approval = grant.approval
        record = {
            "schema": CONSUMPTION_SCHEMA,
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "project_id": self.ledger.project_id,
            "run_id": self.run_id,
            "run_nonce_sha256": self.run_nonce_sha256,
            "operation_id": operation_id,
            "approval_id": approval.payload["approval_id"],
            "approval_sha256": approval.envelope_sha256,
            "key_id": approval.key_id,
            "action": grant.action,
            "consumed_at": grant.consumed_at,
            **_recording_clock(),
        }
        return self._write_record("consumptions", operation_id, record)

    def write_result(self, *, operation_id: str, intent_ref: EvidenceRef,
                     consumption_ref: EvidenceRef, status: str, attempted: bool,
                     attempted_at: str | None, occurred_at: str | None, time_observation: dict,
                     observation: dict, source_class: str) -> EvidenceRef:
        self._validate_operation_id(operation_id)
        if (status not in RESULT_STATUSES or type(attempted) is not bool
                or not isinstance(source_class, str) or source_class not in SOURCE_CLASSES):
            raise EvidenceError("Operation result status or attempted flag is invalid.")
        _validate_time_observation(occurred_at, time_observation)
        _validate_timestamp(attempted_at, "attempted_at")
        recording_clock = _recording_clock()
        record = {
            "schema": RESULT_SCHEMA,
            "schema_version": 1,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "project_id": self.ledger.project_id,
            "run_id": self.run_id,
            "run_nonce_sha256": self.run_nonce_sha256,
            "operation_id": operation_id,
            "intent_ref": intent_ref.as_dict(),
            "consumption_ref": consumption_ref.as_dict(),
            "status": status,
            "attempted": attempted,
            "attempted_at": attempted_at,
            "occurred_at": occurred_at,
            "time_observation": time_observation,
            "observation": observation,
            "source_class": source_class,
            **recording_clock,
        }
        return self._write_record("results", operation_id, record)

    def write_collection(self, *, operation_id: str, intent_ref: EvidenceRef,
                         consumption_ref: EvidenceRef, source_identity: SourceIdentity,
                         destination_id: str, allowlisted_files: dict,
                         observed_files: list[dict], status: str,
                         partial_state: str, error: str | None = None) -> EvidenceRef:
        """Persist fake collection provenance; never overwrites a prior attempt."""
        _validate_source_identity(source_identity)
        if status not in {"completed", "partial", "rejected"}:
            raise EvidenceError("Collection status is invalid.")
        if partial_state not in {"none", "partial", "complete"}:
            raise EvidenceError("Collection partial state is invalid.")
        record = {
            "schema": COLLECTION_SCHEMA, "schema_version": 1,
            "contract_id": CONTRACT_ID, "contract_version": CONTRACT_VERSION,
            "project_id": self.ledger.project_id, "run_id": self.run_id,
            "run_nonce_sha256": self.run_nonce_sha256, "operation_id": operation_id,
            "intent_ref": intent_ref.as_dict(), "consumption_ref": consumption_ref.as_dict(),
            "source_identity": {"source_id": source_identity.source_id,
                                "media_sha256": source_identity.media_sha256,
                                "capacity_bytes": source_identity.capacity_bytes,
                                "access_mode": source_identity.access_mode},
            "approved_artifacts": allowlisted_files,
            "allowlisted_filenames": sorted(allowlisted_files, key=str.casefold),
            "observed_files": observed_files,
            "destination_id": destination_id,
            "collection_occurred_at": _utc_now(),
            "collection_time_observation": _fake_time_observation(),
            "recorded_at": _utc_now(),
            "recording_time_observation": _recording_clock()["recording_time_observation"],
            "observation_source": "simulated", "status": status,
            "partial_state": partial_state, "error": error,
        }
        return self._write_record("collections", operation_id, record)

    def stage_fake_collection(self, *, operation_id: str, intent_ref: EvidenceRef,
                              consumption_ref: EvidenceRef,
                              source: FakeDirectoryCollectionSource,
                              target: dict, maximum_file_bytes: int = 1024 * 1024,
                              maximum_total_bytes: int = 2 * 1024 * 1024,
                              fault_hook: Callable[[str], None] | None = None) -> EvidenceRef:
        """Stage approved files from the explicit fake adapter into a new run directory."""
        if not isinstance(source, FakeDirectoryCollectionSource):
            raise EvidenceError("Milestone 2 collection accepts only its fake directory source.")
        try:
            identity_data = target["source_identity"]
            identity = SourceIdentity(**identity_data)
            artifacts = target["artifacts"]
            destination_id = target["destination_id"]
            if destination_id != f"{self.run_id}/collected":
                raise EvidenceError("Collection destination is not the fixed run-local directory.")
            if not isinstance(artifacts, dict) or not artifacts:
                raise EvidenceError("Approved collection artifacts are malformed.")
            names = frozenset(artifacts)
            inventory = verify_readonly_source_inventory(
                source, expected_identity=identity, allowlisted_names=names,
                maximum_file_bytes=maximum_file_bytes,
                maximum_total_bytes=maximum_total_bytes)
            by_name = {row.name: row for row in inventory}
            for name, approved in artifacts.items():
                if (not isinstance(name, str) or not isinstance(approved, dict)
                        or set(approved) != {"size", "sha256"}
                        or type(approved["size"]) is not int or approved["size"] < 0
                        or not isinstance(approved["sha256"], str)
                        or not HASH_RE.fullmatch(approved["sha256"])
                        or by_name[name].size != approved["size"]):
                    raise EvidenceError("Approved collection sizes or hashes are malformed/mismatched.")
            destination = self.run_directory / "collected"
            destination.mkdir(exist_ok=False)
        except Exception as exc:
            ref = self.write_collection(
                operation_id=operation_id, intent_ref=intent_ref,
                consumption_ref=consumption_ref,
                source_identity=source._identity, destination_id=f"{self.run_id}/collected",
                allowlisted_files=target.get("artifacts", {}), observed_files=[],
                status="rejected", partial_state="none", error=str(exc)[:512])
            raise EvidenceError(f"Fake collection rejected; evidence={ref.relative_path}: {exc}") from exc

        observed: list[dict] = []
        try:
            for index, name in enumerate(sorted(artifacts, key=str.casefold)):
                with source.open_allowlisted(name) as stream:
                    data = stream.read(maximum_file_bytes + 1)
                    if len(data) > maximum_file_bytes or stream.read(1):
                        raise EvidenceError("Fake source file grew beyond the configured bound.")
                digest = hashlib.sha256(data).hexdigest()
                if len(data) != artifacts[name]["size"] or digest != artifacts[name]["sha256"]:
                    raise EvidenceError(f"Fake source file hash or size mismatch: {name}.")
                atomic_create_new_file(destination / name, data)
                observed.append({"name": name, "size": len(data), "sha256": digest})
                _fault(fault_hook, f"after_collection_file_{index + 1}")
            if source.identify() != identity:
                raise EvidenceError("Fake source identity changed during collection.")
            actual_names = {p.name for p in destination.iterdir()}
            if actual_names != set(artifacts):
                raise EvidenceError("Staged collection inventory does not match the allowlist.")
            for row in observed:
                path = destination / row["name"]
                self._require_regular_file(path)
                if (path.stat().st_size != row["size"]
                        or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]):
                    raise EvidenceError("Staged destination verification failed.")
        except Exception as exc:
            self.write_collection(
                operation_id=operation_id, intent_ref=intent_ref,
                consumption_ref=consumption_ref, source_identity=identity,
                destination_id=destination_id, allowlisted_files=artifacts,
                observed_files=observed, status="partial" if observed or destination.exists() else "rejected",
                partial_state="partial" if destination.exists() else "none",
                error=str(exc)[:512])
            raise OperationPendingError(f"Fake collection incomplete; preserve partial evidence: {exc}") from exc

        return self.write_collection(
            operation_id=operation_id, intent_ref=intent_ref,
            consumption_ref=consumption_ref, source_identity=identity,
            destination_id=destination_id, allowlisted_files=artifacts,
            observed_files=observed, status="completed", partial_state="complete")

    def read_ref(self, ref: dict) -> dict:
        if (not isinstance(ref, dict) or set(ref) != {
                "record_type", "record_id", "relative_path", "sha256"}):
            raise EvidenceError("Evidence reference fields are invalid.")
        if not HASH_RE.fullmatch(ref["sha256"]):
            raise EvidenceError("Evidence reference digest is malformed.")
        path = self._path(ref["relative_path"])
        self._require_regular_file(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise EvidenceError("Evidence record hash does not match its reference.")
        try:
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise EvidenceError("Evidence record is not valid UTF-8 JSON.") from exc
        if canonical_json(record) != raw:
            raise EvidenceError("Evidence record is not canonically encoded.")
        _validate_record_shape(record)
        if (record.get("contract_id") != CONTRACT_ID
                or record.get("contract_version") != CONTRACT_VERSION
                or record.get("project_id") != self.ledger.project_id
                or record.get("run_id") != self.run_id
                or record.get("run_nonce_sha256") != self.run_nonce_sha256
                or record.get("operation_id") != ref["record_id"]):
            raise EvidenceError("Evidence record identity does not match the run/reference.")
        return record

    def assert_no_unresolved_operations(self) -> None:
        """Fail closed on pending writes, orphan intents, or uncommitted results."""
        self._check_tree(self.root)
        intents = self._load_records("intents", INTENT_SCHEMA)
        results = self._load_records("results", RESULT_SCHEMA)
        consumptions = self._load_records("consumptions", CONSUMPTION_SCHEMA)
        collections = self._load_records("collections", COLLECTION_SCHEMA)
        archived_approvals = self._scan_approvals()
        if len({row["operation_id"] for row in intents}) != len(intents):
            raise OperationPendingError("Duplicate operation intent IDs exist.")
        if len({row["operation_id"] for row in results}) != len(results):
            raise OperationPendingError("Duplicate operation result IDs exist.")
        by_result = {row["operation_id"]: row for row in results}
        by_consumption = {row["operation_id"]: row for row in consumptions}
        by_collection = {row["operation_id"]: row for row in collections}
        if len(by_consumption) != len(consumptions):
            raise OperationPendingError("Duplicate approval-consumption records exist.")
        intent_ids = {row["operation_id"] for row in intents}
        if set(by_result) - intent_ids or set(by_consumption) - intent_ids:
            raise OperationPendingError("Orphan result or consumption evidence exists.")
        if set(by_collection) - intent_ids:
            raise OperationPendingError("Orphan collection evidence exists.")
        referenced_approvals = {
            intent["approval_ref"]["record_id"]: intent["approval_ref"]["sha256"]
            for intent in intents
        }
        if (len(referenced_approvals) != len(intents)
                or referenced_approvals != archived_approvals):
            raise OperationPendingError("Approval archive contains an orphan or missing envelope.")
        events = self.ledger.get_run(self.run_id)["events"]
        event_ops = {event.get("evidence", {}).get("operation_id"): event
                     for event in events if event.get("evidence", {}).get("operation_id")}
        for intent in intents:
            operation_id = intent["operation_id"]
            result = by_result.get(operation_id)
            if result is None:
                raise OperationPendingError(
                    f"Operation {operation_id} has an intent but no result; do not retry.")
            consumption = by_consumption.get(operation_id)
            if consumption is None:
                raise OperationPendingError(
                    f"Operation {operation_id} has no durable consumption evidence.")
            intent_hash = hashlib.sha256(canonical_json(intent)).hexdigest()
            consumption_hash = hashlib.sha256(canonical_json(consumption)).hexdigest()
            if (result.get("intent_ref", {}).get("sha256") != intent_hash
                    or result.get("consumption_ref", {}).get("sha256") != consumption_hash
                    or consumption.get("approval_id") != intent.get("approval_ref", {}).get("record_id")
                    or consumption.get("approval_sha256") != intent.get("approval_ref", {}).get("sha256")):
                raise EvidenceError("Intent, consumption, and result bindings disagree.")
            if result["status"] != "completed":
                raise OperationPendingError(
                    f"Operation {operation_id} is {result['status']}; operator recovery is required.")
            event = event_ops.get(operation_id)
            if event is None:
                raise OperationPendingError(
                    f"Operation {operation_id} completed without a linked lifecycle event.")
            refs = event["evidence"].get("evidence_refs", [])
            sequence = event["sequence"]
            expected_refs = [
                {"record_type": "operation_intent", "record_id": operation_id,
                 "relative_path": f"evidence/intents/{operation_id}.json", "sha256": intent_hash},
                {"record_type": "approval_consumption", "record_id": operation_id,
                 "relative_path": f"evidence/consumptions/{operation_id}.json",
                 "sha256": consumption_hash},
                {"record_type": "operation_result", "record_id": operation_id,
                 "relative_path": f"evidence/results/{operation_id}.json",
                 "sha256": hashlib.sha256(canonical_json(result)).hexdigest()},
            ]
            collection = by_collection.get(operation_id)
            if intent["action"] == "collect_output":
                if collection is None or collection["status"] != "completed" or collection["partial_state"] != "complete":
                    raise OperationPendingError("Collection operation lacks verified completion evidence.")
                expected_intent_ref = {"record_type": "operation_intent", "record_id": operation_id,
                                       "relative_path": f"evidence/intents/{operation_id}.json",
                                       "sha256": intent_hash}
                expected_consumption_ref = {"record_type": "approval_consumption", "record_id": operation_id,
                                            "relative_path": f"evidence/consumptions/{operation_id}.json",
                                            "sha256": consumption_hash}
                if (collection["source_identity"] != intent["target"].get("source_identity")
                        or collection["approved_artifacts"] != intent["target"].get("artifacts")
                        or collection["destination_id"] != f"{self.run_id}/collected"
                        or collection["intent_ref"] != expected_intent_ref
                        or collection["consumption_ref"] != expected_consumption_ref):
                    raise EvidenceError("Collection evidence is not bound to its approved operation.")
                expected_files = collection["approved_artifacts"]
                destination = self.run_directory / "collected"
                self._require_directory(destination)
                entries = list(destination.iterdir())
                if {p.name for p in entries} != set(expected_files):
                    raise OperationPendingError("Collected destination inventory differs from approval.")
                for path in entries:
                    self._require_regular_file(path)
                    expected = expected_files[path.name]
                    if (path.stat().st_size != expected["size"]
                            or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]):
                        raise EvidenceError("Collected destination no longer matches its evidence hash.")
                expected_refs.append({
                    "record_type": "collection_record", "record_id": operation_id,
                    "relative_path": f"evidence/collections/{operation_id}.json",
                    "sha256": hashlib.sha256(canonical_json(collection)).hexdigest(),
                })
            elif collection is not None:
                raise EvidenceError("Non-collection operation has collection evidence.")
            if (refs != expected_refs or event["state"] != intent["next_state"]
                    or sequence < 1
                    or events[sequence - 1]["state"] != intent["expected_state"]
                    or event["evidence"].get("action") != intent["action"]
                    or event["evidence"].get("approval_id") != consumption["approval_id"]
                    or event["evidence"].get("occurred_at") != result["occurred_at"]):
                raise OperationPendingError(
                    f"Operation {operation_id} lifecycle event does not bind all evidence records.")

    def _load_records(self, directory: str, schema: str) -> list[dict]:
        base = self.root / directory
        if directory == "collections" and not (base.exists() or base.is_symlink()):
            return []
        self._require_directory(base)
        rows = []
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            if path.name.endswith(".pending") or ".pending-" in path.name:
                raise OperationPendingError("An interrupted evidence publication is present.")
            self._require_regular_file(path)
            raw = path.read_bytes()
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
            if canonical_json(record) != raw or record.get("schema") != schema:
                raise EvidenceError("Evidence inventory contains a malformed or wrong-schema record.")
            _validate_record_shape(record)
            if path.name != record.get("operation_id", "") + ".json":
                raise EvidenceError("Evidence filename does not match its operation identifier.")
            if (record.get("project_id") != self.ledger.project_id
                    or record.get("run_id") != self.run_id
                    or record.get("run_nonce_sha256") != self.run_nonce_sha256):
                raise EvidenceError("Evidence inventory contains a cross-run record.")
            rows.append(record)
        return rows

    def _scan_approvals(self) -> dict[str, str]:
        base = self.root / "approvals"
        self._require_directory(base)
        archived = {}
        for path in sorted(base.iterdir(), key=lambda item: item.name):
            if ".pending-" in path.name:
                raise OperationPendingError("An interrupted approval archive write is present.")
            self._require_regular_file(path)
            approval_id = path.name.removesuffix(".signed.json")
            self._validate_operation_id(approval_id)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            archived[approval_id] = digest
        return archived

    def _write_record(self, directory: str, record_id: str, record: dict) -> EvidenceRef:
        self._validate_operation_id(record_id)
        if directory == "collections":
            self._ensure_directory(self.root / directory)
        relative = f"evidence/{directory}/{record_id}.json"
        raw = canonical_json(record)
        atomic_create_new_file(self._path(relative), raw)
        record_types = {
            "intents": "operation_intent",
            "consumptions": "approval_consumption",
            "results": "operation_result",
            "collections": "collection_record",
        }
        return EvidenceRef(record_types[directory], record_id, relative,
                           hashlib.sha256(raw).hexdigest())

    def _path(self, relative: str) -> Path:
        if (not isinstance(relative, str) or relative.startswith(("/", "\\"))
                or "\\" in relative or any(part in {"", ".", ".."}
                                             for part in relative.split("/"))):
            raise EvidenceError("Evidence relative path is unsafe.")
        path = self.run_directory.joinpath(*relative.split("/"))
        try:
            path.relative_to(self.run_directory)
        except ValueError as exc:
            raise EvidenceError("Evidence path escapes the run directory.") from exc
        for parent in (self.run_directory, *path.parents):
            if parent == path:
                continue
            if parent.exists() or parent.is_symlink():
                self._require_directory(parent)
        return path

    def _ensure_directory(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            self._require_directory(path)
            return
        path.mkdir()
        self._require_directory(path)

    @staticmethod
    def _require_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise EvidenceError(f"Evidence directory cannot be inspected: {path.name}.") from exc
        attrs = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or attrs & reparse_flag):
            raise EvidenceError(f"Evidence path is not a plain directory: {path.name}.")

    @staticmethod
    def _require_regular_file(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise EvidenceError(f"Evidence file is unavailable: {path.name}.") from exc
        attrs = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or attrs & reparse_flag or info.st_nlink != 1):
            raise EvidenceError(f"Evidence file is not a regular unlinked file: {path.name}.")

    @staticmethod
    def _validate_operation_id(value: str) -> None:
        if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
            raise EvidenceError("Operation identifier must be 32 lowercase hexadecimal characters.")

    @staticmethod
    def _string_list(value: list[str], label: str) -> list[str]:
        if (not isinstance(value, list) or len(value) > 64
                or any(not isinstance(item, str) or not item or len(item) > 1024
                       for item in value)):
            raise EvidenceError(f"{label} must be a bounded list of nonempty strings.")
        return list(value)

    def _check_tree(self, root: Path) -> None:
        self._require_directory(root)
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            self._require_directory(current_path)
            for name in directories:
                self._require_directory(current_path / name)
            for name in files:
                if ".pending-" in name:
                    raise OperationPendingError("An interrupted evidence publication is present.")
                self._require_regular_file(current_path / name)


class FakeEvidenceBackend:
    """Deterministic in-memory backend; it performs no host or guest effects."""

    def __init__(self, *, fail: str | None = None):
        if fail not in {None, "before_effect", "after_effect"}:
            raise ValueError("Unknown fake failure mode.")
        self.fail = fail
        self.calls: list[dict] = []
        self._operation_ids: set[str] = set()

    def perform(self, *, action: str, target: dict, operation_id: str,
                grant: ConsumedApproval) -> ActionObservation:
        if not is_consumed_approval(grant) or grant.action != action:
            raise TrustContractError("Fake backend requires a consumed approval for this action.")
        target_hash = hashlib.sha256(canonical_json(target)).hexdigest()
        if grant.approval.payload["media_hashes"].get("action_target") != target_hash:
            raise TrustContractError("Consumed approval does not bind the exact action target.")
        if operation_id in self._operation_ids:
            raise EvidenceError("Fake backend refuses operation replay.")
        self._operation_ids.add(operation_id)
        attempted_at = _utc_now()
        if self.fail == "before_effect":
            raise BackendActionFailure(
                "failed", attempted=True, occurred_at=None,
                time_observation=_unknown_time_observation("fake action failed before effect occurrence"),
                observation={"effect": "not_performed", "backend": "fake",
                             "failure_observed_at": attempted_at},
                message="Injected fake failure before the effect.")
        self.calls.append({"action": action, "target": target, "operation_id": operation_id})
        if self.fail == "after_effect":
            raise BackendActionFailure(
                "outcome_unknown", attempted=True, occurred_at=attempted_at,
                time_observation=_fake_time_observation(),
                observation={"effect": "simulated_but_result_unknown", "backend": "fake"},
                message="Injected interruption after simulated effect.")
        return ActionObservation(
            occurred_at=attempted_at,
            time_observation=_fake_time_observation(),
            observation={"effect": "completed", "backend": "fake", "action": action},
        )


class AuditedFakeCoordinator:
    """Exercise approval → intent → fake action → result → ledger transition."""

    def __init__(self, ledger: DiagnosticRunLedger, run_id: str,
                 evidence: EvidenceStore, trust: TrustPolicy,
                 consumption_store: ApprovalConsumptionStore,
                 backend: FakeEvidenceBackend):
        self.ledger = ledger
        self.run_id = run_id
        if evidence.ledger is not ledger or evidence.run_id != run_id:
            raise EvidenceError("Evidence store is not bound to this coordinator run.")
        self.evidence = evidence
        self.trust = trust
        self.consumption_store = consumption_store
        self.backend = backend

    def execute_transition(self, *, expected_state: str, next_state: str,
                           action: str, target: dict, raw_approval: bytes,
                           approval_expected: Mapping[str, object],
                           collection_source: FakeDirectoryCollectionSource | None = None,
                           preconditions: list[str] | None = None,
                           expected_postconditions: list[str] | None = None,
                           operation_id: str | None = None,
                           fault_hook: Callable[[str], None] | None = None) -> dict:
        self.evidence.assert_no_unresolved_operations()
        run = self.ledger.get_run(self.run_id)
        if run["events"][-1]["state"] != expected_state:
            raise DiagnosticRunError("Run is not in the expected state for this action.")
        if ACTION_STATE_TRANSITIONS.get(action) != (expected_state, next_state):
            raise DiagnosticRunError("Action is not permitted to produce this lifecycle transition.")
        if (action == "collect_output") != (collection_source is not None):
            raise EvidenceError("collect_output requires exactly one fake source; other actions forbid it.")
        binding = dict(approval_expected)
        required = {
            "project_id": self.ledger.project_id,
            "run_id": self.run_id,
            "run_nonce": run["run_nonce"],
            "permitted_action": action,
        }
        for field, value in required.items():
            if field in binding and binding[field] != value:
                raise TrustContractError(f"Approval expectation conflicts with run {field}.")
            binding[field] = value
        verified = verify_approval(raw_approval, self.trust, expected=binding,
                                   required_action=action)
        target_hash = hashlib.sha256(canonical_json(target)).hexdigest()
        if verified.payload["media_hashes"].get("action_target") != target_hash:
            raise TrustContractError("Approval does not bind the exact action target.")
        approval_ref = self.evidence.archive_approval(raw_approval, verified)
        _fault(fault_hook, "after_approval_archive")
        op_id = operation_id or uuid.uuid4().hex
        intent_ref = self.evidence.write_intent(
            operation_id=op_id, expected_state=expected_state, next_state=next_state,
            action=action, target=target, approval_ref=approval_ref,
            preconditions=preconditions or [],
            expected_postconditions=expected_postconditions or [])
        _fault(fault_hook, "after_intent")

        grant = consume_approval(verified, self.consumption_store, required_action=action)
        consumption_ref = self.evidence.write_consumption(grant, operation_id=op_id)
        _fault(fault_hook, "after_consumption")
        attempted_at = _utc_now()
        collection_ref = None
        try:
            result = self.backend.perform(action=action, target=target,
                                          operation_id=op_id, grant=grant)
            if action == "collect_output":
                if collection_source is None:
                    raise EvidenceError("Fake collection source is required for collect_output.")
                collection_ref = self.evidence.stage_fake_collection(
                    operation_id=op_id, intent_ref=intent_ref,
                    consumption_ref=consumption_ref, source=collection_source,
                    target=target, fault_hook=fault_hook)
            elif collection_source is not None:
                raise EvidenceError("Collection source supplied for a non-collection action.")
            status, attempted = "completed", True
            occurred_at = result.occurred_at
            time_observation = result.time_observation
            observation = result.observation
            source_class = result.source_class
        except BackendActionFailure as exc:
            status, attempted = exc.status, exc.attempted
            occurred_at = exc.occurred_at
            time_observation = exc.time_observation
            observation = {"message": str(exc), **exc.observation}
            source_class = exc.source_class
        except Exception as exc:
            status, attempted = "outcome_unknown", True
            occurred_at = None
            time_observation = _unknown_time_observation("backend raised without a bound result")
            observation = {"exception_type": type(exc).__name__, "message": str(exc)[:1024]}
            source_class = "unknown"
            result = None
        result_ref = self.evidence.write_result(
            operation_id=op_id, intent_ref=intent_ref,
            consumption_ref=consumption_ref, status=status, attempted=attempted,
            attempted_at=attempted_at, occurred_at=occurred_at,
            time_observation=time_observation,
            observation=observation, source_class=source_class)
        _fault(fault_hook, "after_result")
        if status != "completed":
            raise OperationPendingError(
                f"Action result is {status}; no lifecycle transition was appended.")
        if (time_observation.get("time_status") != "observed"
                or source_class != "simulated"):
            raise OperationPendingError(
                "Fake lifecycle transition requires a simulated observation with an occurrence time.")

        evidence = {
            "operation_id": op_id,
            "action": action,
            "approval_id": verified.payload["approval_id"],
            "occurred_at": occurred_at,
            "time_observation": time_observation,
            "recording_time_observation": _recording_clock()["recording_time_observation"],
            "evidence_refs": [intent_ref.as_dict(), consumption_ref.as_dict(),
                              result_ref.as_dict()],
        }
        if collection_ref is not None:
            evidence["evidence_refs"].append(collection_ref.as_dict())
        transitioned = self.ledger.transition(
            self.run_id, expected_state=expected_state, new_state=next_state,
            evidence=evidence)
        _fault(fault_hook, "after_transition")
        return transitioned


def target_sha256(target: dict) -> str:
    """Return the canonical digest to place in signed approval media_hashes."""
    return hashlib.sha256(canonical_json(target)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _recording_clock() -> dict:
    return {
        "recorded_at": _utc_now(),
        "recording_time_observation": {
            "time_status": "observed",
            "clock_source": "host-python-utc-wall-clock",
            "precision": "1us-format; underlying resolution unknown",
            "uncertainty_ms": None,
            "uncertainty_reason": "Host clock accuracy is not independently calibrated by this module.",
            "monotonic_ticks": time.monotonic_ns(),
            "boot_session_id": _PROCESS_SESSION_ID,
        },
    }


def _fake_time_observation() -> dict:
    return {
        "time_status": "observed",
        "clock_source": "fake-backend-simulated-clock",
        "precision": "simulated-microsecond",
        "uncertainty_ms": 0,
        "monotonic_ticks": time.monotonic_ns(),
        "boot_session_id": "fake-test-session",
    }


def _unknown_time_observation(reason: str) -> dict:
    return {
        "time_status": "unavailable",
        "clock_source": "unavailable",
        "precision": "unknown",
        "uncertainty_ms": None,
        "monotonic_ticks": None,
        "boot_session_id": None,
        "reason": reason,
    }


def _validate_time_observation(occurred_at: str | None, observation: dict) -> None:
    if not isinstance(observation, dict):
        raise EvidenceError("Time observation must be an object.")
    required = {"time_status", "clock_source", "precision", "uncertainty_ms",
                "monotonic_ticks", "boot_session_id"}
    if not required.issubset(observation) or set(observation) - (required | {"reason"}):
        raise EvidenceError("Time observation fields are incomplete or unexpected.")
    status = observation.get("time_status")
    if status not in {"observed", "estimated", "unavailable"}:
        raise EvidenceError("Time status is not recognized.")
    if status == "unavailable":
        if occurred_at is not None or not isinstance(observation.get("reason"), str):
            raise EvidenceError("Unavailable time must have null occurrence and a reason.")
    else:
        if not isinstance(occurred_at, str):
            raise EvidenceError("Observed/estimated time requires an occurrence timestamp.")
        try:
            parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError("Occurrence timestamp is malformed.") from exc
        if parsed.tzinfo is None:
            raise EvidenceError("Occurrence timestamp must include a timezone.")
        if (not isinstance(observation.get("clock_source"), str)
                or not observation["clock_source"]
                or not isinstance(observation.get("precision"), str)
                or not observation["precision"]):
            raise EvidenceError("Clock source and precision are required.")
        uncertainty = observation.get("uncertainty_ms")
        if uncertainty is not None and (type(uncertainty) not in {int, float} or uncertainty < 0):
            raise EvidenceError("Clock uncertainty must be nonnegative or null.")
    canonical_json(observation)


def _validate_timestamp(value: str | None, label: str) -> None:
    if not isinstance(value, str):
        raise EvidenceError(f"{label} timestamp is unavailable.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} timestamp is malformed.") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label} timestamp must include a timezone.")


def _validate_record_shape(record: object) -> None:
    if not isinstance(record, dict):
        raise EvidenceError("Evidence record must be an object.")
    common = {"schema", "schema_version", "contract_id", "contract_version",
              "project_id", "run_id", "run_nonce_sha256", "operation_id"}
    fields = {
        INTENT_SCHEMA: common | {"expected_state", "next_state", "action", "target",
                                 "target_sha256", "approval_ref", "preconditions",
                                 "expected_postconditions", "idempotency", "recovery_class",
                                 "recorded_at", "recording_time_observation"},
        CONSUMPTION_SCHEMA: common | {"approval_id", "approval_sha256", "key_id", "action",
                                      "consumed_at", "recorded_at",
                                      "recording_time_observation"},
        RESULT_SCHEMA: common | {"intent_ref", "consumption_ref", "status", "source_class", "attempted",
                                 "attempted_at", "occurred_at", "time_observation", "observation",
                                 "recorded_at", "recording_time_observation"},
        COLLECTION_SCHEMA: common | {"intent_ref", "consumption_ref", "source_identity",
                                     "approved_artifacts", "allowlisted_filenames", "observed_files",
                                     "destination_id", "collection_occurred_at",
                                     "collection_time_observation", "recorded_at",
                                     "recording_time_observation", "observation_source", "status",
                                     "partial_state", "error"},
    }
    schema = record.get("schema")
    if schema not in fields or set(record) != fields[schema]:
        raise EvidenceError("Evidence record has an unsupported schema or field set.")
    if (type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["contract_id"] != CONTRACT_ID
            or type(record["contract_version"]) is not int
            or record["contract_version"] != CONTRACT_VERSION
            or not isinstance(record["project_id"], str)
            or not isinstance(record["run_id"], str)
            or not RUN_ID_RE.fullmatch(record["run_id"])
            or not isinstance(record["run_nonce_sha256"], str)
            or not HASH_RE.fullmatch(record["run_nonce_sha256"])
            or not isinstance(record["operation_id"], str)
            or not RUN_ID_RE.fullmatch(record["operation_id"])):
        raise EvidenceError("Evidence record identity or version is invalid.")
    _validate_timestamp(record["recorded_at"], "recorded_at")
    _validate_recording_clock(record["recording_time_observation"])
    if schema == INTENT_SCHEMA:
        if (not isinstance(record["action"], str) or record["action"] not in ACTION_SET
                or not isinstance(record["expected_state"], str)
                or not isinstance(record["next_state"], str)
                or ACTION_STATE_TRANSITIONS.get(record["action"])
                != (record["expected_state"], record["next_state"])
                or hashlib.sha256(canonical_json(record["target"])).hexdigest()
                != record["target_sha256"]
                or record["idempotency"] != "never_replay"
                or record["recovery_class"] != "side_effect_ambiguous"):
            raise EvidenceError("Intent action, target, or replay policy is invalid.")
        approval_ref = record["approval_ref"]
        if (not isinstance(approval_ref, dict)
                or set(approval_ref) != {"record_type", "record_id", "relative_path", "sha256"}
                or approval_ref["record_type"] != "approval_envelope"
                or not isinstance(approval_ref["record_id"], str)
                or not RUN_ID_RE.fullmatch(approval_ref["record_id"])
                or not isinstance(approval_ref["sha256"], str)
                or not HASH_RE.fullmatch(approval_ref["sha256"])
                or approval_ref["relative_path"] !=
                f"evidence/approvals/{approval_ref['record_id']}.signed.json"):
            raise EvidenceError("Intent approval reference is malformed.")
    elif schema == CONSUMPTION_SCHEMA:
        if (not isinstance(record["approval_id"], str)
                or not RUN_ID_RE.fullmatch(record["approval_id"])
                or not isinstance(record["approval_sha256"], str)
                or not HASH_RE.fullmatch(record["approval_sha256"])
                or not isinstance(record["action"], str)
                or record["action"] not in ACTION_SET):
            raise EvidenceError("Approval consumption evidence is malformed.")
        _validate_timestamp(record["consumed_at"], "consumed_at")
    elif schema == RESULT_SCHEMA:
        if (not isinstance(record["status"], str)
                or record["status"] not in RESULT_STATUSES
                or not isinstance(record["source_class"], str)
                or record["source_class"] not in SOURCE_CLASSES
                or type(record["attempted"]) is not bool
                or not isinstance(record["observation"], dict)):
            raise EvidenceError("Operation result fields are invalid.")
        _validate_timestamp(record["attempted_at"], "attempted_at")
        _validate_time_observation(record["occurred_at"], record["time_observation"])
        for name, expected_type in (("intent_ref", "operation_intent"),
                                    ("consumption_ref", "approval_consumption")):
            reference = record[name]
            if (not isinstance(reference, dict)
                    or set(reference) != {"record_type", "record_id", "relative_path", "sha256"}
                    or reference["record_type"] != expected_type
                    or reference["record_id"] != record["operation_id"]
                    or reference["relative_path"] != f"evidence/{'intents' if name == 'intent_ref' else 'consumptions'}/{record['operation_id']}.json"
                    or not isinstance(reference["sha256"], str)
                    or not HASH_RE.fullmatch(reference["sha256"])):
                raise EvidenceError("Operation result evidence reference is malformed.")
    else:
        identity = record["source_identity"]
        if (not isinstance(identity, dict)
                or set(identity) != {"source_id", "media_sha256", "capacity_bytes", "access_mode"}
                or not isinstance(identity["source_id"], str) or not identity["source_id"]
                or not isinstance(identity["media_sha256"], str) or not HASH_RE.fullmatch(identity["media_sha256"])
                or type(identity["capacity_bytes"]) is not int or identity["capacity_bytes"] < 1
                or identity["access_mode"] != "read_only"
                or record["observation_source"] != "simulated"
                or record["status"] not in {"completed", "partial", "rejected"}
                or record["partial_state"] not in {"none", "partial", "complete"}
                or not isinstance(record["approved_artifacts"], dict)
                or not isinstance(record["allowlisted_filenames"], list)
                or record["allowlisted_filenames"] != sorted(record["approved_artifacts"], key=str.casefold)
                or not isinstance(record["observed_files"], list)
                or record["destination_id"] != f"{record['run_id']}/collected"
                or (record["status"] == "completed") != (record["partial_state"] == "complete")
                or (record["error"] is not None and not isinstance(record["error"], str))):
            raise EvidenceError("Collection evidence record is malformed.")
        _validate_time_observation(record["collection_occurred_at"], record["collection_time_observation"])
        for name, item in record["approved_artifacts"].items():
            if (not isinstance(name, str) or not name or "/" in name or "\\" in name
                    or not isinstance(item, dict) or set(item) != {"size", "sha256"}
                    or type(item["size"]) is not int or item["size"] < 0
                    or not isinstance(item["sha256"], str) or not HASH_RE.fullmatch(item["sha256"])):
                raise EvidenceError("Collection approved artifact map is malformed.")
        for item in record["observed_files"]:
            if (not isinstance(item, dict) or set(item) != {"name", "size", "sha256"}
                    or item["name"] not in record["approved_artifacts"]
                    or type(item["size"]) is not int or item["size"] < 0
                    or not isinstance(item["sha256"], str) or not HASH_RE.fullmatch(item["sha256"])):
                raise EvidenceError("Collection observed file entry is malformed.")


def _validate_recording_clock(observation: object) -> None:
    if (not isinstance(observation, dict)
            or set(observation) != {"time_status", "clock_source", "precision", "uncertainty_ms",
                                   "uncertainty_reason", "monotonic_ticks", "boot_session_id"}
            or observation.get("time_status") != "observed"
            or not isinstance(observation.get("clock_source"), str)
            or not observation["clock_source"]
            or not isinstance(observation.get("precision"), str)
            or not observation["precision"]):
        raise EvidenceError("Recording-clock metadata is malformed.")
    uncertainty = observation.get("uncertainty_ms")
    if uncertainty is not None and (type(uncertainty) not in {int, float} or uncertainty < 0):
        raise EvidenceError("Recording-clock uncertainty is malformed.")


def _validate_source_identity(identity: SourceIdentity) -> None:
    if (not isinstance(identity, SourceIdentity)
            or not isinstance(identity.source_id, str) or not identity.source_id
            or not isinstance(identity.media_sha256, str)
            or not HASH_RE.fullmatch(identity.media_sha256)
            or type(identity.capacity_bytes) is not int or identity.capacity_bytes < 1
            or identity.access_mode not in {"read_only", "read_write", "unknown"}):
        raise EvidenceError("Collection source identity fields are malformed.")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _fault(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)
