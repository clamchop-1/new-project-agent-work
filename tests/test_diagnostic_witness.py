from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_trust import ApprovalReplayError
from diagnostic_witness import (
    FakeAuditWitness,
    WitnessError,
    WitnessedSqliteReplayLedger,
    verify_checkpoint_chain,
)


class EphemeralSigner:
    """Test-only in-memory Ed25519 signer; no key is persisted."""

    def __init__(self, key_id="witness-test-key"):
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message):
        return self._key.sign(message)


class DiagnosticWitnessTests(unittest.TestCase):
    def setUp(self):
        self.signer = EphemeralSigner()
        self.witness = FakeAuditWitness("test-witness", self.signer)
        self.trusted_keys = {self.signer.key_id: self.signer.public_key}

    def test_signed_checkpoint_chain_verifies_and_detects_missing_entries(self):
        self.witness.append("run_reserved", {"run_id": "a" * 32})
        self.witness.append("approval_consumed", {"approval_id": "b" * 32})
        history = self.witness.history()
        head = self.witness.latest_head()
        self.assertEqual(verify_checkpoint_chain(
            history, witness_id="test-witness", trusted_keys=self.trusted_keys,
            expected_head=head), head)
        with self.assertRaises(WitnessError):
            verify_checkpoint_chain(
                (history[1],), witness_id="test-witness",
                trusted_keys=self.trusted_keys, expected_head=head)

    def test_forgery_rollback_and_conflicting_history_are_rejected(self):
        self.witness.append("one", {"value": 1})
        self.witness.append("two", {"value": 2})
        history = self.witness.history()
        head = self.witness.latest_head()
        forged = (replace(history[0], event={"value": 9}), history[1])
        with self.assertRaises(WitnessError):
            verify_checkpoint_chain(forged, witness_id="test-witness",
                                    trusted_keys=self.trusted_keys, expected_head=head)
        with self.assertRaises(WitnessError):
            verify_checkpoint_chain(history[:1], witness_id="test-witness",
                                    trusted_keys=self.trusted_keys, expected_head=head)

        other_signer = EphemeralSigner("other-witness-key")
        other = FakeAuditWitness("test-witness", other_signer)
        other.append("one", {"value": 1})
        with self.assertRaises(WitnessError):
            verify_checkpoint_chain(
                other.history(), witness_id="test-witness",
                trusted_keys={**self.trusted_keys, other_signer.key_id: other_signer.public_key},
                expected_head=head)

    def test_concurrent_replay_consumption_has_one_witnessed_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = WitnessedSqliteReplayLedger(
                Path(temporary) / "replay.sqlite3", self.witness,
                witness_id="test-witness", witness_public_keys=self.trusted_keys)

            def attempt(_):
                try:
                    ledger.consume_once(
                        approval_id="c" * 32, envelope_sha256="d" * 64,
                        key_id="approval-key", action="invoke_operation",
                        consumed_at="2026-11-22T20:00:00Z")
                    return "consumed"
                except ApprovalReplayError:
                    return "replay"

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(attempt, range(8)))
            self.assertEqual(results.count("consumed"), 1)
            self.assertEqual(results.count("replay"), 7)
            self.assertTrue(ledger.is_consumed("c" * 32))

    def test_interrupted_local_write_is_recovered_from_witness(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replay.sqlite3"

            def interrupt():
                raise OSError("simulated interruption after witness append")

            ledger = WitnessedSqliteReplayLedger(
                path, self.witness, witness_id="test-witness",
                witness_public_keys=self.trusted_keys, after_witness_append=interrupt)
            with self.assertRaises(OSError):
                ledger.consume_once(
                    approval_id="e" * 32, envelope_sha256="f" * 64,
                    key_id="approval-key", action="attach_media",
                    consumed_at="2026-11-22T20:00:00Z")
            recovered = WitnessedSqliteReplayLedger(
                path, self.witness, witness_id="test-witness",
                witness_public_keys=self.trusted_keys)
            self.assertTrue(recovered.is_consumed("e" * 32))
            with self.assertRaises(ApprovalReplayError):
                recovered.consume_once(
                    approval_id="e" * 32, envelope_sha256="f" * 64,
                    key_id="approval-key", action="attach_media",
                    consumed_at="2026-11-22T20:00:00Z")

    def test_local_database_rollback_is_reconciled_not_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replay.sqlite3"
            ledger = WitnessedSqliteReplayLedger(
                path, self.witness, witness_id="test-witness",
                witness_public_keys=self.trusted_keys)
            ledger.consume_once(
                approval_id="1" * 32, envelope_sha256="2" * 64,
                key_id="approval-key", action="start_worker",
                consumed_at="2026-11-22T20:00:00Z")
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DELETE FROM consumed_approvals")
                connection.commit()
            self.assertEqual(ledger.reconcile(), 1)
            self.assertTrue(ledger.is_consumed("1" * 32))

    def test_conflicting_local_replay_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "replay.sqlite3"
            ledger = WitnessedSqliteReplayLedger(
                path, self.witness, witness_id="test-witness",
                witness_public_keys=self.trusted_keys)
            ledger.consume_once(
                approval_id="3" * 32, envelope_sha256="4" * 64,
                key_id="approval-key", action="stop_worker",
                consumed_at="2026-11-22T20:00:00Z")
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE consumed_approvals SET checkpoint_hash=?", ("0" * 64,))
                connection.commit()
            with self.assertRaises(WitnessError):
                ledger.reconcile()


if __name__ == "__main__":
    unittest.main()
