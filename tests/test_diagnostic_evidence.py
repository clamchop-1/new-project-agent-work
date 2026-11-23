from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_evidence import (
    ActionObservation,
    AuditedFakeCoordinator,
    EvidenceError,
    EvidenceStore,
    FakeEvidenceBackend,
    FakeDirectoryCollectionSource,
    OperationPendingError,
    SourceFileEntry,
    SourceIdentity,
    target_sha256,
    verify_readonly_source_inventory,
)
from diagnostic_runs import DiagnosticRunError, DiagnosticRunLedger, RunInterruptedError
from diagnostic_trust import (
    ApprovalIssuanceService,
    ApprovalReplayError,
    SqliteApprovalConsumptionStore,
    TrustContractError,
    TrustPolicy,
    consume_approval,
    verify_approval,
)


class EphemeralSigner:
    key_id = "milestone1-test-key"

    def __init__(self):
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_bytes(self):
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message):
        return self._key.sign(message)


class FakeReadOnlySource:
    def __init__(self, identity, entries):
        self.identity = identity
        self.entries = tuple(entries)
        self.opened = []

    def identify(self):
        return self.identity

    def list_allowlisted(self):
        return self.entries

    def open_allowlisted(self, name):
        self.opened.append(name)
        raise AssertionError("Inventory preflight must not open file contents.")


class DiagnosticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.ledger = DiagnosticRunLedger(
            self.project, self.project / ".local/runs", project_id="localcodingagent")
        self.run_id = uuid.uuid4().hex
        self.run = self.ledger.reserve(self.run_id)
        for state in ("preparing", "prepared", "awaiting_approval", "approved"):
            previous = self.ledger.get_run(self.run_id)["events"][-1]["state"]
            self.ledger.transition(self.run_id, expected_state=previous, new_state=state,
                                   evidence={"test_fixture_state": state})
        self.evidence = EvidenceStore(self.ledger, self.run_id)
        self.signer = EphemeralSigner()
        self.issuer = ApprovalIssuanceService(self.signer)
        self.trust = TrustPolicy(
            {self.signer.key_id: self.signer.public_bytes},
            approval_key_ids=frozenset({self.signer.key_id}))
        self.consumption = SqliteApprovalConsumptionStore(
            self.base / "protected-test-state" / "consumption.sqlite3")
        self.backend = FakeEvidenceBackend()
        self.coordinator = AuditedFakeCoordinator(
            self.ledger, self.run_id, self.evidence, self.trust,
            self.consumption, self.backend)
        self.target = {
            "kind": "vm_media_attachment",
            "vm_guid": "00000000-0000-0000-0000-000000000123",
            "controller": "SCSI 0:1",
            "media_role": "input_iso",
            "media_sha256": "4" * 64,
            "access": "read_only",
        }

    def tearDown(self):
        self.temp.cleanup()

    def approval(self, *, target=None, action="attach_media", expires=None, run_id=None):
        target = self.target if target is None else target
        now = datetime.now(timezone.utc)
        issued = (now - timedelta(minutes=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
        expires = expires or (now + timedelta(minutes=5)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        payload = {
            "approval_id": uuid.uuid4().hex,
            "project_id": self.ledger.project_id,
            "project_instance_id": uuid.uuid4().hex,
            "run_id": run_id or self.run_id,
            "run_nonce": self.run["run_nonce"],
            "source_sha256": "1" * 64,
            "backend_id": "fake-hyperv",
            "backend_version": "test-1",
            "vm_identity": "vm-guid-123",
            "architecture": "x86_64",
            "operation_id": "sentinel-hash-v1",
            "launcher_sha256": "2" * 64,
            "media_hashes": {
                "input_iso": "3" * 64,
                "action_target": target_sha256(target),
            },
            "resource_limits": {
                "cpu_count": 1,
                "memory_bytes": 1024**3,
                "disk_bytes": 128 * 1024**2,
                "timeout_seconds": 60,
                "process_count": 8,
                "output_bytes": 1024**2,
            },
            "permitted_action": action,
            "issued_at": issued,
            "expires_at": expires,
        }
        raw = self.issuer.issue_approval(payload)
        expected = {key: payload[key] for key in (
            "project_instance_id", "source_sha256", "backend_id", "backend_version",
            "vm_identity", "architecture", "operation_id", "launcher_sha256", "media_hashes",
            "resource_limits")}
        return raw, expected, payload

    def execute(self, **kwargs):
        raw, expected, _ = self.approval()
        return self.coordinator.execute_transition(
            expected_state="approved", next_state="attached", action="attach_media",
            target=self.target, raw_approval=raw, approval_expected=expected, **kwargs)

    def _prepare_detached_collection(self, source_root, files, *, target_overrides=None):
        for state in ("attached", "running", "stopped", "detached"):
            previous = self.ledger.get_run(self.run_id)["events"][-1]["state"]
            self.ledger.transition(self.run_id, expected_state=previous, new_state=state,
                                   evidence={"test_fixture_state": state})
        identity = SourceIdentity("fake-temp-source", "a" * 64, 4096, "read_only")
        source = FakeDirectoryCollectionSource(source_root, identity)
        artifacts = {name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                     for name, content in files.items()}
        target = {"source_identity": identity.__dict__, "artifacts": artifacts,
                  "destination_id": f"{self.run_id}/collected"}
        if target_overrides:
            target.update(target_overrides)
        raw, expected, _ = self.approval(target=target, action="collect_output")
        return source, target, raw, expected

    def _collect(self, source, target, raw, expected, **kwargs):
        return self.coordinator.execute_transition(
            expected_state="detached", next_state="collected", action="collect_output",
            target=target, raw_approval=raw, approval_expected=expected,
            collection_source=source, **kwargs)

    def _reset_case(self):
        self.tearDown()
        self.setUp()

    def test_success_orders_approval_intent_action_result_then_hash_linked_transition(self):
        run = self.execute()
        self.assertEqual(run["events"][-1]["state"], "attached")
        event = run["events"][-1]
        self.assertEqual(event["evidence"]["action"], "attach_media")
        self.assertEqual(len(event["evidence"]["evidence_refs"]), 3)
        self.assertTrue(all((self.ledger.run_directory(self.run_id) / ref["relative_path"]).is_file()
                            for ref in event["evidence"]["evidence_refs"]))
        result_ref = next(ref for ref in event["evidence"]["evidence_refs"]
                          if ref["record_type"] == "operation_result")
        result = self.evidence.read_ref(result_ref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_class"], "simulated")
        self.assertLessEqual(result["occurred_at"], result["recorded_at"])
        self.assertLessEqual(result["recorded_at"], event["recorded_at"])
        self.assertEqual(self.backend.calls[0]["action"], "attach_media")
        self.evidence.assert_no_unresolved_operations()

    def test_adding_evidence_does_not_migrate_legacy_journal_schema(self):
        journal_path = self.ledger.run_directory(self.run_id) / "journal.json"
        original = json.loads(journal_path.read_bytes())
        self.assertEqual(original["schema_version"], 1)
        self.assertFalse((self.evidence.root / "collections").exists())
        self.evidence.assert_no_unresolved_operations()
        reopened = DiagnosticRunLedger(
            self.project, self.project / ".local/runs", project_id="localcodingagent")
        read_back = reopened.get_run(self.run_id)
        self.assertEqual(read_back["events"], original["events"])
        self.assertEqual(json.loads(journal_path.read_bytes())["schema_version"], 1)

    def test_missing_expired_and_wrong_target_approvals_never_dispatch(self):
        raw, expected, _ = self.approval()
        with self.assertRaises(TrustContractError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=b"not-an-approval", approval_expected=expected)
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        expired_raw, expired_expected, _ = self.approval(expires=expired)
        with self.assertRaises(TrustContractError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=expired_raw,
                approval_expected=expired_expected)
        wrong_target = dict(self.target, controller="SCSI 0:2")
        with self.assertRaises(TrustContractError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=wrong_target, raw_approval=raw, approval_expected=expected)
        foreign_raw, foreign_expected, _ = self.approval(run_id=uuid.uuid4().hex)
        with self.assertRaises(TrustContractError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=foreign_raw,
                approval_expected=foreign_expected)
        wrong_action_raw, wrong_action_expected, _ = self.approval(action="start_worker")
        with self.assertRaises(TrustContractError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=wrong_action_raw,
                approval_expected=wrong_action_expected)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "approved")

    def test_action_to_state_mismatch_is_rejected_before_approval_or_effect(self):
        raw, expected, _ = self.approval()
        with self.assertRaises(DiagnosticRunError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="running", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(list((self.evidence.root / "approvals").iterdir()), [])

    def test_coordinator_rejects_evidence_store_for_another_run(self):
        other = self.ledger.reserve(uuid.uuid4().hex)
        other_evidence = EvidenceStore(self.ledger, other["run_id"])
        with self.assertRaises(EvidenceError):
            AuditedFakeCoordinator(
                self.ledger, self.run_id, other_evidence, self.trust,
                self.consumption, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_interruption_after_intent_is_detected_and_never_retried(self):
        raw, expected, _ = self.approval()

        def interrupt(stage):
            if stage == "after_intent":
                raise RuntimeError("simulated process interruption")

        with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected,
                fault_hook=interrupt)
        self.assertEqual(self.backend.calls, [])
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        with self.assertRaises(OperationPendingError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(self.backend.calls, [])

    def test_partial_intent_publication_is_detected(self):
        pending = self.evidence.root / "intents" / ".intent.pending-interrupted-write"
        pending.write_bytes(b'{"partial":')
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        with self.assertRaises(RunInterruptedError):
            self.ledger.get_run(self.run_id)

    def test_partial_result_publication_is_detected(self):
        pending = self.evidence.root / "results" / ".result.pending-interrupted-write"
        pending.write_bytes(b'{"partial":')
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        with self.assertRaises(RunInterruptedError):
            self.ledger.get_run(self.run_id)

    def test_ambiguous_after_effect_is_recorded_and_not_replayed(self):
        self.backend = FakeEvidenceBackend(fail="after_effect")
        self.coordinator.backend = self.backend
        raw, expected, _ = self.approval()
        with self.assertRaises(OperationPendingError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "approved")
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        with self.assertRaises(OperationPendingError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(len(self.backend.calls), 1)

    def test_failed_before_effect_is_recorded_without_transition_or_retry(self):
        self.backend = FakeEvidenceBackend(fail="before_effect")
        self.coordinator.backend = self.backend
        raw, expected, _ = self.approval()
        with self.assertRaises(OperationPendingError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "approved")
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        results = list((self.evidence.root / "results").glob("*.json"))
        self.assertEqual(len(results), 1)
        record = json.loads(results[0].read_bytes())
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["attempted"])
        self.assertIsNone(record["occurred_at"])
        self.assertEqual(record["time_observation"]["time_status"], "unavailable")

    def test_operator_report_cannot_satisfy_fake_transition_postcondition(self):
        raw, expected, _ = self.approval()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.backend.perform = lambda **kwargs: ActionObservation(
            occurred_at=now,
            time_observation={
                "time_status": "observed", "clock_source": "fake-test",
                "precision": "simulated-microsecond", "uncertainty_ms": 0,
                "monotonic_ticks": 1, "boot_session_id": "fake-test-session",
            },
            observation={"claim": "operator says action completed"},
            source_class="operator_report")
        with self.assertRaises(OperationPendingError):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "approved")
        result = json.loads(next((self.evidence.root / "results").glob("*.json")).read_bytes())
        self.assertEqual(result["source_class"], "operator_report")

    def test_duplicate_operation_id_cannot_overwrite_intent(self):
        raw, _, payload = self.approval()
        from diagnostic_trust import verify_approval
        verified = verify_approval(raw, self.trust)
        approval_ref = self.evidence.archive_approval(raw, verified)
        operation_id = uuid.uuid4().hex
        self.evidence.write_intent(
            operation_id=operation_id, expected_state="approved", next_state="attached",
            action="attach_media", target=self.target, approval_ref=approval_ref,
            preconditions=[], expected_postconditions=[])
        with self.assertRaises(FileExistsError):
            self.evidence.write_intent(
                operation_id=operation_id, expected_state="approved", next_state="attached",
                action="attach_media", target=self.target, approval_ref=approval_ref,
                preconditions=[], expected_postconditions=[])
        self.assertEqual(payload["approval_id"], verified.payload["approval_id"])

    def test_concurrent_approval_consumption_has_exactly_one_winner(self):
        raw, _, payload = self.approval()
        verified = verify_approval(raw, self.trust)

        def consume():
            try:
                consume_approval(verified, self.consumption,
                                 required_action="attach_media")
                return "consumed"
            except ApprovalReplayError:
                return "replayed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: consume(), range(2)))
        self.assertCountEqual(results, ["consumed", "replayed"])
        self.assertTrue(payload["approval_id"])

    def test_sequential_approval_replay_is_rejected(self):
        raw, _, _ = self.approval()
        verified = verify_approval(raw, self.trust)
        consume_approval(verified, self.consumption, required_action="attach_media")
        with self.assertRaises(ApprovalReplayError):
            consume_approval(verified, self.consumption, required_action="attach_media")

    def test_tampered_evidence_hash_is_rejected(self):
        run = self.execute()
        ref = next(ref for ref in run["events"][-1]["evidence"]["evidence_refs"]
                   if ref["record_type"] == "operation_result")
        path = self.ledger.run_directory(self.run_id) / ref["relative_path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises((EvidenceError, OperationPendingError)):
            self.evidence.read_ref(ref)
        with self.assertRaises((EvidenceError, OperationPendingError)):
            self.evidence.assert_no_unresolved_operations()

    def test_readonly_collection_source_preflight_checks_identity_and_exact_inventory(self):
        identity = SourceIdentity("fake-media", "a" * 64, 1024, "read_only")
        rows = (SourceFileEntry("result.txt", 20),
                SourceFileEntry("output-manifest.json", 80))
        source = FakeReadOnlySource(identity, rows)
        verified = verify_readonly_source_inventory(
            source, expected_identity=identity,
            allowlisted_names=frozenset({"result.txt", "output-manifest.json"}),
            maximum_file_bytes=100, maximum_total_bytes=100)
        self.assertEqual(verified, tuple(sorted(rows, key=lambda row: row.name.casefold())))
        self.assertEqual(source.opened, [])
        with self.assertRaises(EvidenceError):
            verify_readonly_source_inventory(
                source, expected_identity=SourceIdentity("other", "a" * 64, 1024, "read_only"),
                allowlisted_names=frozenset({"result.txt", "output-manifest.json"}),
                maximum_file_bytes=100, maximum_total_bytes=100)
        with self.assertRaises(EvidenceError):
            verify_readonly_source_inventory(
                FakeReadOnlySource(identity, (*rows, SourceFileEntry("extra.bin", 1))),
                expected_identity=identity,
                allowlisted_names=frozenset({"result.txt", "output-manifest.json"}),
                maximum_file_bytes=100, maximum_total_bytes=100)
        with self.assertRaises(EvidenceError):
            verify_readonly_source_inventory(
                FakeReadOnlySource(identity, (
                    SourceFileEntry("result.txt", 20, kind="symlink"), rows[1])),
                expected_identity=identity,
                allowlisted_names=frozenset({"result.txt", "output-manifest.json"}),
                maximum_file_bytes=100, maximum_total_bytes=100)

    def test_uncommitted_completed_result_is_detected(self):
        raw, expected, _ = self.approval()

        def interrupt(stage):
            if stage == "after_result":
                raise RuntimeError("simulated journal interruption")

        with self.assertRaisesRegex(RuntimeError, "journal interruption"):
            self.coordinator.execute_transition(
                expected_state="approved", next_state="attached", action="attach_media",
                target=self.target, raw_approval=raw, approval_expected=expected,
                fault_hook=interrupt)
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "approved")

    def test_fake_collection_success_binds_exact_inventory_and_gates_collected(self):
        source_root = self.base / "source-success"
        source_root.mkdir()
        files = {"result.txt": b"fake result\n", "output-manifest.json": b"{}\n"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        run = self._collect(source, target, raw, expected)
        self.assertEqual(run["events"][-1]["state"], "collected")
        self.assertCountEqual((self.ledger.run_directory(self.run_id) / "collected").iterdir(),
                              [self.ledger.run_directory(self.run_id) / "collected" / name for name in files])
        event = run["events"][-1]
        collection_ref = next(ref for ref in event["evidence"]["evidence_refs"]
                              if ref["record_type"] == "collection_record")
        record = self.evidence.read_ref(collection_ref)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["partial_state"], "complete")
        self.assertEqual(record["allowlisted_filenames"], sorted(files))
        self.assertEqual(record["observed_files"], [
            {"name": name, "size": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
            for name in sorted(files)])
        self.evidence.assert_no_unresolved_operations()

    def test_fake_collection_rejects_missing_and_extra_files_before_collected(self):
        for variant in ("missing", "extra"):
            self._reset_case()
            with self.subTest(variant=variant):
                source_root = self.base / f"source-{variant}"
                source_root.mkdir()
                files = {"result.txt": b"a", "output-manifest.json": b"b"}
                for name, data in files.items():
                    (source_root / name).write_bytes(data)
                if variant == "missing":
                    (source_root / "result.txt").unlink()
                else:
                    (source_root / "unexpected.bin").write_bytes(b"extra")
                source, target, raw, expected = self._prepare_detached_collection(source_root, files)
                with self.assertRaises(OperationPendingError):
                    self._collect(source, target, raw, expected)
                self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "detached")
                with self.assertRaises(OperationPendingError):
                    self.evidence.assert_no_unresolved_operations()

    def test_fake_collection_rejects_altered_content_and_approved_hash_mismatch(self):
        source_root = self.base / "source-altered"
        source_root.mkdir()
        files = {"result.txt": b"expected", "output-manifest.json": b"manifest"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        (source_root / "result.txt").write_bytes(b"changed!")
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        with self.assertRaises(OperationPendingError):
            self._collect(source, target, raw, expected)
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "detached")

        self._reset_case()
        source_root2 = self.base / "source-hash-mismatch"
        source_root2.mkdir()
        for name, data in files.items():
            (source_root2 / name).write_bytes(data)
        source2, target2, raw2, expected2 = self._prepare_detached_collection(source_root2, files)
        target2["artifacts"]["result.txt"]["sha256"] = "f" * 64
        # The target mutation is rejected by the signed exact-target binding.
        with self.assertRaises(TrustContractError):
            self._collect(source2, target2, raw2, expected2)

    def test_fake_collection_rejects_symlink_source_entry(self):
        source_root = self.base / "source-link"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        linked = self.base / "link-target"
        linked.write_bytes(b"a")
        (source_root / "result.txt").unlink()
        try:
            (source_root / "result.txt").symlink_to(linked)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable in this test environment.")
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        with self.assertRaises(OperationPendingError):
            self._collect(source, target, raw, expected)
        self.assertFalse((self.ledger.run_directory(self.run_id) / "collected" / "result.txt").exists())

    def test_fake_collection_preserves_partial_copy_and_forbids_retry(self):
        source_root = self.base / "source-interrupted"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)

        def interrupt(stage):
            if stage == "after_collection_file_1":
                raise RuntimeError("injected copy interruption")

        with self.assertRaises(OperationPendingError):
            self._collect(source, target, raw, expected, fault_hook=interrupt)
        collected = self.ledger.run_directory(self.run_id) / "collected"
        self.assertTrue(collected.is_dir())
        self.assertEqual(len(list(collected.iterdir())), 1)
        with self.assertRaises(OperationPendingError):
            self.evidence.assert_no_unresolved_operations()
        with self.assertRaises(OperationPendingError):
            self._collect(source, target, raw, expected)

    def test_fake_collection_refuses_existing_destination_and_duplicate_attempt(self):
        source_root = self.base / "source-existing-dest"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        destination = self.ledger.run_directory(self.run_id) / "collected"
        destination.mkdir()
        with self.assertRaises(OperationPendingError):
            self._collect(source, target, raw, expected)
        self.assertEqual(list(destination.iterdir()), [])


    def test_successful_collection_cannot_be_replayed(self):
        source_root = self.base / "source-replay"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        self._collect(source, target, raw, expected)
        with self.assertRaises((OperationPendingError, DiagnosticRunError)):
            self._collect(source, target, raw, expected)

    def test_collect_transition_requires_source_and_exact_lifecycle_state(self):
        source_root = self.base / "source-requirements"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        with self.assertRaises(EvidenceError):
            self.coordinator.execute_transition(
                expected_state="detached", next_state="collected", action="collect_output",
                target=target, raw_approval=raw, approval_expected=expected)
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "detached")

    def test_collection_evidence_tampering_is_detected(self):
        source_root = self.base / "source-tamper"
        source_root.mkdir()
        files = {"result.txt": b"a", "output-manifest.json": b"b"}
        for name, data in files.items():
            (source_root / name).write_bytes(data)
        source, target, raw, expected = self._prepare_detached_collection(source_root, files)
        run = self._collect(source, target, raw, expected)
        ref = next(ref for ref in run["events"][-1]["evidence"]["evidence_refs"]
                   if ref["record_type"] == "collection_record")
        path = self.ledger.run_directory(self.run_id) / ref["relative_path"]
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(EvidenceError):
            self.evidence.read_ref(ref)
        with self.assertRaises(EvidenceError):
            self.evidence.assert_no_unresolved_operations()


if __name__ == "__main__":
    unittest.main()
