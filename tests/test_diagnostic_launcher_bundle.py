import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest import mock

import diagnostic_launcher_bundle as builder
import diagnostic_bundle
import ubuntu_diagnostic_launcher as launcher
from diagnostic_bundle import OutputPolicy, validate_output
from diagnostic_runs import DiagnosticRunLedger


class DiagnosticLauncherBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ledger = DiagnosticRunLedger(self.project, self.project / ".local/runs",
                                          project_id=launcher.PROJECT_ID)
        self.run_id = uuid.uuid4().hex
        self.run = self.ledger.reserve(self.run_id)
        self.source = self.project / "source"
        self.source.mkdir()
        self.sentinel = b"fixed inert sentinel for v2 bundle\n"
        (self.source / "sentinel.txt").write_bytes(self.sentinel)
        self.destination = self.ledger.run_directory(self.run_id) / "input"

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        return builder.build_sentinel_bundle(self.source, self.destination,
                                             ledger=self.ledger, run_id=self.run_id)

    def finish_collection(self):
        states = ("awaiting_approval", "approved", "attached", "running", "stopped",
                  "detached", "collected")
        previous = "prepared"
        for state in states:
            self.ledger.transition(self.run_id, expected_state=previous, new_state=state,
                                   evidence={"stage": state})
            previous = state

    def test_build_inventory_is_deterministic_and_run_bound(self):
        report = self.build()
        self.assertEqual([row["path"] for row in report["files"]],
                         ["manifest.json", "sentinel.txt"])
        self.assertEqual(report["run_id"], self.run_id)
        manifest = json.loads((self.destination / "manifest.json").read_bytes())
        self.assertEqual(manifest["run_nonce"], self.run["run_nonce"])
        self.assertEqual(manifest["launcher_sha256"], launcher.launcher_sha256())
        self.assertEqual(builder.validate_sentinel_bundle(self.destination, ledger=self.ledger,
                                                           expected_run_id=self.run_id), report)

    def test_dry_run_round_trips_through_fixed_launcher_and_host_validator(self):
        report = self.build()
        output = self.ledger.run_directory(self.run_id) / "collected"
        output.mkdir()
        result = launcher.run(self.destination, output, expected_run_id=self.run_id,
                              expected_run_nonce=self.run["run_nonce"])
        self.assertEqual(result["exit_status"], 0)
        self.finish_collection()
        policy = OutputPolicy(
            frozenset({launcher.RESULT_NAME}), policy_version=2,
            manifest_schema_version=2, expected_operation_id=launcher.OPERATION_ID,
            expected_launcher_version=launcher.LAUNCHER_VERSION,
            expected_launcher_sha256=launcher.launcher_sha256(),
            expected_input_manifest_sha256=report["manifest_sha256"],
            expected_input_hashes=(("sentinel.txt", len(self.sentinel),
                                    hashlib.sha256(self.sentinel).hexdigest()),))
        receipt = validate_output(output, expected_run_id=self.run_id, policy=policy,
                                  receipt_path=self.ledger.receipt_path(self.run_id),
                                  ledger=self.ledger)
        self.assertEqual(receipt["outcome"], "validated")
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "validated")

    def test_replay_and_destination_collision_do_not_overwrite(self):
        self.build()
        original = (self.destination / "sentinel.txt").read_bytes()
        with self.assertRaises(builder.LauncherBundleError):
            self.build()
        self.assertEqual((self.destination / "sentinel.txt").read_bytes(), original)

    def test_unexpected_source_file_and_malformed_project_are_rejected(self):
        (self.source / "extra").write_text("unexpected")
        with self.assertRaises(builder.LauncherBundleError):
            self.build()
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "reserved")
        (self.source / "extra").unlink()
        bad = DiagnosticRunLedger(self.project, self.project / ".local/other-runs",
                                  project_id="other-project")
        other_run = bad.reserve()
        with self.assertRaises(builder.LauncherBundleError):
            builder.build_sentinel_bundle(self.source,
                bad.run_directory(other_run["run_id"]) / "input", ledger=bad,
                run_id=other_run["run_id"])

    def test_path_traversal_outside_source_and_stale_run_rejected(self):
        with self.assertRaises(builder.LauncherBundleError):
            builder.build_sentinel_bundle(self.source, self.project / ".." / "escape",
                ledger=self.ledger, run_id=self.run_id)
        external = self.root / "external"
        external.mkdir()
        (external / "sentinel.txt").write_bytes(self.sentinel)
        with self.assertRaises(builder.LauncherBundleError):
            builder.build_sentinel_bundle(external, self.destination,
                ledger=self.ledger, run_id=self.run_id)
        self.build()
        with self.assertRaises(builder.LauncherBundleError):
            self.build()

    def test_malformed_or_tampered_manifest_and_payload_fail_revalidation(self):
        self.build()
        manifest_path = self.destination / "manifest.json"
        original = manifest_path.read_bytes()
        data = json.loads(original)
        data["files"].append(data["files"][0])
        manifest_path.write_bytes(launcher.canonical_json(data))
        with self.assertRaises(builder.LauncherBundleError):
            builder.validate_sentinel_bundle(self.destination, ledger=self.ledger,
                                              expected_run_id=self.run_id)
        manifest_path.write_bytes(original)
        (self.destination / "sentinel.txt").write_bytes(b"tampered")
        with self.assertRaises(builder.LauncherBundleError):
            builder.validate_sentinel_bundle(self.destination, ledger=self.ledger,
                                              expected_run_id=self.run_id)

    def test_launcher_pin_mismatch_rejected_before_lifecycle_change(self):
        with mock.patch.object(builder, "PINNED_LAUNCHER_SHA256", "0" * 64):
            with self.assertRaises(builder.LauncherBundleError):
                self.build()
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "reserved")
        self.assertFalse(self.destination.exists())

    def test_interrupted_final_publication_is_incomplete_and_not_trusted(self):
        original_writer = builder._write_new_file
        calls = 0
        def fail_on_final_manifest(path, data):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("injected partial publication")
            return original_writer(path, data)
        with mock.patch.object(builder, "_write_new_file", side_effect=fail_on_final_manifest):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "incomplete")
        self.assertTrue((self.destination / "sentinel.txt").exists())
        with self.assertRaises(builder.LauncherBundleError):
            builder.validate_sentinel_bundle(self.destination, ledger=self.ledger,
                                               expected_run_id=self.run_id)

    def test_input_symlink_is_rejected_when_platform_allows_creation(self):
        target = self.source / "sentinel.txt"
        target.unlink()
        external = self.root / "target.txt"
        external.write_bytes(self.sentinel)
        try:
            target.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(builder.LauncherBundleError):
            self.build()


if __name__ == "__main__":
    unittest.main()
