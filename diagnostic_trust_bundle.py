"""Root-signed, versioned trust-bundle reference contracts (schema v2).

No production roots or signing keys are created here. The anchor and publisher
are injected so tests can model trust updates and interrupted publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
from typing import Callable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from diagnostic_trust import TrustContractError, TrustPolicy, canonical_json


TRUST_BUNDLE_SCHEMA = "localcodingagent.diagnostic.trust-bundle"
TRUST_BUNDLE_VERSION = 2
TRUST_BUNDLE_DOMAIN = b"LocalCodingAgent/diagnostic-trust-bundle/v2\0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ROLES = frozenset({"approval", "receipt", "witness"})


class TrustBundleError(ValueError):
    """Trust-bundle schema, signature, sequence, or publication failed."""


class TrustBundleReplayError(TrustBundleError):
    """Bundle sequence was already used or would roll trust state backward."""


class RootSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


@dataclass(frozen=True)
class TrustBundleCheckpoint:
    sequence: int
    bundle_sha256: str
    previous_sha256: str | None


class TrustBundleAnchor(Protocol):
    def latest(self) -> TrustBundleCheckpoint | None: ...

    def advance(self, checkpoint: TrustBundleCheckpoint) -> None: ...


class FakeTrustBundleAnchor:
    """In-memory monotonic anchor for tests; not a protected production store."""

    def __init__(self):
        self._checkpoints: list[TrustBundleCheckpoint] = []
        self._protected_head: TrustBundleCheckpoint | None = None

    def latest(self) -> TrustBundleCheckpoint | None:
        return self._protected_head

    def advance(self, checkpoint: TrustBundleCheckpoint) -> None:
        latest = self.latest()
        if latest == checkpoint:
            return
        expected_sequence = 1 if latest is None else latest.sequence + 1
        expected_previous = None if latest is None else latest.bundle_sha256
        if (checkpoint.sequence != expected_sequence
                or checkpoint.previous_sha256 != expected_previous):
            raise TrustBundleReplayError("Trust anchor rejected stale or conflicting bundle.")
        self._checkpoints.append(checkpoint)
        self._protected_head = checkpoint


@dataclass(frozen=True)
class VerifiedTrustBundle:
    authority_id: str
    bundle_sequence: int
    issued_at: str
    expires_at: str
    previous_bundle_sha256: str | None
    signers: tuple[Mapping[str, object], ...]
    revoked_key_ids: frozenset[str]
    root_key_id: str
    raw: bytes
    sha256: str

    def trust_policy(self, *, now: datetime | None = None) -> TrustPolicy:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if current < _timestamp(self.issued_at) or current >= _timestamp(self.expires_at):
            raise TrustBundleError("Trust bundle is outside its validity period.")
        public_keys: dict[str, bytes] = {}
        approval_ids: set[str] = set()
        receipt_ids: set[str] = set()
        for signer in self.signers:
            key_id = str(signer["key_id"])
            public_keys[key_id] = base64.urlsafe_b64decode(
                str(signer["public_key"]) + "=" * (-len(str(signer["public_key"])) % 4))
            roles = signer["roles"]
            active = (_timestamp(str(signer["not_before"])) <= current
                      < _timestamp(str(signer["not_after"])))
            if active and "approval" in roles and key_id not in self.revoked_key_ids:
                approval_ids.add(key_id)
            if active and "receipt" in roles and key_id not in self.revoked_key_ids:
                receipt_ids.add(key_id)
        return TrustPolicy(
            public_keys=public_keys,
            revoked_key_ids=frozenset(self.revoked_key_ids),
            approval_key_ids=frozenset(approval_ids),
            receipt_key_ids=frozenset(receipt_ids),
        )


def issue_trust_bundle(payload: Mapping[str, object], root_signer: RootSigner) -> bytes:
    """Sign validated schema-v2 claims with the already trusted root provider."""
    normalized = _validate_payload(dict(payload))
    if not KEY_ID_RE.fullmatch(root_signer.key_id):
        raise TrustBundleError("Root key ID is malformed.")
    signed = _signed_payload(normalized, root_signer.key_id)
    signature = root_signer.sign(TRUST_BUNDLE_DOMAIN + canonical_json(signed))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise TrustBundleError("Root signer returned a malformed Ed25519 signature.")
    envelope = {**signed, "signature": base64.urlsafe_b64encode(signature)
                .decode("ascii").rstrip("=")}
    return canonical_json(envelope)


def verify_trust_bundle(raw: bytes, *, root_key_id: str,
                        root_public_key: bytes) -> VerifiedTrustBundle:
    if not isinstance(raw, bytes) or not raw or len(raw) > 256 * 1024:
        raise TrustBundleError("Trust bundle is empty or exceeds its size limit.")
    try:
        envelope = json.loads(raw.decode("utf-8", errors="strict"),
                              object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TrustBundleError("Trust bundle is not strict JSON.") from exc
    if not isinstance(envelope, dict) or canonical_json(envelope) != raw:
        raise TrustBundleError("Trust bundle is not canonical JSON.")
    expected_fields = {
        "schema", "schema_version", "authority_id", "bundle_sequence", "issued_at",
        "expires_at", "previous_bundle_sha256", "signers", "revoked_key_ids",
        "root_key_id", "signature",
    }
    if set(envelope) != expected_fields:
        raise TrustBundleError("Trust bundle envelope fields are invalid.")
    signature_text = envelope.pop("signature")
    if envelope["root_key_id"] != root_key_id:
        raise TrustBundleError("Trust bundle was not signed by the pinned root identity.")
    if (not isinstance(root_public_key, bytes) or len(root_public_key) != 32
            or not isinstance(signature_text, str)):
        raise TrustBundleError("Pinned root or signature encoding is invalid.")
    payload = {key: envelope[key] for key in envelope if key not in {
        "schema", "schema_version", "root_key_id"}}
    normalized = _validate_payload(payload)
    signed = _signed_payload(normalized, root_key_id)
    try:
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4))
        Ed25519PublicKey.from_public_bytes(root_public_key).verify(
            signature, TRUST_BUNDLE_DOMAIN + canonical_json(signed))
    except (ValueError, InvalidSignature) as exc:
        raise TrustBundleError("Trust bundle root signature is invalid.") from exc
    return VerifiedTrustBundle(
        authority_id=normalized["authority_id"],
        bundle_sequence=normalized["bundle_sequence"],
        issued_at=normalized["issued_at"],
        expires_at=normalized["expires_at"],
        previous_bundle_sha256=normalized["previous_bundle_sha256"],
        signers=tuple(normalized["signers"]),
        revoked_key_ids=frozenset(normalized["revoked_key_ids"]),
        root_key_id=root_key_id,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


class TrustBundleManager:
    """Apply root-verified bundles through a monotonic external anchor.

    Anchor advancement precedes publication. If publication is interrupted, the
    exact same anchored bundle can be reapplied to finish the update; older or
    conflicting signed bundles remain rejected.
    """

    def __init__(self, *, root_key_id: str, root_public_key: bytes,
                 anchor: TrustBundleAnchor, current_raw: bytes | None = None,
                 publish: Callable[[bytes], None] | None = None,
                 now: datetime | None = None):
        self.root_key_id = root_key_id
        self.root_public_key = root_public_key
        self.anchor = anchor
        self.current = (verify_trust_bundle(current_raw, root_key_id=root_key_id,
                                            root_public_key=root_public_key)
                        if current_raw is not None else None)
        self.publish = publish or (lambda _: None)
        self.now = now
        self._check_current_anchor()

    def apply(self, raw: bytes) -> VerifiedTrustBundle:
        candidate = verify_trust_bundle(
            raw, root_key_id=self.root_key_id, root_public_key=self.root_public_key)
        current_sequence = self.current.bundle_sequence if self.current else 0
        current_hash = self.current.sha256 if self.current else None
        if (self.current is not None
                and candidate.authority_id != self.current.authority_id):
            raise TrustBundleError("Trust bundle authority identity cannot change in-place.")
        current_time = (self.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if (current_time < _timestamp(candidate.issued_at)
                or current_time >= _timestamp(candidate.expires_at)):
            raise TrustBundleError("Trust bundle is not currently valid for update.")
        checkpoint = TrustBundleCheckpoint(
            candidate.bundle_sequence, candidate.sha256,
            candidate.previous_bundle_sha256)
        anchor_head = self.anchor.latest()

        # Recovery after anchor commit but before local publication is limited to
        # the byte-identical, already anchored next bundle.
        if anchor_head == checkpoint and candidate.bundle_sequence == current_sequence + 1 \
                and candidate.previous_bundle_sha256 == current_hash:
            self.publish(raw)
            self.current = candidate
            return candidate

        expected_sequence = current_sequence + 1
        if (candidate.bundle_sequence != expected_sequence
                or candidate.previous_bundle_sha256 != current_hash):
            raise TrustBundleReplayError("Trust bundle is stale, replayed, or forked.")
        expected_anchor = (TrustBundleCheckpoint(current_sequence, current_hash,
                                                self.current.previous_bundle_sha256)
                          if self.current is not None else None)
        if anchor_head != expected_anchor:
            raise TrustBundleReplayError("Local trust bundle does not match monotonic anchor.")
        self.anchor.advance(checkpoint)
        self.publish(raw)
        self.current = candidate
        return candidate

    def _check_current_anchor(self) -> None:
        latest = self.anchor.latest()
        if self.current is None:
            if latest is not None and not (latest.sequence == 1
                                           and latest.previous_sha256 is None):
                raise TrustBundleReplayError("Local trust bundle is stale or rolled back.")
            return
        expected = TrustBundleCheckpoint(
            self.current.bundle_sequence, self.current.sha256,
            self.current.previous_bundle_sha256,
        )
        interrupted_next = (latest is not None
                            and latest.sequence == self.current.bundle_sequence + 1
                            and latest.previous_sha256 == self.current.sha256)
        if latest != expected and not interrupted_next:
            raise TrustBundleReplayError("Local trust bundle is stale or rolled back.")


def _signed_payload(payload: dict[str, object], root_key_id: str) -> dict[str, object]:
    return {
        "schema": TRUST_BUNDLE_SCHEMA,
        "schema_version": TRUST_BUNDLE_VERSION,
        "authority_id": payload["authority_id"],
        "bundle_sequence": payload["bundle_sequence"],
        "issued_at": payload["issued_at"],
        "expires_at": payload["expires_at"],
        "previous_bundle_sha256": payload["previous_bundle_sha256"],
        "signers": payload["signers"],
        "revoked_key_ids": payload["revoked_key_ids"],
        "root_key_id": root_key_id,
    }


def _validate_payload(payload: dict[str, object]) -> dict[str, object]:
    required = {"authority_id", "bundle_sequence", "issued_at", "expires_at",
                "previous_bundle_sha256", "signers", "revoked_key_ids"}
    if set(payload) != required:
        raise TrustBundleError("Trust bundle payload fields are invalid.")
    if (not isinstance(payload["authority_id"], str)
            or not KEY_ID_RE.fullmatch(payload["authority_id"])):
        raise TrustBundleError("Authority identity is malformed.")
    sequence = payload["bundle_sequence"]
    if type(sequence) is not int or sequence < 1:
        raise TrustBundleError("Trust bundle sequence must be a positive integer.")
    issued = _timestamp(payload["issued_at"])
    expires = _timestamp(payload["expires_at"])
    if expires <= issued:
        raise TrustBundleError("Trust bundle validity interval is invalid.")
    previous = payload["previous_bundle_sha256"]
    if previous is not None and (not isinstance(previous, str)
                                 or not SHA256_RE.fullmatch(previous)):
        raise TrustBundleError("Previous bundle digest is malformed.")
    signers = payload["signers"]
    if not isinstance(signers, list) or not signers:
        raise TrustBundleError("Trust bundle must contain at least one signer.")
    normalized_signers: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for signer in signers:
        if not isinstance(signer, dict) or set(signer) != {
                "key_id", "identity", "public_key", "roles", "not_before", "not_after"}:
            raise TrustBundleError("Signer entry fields are invalid.")
        key_id, identity, public_key, roles = (signer["key_id"], signer["identity"],
                                               signer["public_key"], signer["roles"])
        if (not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id)
                or key_id in seen_ids or not isinstance(identity, str) or not identity
                or len(identity) > 200):
            raise TrustBundleError("Signer identity is malformed or duplicated.")
        if not isinstance(public_key, str):
            raise TrustBundleError("Signer public key encoding is malformed.")
        try:
            decoded_key = base64.urlsafe_b64decode(
                public_key + "=" * (-len(public_key) % 4))
        except (ValueError, TypeError) as exc:
            raise TrustBundleError("Signer public key encoding is malformed.") from exc
        if len(decoded_key) != 32 or not isinstance(roles, list) or not roles:
            raise TrustBundleError("Signer public key or roles are invalid.")
        if (any(not isinstance(role, str) for role in roles)
                or len(set(roles)) != len(roles) or not set(roles).issubset(ROLES)):
            raise TrustBundleError("Signer roles are duplicated or unsupported.")
        not_before = _timestamp(signer["not_before"])
        not_after = _timestamp(signer["not_after"])
        if not_after <= not_before:
            raise TrustBundleError("Signer validity interval is invalid.")
        seen_ids.add(key_id)
        normalized_signers.append({
            "key_id": key_id, "identity": identity,
            "public_key": base64.urlsafe_b64encode(decoded_key).decode("ascii").rstrip("="),
            "roles": sorted(roles), "not_before": _format(not_before),
            "not_after": _format(not_after),
        })
    if normalized_signers != sorted(normalized_signers, key=lambda item: item["key_id"]):
        raise TrustBundleError("Signer entries must be sorted by key ID.")
    revoked = payload["revoked_key_ids"]
    if not isinstance(revoked, list) or any(
            not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id) for key_id in revoked):
        raise TrustBundleError("Revoked key list is malformed.")
    if len(revoked) != len(set(revoked)) or revoked != sorted(revoked):
        raise TrustBundleError("Revoked key IDs must be unique and sorted.")
    if not set(revoked).issubset(seen_ids):
        raise TrustBundleError("Revocation references a signer absent from this bundle.")
    return {
        "authority_id": payload["authority_id"], "bundle_sequence": sequence,
        "issued_at": _format(issued), "expires_at": _format(expires),
        "previous_bundle_sha256": previous, "signers": normalized_signers,
        "revoked_key_ids": revoked,
    }


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TrustBundleError("Timestamps must be UTC RFC3339 strings ending in Z.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise TrustBundleError("Timestamp is malformed.") from exc
    if parsed.tzinfo is None:
        raise TrustBundleError("Timestamp timezone is missing.")
    return parsed.astimezone(timezone.utc)


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result
