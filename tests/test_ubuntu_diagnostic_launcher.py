import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import diagnostic_bundle
import ubuntu_diagnostic_launcher as launcher
from diagnostic_bundle import OutputPolicy, validate_output
from diagnostic_runs import DiagnosticRunLedger


class UbuntuDiagnosticLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.ledger = DiagnosticRunLedger(
            self.project, self.project / ".local" / "diagnostic-runs",
            project_id=launcher.PROJECT_ID)
        self.run_id = uuid.uuid4().hex
        self.run = self.ledger.reserve(self.run_id)
        self.run_nonce = self.run["run_nonce"]
        self.input_root = self.root / "offline-input"
        self.input_root.mkdir()
        self.output_root = self.ledger.run_directory(self.run_id) / "collected"
        self.output_root.mkdir()
        self.sentinel = b"inert sentinel bytes; never executable\n"
        self.write_input(self.sentinel)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def canonical(value):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    def write_input(self, sentinel, *, run_id=None, nonce=None, operation=None,
                    launcher_hash=None, version=None, files=None, limits=None,
                    raw_manifest=None):
        sentinel_path = self.input_root / "sentinel.txt"
        if sentinel_path.exists():
            sentinel_path.unlink()
        sentinel_path.write_bytes(sentinel)
        manifest = {
            "schema": launcher.INPUT_SCHEMA,
            "schema_version": launcher.INPUT_SCHEMA_VERSION,
            "project_id": launcher.PROJECT_ID,
            "run_id": self.run_id if run_id is None else run_id,
            "run_nonce": self.run_nonce if nonce is None else nonce,
            "operation_id": launcher.OPERATION_ID if operation is None else operation,
            "launcher_version": launcher.LAUNCHER_VERSION if version is None else version,
            "launcher_sha256": launcher.launcher_sha256() if launcher_hash is None else launcher_hash,
            "limits": limits or {"max_files": 1,
                                 "max_file_bytes": launcher.MAX_INPUT_BYTES,
                                 "max_total_bytes": launcher.MAX_TOTAL_INPUT_BYTES},
            "files": files or [{"path": "sentinel.txt", "size": len(sentinel),
                                "sha256": hashlib.sha256(sentinel).hexdigest()}],
        }
        encoded = self.canonical(manifest) if raw_manifest is None else raw_manifest
        (self.input_root / launcher.MANIFEST_NAME).write_bytes(encoded)
        return manifest, encoded

    def invoke(self):
        return launcher.run(self.input_root, self.output_root,
                            expected_run_id=self.run_id,
                            expected_run_nonce=self.run_nonce)

    def make_collected(self):
        states = ("preparing", "prepared", "awaiting_approval", "approved", "attached",
                  "running", "stopped", "detached", "collected")
        previous = "reserved"
        for state in states:
            self.ledger.transition(self.run_id, expected_state=previous, new_state=state,
                                   evidence={"stage": state})
            previous = state

    def output_policy(self, input_manifest, manifest_raw, *, expected_hash=None):
        return OutputPolicy(
            frozenset({launcher.RESULT_NAME}),
            policy_version=2,
            manifest_schema_version=2,
            expected_operation_id=launcher.OPERATION_ID,
            expected_launcher_version=launcher.LAUNCHER_VERSION,
            expected_launcher_sha256=launcher.launcher_sha256(),
            expected_input_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            expected_input_hashes=(("sentinel.txt", len(self.sentinel),
                                    expected_hash or hashlib.sha256(self.sentinel).hexdigest()),),
        )

    def test_host_side_dry_run_passes_existing_v2_output_validator(self):
        input_manifest, raw_manifest = self.write_input(self.sentinel)
        result = self.invoke()
        self.assertEqual(result["exit_status"], 0)
        self.assertEqual(result["operation_id"], launcher.OPERATION_ID)
        self.assertEqual(result["launcher_sha256"], launcher.launcher_sha256())
        self.assertEqual((self.output_root / launcher.RESULT_NAME).exists(), True)
        self.assertNotIn(self.sentinel.decode("ascii").strip(),
                         (self.output_root / launcher.RESULT_NAME).read_text())

        self.make_collected()
        receipt = validate_output(
            self.output_root, expected_run_id=self.run_id,
            policy=self.output_policy(input_manifest, raw_manifest),
            receipt_path=self.ledger.receipt_path(self.run_id), ledger=self.ledger)
        self.assertEqual(receipt["outcome"], "validated")
        self.assertEqual(self.ledger.get_run(self.run_id)["events"][-1]["state"], "validated")

    def test_wrong_expected_run_or_nonce_is_rejected_before_output(self):
        self.write_input(self.sentinel)
        with self.assertRaises(launcher.LauncherError):
            launcher.run(self.input_root, self.output_root,
                         expected_run_id="0" * 32,
                         expected_run_nonce=self.run_nonce)
        with self.assertRaises(launcher.LauncherError):
            launcher.run(self.input_root, self.output_root,
                         expected_run_id=self.run_id,
                         expected_run_nonce="f" * 32)
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_malformed_duplicate_noncanonical_and_wrong_version_manifests_fail_closed(self):
        for bad_raw in (b"{\"a\":1,\"a\":1}\n", b"{ \"schema\": \"x\" }\n"):
            with self.subTest(raw=bad_raw), self.assertRaises(launcher.LauncherError):
                self.write_input(self.sentinel, raw_manifest=bad_raw)
                self.invoke()
        self.write_input(self.sentinel, version=999)
        with self.assertRaises(launcher.LauncherError):
            self.invoke()
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_operation_launcher_hash_and_inventory_are_fixed(self):
        for kwargs in (
            {"operation": "arbitrary-shell-v1"},
            {"launcher_hash": "0" * 64},
            {"files": [{"path": "script.py", "size": 1, "sha256": "0" * 64}]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(launcher.LauncherError):
                self.write_input(self.sentinel, **kwargs)
                self.invoke()
            self.output_root.mkdir(exist_ok=True)
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_cross_project_manifest_is_rejected(self):
        self.write_input(self.sentinel)
        manifest = json.loads((self.input_root / launcher.MANIFEST_NAME).read_bytes())
        manifest["project_id"] = "another-project"
        (self.input_root / launcher.MANIFEST_NAME).write_bytes(self.canonical(manifest))
        with self.assertRaises(launcher.LauncherError):
            self.invoke()
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_tampered_unexpected_and_oversized_input_fail_without_writes(self):
        self.write_input(self.sentinel)
        (self.input_root / "sentinel.txt").write_bytes(b"tampered")
        with self.assertRaises(launcher.LauncherError):
            self.invoke()

        self.write_input(self.sentinel)
        (self.input_root / "extra.txt").write_bytes(b"unexpected")
        with self.assertRaises(launcher.LauncherError):
            self.invoke()
        (self.input_root / "extra.txt").unlink()

        oversized = b"x" * (launcher.MAX_INPUT_BYTES + 1)
        self.write_input(oversized)
        with self.assertRaises(launcher.LauncherError):
            self.invoke()
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_output_directory_is_create_new_and_cli_has_no_device_or_command_flags(self):
        marker = self.output_root / launcher.RESULT_NAME
        marker.write_bytes(b"preserve")
        self.write_input(self.sentinel)
        with self.assertRaises(launcher.LauncherError):
            self.invoke()
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(launcher.main(["--run-id", self.run_id, "--run-nonce",
                                        self.run_nonce, "--device", "/dev/sdb"]), 64)

    def test_launcher_source_has_no_process_or_dynamic_code_execution_apis(self):
        tree = ast.parse(Path(launcher.__file__).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("importlib", imported)
        self.assertNotIn("exec", called)
        self.assertNotIn("eval", called)

    def test_host_policy_mismatch_rejects_output_manifest_v2(self):
        _, raw_manifest = self.write_input(self.sentinel)
        self.invoke()
        self.make_collected()
        receipt = validate_output(
            self.output_root, expected_run_id=self.run_id,
            policy=self.output_policy({}, raw_manifest, expected_hash="0" * 64),
            receipt_path=self.ledger.receipt_path(self.run_id), ledger=self.ledger)
        self.assertEqual(receipt["outcome"], "rejected")
        self.assertIn("input hashes", receipt["failures"][0])


if __name__ == "__main__":
    unittest.main()
