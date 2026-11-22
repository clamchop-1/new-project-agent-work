from datetime import datetime, timezone
import base64
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_trust import canonical_json
from diagnostic_trust_bundle import (
    FakeTrustBundleAnchor,
    TrustBundleError,
    TrustBundleManager,
    TrustBundleReplayError,
    issue_trust_bundle,
    verify_trust_bundle,
)


class EphemeralSigner:
    def __init__(self, key_id):
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message):
        return self._key.sign(message)


class DiagnosticTrustBundleTests(unittest.TestCase):
    def setUp(self):
        self.root = EphemeralSigner("test-root")
        self.approval = EphemeralSigner("approval-old")
        self.next_approval = EphemeralSigner("approval-next")
        self.now = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)

    def signer_record(self, signer, roles=("approval",), *, not_after="2027-01-01T00:00:00Z"):
        return {
            "key_id": signer.key_id,
            "identity": "test identity " + signer.key_id,
            "public_key": base64.urlsafe_b64encode(signer.public_key).decode("ascii").rstrip("="),
            "roles": sorted(roles),
            "not_before": "2026-01-01T00:00:00Z",
            "not_after": not_after,
        }

    def bundle(self, sequence, previous, signers, revoked=()):
        payload = {
            "authority_id": "windows-broker-test",
            "bundle_sequence": sequence,
            "issued_at": "2026-11-22T19:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
            "previous_bundle_sha256": previous,
            "signers": sorted(signers, key=lambda row: row["key_id"]),
            "revoked_key_ids": sorted(revoked),
        }
        return issue_trust_bundle(payload, self.root)

    def manager(self, anchor, current=None, publish=None):
        return TrustBundleManager(
            root_key_id=self.root.key_id,
            root_public_key=self.root.public_key,
            anchor=anchor,
            current_raw=current,
            publish=publish,
            now=self.now,
        )

    def test_root_signed_v2_bundle_builds_time_limited_trust_policy(self):
        raw = self.bundle(1, None, [self.signer_record(self.approval)])
        verified = verify_trust_bundle(
            raw, root_key_id=self.root.key_id, root_public_key=self.root.public_key)
        policy = verified.trust_policy(now=self.now)
        self.assertEqual(policy.approval_key_ids, frozenset({self.approval.key_id}))
        self.assertEqual(verified.bundle_sequence, 1)
        self.assertEqual(verified.authority_id, "windows-broker-test")

    def test_forged_or_unpinned_root_bundle_is_rejected(self):
        raw = self.bundle(1, None, [self.signer_record(self.approval)])
        envelope = json.loads(raw)
        signature = envelope["signature"]
        envelope["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(TrustBundleError):
            verify_trust_bundle(canonical_json(envelope), root_key_id=self.root.key_id,
                                root_public_key=self.root.public_key)
        with self.assertRaises(TrustBundleError):
            verify_trust_bundle(raw, root_key_id="other-root",
                                root_public_key=self.root.public_key)

    def test_rotation_and_revocation_require_next_root_signed_sequence(self):
        anchor = FakeTrustBundleAnchor()
        first = self.bundle(1, None, [self.signer_record(self.approval)])
        manager = self.manager(anchor)
        active_first = manager.apply(first)
        second = self.bundle(
            2, active_first.sha256,
            [self.signer_record(self.approval), self.signer_record(self.next_approval)],
            revoked=[self.approval.key_id],
        )
        active_second = manager.apply(second)
        policy = active_second.trust_policy(now=self.now)
        self.assertEqual(policy.approval_key_ids, frozenset({self.next_approval.key_id}))
        self.assertIn(self.approval.key_id, policy.revoked_key_ids)
        with self.assertRaises(TrustBundleReplayError):
            manager.apply(first)

    def test_expired_signer_is_not_added_to_current_policy(self):
        expired = self.signer_record(self.approval, not_after="2026-11-22T19:59:00Z")
        bundle = verify_trust_bundle(
            self.bundle(1, None, [expired]), root_key_id=self.root.key_id,
            root_public_key=self.root.public_key)
        self.assertFalse(bundle.trust_policy(now=self.now).approval_key_ids)

    def test_interrupted_publication_can_only_recover_exact_anchored_bundle(self):
        anchor = FakeTrustBundleAnchor()
        raw = self.bundle(1, None, [self.signer_record(self.approval)])

        def fail_publish(_):
            raise OSError("simulated crash after anchor commit")

        manager = self.manager(anchor, publish=fail_publish)
        with self.assertRaises(OSError):
            manager.apply(raw)
        published = []
        recovery = self.manager(anchor, publish=published.append)
        recovered = recovery.apply(raw)
        self.assertEqual(recovered.sha256, anchor.latest().bundle_sha256)
        self.assertEqual(published, [raw])
        with self.assertRaises(TrustBundleReplayError):
            recovery.apply(self.bundle(1, None, [self.signer_record(self.next_approval)]))

    def test_wrong_sequence_predecessor_and_anchor_rollback_are_rejected(self):
        anchor = FakeTrustBundleAnchor()
        first = self.bundle(1, None, [self.signer_record(self.approval)])
        manager = self.manager(anchor)
        verified = manager.apply(first)
        with self.assertRaises(TrustBundleReplayError):
            manager.apply(self.bundle(3, verified.sha256,
                                      [self.signer_record(self.next_approval)]))
        with self.assertRaises(TrustBundleReplayError):
            manager.apply(self.bundle(2, "0" * 64,
                                      [self.signer_record(self.next_approval)]))
        rolled_back_anchor = FakeTrustBundleAnchor()
        with self.assertRaises(TrustBundleReplayError):
            self.manager(rolled_back_anchor, current=first)

    def test_bundle_cannot_change_authority_identity(self):
        anchor = FakeTrustBundleAnchor()
        first = self.bundle(1, None, [self.signer_record(self.approval)])
        manager = self.manager(anchor)
        verified = manager.apply(first)
        raw = issue_trust_bundle({
            "authority_id": "different-authority",
            "bundle_sequence": 2,
            "issued_at": "2026-11-22T19:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
            "previous_bundle_sha256": verified.sha256,
            "signers": [self.signer_record(self.next_approval)],
            "revoked_key_ids": [],
        }, self.root)
        with self.assertRaises(TrustBundleError):
            manager.apply(raw)


if __name__ == "__main__":
    unittest.main()
