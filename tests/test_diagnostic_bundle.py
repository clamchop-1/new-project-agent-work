import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from diagnostic_bundle import (
    DiagnosticBundleError,
    OUTPUT_POLICY_VERSION,
    OutputPolicy,
    ScriptAllowlistEntry,
    build_bundle,
    validate_bundle,
    validate_output,
)
from diagnostic_runs import (
    DiagnosticRunError,
    DiagnosticRunLedger,
    RunInterruptedError,
    RunJournalCorruptError,
)


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ledger = DiagnosticRunLedger(
            self.project, self.project / ".local" / "diagnostic-runs", project_id="test-project")
        self.run_id = uuid.uuid4().hex
        self.ledger.reserve(self.run_id)
        self.receipt = self.ledger.receipt_path(self.run_id)

    def tearDown(self):
        self.temporary.cleanup()

    def make_output(self, *, run_id=None, run_nonce=None, files=None, manifest_bytes=None):
        self._new_collected_run()
        output = self.ledger.run_directory(self.run_id) / "collected"
        output.mkdir()
        files = files or {"result.txt": b"diagnostic complete\n"}
        rows = []
        for name, data in files.items():
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            rows.append({"path": name, "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
        if manifest_bytes is None:
            manifest_bytes = self.canonical_json({"schema_version": 1,
                                                   "run_id": run_id or self.run_id,
                                                   "run_nonce": run_nonce or self.ledger.get_run(
                                                       self.run_id)["run_nonce"],
                                                   "files": rows})
        (output / "output-manifest.json").write_bytes(manifest_bytes)
        return output

    def _new_collected_run(self):
        self.run_id = self.ledger.reserve()["run_id"]
        self.receipt = self.ledger.receipt_path(self.run_id)
        states = ("preparing", "prepared", "awaiting_approval", "approved", "attached",
                  "running", "stopped", "detached")
        previous = "reserved"
        for state in states:
            self.ledger.transition(self.run_id, expected_state=previous, new_state=state,
                                   evidence={"stage": state})
            previous = state
        return self.ledger.transition(self.run_id, expected_state="detached", new_state="collected",
                                      evidence={"staging_inventory": "ready"})

    @staticmethod
    def canonical_json(value):
        return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    def validate(self, output, *, allowed=("result.txt",), **limits):
        policy = OutputPolicy(frozenset(allowed), **limits)
        return validate_output(output, expected_run_id=self.run_id,
                               policy=policy, receipt_path=self.receipt, ledger=self.ledger)

    def test_builder_copies_only_hash_pinned_allowlisted_script(self):
        source = self.project / "inventory.sh"
        content = b"#!/bin/sh\nprintf 'inventory\\n'\n"
        source.write_bytes(content)
        destination = self.ledger.run_directory(self.run_id) / "input"
        manifest = build_bundle(destination,
                                (ScriptAllowlistEntry("diagnostics/inventory.sh", source,
                                                      hashlib.sha256(content).hexdigest()),),
                                run_id=self.run_id, ledger=self.ledger)
        self.assertEqual(manifest["run_id"], self.run_id)
        self.assertEqual((destination / "diagnostics/inventory.sh").read_bytes(), content)
        self.assertEqual(json.loads((destination / "manifest.json").read_text())["files"][0]["sha256"],
                         hashlib.sha256(content).hexdigest())
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "prepared")
        verified = validate_bundle(destination, ledger=self.ledger, expected_run_id=self.run_id)
        self.assertEqual(verified["manifest_sha256"], manifest["manifest_sha256"])

    def test_builder_rejects_hash_mismatch_and_preserves_no_bundle(self):
        source = self.project / "inventory.sh"
        source.write_bytes(b"echo safe\n")
        destination = self.ledger.run_directory(self.run_id) / "input"
        with self.assertRaises(DiagnosticBundleError):
            build_bundle(destination,
                         (ScriptAllowlistEntry("inventory.sh", source, "0" * 64),),
                         run_id=self.run_id, ledger=self.ledger)
        self.assertFalse(destination.exists())

    def test_builder_rejects_traversal_duplicate_and_oversized_script(self):
        source = self.project / "inventory.sh"
        source.write_bytes(b"12345")
        pinned = hashlib.sha256(source.read_bytes()).hexdigest()
        for name in ("../escape.sh", "nested/../../escape.sh", "Inventory.sh", "Manifest.json"):
            with self.subTest(name=name), self.assertRaises(DiagnosticBundleError):
                build_bundle(self.ledger.run_directory(self.run_id) / "input",
                             (ScriptAllowlistEntry(name, source, pinned),
                              ScriptAllowlistEntry("inventory.sh", source, pinned)),
                             max_file_bytes=4, run_id=self.run_id, ledger=self.ledger)

    def test_output_with_matching_manifest_and_hash_gets_receipt(self):
        output = self.make_output()
        receipt = self.validate(output)
        self.assertEqual(receipt["status"], "validated")
        self.assertEqual(receipt["outcome"], "validated")
        self.assertEqual(receipt["policy_version"], OUTPUT_POLICY_VERSION)
        self.assertEqual(receipt["artifact_hashes"]["result.txt"], hashlib.sha256(
            b"diagnostic complete\n").hexdigest())
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "validated")
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "validated")

    def test_rejects_wrong_run_id_and_records_failure(self):
        output = self.make_output(run_id=uuid.uuid4().hex)
        receipt = self.validate(output)
        self.assertEqual(receipt["status"], "rejected")
        self.assertIn("run ID", receipt["failures"][0])
        self.assertEqual(json.loads(self.receipt.read_text())["verified_artifacts"], [])

    def test_rejects_malformed_json_duplicate_json_keys_and_duplicate_paths(self):
        malformed = self.make_output(manifest_bytes=b"{")
        self.assertEqual(self.validate(malformed)["status"], "rejected")

        duplicate_keys = (b'{"schema_version":1,"run_id":"' + self.run_id.encode()
                          + b'","run_id":"' + self.run_id.encode()
                          + b'","files":[]}')
        output = self.make_output(manifest_bytes=duplicate_keys)
        self.assertIn("duplicate JSON", self.validate(output)["failures"][0])

        output = self.make_output(files={"result.txt": b"x"})
        rows = [{"path": "result.txt", "size": 1, "sha256": "0" * 64}] * 2
        duplicate_paths = self.canonical_json({"schema_version": 1, "run_id": self.run_id,
                                               "run_nonce": self.ledger.get_run(self.run_id)["run_nonce"],
                                               "files": rows})
        (output / "output-manifest.json").write_bytes(duplicate_paths)
        self.assertIn("duplicate paths", self.validate(output)["failures"][0])

    def test_rejects_valid_but_noncanonical_manifest_encoding(self):
        output = self.make_output(manifest_bytes=json.dumps({
            "schema_version": 1,
            "run_id": self.run_id,
            "files": [{"path": "result.txt", "size": 21,
                       "sha256": hashlib.sha256(b"diagnostic complete\n").hexdigest()}],
        }, indent=2).encode("utf-8"))
        self.assertIn("canonical", self.validate(output)["failures"][0])

    def test_rejects_traversal_undeclared_files_and_hash_mismatch(self):
        traversal = self.make_output(files={"result.txt": b"x"})
        manifest = {"schema_version": 1, "run_id": self.run_id,
                    "run_nonce": self.ledger.get_run(self.run_id)["run_nonce"],
                    "files": [{"path": "../escape", "size": 1, "sha256": "0" * 64}]}
        (traversal / "output-manifest.json").write_bytes(self.canonical_json(manifest))
        self.assertEqual(self.validate(traversal)["status"], "rejected")

        undeclared = self.make_output(files={"result.txt": b"x", "extra.txt": b"y"})
        (undeclared / "output-manifest.json").write_bytes(self.canonical_json({
            "schema_version": 1,
            "run_id": self.run_id,
            "run_nonce": self.ledger.get_run(self.run_id)["run_nonce"],
            "files": [{"path": "result.txt", "size": 1,
                       "sha256": hashlib.sha256(b"x").hexdigest()}],
        }))
        self.assertIn("undeclared", self.validate(undeclared, allowed=("result.txt", "extra.txt"))["failures"][0])

        hash_mismatch = self.make_output(files={"result.txt": b"actual"})
        rows = [{"path": "result.txt", "size": 6, "sha256": "0" * 64}]
        (hash_mismatch / "output-manifest.json").write_bytes(
            self.canonical_json({"schema_version": 1, "run_id": self.run_id,
                                 "run_nonce": self.ledger.get_run(self.run_id)["run_nonce"],
                                 "files": rows}))
        self.assertIn("hash", self.validate(hash_mismatch)["failures"][0])

    def test_rejects_symbolic_links_hard_links_and_unexpected_directories(self):
        output = self.make_output()
        link = output / "linked.txt"
        try:
            link.symlink_to(output / "result.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic links are unavailable on this host.")
        self.assertEqual(self.validate(output, allowed=("result.txt", "linked.txt"))["status"], "rejected")
        link.unlink()

        output = self.make_output()
        hardlink = output / "hardlink.txt"
        try:
            os.link(output / "result.txt", hardlink)
        except OSError:
            self.skipTest("Hard links are unavailable on this filesystem.")
        self.assertEqual(self.validate(output, allowed=("result.txt", "hardlink.txt"))["status"], "rejected")
        hardlink.unlink()

        output = self.make_output()
        unexpected = output / "folder"
        unexpected.mkdir()
        self.assertEqual(self.validate(output)["status"], "rejected")

    def test_enforces_file_count_individual_and_total_size_limits(self):
        output = self.make_output(files={"result.txt": b"12345", "second.txt": b"678"})
        too_many_files = self.validate(output, allowed=("result.txt",), max_files=1)
        self.assertEqual(too_many_files["status"], "rejected")
        output = self.make_output(files={"result.txt": b"12345", "second.txt": b"678"})
        too_big_file = self.validate(output, allowed=("result.txt", "second.txt"),
                                     max_file_bytes=4)
        self.assertEqual(too_big_file["status"], "rejected")
        output = self.make_output(files={"result.txt": b"123", "second.txt": b"456"})
        too_big_total = self.validate(output, allowed=("result.txt", "second.txt"),
                                      max_file_bytes=5, max_total_bytes=5)
        self.assertIn("total size", too_big_total["failures"][0])

    def test_input_bundle_tampering_is_detected_after_preparation(self):
        source = self.project / "inventory.sh"
        source.write_bytes(b"printf safe\n")
        destination = self.ledger.run_directory(self.run_id) / "input"
        build_bundle(destination,
                     (ScriptAllowlistEntry("inventory.sh", source,
                                           hashlib.sha256(source.read_bytes()).hexdigest()),),
                     ledger=self.ledger, run_id=self.run_id)
        (destination / "inventory.sh").write_bytes(b"printf changed\n")
        with self.assertRaises(DiagnosticBundleError):
            validate_bundle(destination, ledger=self.ledger, expected_run_id=self.run_id)

    def test_output_file_tampering_is_receipted_as_rejected(self):
        output = self.make_output()
        (output / "result.txt").write_bytes(b"DIAGNOSTIC COMPLETE\n")
        receipt = self.validate(output)
        self.assertEqual(receipt["outcome"], "rejected")
        self.assertIn("result.txt", receipt["artifact_hashes"])
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "rejected")

    def test_old_run_nonce_rejects_stale_output_in_a_new_run(self):
        self.make_output()
        previous_nonce = self.ledger.get_run(self.run_id)["run_nonce"]
        stale_output = self.make_output(run_nonce=previous_nonce)
        receipt = self.validate(stale_output)
        self.assertEqual(receipt["outcome"], "rejected")
        self.assertIn("stale", receipt["failures"][0])

    def test_validated_output_cannot_be_replayed_or_overwrite_its_receipt(self):
        output = self.make_output()
        self.assertEqual(self.validate(output)["outcome"], "validated")
        original = self.receipt.read_bytes()
        with self.assertRaises(DiagnosticBundleError):
            self.validate(output)
        self.assertEqual(self.receipt.read_bytes(), original)

    def test_receipt_collision_never_overwrites_existing_evidence(self):
        output = self.make_output()
        self.receipt.write_bytes(b"operator-owned receipt\n")
        with self.assertRaises(RunInterruptedError):
            self.validate(output)
        self.assertEqual(self.receipt.read_bytes(), b"operator-owned receipt\n")
        journal = json.loads((self.ledger.run_directory(self.run_id) / "journal.json").read_text())
        self.assertEqual(journal["events"][-1]["state"], "collected")

    def test_receipt_tampering_is_detected_by_terminal_journal_hash(self):
        output = self.make_output()
        self.validate(output)
        receipt = json.loads(self.receipt.read_text())
        receipt["artifact_hashes"]["result.txt"] = "0" * 64
        self.receipt.write_bytes(self.canonical_json(receipt))
        with self.assertRaises(RunJournalCorruptError):
            self.ledger.get_run(self.run_id)

    def test_receipt_published_before_terminal_journal_is_recovered_incomplete(self):
        from unittest.mock import patch

        output = self.make_output()
        original_transition = self.ledger.transition

        def interrupt_terminal(run_id, *, expected_state, new_state, evidence):
            if new_state in {"validated", "rejected"}:
                raise OSError("simulated power loss after receipt publication")
            return original_transition(run_id, expected_state=expected_state,
                                       new_state=new_state, evidence=evidence)

        with patch.object(self.ledger, "transition", side_effect=interrupt_terminal):
            with self.assertRaises(OSError):
                self.validate(output)
        with self.assertRaises(RunInterruptedError):
            self.ledger.get_run(self.run_id)
        recovered = self.ledger.recover_incomplete(
            self.run_id, operator_id="test-operator", reason="receipt/journal commit interrupted")
        self.assertEqual(recovered["events"][-1]["state"], "incomplete")
        self.assertTrue(self.receipt.exists())
        with self.assertRaises(DiagnosticBundleError):
            self.validate(output)

    def test_partial_bundle_write_consumes_run_as_incomplete(self):
        from unittest.mock import patch

        source = self.project / "inventory.sh"
        source.write_bytes(b"printf safe\n")
        destination = self.ledger.run_directory(self.run_id) / "input"
        entry = ScriptAllowlistEntry("inventory.sh", source,
                                     hashlib.sha256(source.read_bytes()).hexdigest())
        with patch("diagnostic_bundle._write_new_file", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                build_bundle(destination, (entry,), ledger=self.ledger, run_id=self.run_id)
        run = self.ledger.get_run(self.run_id)
        self.assertEqual(run["events"][-1]["state"], "incomplete")
        self.assertTrue(any(item.startswith("unexpected:.input.partial-")
                            for item in run["integrity_warnings"]))
        with self.assertRaises(DiagnosticRunError):
            self.ledger.reserve(self.run_id)

    def test_interrupted_journal_is_not_trusted_until_explicit_recovery(self):
        pending = self.ledger.run_directory(self.run_id) / "journal.json.pending-crash"
        pending.write_bytes(b'{"partial":')
        with self.assertRaises(RunInterruptedError):
            self.ledger.get_run(self.run_id)
        recovered = self.ledger.recover_incomplete(
            self.run_id, operator_id="test-operator", reason="simulated process interruption")
        self.assertEqual(recovered["events"][-1]["state"], "incomplete")
        self.assertTrue(recovered["integrity_warnings"])
        with self.assertRaises(DiagnosticRunError):
            self.ledger.transition(self.run_id, expected_state="incomplete", new_state="preparing",
                                   evidence={"attempt": "resume"})

    def test_missing_journal_can_only_be_recovered_as_incomplete(self):
        journal = self.ledger.run_directory(self.run_id) / "journal.json"
        journal.unlink()
        with self.assertRaises(RunInterruptedError):
            self.ledger.get_run(self.run_id)
        recovered = self.ledger.recover_incomplete(
            self.run_id, operator_id="test-operator", reason="journal missing after interruption")
        self.assertEqual(recovered["events"][0]["state"], "incomplete")

    def test_corrupted_journal_fails_closed_without_automatic_recovery(self):
        journal = self.ledger.run_directory(self.run_id) / "journal.json"
        journal.write_bytes(b'{"run_id":"tampered"}\n')
        with self.assertRaises(RunJournalCorruptError):
            self.ledger.get_run(self.run_id)
        with self.assertRaises(RunJournalCorruptError):
            self.ledger.recover_incomplete(
                self.run_id, operator_id="test-operator", reason="must not bless corruption")

    def test_run_id_remains_reserved_if_run_directory_disappears(self):
        import shutil

        with self.assertRaises(DiagnosticRunError):
            self.ledger.reserve(self.run_id)
        shutil.rmtree(self.ledger.run_directory(self.run_id))
        with self.assertRaises(DiagnosticRunError):
            self.ledger.reserve(self.run_id)
        recovered = self.ledger.recover_incomplete(
            self.run_id, operator_id="test-operator", reason="run directory disappeared")
        self.assertEqual(recovered["events"][-1]["state"], "incomplete")

    def test_lifecycle_rejects_skipped_or_stale_transitions(self):
        with self.assertRaises(DiagnosticRunError):
            self.ledger.reserve("")
        with self.assertRaises(DiagnosticRunError):
            self.ledger.transition(self.run_id, expected_state="reserved", new_state="prepared",
                                   evidence={"stage": "skip-preparing"})
        self.ledger.transition(self.run_id, expected_state="reserved", new_state="preparing",
                               evidence={"stage": "preparing"})
        with self.assertRaises(DiagnosticRunError):
            self.ledger.transition(self.run_id, expected_state="reserved", new_state="prepared",
                                   evidence={"stage": "stale-write"})

    def test_interrupted_reservation_id_cannot_be_reused(self):
        run_id = uuid.uuid4().hex
        pending = self.ledger.reservations_dir / ("." + run_id + ".json.pending-crash")
        pending.write_bytes(b'{"partial":')
        with self.assertRaises(RunInterruptedError):
            self.ledger.reserve(run_id)
        recovered = self.ledger.recover_incomplete(
            run_id, operator_id="test-operator", reason="reservation publication interrupted")
        self.assertEqual(recovered["events"][-1]["state"], "incomplete")
        with self.assertRaises(DiagnosticRunError):
            self.ledger.reserve(run_id)

    def test_cross_project_sources_and_output_paths_are_rejected(self):
        other_project = self.root / "other-project"
        other_project.mkdir()
        other_source = other_project / "outside.sh"
        other_source.write_bytes(b"outside project\n")
        destination = self.ledger.run_directory(self.run_id) / "input"
        with self.assertRaises(DiagnosticBundleError):
            build_bundle(destination,
                         (ScriptAllowlistEntry("outside.sh", other_source,
                                               hashlib.sha256(other_source.read_bytes()).hexdigest()),),
                         ledger=self.ledger, run_id=self.run_id)

        output = self.make_output()
        foreign_output = other_project / "collected"
        foreign_output.mkdir()
        with self.assertRaises(DiagnosticBundleError):
            validate_output(foreign_output, expected_run_id=self.run_id,
                            policy=OutputPolicy(frozenset({"result.txt"})),
                            receipt_path=self.receipt, ledger=self.ledger)

    def test_ledger_storage_cannot_be_reopened_as_a_different_project(self):
        with self.assertRaises(DiagnosticRunError):
            DiagnosticRunLedger(self.project, self.project / ".local" / "diagnostic-runs",
                                project_id="another-project")

    def test_output_policy_requires_exact_inventory_and_safe_windows_names(self):
        output = self.make_output()
        receipt = self.validate(output, allowed=("result.txt", "optional.txt"))
        self.assertEqual(receipt["outcome"], "rejected")
        self.assertIn("exact host policy inventory", receipt["failures"][0])
        with self.assertRaises(DiagnosticBundleError):
            OutputPolicy(frozenset({"CON.txt"}))
        with self.assertRaises(DiagnosticBundleError):
            OutputPolicy(frozenset({"Output-Manifest.json"}))


if __name__ == "__main__":
    unittest.main()
