"""Durable, nonproduction audit-witness service protocol and SQLite provider.

This module exposes a byte-oriented request/response boundary but opens no
network listener. SQLite is durable local service storage, not privileged-
rollback protection. An optional independent monotonic anchor can detect a
restored or truncated database when the anchor remains available.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from diagnostic_trust import canonical_json
from diagnostic_trust_bundle import TrustBundleError, verify_trust_bundle
from diagnostic_witness import (
    CHECKPOINT_DOMAIN,
    SignedCheckpoint,
    WitnessError,
    verify_checkpoint_chain,
)


REQUEST_SCHEMA = "localcodingagent.audit-witness.request"
ACK_SCHEMA = "localcodingagent.audit-witness.ack"
PROTOCOL_VERSION = 1
REQUEST_DOMAIN = b"LocalCodingAgent/audit-witness-request/v1\0"
ACK_DOMAIN = b"LocalCodingAgent/audit-witness-ack/v1\0"
HEX32 = frozenset("0123456789abcdef")
SHA256_HEX = frozenset("0123456789abcdef")
EVENT_APPROVAL_CONSUMED = "approval_consumed"
EVENT_TRUST_BUNDLE_UPDATED = "trust_bundle_updated"
CLIENT_ROLES = frozenset({"broker", "trust_admin"})


class WitnessProtocolError(ValueError):
    """Malformed or unauthenticated protocol request; no state was changed."""


class WitnessUnavailable(RuntimeError):
    """The durable store or configured independent anchor cannot be confirmed."""


class WitnessConflict(RuntimeError):
    """The authenticated request conflicts with current witness state."""


@dataclass(frozen=True)
class ClientIdentity:
    public_key: bytes
    roles: frozenset[str]

    def __post_init__(self):
        if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
            raise WitnessProtocolError("Client Ed25519 public key must be 32 bytes.")
        if not self.roles or not self.roles.issubset(CLIENT_ROLES):
            raise WitnessProtocolError("Client role assignment is invalid.")


class RequestSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class AcknowledgmentSigner(RequestSigner, Protocol):
    pass


@dataclass(frozen=True)
class AnchorHead:
    sequence: int
    checkpoint_hash: str
    previous_hash: str | None


class ExternalHeadAnchor(Protocol):
    def latest(self, witness_id: str) -> AnchorHead | None: ...

    def advance(self, witness_id: str, checkpoint: AnchorHead) -> None: ...


class FakeExternalHeadAnchor:
    """Test-only separate monotonic head; not a production audit service."""

    def __init__(self):
        self._heads: dict[str, AnchorHead] = {}
        self._lock = threading.Lock()

    def latest(self, witness_id: str) -> AnchorHead | None:
        with self._lock:
            return self._heads.get(witness_id)

    def advance(self, witness_id: str, checkpoint: AnchorHead) -> None:
        with self._lock:
            prior = self._heads.get(witness_id)
            if prior == checkpoint:
                return
            expected_sequence = 1 if prior is None else prior.sequence + 1
            expected_previous = None if prior is None else prior.checkpoint_hash
            if (checkpoint.sequence != expected_sequence
                    or checkpoint.previous_hash != expected_previous):
                raise WitnessConflict("External head rejected stale or conflicting checkpoint.")
            self._heads[witness_id] = checkpoint


def signed_request(payload: Mapping[str, object], signer: RequestSigner, *,
                   witness_id: str) -> bytes:
    """Create a canonical signed request (normally called by a client adapter)."""
    normalized = _validate_request_payload(dict(payload))
    unsigned = {"schema": REQUEST_SCHEMA, "protocol_version": PROTOCOL_VERSION,
                "witness_id": witness_id, "client_key_id": signer.key_id,
                "payload": normalized}
    signature = signer.sign(REQUEST_DOMAIN + canonical_json(unsigned))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise WitnessProtocolError("Client signer returned a malformed signature.")
    return canonical_json({**unsigned, "signature": _b64(signature)})


def verify_acknowledgment(raw: bytes, *, witness_id: str,
                          trusted_keys: Mapping[str, bytes]) -> dict[str, object]:
    """Verify the witness signature and canonical encoding of an acknowledgment."""
    value = _parse_canonical_object(raw, max_bytes=32 * 1024)
    if set(value) != {"schema", "protocol_version", "witness_id", "request_id",
                      "request_sha256", "disposition", "sequence", "checkpoint_hash",
                      "error_code", "key_id", "signature"}:
        raise WitnessProtocolError("Acknowledgment fields are invalid.")
    if value["schema"] != ACK_SCHEMA or value["protocol_version"] != PROTOCOL_VERSION:
        raise WitnessProtocolError("Acknowledgment protocol version is unsupported.")
    if value["witness_id"] != witness_id:
        raise WitnessProtocolError("Acknowledgment is for a different witness.")
    key_id = value["key_id"]
    public_key = trusted_keys.get(key_id)
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise WitnessProtocolError("Acknowledgment signer is not trusted.")
    signature_text = value.pop("signature")
    try:
        signature = _unb64(signature_text)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, ACK_DOMAIN + canonical_json(value))
    except (ValueError, InvalidSignature) as exc:
        raise WitnessProtocolError("Acknowledgment signature is invalid.") from exc
    return value


class SqliteAuditWitnessService:
    """Portable authenticated service implementation backed by SQLite WAL.

    All callers use `handle(bytes) -> bytes`; no socket or privileged provider is
    created. Checkpoints, one-time approval IDs, trust-bundle head, request
    idempotency records and successful acknowledgments commit in one transaction.
    """

    def __init__(self, database_path: Path, *, witness_id: str,
                 client_identities: Mapping[str, ClientIdentity],
                 checkpoint_signer: AcknowledgmentSigner,
                 checkpoint_public_key: bytes,
                 trust_root_key_id: str, trust_root_public_key: bytes,
                 external_anchor: ExternalHeadAnchor | None = None,
                 before_commit: Callable[[], None] | None = None,
                 after_commit: Callable[[], None] | None = None,
                 now=None):
        if not witness_id or len(witness_id) > 128:
            raise WitnessProtocolError("Witness identity is malformed.")
        if len(trust_root_public_key) != 32:
            raise WitnessProtocolError("Pinned trust root must be 32 bytes.")
        if len(checkpoint_public_key) != 32:
            raise WitnessProtocolError("Checkpoint public key must be 32 bytes.")
        self.database_path = Path(database_path).absolute()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.witness_id = witness_id
        self.client_identities = dict(client_identities)
        self.checkpoint_signer = checkpoint_signer
        self.checkpoint_public_key = checkpoint_public_key
        self.trust_root_key_id = trust_root_key_id
        self.trust_root_public_key = trust_root_public_key
        self.external_anchor = external_anchor
        self.before_commit = before_commit
        self.after_commit = after_commit
        self.now = now
        self._lock = threading.RLock()
        self._initialize()
        self.verify_storage()
        self.reconcile_external_anchor()

    @property
    def public_key(self) -> bytes:
        return self.checkpoint_public_key

    def handle(self, raw_request: bytes) -> bytes:
        """Authenticate, validate and atomically append or return a signed error."""
        request = self._parse_request(raw_request)
        payload = request["payload"]
        client_key_id = request["client_key_id"]
        identity = self.client_identities.get(client_key_id)
        if identity is None or not self._verify_client_signature(request, identity):
            return self._ack(payload, request, "rejected", None, "UNAUTHENTICATED")
        required_role = ("broker" if payload["event_type"] == EVENT_APPROVAL_CONSUMED
                         else "trust_admin" if payload["event_type"] == EVENT_TRUST_BUNDLE_UPDATED
                         else "auditor")
        if required_role not in identity.roles:
            return self._ack(payload, request, "rejected", None, "ROLE_FORBIDDEN")

        with self._lock:
            try:
                return self._handle_authenticated(request)
            except WitnessUnavailable:
                raise

    def history(self) -> tuple[SignedCheckpoint, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT record_json, signature FROM checkpoints ORDER BY sequence").fetchall()
        return tuple(_checkpoint_from_storage(record, signature) for record, signature in rows)

    def head(self) -> AnchorHead | None:
        history = self.history()
        if not history:
            return None
        last = history[-1]
        return AnchorHead(last.sequence, last.checkpoint_hash, last.previous_hash)

    def verify_storage(self) -> tuple[int, str] | None:
        history = self.history()
        actual = verify_checkpoint_chain(
            history, witness_id=self.witness_id,
                trusted_keys={self.checkpoint_signer.key_id: self.public_key},
            expected_head=((history[-1].sequence, history[-1].checkpoint_hash)
                           if history else None),
        )
        with closing(self._connect()) as connection:
            stored = connection.execute(
                "SELECT sequence, checkpoint_hash FROM witness_meta WHERE singleton=1"
            ).fetchone()
            local_consumed = {
                row[0]: (row[1], row[2]) for row in connection.execute(
                    "SELECT approval_id, checkpoint_sequence, checkpoint_hash "
                    "FROM consumed_approvals")
            }
            local_bundle = connection.execute(
                "SELECT bundle_sequence, bundle_sha256 FROM trust_bundle_state WHERE singleton=1"
            ).fetchone()
            local_authority = connection.execute(
                "SELECT authority_id FROM trust_bundle_authority WHERE singleton=1"
            ).fetchone()[0]
        expected = (actual if actual is not None else (0, None))
        if stored != expected:
            raise WitnessUnavailable("Local witness head metadata conflicts with signed history.")
        witnessed_consumed: dict[str, tuple[int, str]] = {}
        bundle_sequence = 0
        bundle_sha256 = None
        bundle_authority = None
        for checkpoint in history:
            event = checkpoint.event.get("event")
            if not isinstance(event, dict):
                raise WitnessUnavailable("Signed checkpoint event is malformed.")
            if checkpoint.event_type == EVENT_APPROVAL_CONSUMED:
                approval_id = event.get("approval_id")
                if not isinstance(approval_id, str) or approval_id in witnessed_consumed:
                    raise WitnessUnavailable("Witness has duplicate or malformed approval use.")
                witnessed_consumed[approval_id] = (
                    checkpoint.sequence, checkpoint.checkpoint_hash)
            elif checkpoint.event_type == EVENT_TRUST_BUNDLE_UPDATED:
                try:
                    bundle = verify_trust_bundle(
                        _unb64(event["bundle_b64"]),
                        root_key_id=self.trust_root_key_id,
                        root_public_key=self.trust_root_public_key,
                    )
                except (KeyError, ValueError, TrustBundleError) as exc:
                    raise WitnessUnavailable(
                        "Witnessed trust bundle does not verify under the configured root.") from exc
                if (bundle.bundle_sequence != bundle_sequence + 1
                        or bundle.previous_bundle_sha256 != bundle_sha256
                        or (bundle_authority is not None
                            and bundle.authority_id != bundle_authority)):
                    raise WitnessUnavailable("Witnessed trust-bundle history is inconsistent.")
                bundle_sequence = bundle.bundle_sequence
                bundle_sha256 = bundle.sha256
                bundle_authority = bundle.authority_id
        if local_consumed != witnessed_consumed:
            raise WitnessUnavailable("Local replay table conflicts with witnessed consumption history.")
        if local_bundle != (bundle_sequence, bundle_sha256):
            raise WitnessUnavailable("Local trust-bundle state conflicts with witness history.")
        if local_authority != bundle_authority:
            raise WitnessUnavailable("Local trust-bundle authority conflicts with witness history.")
        with closing(self._connect()) as connection:
            acknowledgments = connection.execute(
                "SELECT request_id, request_sha256, ack_json FROM requests").fetchall()
        for request_id, request_digest, ack_raw in acknowledgments:
            try:
                ack = verify_acknowledgment(
                    bytes(ack_raw), witness_id=self.witness_id,
                    trusted_keys={self.checkpoint_signer.key_id: self.public_key},
                )
            except WitnessProtocolError as exc:
                raise WitnessUnavailable("Stored acknowledgment signature is invalid.") from exc
            if (ack["request_id"] != request_id
                    or ack["request_sha256"] != request_digest):
                raise WitnessUnavailable("Stored acknowledgment conflicts with request index.")
        return actual

    def reconcile_external_anchor(self, *, allow_first_commit: bool = False) -> None:
        if self.external_anchor is None:
            return
        history = self.history()
        external = self.external_anchor.latest(self.witness_id)
        if not history:
            if external is not None:
                raise WitnessUnavailable("External anchor is ahead of an empty local witness.")
            return
        if external is None:
            if not allow_first_commit:
                raise WitnessUnavailable("External anchor is missing for nonempty witness history.")
            for checkpoint in history:
                self.external_anchor.advance(self.witness_id, AnchorHead(
                    checkpoint.sequence, checkpoint.checkpoint_hash,
                    checkpoint.previous_hash))
            return
        if external.sequence > len(history):
            raise WitnessUnavailable("Local witness database was rolled back behind external anchor.")
        if external.sequence:
            anchored = history[external.sequence - 1]
            if (anchored.checkpoint_hash != external.checkpoint_hash
                    or anchored.previous_hash != external.previous_hash):
                raise WitnessUnavailable("Local witness conflicts with external anchor history.")
        for checkpoint in history[external.sequence:]:
            self.external_anchor.advance(self.witness_id, AnchorHead(
                checkpoint.sequence, checkpoint.checkpoint_hash, checkpoint.previous_hash))

    def _handle_authenticated(self, request: dict[str, object]) -> bytes:
        payload = request["payload"]
        request_id = payload["request_id"]
        request_digest = hashlib.sha256(canonical_json(request)).hexdigest()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior = connection.execute(
                    "SELECT request_sha256, ack_json FROM requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if prior is not None:
                    connection.rollback()
                    if prior[0] != request_digest:
                        return self._ack(payload, request, "rejected", None,
                                         "REQUEST_ID_CONFLICT")
                    self.reconcile_external_anchor(allow_first_commit=True)
                    return bytes(prior[1])

                current_sequence, current_hash = self._stored_head(connection)
                if (payload["expected_sequence"] != current_sequence
                        or payload["previous_hash"] != current_hash):
                    ack = self._ack(payload, request, "rejected", None, "STALE_HEAD",
                                    sequence=current_sequence, checkpoint_hash=current_hash)
                    self._store_ack(connection, request_id, request_digest, ack)
                    connection.commit()
                    if self.after_commit:
                        self.after_commit()
                    self.reconcile_external_anchor()
                    return ack

                event_type = payload["event_type"]
                event = payload["event"]
                if event_type == EVENT_APPROVAL_CONSUMED:
                    approval_id = event["approval_id"]
                    if payload["idempotency_key"] != "approval:" + approval_id:
                        ack = self._ack(payload, request, "rejected", None,
                                        "IDEMPOTENCY_KEY_MISMATCH",
                                        sequence=current_sequence, checkpoint_hash=current_hash)
                        self._store_ack(connection, request_id, request_digest, ack)
                        connection.commit()
                        self.reconcile_external_anchor(allow_first_commit=True)
                        return ack
                    already = connection.execute(
                        "SELECT 1 FROM consumed_approvals WHERE approval_id=?",
                        (approval_id,),
                    ).fetchone()
                    if already is not None:
                        ack = self._ack(payload, request, "rejected", None, "APPROVAL_REPLAY",
                                        sequence=current_sequence, checkpoint_hash=current_hash)
                        self._store_ack(connection, request_id, request_digest, ack)
                        connection.commit()
                        self.reconcile_external_anchor(allow_first_commit=True)
                        return ack
                elif event_type == EVENT_TRUST_BUNDLE_UPDATED:
                    bundle_error = self._validate_bundle_update(
                        connection, event, payload["idempotency_key"])
                    if bundle_error is not None:
                        ack = self._ack(payload, request, "rejected", None, bundle_error,
                                        sequence=current_sequence, checkpoint_hash=current_hash)
                        self._store_ack(connection, request_id, request_digest, ack)
                        connection.commit()
                        self.reconcile_external_anchor(allow_first_commit=True)
                        return ack

                checkpoint = self._make_checkpoint(
                    sequence=current_sequence + 1, previous_hash=current_hash,
                    event_type=event_type,
                    event={"client_key_id": request["client_key_id"],
                           "request_id": request_id, "event": event},
                )
                serialized_checkpoint = _checkpoint_storage_json(checkpoint)
                connection.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, ?, ?)",
                    (checkpoint.sequence, serialized_checkpoint,
                     checkpoint.checkpoint_hash, _b64(checkpoint.signature)),
                )
                if event_type == EVENT_APPROVAL_CONSUMED:
                    connection.execute(
                        "INSERT INTO consumed_approvals VALUES (?, ?, ?)",
                        (event["approval_id"], checkpoint.sequence,
                         checkpoint.checkpoint_hash),
                    )
                elif event_type == EVENT_TRUST_BUNDLE_UPDATED:
                    bundle = verify_trust_bundle(
                        _unb64(event["bundle_b64"]),
                        root_key_id=self.trust_root_key_id,
                        root_public_key=self.trust_root_public_key,
                    )
                    connection.execute(
                        "UPDATE trust_bundle_state SET bundle_sequence=?, bundle_sha256=? "
                        "WHERE singleton=1",
                        (bundle.bundle_sequence, bundle.sha256),
                    )
                connection.execute(
                    "UPDATE witness_meta SET sequence=?, checkpoint_hash=? WHERE singleton=1",
                    (checkpoint.sequence, checkpoint.checkpoint_hash),
                )
                ack = self._ack(payload, request, "appended", checkpoint, None)
                self._store_ack(connection, request_id, request_digest, ack)
                if self.before_commit:
                    self.before_commit()
                connection.commit()
            except (sqlite3.DatabaseError, TrustBundleError) as exc:
                connection.rollback()
                raise WitnessUnavailable("Witness transaction did not commit.") from exc
        self.reconcile_external_anchor(allow_first_commit=True)
        if self.after_commit:
            self.after_commit()
        return ack

    def _validate_bundle_update(self, connection, event: Mapping[str, object],
                                idempotency_key: str | None) -> str | None:
        try:
            if set(event) != {"bundle_b64"}:
                return "INVALID_TRUST_BUNDLE"
            bundle = verify_trust_bundle(
                _unb64(event["bundle_b64"]),
                root_key_id=self.trust_root_key_id,
                root_public_key=self.trust_root_public_key,
            )
            bundle.trust_policy(now=self.now)
            if idempotency_key != "trust-bundle:" + str(bundle.bundle_sequence):
                return "IDEMPOTENCY_KEY_MISMATCH"
            current_sequence, current_hash = connection.execute(
                "SELECT bundle_sequence, bundle_sha256 FROM trust_bundle_state WHERE singleton=1"
            ).fetchone()
            if (bundle.bundle_sequence != current_sequence + 1
                    or bundle.previous_bundle_sha256 != current_hash):
                return "STALE_TRUST_BUNDLE"
            prior_authority = connection.execute(
                "SELECT authority_id FROM trust_bundle_authority WHERE singleton=1"
            ).fetchone()[0]
            if prior_authority is not None and bundle.authority_id != prior_authority:
                return "TRUST_AUTHORITY_MISMATCH"
            connection.execute(
                "UPDATE trust_bundle_state SET bundle_sequence=?, bundle_sha256=? WHERE singleton=1",
                (bundle.bundle_sequence, bundle.sha256),
            )
            connection.execute(
                "UPDATE trust_bundle_authority SET authority_id=? WHERE singleton=1",
                (bundle.authority_id,),
            )
            return None
        except (ValueError, KeyError, TypeError, TrustBundleError):
            return "INVALID_TRUST_BUNDLE"

    def _make_checkpoint(self, *, sequence: int, previous_hash: str | None,
                         event_type: str, event: Mapping[str, object]) -> SignedCheckpoint:
        record = {
            "witness_id": self.witness_id, "sequence": sequence,
            "event_type": event_type, "event": dict(event),
            "previous_hash": previous_hash,
            "key_id": self.checkpoint_signer.key_id,
        }
        digest = hashlib.sha256(canonical_json(record)).hexdigest()
        signature = self.checkpoint_signer.sign(
            CHECKPOINT_DOMAIN + bytes.fromhex(digest))
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise WitnessUnavailable("Checkpoint signer returned invalid signature bytes.")
        return SignedCheckpoint(self.witness_id, sequence, event_type, dict(event),
                                previous_hash, self.checkpoint_signer.key_id,
                                digest, signature)

    def _ack(self, payload, request, disposition, checkpoint, error_code, *,
             sequence=None, checkpoint_hash=None) -> bytes:
        head = checkpoint.sequence if checkpoint is not None else sequence
        digest = checkpoint.checkpoint_hash if checkpoint is not None else checkpoint_hash
        unsigned = {
            "schema": ACK_SCHEMA, "protocol_version": PROTOCOL_VERSION,
            "witness_id": self.witness_id, "request_id": payload["request_id"],
            "request_sha256": hashlib.sha256(canonical_json(request)).hexdigest(),
            "disposition": disposition, "sequence": head,
            "checkpoint_hash": digest, "error_code": error_code,
            "key_id": self.checkpoint_signer.key_id,
        }
        signature = self.checkpoint_signer.sign(ACK_DOMAIN + canonical_json(unsigned))
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise WitnessUnavailable("Acknowledgment signer returned invalid signature bytes.")
        return canonical_json({**unsigned, "signature": _b64(signature)})

    def _parse_request(self, raw_request: bytes) -> dict[str, object]:
        value = _parse_canonical_object(raw_request, max_bytes=128 * 1024)
        if set(value) != {"schema", "protocol_version", "witness_id", "client_key_id",
                          "payload", "signature"}:
            raise WitnessProtocolError("Request envelope fields are invalid.")
        if value["schema"] != REQUEST_SCHEMA or type(value["protocol_version"]) is not int \
                or value["protocol_version"] != PROTOCOL_VERSION:
            raise WitnessProtocolError("Request protocol version is unsupported.")
        if not isinstance(value["client_key_id"], str) or not isinstance(value["signature"], str):
            raise WitnessProtocolError("Request identity or signature is malformed.")
        if value["witness_id"] != self.witness_id:
            raise WitnessProtocolError("Request targets a different witness.")
        value["payload"] = _validate_request_payload(value["payload"])
        return value

    @staticmethod
    def _verify_client_signature(request, identity: ClientIdentity) -> bool:
        unsigned = {key: request[key] for key in
                    ("schema", "protocol_version", "witness_id", "client_key_id", "payload")}
        try:
            Ed25519PublicKey.from_public_bytes(identity.public_key).verify(
                _unb64(request["signature"]),
                REQUEST_DOMAIN + canonical_json(unsigned),
            )
            return True
        except (ValueError, InvalidSignature):
            return False

    def _initialize(self):
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS witness_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    witness_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    checkpoint_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    sequence INTEGER PRIMARY KEY,
                    record_json BLOB NOT NULL,
                    checkpoint_hash TEXT NOT NULL UNIQUE,
                    signature TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    ack_json BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consumed_approvals (
                    approval_id TEXT PRIMARY KEY,
                    checkpoint_sequence INTEGER NOT NULL,
                    checkpoint_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS trust_bundle_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    bundle_sequence INTEGER NOT NULL,
                    bundle_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS trust_bundle_authority (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    authority_id TEXT
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO witness_meta VALUES (1, ?, 0, NULL)",
                (self.witness_id,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO trust_bundle_state VALUES (1, 0, NULL)")
            connection.execute(
                "INSERT OR IGNORE INTO trust_bundle_authority VALUES (1, NULL)")
            stored = connection.execute(
                "SELECT witness_id FROM witness_meta WHERE singleton=1").fetchone()[0]
            if stored != self.witness_id:
                raise WitnessUnavailable("Database belongs to a different witness identity.")

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _stored_head(connection):
        return connection.execute(
            "SELECT sequence, checkpoint_hash FROM witness_meta WHERE singleton=1"
        ).fetchone()

    @staticmethod
    def _store_ack(connection, request_id, digest, ack):
        connection.execute("INSERT INTO requests VALUES (?, ?, ?)",
                           (request_id, digest, ack))


def _validate_request_payload(payload: object) -> dict[str, object]:
    fields = {"request_id", "expected_sequence", "previous_hash", "event_type",
              "event", "idempotency_key"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise WitnessProtocolError("Request payload fields are invalid.")
    request_id = payload["request_id"]
    if (not isinstance(request_id, str) or len(request_id) != 32
            or any(char not in HEX32 for char in request_id)):
        raise WitnessProtocolError("Request ID must be 32 lowercase hexadecimal characters.")
    sequence = payload["expected_sequence"]
    if type(sequence) is not int or sequence < 0:
        raise WitnessProtocolError("Expected sequence must be a nonnegative integer.")
    previous = payload["previous_hash"]
    if previous is not None and (not isinstance(previous, str) or len(previous) != 64
                                 or any(char not in SHA256_HEX for char in previous)):
        raise WitnessProtocolError("Expected predecessor hash is malformed.")
    event_type = payload["event_type"]
    if event_type not in {EVENT_APPROVAL_CONSUMED, EVENT_TRUST_BUNDLE_UPDATED}:
        raise WitnessProtocolError("Event type is not allowlisted.")
    event = payload["event"]
    if not isinstance(event, dict):
        raise WitnessProtocolError("Event must be an object.")
    if event_type == EVENT_APPROVAL_CONSUMED:
        if set(event) != {"approval_id", "envelope_sha256", "key_id", "action", "consumed_at"}:
            raise WitnessProtocolError("Approval consumption event fields are invalid.")
        approval_id, digest = event["approval_id"], event["envelope_sha256"]
        if (not isinstance(approval_id, str) or len(approval_id) != 32
                or any(char not in HEX32 for char in approval_id)):
            raise WitnessProtocolError("Approval ID is malformed.")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in SHA256_HEX for char in digest)):
            raise WitnessProtocolError("Approval envelope digest is malformed.")
        if any(not isinstance(event[field], str) or not event[field]
               for field in ("key_id", "action", "consumed_at")):
            raise WitnessProtocolError("Approval consumption event has empty fields.")
        if payload["idempotency_key"] != "approval:" + approval_id:
            raise WitnessProtocolError("Approval idempotency key must bind the approval ID.")
    elif set(event) != {"bundle_b64"} or not isinstance(event["bundle_b64"], str):
        raise WitnessProtocolError("Trust-bundle event fields are invalid.")
    idem = payload["idempotency_key"]
    if idem is not None and (not isinstance(idem, str) or not 1 <= len(idem) <= 256):
        raise WitnessProtocolError("Idempotency key is malformed.")
    return payload


def _checkpoint_storage_json(checkpoint: SignedCheckpoint) -> bytes:
    return canonical_json(checkpoint.unsigned_record())


def _checkpoint_from_storage(record_json: bytes, signature_text: str) -> SignedCheckpoint:
    record = _parse_canonical_object(record_json, max_bytes=128 * 1024)
    return SignedCheckpoint(
        witness_id=record["witness_id"], sequence=record["sequence"],
        event_type=record["event_type"], event=record["event"],
        previous_hash=record["previous_hash"], key_id=record["key_id"],
        checkpoint_hash=hashlib.sha256(canonical_json(record)).hexdigest(),
        signature=_unb64(signature_text),
    )


def _parse_canonical_object(raw: bytes, *, max_bytes: int) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise WitnessProtocolError("Protocol record is empty or too large.")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise WitnessProtocolError("Protocol record is not strict JSON.") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise WitnessProtocolError("Protocol record is not canonical JSON.")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("base64 value must be text")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


\n