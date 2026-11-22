"""Versioned, signed trust contracts for offline diagnostic runs.

This module contains schemas and verification/consumption boundaries only. It
does not create, persist, or activate a production signing key. Signing is
provided through an injected provider owned by a future authorization service.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


APPROVAL_SCHEMA = "localcodingagent.diagnostic.approval"
RECEIPT_SCHEMA = "localcodingagent.diagnostic.validation-receipt"
SCHEMA_VERSION = 1
APPROVAL_DOMAIN = b"LocalCodingAgent/diagnostic-approval/v1\0"
RECEIPT_DOMAIN = b"LocalCodingAgent/diagnostic-receipt/v1\0"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ARCHITECTURES = frozenset({"x86_64", "arm64"})
APPROVED_ACTIONS = frozenset({
    "prepare_media", "attach_media", "start_worker", "invoke_operation",
    "stop_worker", "detach_media", "collect_output", "cleanup",
})
ALLOWED_OPERATION_IDS = frozenset({
    "diagnostic-noop-v1", "inventory-report-v1", "offline-tests-v1",
    "sentinel-hash-v1",
})
RESOURCE_FIELDS = frozenset({
    "cpu_count", "memory_bytes", "disk_bytes", "timeout_seconds",
    "process_count", "output_bytes",
})


class TrustContractError(ValueError):
    """A signed record or trust contract is malformed or unauthenticated."""


class ApprovalReplayError(TrustContractError):
    """An approval ID has already been consumed."""


class ApprovalScopeError(TrustContractError):
    """An approval does not cover the requested target or action."""


class SignatureProvider(Protocol):
    """Injected signing service; production key custody belongs elsewhere."""

    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class TrustPolicy:
    """Public trust anchors supplied by protected Broker configuration."""

    public_keys: Mapping[str, bytes]
    revoked_key_ids: frozenset[str] = frozenset()
    revoked_approval_ids: frozenset[str] = frozenset()
    approval_key_ids: frozenset[str] = frozenset()
    receipt_key_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for key_id, raw_key in self.public_keys.items():
            if not isinstance(key_id, str) or not IDENTIFIER.fullmatch(key_id):
                raise TrustContractError("Trust key ID is malformed.")
            if not isinstance(raw_key, bytes) or len(raw_key) != 32:
                raise TrustContractError("Ed25519 public keys must contain exactly 32 bytes.")
        if not self.revoked_key_ids.issubset(self.public_keys.keys()):
            raise TrustContractError("Revocation references an unknown key ID.")
        if (not self.approval_key_ids.issubset(self.public_keys.keys())
                or not self.receipt_key_ids.issubset(self.public_keys.keys())):
            raise TrustContractError("Signing role references an unknown key ID.")
        if self.approval_key_ids.intersection(self.receipt_key_ids):
            raise TrustContractError("Approval and receipt roles must use separate keys.")
        object.__setattr__(self, "public_keys", MappingProxyType(dict(self.public_keys)))
        object.__setattr__(self, "revoked_key_ids", frozenset(self.revoked_key_ids))
        object.__setattr__(self, "revoked_approval_ids", frozenset(self.revoked_approval_ids))
        object.__setattr__(self, "approval_key_ids", frozenset(self.approval_key_ids))
        object.__setattr__(self, "receipt_key_ids", frozenset(self.receipt_key_ids))
        for approval_id in self.revoked_approval_ids:
            _require_hex32(approval_id, "Revoked approval ID")


@dataclass(frozen=True)
class VerifiedApproval:
    """A verified approval record, not a permission until consumed once."""

    payload: Mapping[str, object]
    key_id: str
    envelope_sha256: str
    _verification_marker: object


@dataclass(frozen=True)
class VerifiedReceipt:
    """A cryptographically verified receipt, still untrusted as executable data."""

    payload: Mapping[str, object]
    key_id: str
    envelope_sha256: str
    _verification_marker: object


@dataclass(frozen=True)
class ConsumedApproval:
    """Evidence that a verified approval ID was durably consumed once."""

    approval: VerifiedApproval
    action: str
    consumed_at: str
    _consumption_marker: object


class ApprovalIssuanceService:
    """Issue approved records using an injected, externally controlled signer."""

    def __init__(self, signer: SignatureProvider):
        self._signer = signer

    def issue_approval(self, payload: Mapping[str, object]) -> bytes:
        normalized = _validate_approval_payload(dict(payload))
        return _sign_record(APPROVAL_SCHEMA, APPROVAL_DOMAIN, normalized, self._signer)


class ValidationReceiptIssuer:
    """Sign host validation evidence with a separate receipt-role signer."""

    def __init__(self, signer: SignatureProvider):
        self._signer = signer

    def issue_for_approval(self, validation_result: Mapping[str, object],
                           approval: VerifiedApproval, *, validated_at: str,
                           receipt_id: str | None = None) -> bytes:
        if (not isinstance(approval, VerifiedApproval)
                or approval._verification_marker is not _VERIFICATION_MARKER):
            raise TrustContractError("Receipt signing requires a verified approval.")
        outcome = validation_result.get("outcome")
        if (not isinstance(outcome, str)
                or outcome not in {"validated", "rejected", "incomplete"}):
            raise TrustContractError("Host validation result outcome is invalid.")
        failures = validation_result.get("failures")
        if (not isinstance(failures, list)
                or (outcome == "validated" and failures)
                or (outcome in {"rejected", "incomplete"} and not failures)):
            raise TrustContractError("Host validation outcome and failure evidence disagree.")
        artifacts_field = "verified_artifacts" if outcome == "validated" else "observed_artifacts"
        artifacts = validation_result.get(artifacts_field)
        payload = {
            "receipt_id": receipt_id or uuid.uuid4().hex,
            "approval_id": approval.payload["approval_id"],
            "project_id": approval.payload["project_id"],
            "project_instance_id": approval.payload["project_instance_id"],
            "run_id": approval.payload["run_id"],
            "run_nonce_sha256": hashlib.sha256(
                str(approval.payload["run_nonce"]).encode("ascii")).hexdigest(),
            "source_sha256": approval.payload["source_sha256"],
            "backend_id": approval.payload["backend_id"],
            "backend_version": approval.payload["backend_version"],
            "vm_identity": approval.payload["vm_identity"],
            "architecture": approval.payload["architecture"],
            "operation_id": approval.payload["operation_id"],
            "launcher_sha256": approval.payload["launcher_sha256"],
            "media_hashes": dict(approval.payload["media_hashes"]),
            "resource_limits": dict(approval.payload["resource_limits"]),
            "policy_version": validation_result.get("policy_version"),
            "outcome": outcome,
            "validated_at": validated_at,
            "artifacts": artifacts,
            "failures": failures,
        }
        return _sign_record(RECEIPT_SCHEMA, RECEIPT_DOMAIN,
                            _validate_receipt_payload(payload), self._signer)


def verify_approval(raw: bytes, trust: TrustPolicy, *, now: datetime | None = None,
                    expected: Mapping[str, object] | None = None,
                    required_action: str | None = None) -> VerifiedApproval:
    """Verify signature, schema, time, revocation, and optional exact scope."""
    envelope = _parse_signed_record(raw, APPROVAL_SCHEMA)
    key_id = envelope["key_id"]
    payload = _validate_approval_payload(envelope["payload"])
    if key_id in trust.revoked_key_ids or key_id not in trust.approval_key_ids:
        raise TrustContractError("Approval signing key has been revoked.")
    if payload["approval_id"] in trust.revoked_approval_ids:
        raise TrustContractError("Approval has been revoked.")
    public_bytes = trust.public_keys.get(key_id)
    if public_bytes is None:
        raise TrustContractError("Approval signing key is not trusted.")
    message = _signed_bytes(APPROVAL_SCHEMA, key_id, payload)
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            _decode_signature(envelope["signature"]), APPROVAL_DOMAIN + message)
    except (InvalidSignature, ValueError) as exc:
        raise TrustContractError("Approval signature verification failed.") from exc

    current = _utc(now or datetime.now(timezone.utc))
    issued = _parse_timestamp(payload["issued_at"])
    expires = _parse_timestamp(payload["expires_at"])
    if issued > current or expires <= current or expires <= issued:
        raise TrustContractError("Approval is not currently valid.")
    if expires - issued > timedelta(hours=24):
        raise TrustContractError("Approval validity exceeds the 24-hour policy cap.")
    if required_action is not None and payload["permitted_action"] != required_action:
        raise ApprovalScopeError("Approval does not permit the requested action.")
    if expected is not None:
        for field, expected_value in expected.items():
            if field not in payload or payload[field] != expected_value:
                raise ApprovalScopeError(f"Approval scope mismatch for {field}.")
    return VerifiedApproval(_freeze_json(payload), key_id,
                            hashlib.sha256(raw).hexdigest(), _VERIFICATION_MARKER)


def verify_validation_receipt(raw: bytes, trust: TrustPolicy) -> VerifiedReceipt:
    """Verify a host-signed validation receipt; this does not promote its data."""
    envelope = _parse_signed_record(raw, RECEIPT_SCHEMA)
    key_id = envelope["key_id"]
    payload = _validate_receipt_payload(envelope["payload"])
    if (key_id in trust.revoked_key_ids or key_id not in trust.public_keys
            or key_id not in trust.receipt_key_ids):
        raise TrustContractError("Receipt signing key is revoked or untrusted.")
    if payload["approval_id"] in trust.revoked_approval_ids:
        raise TrustContractError("Receipt references a revoked approval.")
    try:
        Ed25519PublicKey.from_public_bytes(trust.public_keys[key_id]).verify(
            _decode_signature(envelope["signature"]),
            RECEIPT_DOMAIN + _signed_bytes(RECEIPT_SCHEMA, key_id, payload))
    except (InvalidSignature, ValueError) as exc:
        raise TrustContractError("Validation receipt signature verification failed.") from exc
    return VerifiedReceipt(_freeze_json(payload), key_id,
                           hashlib.sha256(raw).hexdigest(), _VERIFICATION_MARKER)


def validate_receipt_scope(receipt: VerifiedReceipt, approval: VerifiedApproval) -> None:
    """Require a signed receipt to describe the exact approved run context."""
    if (not isinstance(receipt, VerifiedReceipt)
            or receipt._verification_marker is not _VERIFICATION_MARKER
            or not isinstance(approval, VerifiedApproval)
            or approval._verification_marker is not _VERIFICATION_MARKER):
        raise TrustContractError("Receipt scope needs verified receipt and approval records.")
    receipt_payload = receipt.payload
    bindings = (
        "approval_id", "project_id", "project_instance_id", "run_id", "source_sha256",
        "backend_id", "backend_version", "vm_identity", "architecture", "operation_id",
        "launcher_sha256", "media_hashes", "resource_limits",
    )
    for field in bindings:
        if receipt_payload.get(field) != approval.payload[field]:
            raise ApprovalScopeError(f"Validation receipt does not match approved {field}.")
    nonce_hash = hashlib.sha256(str(approval.payload["run_nonce"]).encode("ascii")).hexdigest()
    if receipt_payload.get("run_nonce_sha256") != nonce_hash:
        raise ApprovalScopeError("Validation receipt run nonce does not match approval.")


class ApprovalConsumptionStore(Protocol):
    def consume_once(self, *, approval_id: str, envelope_sha256: str,
                     key_id: str, action: str, consumed_at: str) -> None: ...


class ProtectedAuditAnchor(Protocol):
    """Independent append-only trust anchor, outside project/run writable state.

    Implementations atomically compare the prior anchor and append one
    checkpoint. This interface deliberately has no local file implementation.
    """

    def append_checkpoint(self, *, sequence: int, journal_sha256: str,
                          previous_anchor_sha256: str | None) -> str: ...

    def latest_checkpoint(self) -> tuple[int, str, str | None] | None: ...


class SqliteApprovalConsumptionStore:
    """Durable local replay guard; production must protect this DB independently."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).absolute()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS consumed_approvals (
                    approval_id TEXT PRIMARY KEY,
                    envelope_sha256 TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                )
            """)

    def consume_once(self, *, approval_id: str, envelope_sha256: str,
                     key_id: str, action: str, consumed_at: str) -> None:
        _require_hex32(approval_id, "Approval ID")
        if not SHA256.fullmatch(envelope_sha256):
            raise TrustContractError("Approval digest is malformed.")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO consumed_approvals VALUES (?, ?, ?, ?, ?)",
                    (approval_id, envelope_sha256, key_id, action, consumed_at),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApprovalReplayError("Approval ID was already consumed.") from exc
        except sqlite3.DatabaseError as exc:
            raise TrustContractError("Approval consumption could not be durably recorded.") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def consume_approval(approval: VerifiedApproval, store: ApprovalConsumptionStore, *,
                     required_action: str, now: datetime | None = None) -> ConsumedApproval:
    """Consume a previously verified approval exactly once for one action."""
    if (not isinstance(approval, VerifiedApproval)
            or approval._verification_marker is not _VERIFICATION_MARKER):
        raise TrustContractError("Approval must be cryptographically verified first.")
    if approval.payload["permitted_action"] != required_action:
        raise ApprovalScopeError("Verified approval is scoped to another action.")
    current = _utc(now or datetime.now(timezone.utc))
    if _parse_timestamp(approval.payload["expires_at"]) <= current:
        raise TrustContractError("Approval expired before consumption.")
    consumed_at = _format_timestamp(current)
    store.consume_once(approval_id=str(approval.payload["approval_id"]),
                       envelope_sha256=approval.envelope_sha256,
                       key_id=approval.key_id,
                       action=required_action,
                       consumed_at=consumed_at)
    return ConsumedApproval(approval, required_action, consumed_at, _CONSUMPTION_MARKER)


def is_consumed_approval(value: object) -> bool:
    """Check the local consumed-grant marker, not a replacement for Broker policy."""
    return (isinstance(value, ConsumedApproval)
            and value._consumption_marker is _CONSUMPTION_MARKER
            and isinstance(value.approval, VerifiedApproval)
            and value.approval._verification_marker is _VERIFICATION_MARKER)


def canonical_json(value: object) -> bytes:
    """Return the contract's deterministic UTF-8 JSON representation."""
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise TrustContractError("Record is not canonical JSON data.") from exc


def _sign_record(schema: str, domain: bytes, payload: dict,
                 signer: SignatureProvider) -> bytes:
    key_id = signer.key_id
    if not isinstance(key_id, str) or not IDENTIFIER.fullmatch(key_id):
        raise TrustContractError("Signer key ID is malformed.")
    message = domain + _signed_bytes(schema, key_id, payload)
    signature = signer.sign(message)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise TrustContractError("Ed25519 signer returned a malformed signature.")
    envelope = {"schema": schema, "schema_version": SCHEMA_VERSION, "key_id": key_id,
                "payload": payload,
                "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")}
    return canonical_json(envelope)


def _signed_bytes(schema: str, key_id: str, payload: dict) -> bytes:
    return canonical_json({"schema": schema, "schema_version": SCHEMA_VERSION,
                           "key_id": key_id, "payload": payload})


def _parse_signed_record(raw: bytes, expected_schema: str) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > 128 * 1024:
        raise TrustContractError("Signed record is empty or exceeds its size limit.")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TrustContractError("Signed record is not strict JSON.") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise TrustContractError("Signed record is not a canonical JSON object.")
    if set(value) != {"schema", "schema_version", "key_id", "payload", "signature"}:
        raise TrustContractError("Signed record envelope fields are invalid.")
    if (value["schema"] != expected_schema or type(value["schema_version"]) is not int
            or value["schema_version"] != SCHEMA_VERSION
            or not isinstance(value["key_id"], str) or not IDENTIFIER.fullmatch(value["key_id"])
            or not isinstance(value["payload"], dict)
            or not isinstance(value["signature"], str)):
        raise TrustContractError("Signed record envelope identity is invalid.")
    return value


def _validate_approval_payload(payload: dict) -> dict:
    fields = {
        "approval_id", "project_id", "project_instance_id", "run_id", "run_nonce",
        "source_sha256", "backend_id", "backend_version", "vm_identity", "architecture",
        "operation_id", "launcher_sha256", "media_hashes", "resource_limits",
        "permitted_action", "issued_at", "expires_at",
    }
    if set(payload) != fields:
        raise TrustContractError("Approval payload fields are incomplete or unexpected.")
    for name in ("approval_id", "project_instance_id", "run_id", "run_nonce"):
        _require_hex32(payload[name], name)
    for name in ("project_id", "backend_id", "backend_version", "vm_identity", "operation_id"):
        if not isinstance(payload[name], str) or not IDENTIFIER.fullmatch(payload[name]):
            raise TrustContractError(f"Approval field {name} is malformed.")
    for name in ("source_sha256", "launcher_sha256"):
        if not isinstance(payload[name], str) or not SHA256.fullmatch(payload[name]):
            raise TrustContractError(f"Approval field {name} is malformed.")
    if not isinstance(payload["architecture"], str) or payload["architecture"] not in ARCHITECTURES:
        raise TrustContractError("Approval architecture is unsupported.")
    if payload["operation_id"] not in ALLOWED_OPERATION_IDS:
        raise TrustContractError("Approval operation is not in the fixed launcher allowlist.")
    action = payload["permitted_action"]
    if not isinstance(action, str) or action not in APPROVED_ACTIONS:
        raise TrustContractError("Approval action is not allowlisted.")
    media = payload["media_hashes"]
    if (not isinstance(media, dict) or not media or len(media) > 8
            or any(not isinstance(name, str) or not IDENTIFIER.fullmatch(name) for name in media)
            or any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in media.values())):
        raise TrustContractError("Approval media hash inventory is invalid.")
    limits = payload["resource_limits"]
    if not isinstance(limits, dict) or set(limits) != RESOURCE_FIELDS:
        raise TrustContractError("Approval resource limit fields are invalid.")
    absolute_caps = {"cpu_count": 256, "memory_bytes": 2**50, "disk_bytes": 2**50,
                     "timeout_seconds": 7 * 24 * 3600, "process_count": 65536,
                     "output_bytes": 2**40}
    for name, maximum in absolute_caps.items():
        value = limits[name]
        if type(value) is not int or value < 1 or value > maximum:
            raise TrustContractError(f"Approval resource limit {name} is invalid.")
    _validate_timestamp(payload["issued_at"], "issued_at")
    _validate_timestamp(payload["expires_at"], "expires_at")
    return json.loads(canonical_json(payload).decode("utf-8"))


def _validate_receipt_payload(payload: dict) -> dict:
    fields = {
        "receipt_id", "approval_id", "project_id", "project_instance_id", "run_id",
        "run_nonce_sha256", "source_sha256", "backend_id", "backend_version", "vm_identity",
        "architecture", "operation_id", "launcher_sha256", "media_hashes", "resource_limits",
        "policy_version", "outcome", "validated_at", "artifacts", "failures",
    }
    if set(payload) != fields:
        raise TrustContractError("Validation receipt fields are incomplete or unexpected.")
    for name in ("receipt_id", "approval_id", "project_instance_id", "run_id"):
        _require_hex32(payload[name], name)
    for name in ("run_nonce_sha256", "source_sha256", "launcher_sha256"):
        if not isinstance(payload[name], str) or not SHA256.fullmatch(payload[name]):
            raise TrustContractError(f"Receipt field {name} is malformed.")
    for name in ("project_id", "backend_id", "backend_version", "vm_identity", "operation_id"):
        if not isinstance(payload[name], str) or not IDENTIFIER.fullmatch(payload[name]):
            raise TrustContractError(f"Receipt field {name} is malformed.")
    if not isinstance(payload["architecture"], str) or payload["architecture"] not in ARCHITECTURES:
        raise TrustContractError("Receipt architecture is unsupported.")
    if payload["operation_id"] not in ALLOWED_OPERATION_IDS:
        raise TrustContractError("Receipt operation is not in the fixed launcher allowlist.")
    if type(payload["policy_version"]) is not int or payload["policy_version"] < 1:
        raise TrustContractError("Receipt policy version is invalid.")
    if (not isinstance(payload["outcome"], str)
            or payload["outcome"] not in {"validated", "rejected", "incomplete"}):
        raise TrustContractError("Receipt outcome is not terminal or recognized.")
    _validate_timestamp(payload["validated_at"], "validated_at")
    _validate_hash_map(payload["media_hashes"], "Receipt media inventory")
    _validate_limits(payload["resource_limits"])
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 1024:
        raise TrustContractError("Receipt artifact inventory is invalid.")
    seen_paths: set[str] = set()
    for artifact in artifacts:
        if (not isinstance(artifact, dict) or set(artifact) != {"path", "size", "sha256"}
                or not isinstance(artifact["path"], str) or not artifact["path"]
                or "\\" in artifact["path"] or artifact["path"].startswith("/")
                or any(part in {"", ".", ".."} for part in artifact["path"].split("/"))
                or artifact["path"] in seen_paths
                or type(artifact["size"]) is not int or artifact["size"] < 0
                or not isinstance(artifact["sha256"], str) or not SHA256.fullmatch(artifact["sha256"])):
            raise TrustContractError("Receipt artifact entry is malformed or duplicated.")
        seen_paths.add(artifact["path"])
    failures = payload["failures"]
    if (not isinstance(failures, list) or len(failures) > 64
            or any(not isinstance(item, str) or not item or len(item) > 1024 for item in failures)):
        raise TrustContractError("Receipt failure inventory is invalid.")
    return json.loads(canonical_json(payload).decode("utf-8"))


def _validate_hash_map(value: object, label: str) -> None:
    if (not isinstance(value, dict) or not value or len(value) > 8
            or any(not isinstance(name, str) or not IDENTIFIER.fullmatch(name) for name in value)
            or any(not isinstance(digest, str) or not SHA256.fullmatch(digest)
                   for digest in value.values())):
        raise TrustContractError(f"{label} is invalid.")


def _validate_limits(value: object) -> None:
    if not isinstance(value, dict) or set(value) != RESOURCE_FIELDS:
        raise TrustContractError("Resource limit fields are invalid.")
    caps = {"cpu_count": 256, "memory_bytes": 2**50, "disk_bytes": 2**50,
            "timeout_seconds": 7 * 24 * 3600, "process_count": 65536, "output_bytes": 2**40}
    for field, maximum in caps.items():
        if type(value[field]) is not int or value[field] < 1 or value[field] > maximum:
            raise TrustContractError(f"Resource limit {field} is invalid.")


def _validate_timestamp(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TrustContractError(f"Timestamp {name} must be canonical UTC RFC 3339.")
    if _format_timestamp(_parse_timestamp(value)) != value:
        raise TrustContractError(f"Timestamp {name} is not canonical.")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise TrustContractError("Timestamp is malformed.") from exc
    return parsed


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TrustContractError("Current time must be timezone-aware.")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _format_timestamp(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_signature(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]{86}", value):
        raise TrustContractError("Signature encoding is malformed.")
    try:
        decoded = base64.urlsafe_b64decode(value + "==")
    except (ValueError, base64.binascii.Error) as exc:
        raise TrustContractError("Signature encoding is malformed.") from exc
    if len(decoded) != 64:
        raise TrustContractError("Signature length is invalid.")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise TrustContractError("Signature encoding is not canonical.")
    return decoded


def _require_hex32(value: object, label: str) -> None:
    if not isinstance(value, str) or not HEX32.fullmatch(value):
        raise TrustContractError(f"{label} must be 32 lowercase hexadecimal characters.")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


_VERIFICATION_MARKER = object()
_CONSUMPTION_MARKER = object()


def _freeze_json(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value
