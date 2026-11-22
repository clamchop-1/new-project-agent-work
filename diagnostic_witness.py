"""Nonproduction reference contracts for witnessed audit and replay state.

The in-memory witness is a test provider. It demonstrates signatures, sequence
continuity, idempotency, and reconciliation contracts; it is not an independent
or durable trust anchor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
from typing import Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from diagnostic_trust import ApprovalReplayError, TrustContractError, canonical_json


CHECKPOINT_DOMAIN = b"LocalCodingAgent/audit-checkpoint/v1\0"


class WitnessError(ValueError):
    """A witness checkpoint is invalid, conflicting, or cannot be reconciled."""


class WitnessReplayError(WitnessError):
    """A one-time witness event was already appended."""


class CheckpointSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class SignedCheckpoint:
    witness_id: str
    sequence: int
    event_type: str
    event: Mapping[str, object]
    previous_hash: str | None
    key_id: str
    checkpoint_hash: str
    signature: bytes

    def unsigned_record(self) -> dict[str, object]:
        return {
            "witness_id": self.witness_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "event": dict(self.event),
            "previous_hash": self.previous_hash,
            "key_id": self.key_id,
        }


class AuditWitness(Protocol):
    """Append-only audit contract backed by an independently protected service."""

    def append(self, event_type: str, event: Mapping[str, object], *,
               idempotency_key: str | None = None) -> SignedCheckpoint: ...

    def history(self) -> tuple[SignedCheckpoint, ...]: ...

    def latest_head(self) -> tuple[int, str] | None: ...


def verify_checkpoint_chain(
    checkpoints: tuple[SignedCheckpoint, ...] | list[SignedCheckpoint],
    *, witness_id: str, trusted_keys: Mapping[str, bytes],
    expected_head: tuple[int, str] | None = None,
) -> tuple[int, str] | None:
    """Verify signatures and the complete sequence from genesis to expected head."""
    previous_hash = None
    for expected_sequence, checkpoint in enumerate(checkpoints, start=1):
        if checkpoint.witness_id != witness_id or checkpoint.sequence != expected_sequence:
            raise WitnessError("Checkpoint history has a missing, reordered, or foreign entry.")
        if checkpoint.previous_hash != previous_hash:
            raise WitnessError("Checkpoint history has a broken predecessor link.")
        public_key = trusted_keys.get(checkpoint.key_id)
        if public_key is None or len(public_key) != 32:
            raise WitnessError("Checkpoint signing key is not trusted.")
        unsigned = checkpoint.unsigned_record()
        expected_hash = hashlib.sha256(canonical_json(unsigned)).hexdigest()
        if checkpoint.checkpoint_hash != expected_hash:
            raise WitnessError("Checkpoint content hash does not match its record.")
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                checkpoint.signature,
                CHECKPOINT_DOMAIN + bytes.fromhex(expected_hash),
            )
        except (InvalidSignature, ValueError) as exc:
            raise WitnessError("Checkpoint signature verification failed.") from exc
        previous_hash = expected_hash

    actual_head = (len(checkpoints), previous_hash) if checkpoints else None
    if expected_head != actual_head:
        raise WitnessError("Checkpoint head is stale, rolled back, or conflicts with the anchor.")
    return actual_head


class FakeAuditWitness:
    """Deterministic in-process witness for contract tests only."""

    def __init__(self, witness_id: str, signer: CheckpointSigner):
        if not witness_id or not signer.key_id:
            raise WitnessError("Witness and key identities are required.")
        self.witness_id = witness_id
        self._signer = signer
        self._checkpoints: list[SignedCheckpoint] = []
        self._idempotency_keys: set[str] = set()
        self._protected_head: tuple[int, str] | None = None
        self._lock = threading.RLock()

    @property
    def public_key_id(self) -> str:
        return self._signer.key_id

    def append(self, event_type: str, event: Mapping[str, object], *,
               idempotency_key: str | None = None) -> SignedCheckpoint:
        if not isinstance(event_type, str) or not event_type:
            raise WitnessError("Event type is required.")
        try:
            event_bytes = canonical_json(dict(event))
        except (TypeError, TrustContractError) as exc:
            raise WitnessError("Witness event must be canonical JSON data.") from exc
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency_keys:
                raise WitnessReplayError("Witness idempotency key was already used.")
            previous_hash = (self._checkpoints[-1].checkpoint_hash
                             if self._checkpoints else None)
            record = {
                "witness_id": self.witness_id,
                "sequence": len(self._checkpoints) + 1,
                "event_type": event_type,
                "event": json.loads(event_bytes),
                "previous_hash": previous_hash,
                "key_id": self._signer.key_id,
            }
            checkpoint_hash = hashlib.sha256(canonical_json(record)).hexdigest()
            signature = self._signer.sign(
                CHECKPOINT_DOMAIN + bytes.fromhex(checkpoint_hash))
            if not isinstance(signature, bytes) or len(signature) != 64:
                raise WitnessError("Witness signer returned a malformed signature.")
            checkpoint = SignedCheckpoint(
                witness_id=self.witness_id,
                sequence=record["sequence"],
                event_type=event_type,
                event=record["event"],
                previous_hash=previous_hash,
                key_id=self._signer.key_id,
                checkpoint_hash=checkpoint_hash,
                signature=signature,
            )
            self._checkpoints.append(checkpoint)
            self._protected_head = (checkpoint.sequence, checkpoint.checkpoint_hash)
            if idempotency_key is not None:
                self._idempotency_keys.add(idempotency_key)
            return checkpoint

    def history(self) -> tuple[SignedCheckpoint, ...]:
        with self._lock:
            return tuple(self._checkpoints)

    def latest_head(self) -> tuple[int, str] | None:
        """Return fake protected head metadata separately from an exported history."""
        with self._lock:
            return self._protected_head


class ReplayLedger(Protocol):
    def consume_once(self, *, approval_id: str, envelope_sha256: str,
                     key_id: str, action: str, consumed_at: str) -> SignedCheckpoint: ...

    def reconcile(self) -> int: ...


class WitnessedSqliteReplayLedger:
    """Reference replay store: local durability plus witness-first consumption.

    SQLite provides local transactional uniqueness and process-level concurrency.
    The independently trusted witness provides rollback detection/reconciliation.
    This class is only as trustworthy as the injected witness and its key custody.
    """

    def __init__(self, database_path: Path, witness: AuditWitness, *,
                 witness_id: str, witness_public_keys: Mapping[str, bytes],
                 after_witness_append=None):
        self.database_path = Path(database_path).absolute()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.witness = witness
        self.witness_id = witness_id
        self.witness_public_keys = dict(witness_public_keys)
        self.after_witness_append = after_witness_append
        self._lock = threading.RLock()
        with closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS consumed_approvals (
                    approval_id TEXT PRIMARY KEY,
                    envelope_sha256 TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    witness_sequence INTEGER NOT NULL,
                    checkpoint_hash TEXT NOT NULL
                )
            """)
        self.reconcile()

    def consume_once(self, *, approval_id: str, envelope_sha256: str,
                     key_id: str, action: str, consumed_at: str) -> SignedCheckpoint:
        self._validate_record(approval_id, envelope_sha256, key_id, action, consumed_at)
        with self._lock:
            self.reconcile()
            try:
                checkpoint = self.witness.append(
                    "approval_consumed",
                    {"approval_id": approval_id, "envelope_sha256": envelope_sha256,
                     "key_id": key_id, "action": action, "consumed_at": consumed_at},
                    idempotency_key="approval:" + approval_id,
                )
            except WitnessReplayError as exc:
                raise ApprovalReplayError("Approval ID was already consumed.") from exc
            if self.after_witness_append is not None:
                self.after_witness_append()
            self._insert_consumed(checkpoint)
            return checkpoint

    def reconcile(self) -> int:
        with self._lock:
            history = self.witness.history()
            anchor_head = self.witness.latest_head()
            actual_head = verify_checkpoint_chain(
                history, witness_id=self.witness_id,
                trusted_keys=self.witness_public_keys,
                expected_head=anchor_head,
            )
            observed: dict[str, SignedCheckpoint] = {}
            for checkpoint in history:
                if checkpoint.event_type != "approval_consumed":
                    continue
                approval_id = checkpoint.event.get("approval_id")
                if not isinstance(approval_id, str) or approval_id in observed:
                    raise WitnessError("Witness contains malformed or duplicate consumption.")
                observed[approval_id] = checkpoint
            with closing(self._connect()) as connection:
                local = {row[0]: row[1] for row in connection.execute(
                    "SELECT approval_id, checkpoint_hash FROM consumed_approvals")}
                for approval_id, checkpoint_hash in local.items():
                    checkpoint = observed.get(approval_id)
                    if checkpoint is None or checkpoint.checkpoint_hash != checkpoint_hash:
                        raise WitnessError("Local replay store conflicts with witnessed history.")
                recovered = 0
                for checkpoint in observed.values():
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO consumed_approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (checkpoint.event["approval_id"], checkpoint.event["envelope_sha256"],
                         checkpoint.event["key_id"], checkpoint.event["action"],
                         checkpoint.event["consumed_at"], checkpoint.sequence,
                         checkpoint.checkpoint_hash),
                    )
                    recovered += cursor.rowcount
                connection.commit()
                return recovered

    def is_consumed(self, approval_id: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM consumed_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone() is not None

    def _insert_consumed(self, checkpoint: SignedCheckpoint) -> None:
        event = checkpoint.event
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO consumed_approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event["approval_id"], event["envelope_sha256"], event["key_id"],
                     event["action"], event["consumed_at"], checkpoint.sequence,
                     checkpoint.checkpoint_hash),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ApprovalReplayError("Approval ID was already consumed.") from exc
        except sqlite3.DatabaseError as exc:
            raise TrustContractError("Local replay state could not be committed.") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _validate_record(approval_id: str, envelope_sha256: str,
                         key_id: str, action: str, consumed_at: str) -> None:
        if (not isinstance(approval_id, str) or len(approval_id) != 32
                or any(char not in "0123456789abcdef" for char in approval_id)):
            raise TrustContractError("Approval ID is malformed.")
        if (not isinstance(envelope_sha256, str) or len(envelope_sha256) != 64
                or any(char not in "0123456789abcdef" for char in envelope_sha256)):
            raise TrustContractError("Approval digest is malformed.")
        if not key_id or not action or not consumed_at:
            raise TrustContractError("Replay event fields are incomplete.")
