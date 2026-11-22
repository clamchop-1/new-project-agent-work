from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_trust import canonical_json
from diagnostic_trust_bundle import issue_trust_bundle
from diagnostic_witness_service import (
    AnchorHead,
    ClientIdentity,
    FakeExternalHeadAnchor,
    SqliteAuditWitnessService,
    WitnessConflict,
    WitnessProtocolError,
    WitnessUnavailable,
    signed_request,
    verify_acknowledgment,
)


class EphemeralSigner:
    """In-memory test signer; private key material is never written to disk."""

    def __init__(self, key_id):
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message):
        return self._key.sign(message)


class DiagnosticWitnessServiceTests(unittest.TestCase):
    def setUp(self):
        self.broker = EphemeralSigner("broker-client")
        self.admin = EphemeralSigner("trust-admin")
        self.service_signer = EphemeralSigner("witness-service")
        self.root = EphemeralSigner("pinned-test-root")
        self.identities = {
            self.broker.key_id: ClientIdentity(self.broker.public_key, frozenset({"broker"})),
            self.admin.key_id: ClientIdentity(self.admin.public_key, frozenset({"trust_admin"})),
        }

    def service(self, path, *, anchor=None, before_commit=None, after_commit=None):
        return SqliteAuditWitnessService(
            path, witness_id="test-durable-witness",
            client_identities=self.identities,
            checkpoint_signer=self.service_signer,
            checkpoint_public_key=self.service_signer.public_key,
            trust_root_key_id=self.root.key_id,
            trust_root_public_key=self.root.public_key,
            external_anchor=anchor,
            before_commit=before_commit,
            after_commit=after_commit,
        )

    def approval_payload(self, request_id, *, expected_sequence=0, previous_hash=None,
                         approval_id="a" * 32):
        return {
            "request_id": request_id,
            "expected_sequence": expected_sequence,
            "previous_hash": previous_hash,
            "event_type": "approval_consumed",
            "event": {
                "approval_id": approval_id,
                "envelope_sha256": "b" * 64,
                "key_id": "approval-signing-key",
                "action": "invoke_operation",
                "consumed_at": "2026-11-22T20:00:00Z",
            },
            "idempotency_key": "approval:" + approval_id,
        }

    def request(self, payload, signer=None):
        return signed_request(payload, signer or self.broker,
                              witness_id="test-durable-witness")

    def ack(self, raw):
        return verify_acknowledgment(
            raw, witness_id="test-durable-witness",
            trusted_keys={self.service_signer.key_id: self.service_signer.public_key},
        )

    def test_authenticated_append_persists_checkpoint_and_signed_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            service = self.service(path)
            raw_request = self.request(self.approval_payload("1" * 32))
            ack = self.ack(service.handle(raw_request))
            self.assertEqual(ack["disposition"], "appended")
            self.assertEqual(ack["sequence"], 1)
            self.assertEqual(len(service.history()), 1)
            self.assertEqual(service.verify_storage(), (1, ack["checkpoint_hash"]))

    def test_lost_acknowledgment_retry_returns_identical_committed_ack_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            fail_once = {"enabled": True}

            def lose_ack():
                if fail_once["enabled"]:
                    fail_once["enabled"] = False
                    raise OSError("simulated response loss after commit")

            service = self.service(path, after_commit=lose_ack)
            raw_request = self.request(self.approval_payload("2" * 32))
            with self.assertRaises(OSError):
                service.handle(raw_request)
            restarted = self.service(path)
            ack_raw = restarted.handle(raw_request)
            ack = self.ack(ack_raw)
            self.assertEqual(ack["disposition"], "appended")
            self.assertEqual(len(restarted.history()), 1)
            self.assertEqual(restarted.handle(raw_request), ack_raw)

    def test_interrupted_precommit_write_is_rolled_back_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"

            def interrupt():
                raise OSError("simulated power loss before commit")

            service = self.service(path, before_commit=interrupt)
            raw_request = self.request(self.approval_payload("3" * 32))
            with self.assertRaises(OSError):
                service.handle(raw_request)
            restarted = self.service(path)
            self.assertEqual(restarted.history(), ())
            ack = self.ack(restarted.handle(raw_request))
            self.assertEqual(ack["disposition"], "appended")

    def test_concurrent_approval_requests_produce_one_consumption(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.service(Path(temporary) / "witness.sqlite3")
            raw_requests = [self.request(self.approval_payload(
                format(index, "032x"))) for index in range(8)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                acks = list(pool.map(service.handle, raw_requests))
            dispositions = [self.ack(raw)["disposition"] for raw in acks]
            self.assertEqual(dispositions.count("appended"), 1)
            self.assertEqual(dispositions.count("rejected"), 7)
            self.assertEqual(len(service.history()), 1)

    def test_duplicate_approval_replay_and_stale_sequence_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.service(Path(temporary) / "witness.sqlite3")
            first = self.ack(service.handle(
                self.request(self.approval_payload("4" * 32))))
            replay = self.ack(service.handle(self.request(self.approval_payload(
                "5" * 32, expected_sequence=1,
                previous_hash=first["checkpoint_hash"]))))
            self.assertEqual(replay["error_code"], "APPROVAL_REPLAY")
            stale = self.ack(service.handle(
                self.request(self.approval_payload("6" * 32))))
            self.assertEqual(stale["error_code"], "STALE_HEAD")
            self.assertEqual(len(service.history()), 1)

    def test_reused_request_id_with_different_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.service(Path(temporary) / "witness.sqlite3")
            first_payload = self.approval_payload("d" * 32, approval_id="1" * 32)
            first = service.handle(self.request(first_payload))
            self.assertEqual(self.ack(first)["disposition"], "appended")
            changed = self.approval_payload("d" * 32, expected_sequence=1,
                                            previous_hash=self.ack(first)["checkpoint_hash"],
                                            approval_id="2" * 32)
            result = self.ack(service.handle(self.request(changed)))
            self.assertEqual(result["error_code"], "REQUEST_ID_CONFLICT")
            self.assertEqual(len(service.history()), 1)

    def test_invalid_signature_and_wrong_witness_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = self.service(Path(temporary) / "witness.sqlite3")
            raw = self.request(self.approval_payload("7" * 32))
            envelope = __import__("json").loads(raw)
            envelope["signature"] = "A" + envelope["signature"][1:]
            ack = self.ack(service.handle(canonical_json(envelope)))
            self.assertEqual(ack["error_code"], "UNAUTHENTICATED")
            wrong_target = signed_request(
                self.approval_payload("8" * 32), self.broker, witness_id="other-witness")
            with self.assertRaises(WitnessProtocolError):
                service.handle(wrong_target)

    def test_trust_bundle_update_requires_trust_admin_and_pinned_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            service = self.service(path)
            signer = EphemeralSigner("approval-pub")
            bundle = issue_trust_bundle({
                "authority_id": "windows-broker",
                "bundle_sequence": 1,
                "issued_at": "2026-11-22T16:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
                "previous_bundle_sha256": None,
                "signers": [{
                    "key_id": signer.key_id, "identity": "approval signer",
                    "public_key": base64.urlsafe_b64encode(signer.public_key)
                    .decode("ascii").rstrip("="),
                    "roles": ["approval"], "not_before": "2026-01-01T00:00:00Z",
                    "not_after": "2027-01-01T00:00:00Z",
                }],
                "revoked_key_ids": [],
            }, self.root)
            event = {"bundle_b64": base64.urlsafe_b64encode(bundle)
                     .decode("ascii").rstrip("=")}
            payload = {"request_id": "9" * 32, "expected_sequence": 0,
                       "previous_hash": None, "event_type": "trust_bundle_updated",
                       "event": event, "idempotency_key": "trust-bundle:1"}
            forbidden = self.ack(service.handle(self.request(payload, self.broker)))
            self.assertEqual(forbidden["error_code"], "ROLE_FORBIDDEN")
            authorized = self.ack(service.handle(self.request(payload, self.admin)))
            self.assertEqual(authorized["disposition"], "appended", authorized)

    def test_forged_bundle_and_inconsistent_root_on_restart_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            service = self.service(path)
            signer = EphemeralSigner("approval-pub")
            other_root = EphemeralSigner("other-root")
            bundle = issue_trust_bundle({
                "authority_id": "windows-broker",
                "bundle_sequence": 1,
                "issued_at": "2026-11-22T16:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
                "previous_bundle_sha256": None,
                "signers": [{
                    "key_id": signer.key_id, "identity": "approval signer",
                    "public_key": base64.urlsafe_b64encode(signer.public_key)
                    .decode("ascii").rstrip("="),
                    "roles": ["approval"], "not_before": "2026-01-01T00:00:00Z",
                    "not_after": "2027-01-01T00:00:00Z",
                }],
                "revoked_key_ids": [],
            }, other_root)
            payload = {"request_id": "e" * 32, "expected_sequence": 0,
                       "previous_hash": None, "event_type": "trust_bundle_updated",
                       "event": {"bundle_b64": base64.urlsafe_b64encode(bundle)
                                 .decode("ascii").rstrip("=")},
                       "idempotency_key": "trust-bundle:1"}
            rejected = self.ack(service.handle(self.request(payload, self.admin)))
            self.assertEqual(rejected["error_code"], "INVALID_TRUST_BUNDLE")

            valid_bundle = issue_trust_bundle({
                "authority_id": "windows-broker",
                "bundle_sequence": 1,
                "issued_at": "2026-11-22T16:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
                "previous_bundle_sha256": None,
                "signers": [{
                    "key_id": signer.key_id, "identity": "approval signer",
                    "public_key": base64.urlsafe_b64encode(signer.public_key)
                    .decode("ascii").rstrip("="),
                    "roles": ["approval"], "not_before": "2026-01-01T00:00:00Z",
                    "not_after": "2027-01-01T00:00:00Z",
                }],
                "revoked_key_ids": [],
            }, self.root)
            valid_payload = {**payload, "request_id": "f" * 32,
                             "event": {"bundle_b64": base64.urlsafe_b64encode(valid_bundle)
                                       .decode("ascii").rstrip("=")}}
            accepted = self.ack(service.handle(self.request(valid_payload, self.admin)))
            self.assertEqual(accepted["disposition"], "appended")
            inconsistent_root = EphemeralSigner("replacement-root")
            with self.assertRaises(WitnessUnavailable):
                SqliteAuditWitnessService(
                    path, witness_id="test-durable-witness",
                    client_identities=self.identities,
                    checkpoint_signer=self.service_signer,
                    checkpoint_public_key=self.service_signer.public_key,
                    trust_root_key_id=inconsistent_root.key_id,
                    trust_root_public_key=inconsistent_root.public_key,
                )

    def test_local_database_rollback_is_detected_by_external_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            anchor = FakeExternalHeadAnchor()
            service = self.service(path, anchor=anchor)
            service.handle(self.request(self.approval_payload("a" * 32)))
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DELETE FROM checkpoints")
                connection.execute("DELETE FROM consumed_approvals")
                connection.execute("DELETE FROM requests")
                connection.execute(
                    "UPDATE witness_meta SET sequence=0, checkpoint_hash=NULL WHERE singleton=1")
                connection.commit()
            with self.assertRaises(WitnessUnavailable):
                self.service(path, anchor=anchor)

    def test_external_anchor_failure_after_commit_recovers_on_restart(self):
        class FailOnceAnchor(FakeExternalHeadAnchor):
            fail = True

            def advance(self, witness_id, checkpoint):
                if self.fail:
                    self.fail = False
                    super().advance(witness_id, checkpoint)
                    raise OSError("simulated external anchor outage")
                return super().advance(witness_id, checkpoint)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            anchor = FailOnceAnchor()
            service = self.service(path, anchor=anchor)
            request = self.request(self.approval_payload("b" * 32))
            with self.assertRaises(OSError):
                service.handle(request)
            restarted = self.service(path, anchor=anchor)
            ack = self.ack(restarted.handle(request))
            self.assertEqual(ack["disposition"], "appended")
            self.assertEqual(anchor.latest("test-durable-witness").sequence, 1)

    def test_conflicting_local_metadata_fails_closed_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "witness.sqlite3"
            service = self.service(path)
            service.handle(self.request(self.approval_payload("c" * 32)))
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE witness_meta SET checkpoint_hash=? WHERE singleton=1",
                    ("0" * 64,),
                )
                connection.commit()
            with self.assertRaises(WitnessUnavailable):
                self.service(path)


if __name__ == "__main__":
    unittest.main()
