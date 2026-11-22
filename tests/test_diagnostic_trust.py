from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_trust import (
    APPROVAL_SCHEMA,
    ApprovalIssuanceService,
    ApprovalReplayError,
    ApprovalScopeError,
    SqliteApprovalConsumptionStore,
    TrustContractError,
    TrustPolicy,
    ValidationReceiptIssuer,
    canonical_json,
    consume_approval,
    validate_receipt_scope,
    verify_approval,
    verify_validation_receipt,
)


class EphemeralTestSigner:
    """Test-only in-memory Ed25519 signer. Private bytes are never persisted."""

    def __init__(self, key_id="test-approval-key"):
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()

    def sign(self, message):
        return self._key.sign(message)

    @property
    def public_bytes(self):
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


class DiagnosticTrustTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 20, 0, 0, tzinfo=timezone.utc)
        self.approval_signer = EphemeralTestSigner("test-approval-key")
        self.receipt_signer = EphemeralTestSigner("test-receipt-key")
        self.issuer = ApprovalIssuanceService(self.approval_signer)
        self.trust = TrustPolicy(
            public_keys={
                self.approval_signer.key_id: self.approval_signer.public_bytes,
                self.receipt_signer.key_id: self.receipt_signer.public_bytes,
            },
            approval_key_ids=frozenset({self.approval_signer.key_id}),
            receipt_key_ids=frozenset({self.receipt_signer.key_id}),
        )
        self.payload = {
            "approval_id": "a" * 32,
            "project_id": "localcodingagent",
            "project_instance_id": "b" * 32,
            "run_id": "c" * 32,
            "run_nonce": "d" * 32,
            "source_sha256": "1" * 64,
            "backend_id": "fake-hyperv",
            "backend_version": "1.0",
            "vm_identity": "vm-guid-123",
            "architecture": "x86_64",
            "operation_id": "diagnostic-noop-v1",
            "launcher_sha256": "2" * 64,
            "media_hashes": {"input_iso": "3" * 64, "output_template": "4" * 64},
            "resource_limits": {
                "cpu_count": 2,
                "memory_bytes": 2 * 1024**3,
                "disk_bytes": 1024**3,
                "timeout_seconds": 600,
                "process_count": 32,
                "output_bytes": 4 * 1024**2,
            },
            "permitted_action": "invoke_operation",
            "issued_at": "2026-11-22T20:00:00Z",
            "expires_at": "2026-11-22T20:10:00Z",
        }

    def signed_approval(self, payload=None, signer=None):
        return (self.issuer if signer is None else ApprovalIssuanceService(signer)).issue_approval(
            self.payload if payload is None else payload)

    def verify(self, raw=None, trust=None, expected=None, action="invoke_operation", now=None):
        return verify_approval(
            self.signed_approval() if raw is None else raw,
            self.trust if trust is None else trust,
            now=self.now if now is None else now,
            expected=expected,
            required_action=action,
        )

    def test_approval_is_canonical_versioned_and_binds_exact_run_scope(self):
        raw = self.signed_approval()
        envelope = json.loads(raw)
        self.assertEqual(canonical_json(envelope), raw)
        self.assertEqual(envelope["schema"], APPROVAL_SCHEMA)
        self.assertEqual(envelope["schema_version"], 1)
        expected = {name: self.payload[name] for name in (
            "project_id", "project_instance_id", "run_id", "source_sha256", "backend_id",
            "backend_version", "vm_identity", "architecture", "operation_id",
            "launcher_sha256", "media_hashes", "resource_limits",
        )}
        verified = self.verify(raw=raw, expected=expected)
        self.assertEqual(verified.payload["approval_id"], self.payload["approval_id"])

    def test_signature_tampering_and_noncanonical_json_fail_closed(self):
        envelope = json.loads(self.signed_approval())
        signature = envelope["signature"]
        envelope["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(TrustContractError):
            self.verify(raw=canonical_json(envelope))
        with self.assertRaises(TrustContractError):
            self.verify(raw=b'{ "schema":"localcodingagent.diagnostic.approval" }')

    def test_expired_approval_and_wrong_action_or_target_are_rejected(self):
        with self.assertRaises(TrustContractError):
            self.verify(now=self.now + timedelta(minutes=11))
        with self.assertRaises(ApprovalScopeError):
            self.verify(expected={"vm_identity": "different-vm"})
        with self.assertRaises(ApprovalScopeError):
            self.verify(action="attach_media")

    def test_cross_project_and_malformed_contracts_are_rejected(self):
        with self.assertRaises(ApprovalScopeError):
            self.verify(expected={"project_instance_id": "e" * 32})
        malformed = deepcopy(self.payload)
        malformed["resource_limits"]["cpu_count"] = True
        with self.assertRaises(TrustContractError):
            self.signed_approval(malformed)
        malformed = deepcopy(self.payload)
        malformed["media_hashes"] = {"input_iso": "bad"}
        with self.assertRaises(TrustContractError):
            self.signed_approval(malformed)

    def test_untrusted_or_revoked_signing_key_is_rejected(self):
        unknown = EphemeralTestSigner("untrusted-key")
        raw = self.signed_approval(signer=unknown)
        with self.assertRaises(TrustContractError):
            self.verify(raw=raw)
        revoked = TrustPolicy(
            public_keys=self.trust.public_keys,
            revoked_key_ids=frozenset({self.approval_signer.key_id}),
            approval_key_ids=frozenset({self.approval_signer.key_id}),
            receipt_key_ids=frozenset({self.receipt_signer.key_id}),
        )
        with self.assertRaises(TrustContractError):
            self.verify(trust=revoked)

    def test_rotation_accepts_new_key_and_revocation_rejects_old_key(self):
        next_signer = EphemeralTestSigner("rotated-approval-key")
        rotated_trust = TrustPolicy(
            public_keys={**self.trust.public_keys, next_signer.key_id: next_signer.public_bytes},
            approval_key_ids=frozenset({self.approval_signer.key_id, next_signer.key_id}),
            receipt_key_ids=frozenset({self.receipt_signer.key_id}),
        )
        new_raw = self.signed_approval(signer=next_signer)
        self.verify(raw=new_raw, trust=rotated_trust)
        revoked = TrustPolicy(
            public_keys=rotated_trust.public_keys,
            revoked_key_ids=frozenset({self.approval_signer.key_id}),
            approval_key_ids=rotated_trust.approval_key_ids,
            receipt_key_ids=rotated_trust.receipt_key_ids,
        )
        with self.assertRaises(TrustContractError):
            self.verify(trust=revoked)

    def test_approval_consumption_is_one_time_and_durable(self):
        raw = self.signed_approval()
        approved = self.verify(raw=raw)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broker-state.sqlite3"
            store = SqliteApprovalConsumptionStore(path)
            grant = consume_approval(approved, store, required_action="invoke_operation", now=self.now)
            self.assertEqual(grant.action, "invoke_operation")
            reopened = SqliteApprovalConsumptionStore(path)
            with self.assertRaises(ApprovalReplayError):
                consume_approval(approved, reopened, required_action="invoke_operation", now=self.now)

    def test_concurrent_consumption_has_exactly_one_winner(self):
        approval = self.verify(raw=self.signed_approval())
        with tempfile.TemporaryDirectory() as temporary:
            store_path = Path(temporary) / "concurrent.sqlite3"
            store = SqliteApprovalConsumptionStore(store_path)

            def attempt(_):
                try:
                    consume_approval(approval, store, required_action="invoke_operation", now=self.now)
                    return "consumed"
                except ApprovalReplayError:
                    return "replay"

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("consumed"), 1)
        self.assertEqual(results.count("replay"), 7)

    def test_failed_modeled_action_does_not_refund_consumed_approval(self):
        approval = self.verify(raw=self.signed_approval())
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteApprovalConsumptionStore(Path(temporary) / "consume.sqlite3")
            grant = consume_approval(
                approval, store, required_action="invoke_operation", now=self.now)
            self.assertEqual(grant.action, "invoke_operation")
            # Model a crash/failure after the durable consume boundary. No code
            # here invokes a guest or host action; the invariant is that failure
            # cannot make this one-time approval reusable.
            with self.assertRaises(RuntimeError):
                raise RuntimeError("simulated action failure after consumption")
            with self.assertRaises(ApprovalReplayError):
                consume_approval(
                    approval, store, required_action="invoke_operation", now=self.now)

    def test_receipt_is_bound_to_approval_and_tampering_is_rejected(self):
        approval = self.verify(raw=self.signed_approval())
        result = {
            "outcome": "validated",
            "policy_version": 3,
            "verified_artifacts": [{"path": "result.txt", "size": 5, "sha256": "5" * 64}],
            "observed_artifacts": [],
            "failures": [],
        }
        receipt_raw = ValidationReceiptIssuer(self.receipt_signer).issue_for_approval(
            result, approval, validated_at="2026-11-22T20:05:00Z", receipt_id="f" * 32)
        receipt = verify_validation_receipt(receipt_raw, self.trust)
        validate_receipt_scope(receipt, approval)
        self.assertEqual(receipt.payload["run_id"], self.payload["run_id"])
        self.assertEqual(receipt.payload["artifacts"][0]["sha256"], "5" * 64)
        with self.assertRaises(TypeError):
            approval.payload["run_id"] = "9" * 32

        tampered = json.loads(receipt_raw)
        tampered["payload"]["artifacts"][0]["sha256"] = "6" * 64
        with self.assertRaises(TrustContractError):
            verify_validation_receipt(canonical_json(tampered), self.trust)

    def test_signed_receipt_from_another_run_is_not_accepted(self):
        approval = self.verify(raw=self.signed_approval())
        result = {
            "outcome": "rejected", "policy_version": 1, "verified_artifacts": [],
            "observed_artifacts": [{"path": "result.txt", "size": 3, "sha256": "7" * 64}],
            "failures": ["hash mismatch"],
        }
        raw = ValidationReceiptIssuer(self.receipt_signer).issue_for_approval(
            result, approval, validated_at="2026-11-22T20:05:00Z")
        other_payload = deepcopy(self.payload)
        other_payload["approval_id"] = "e" * 32
        other_payload["run_id"] = "9" * 32
        other_approval = self.verify(raw=self.signed_approval(other_payload))
        other_raw = ValidationReceiptIssuer(self.receipt_signer).issue_for_approval(
            result, other_approval, validated_at="2026-11-22T20:05:00Z")
        receipt = verify_validation_receipt(other_raw, self.trust)
        with self.assertRaises(ApprovalScopeError):
            validate_receipt_scope(receipt, approval)

    def test_wrong_signing_role_cannot_issue_the_other_record_type(self):
        receipt_only_trust = TrustPolicy(
            public_keys=self.trust.public_keys,
            approval_key_ids=frozenset(),
            receipt_key_ids=frozenset({self.receipt_signer.key_id}),
        )
        raw = self.signed_approval()
        with self.assertRaises(TrustContractError):
            self.verify(raw=raw, trust=receipt_only_trust)


if __name__ == "__main__":
    unittest.main()
