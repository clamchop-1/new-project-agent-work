import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from diagnostic_bundle import (
    DiagnosticBundleError,
    OutputPolicy,
    ScriptAllowlistEntry,
    build_bundle,
    validate_output,
)


class DiagnosticBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = uuid.uuid4().hex
        self.receipt = self.root / "host-receipt.json"

    def tearDown(self):
        self.temporary.cleanup()

    def make_output(self, *, run_id=None, files=None, manifest_bytes=None):
        output = self.root / "output"
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
                                                   "files": rows})
        (output / "output-manifest.json").write_bytes(manifest_bytes)
        return output

    @staticmethod
    def canonical_json(value):
        return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")

    def validate(self, output, *, allowed=("result.txt",), **limits):
        policy = OutputPolicy(frozenset(allowed), **limits)
        return validate_output(output, expected_run_id=self.run_id,
                               policy=policy, receipt_path=self.receipt)

    def test_builder_copies_only_hash_pinned_allowlisted_script(self):
        source = self.root / "inventory.sh"
        content = b"#!/bin/sh\nprintf 'inventory\\n'\n"
        source.write_bytes(content)
        destination = self.root / "bundle"
        manifest = build_bundle(destination,
                                (ScriptAllowlistEntry("diagnostics/inventory.sh", source,
                                                      hashlib.sha256(content).hexdigest()),),
                                run_id=self.run_id)
        self.assertEqual(manifest["run_id"], self.run_id)
        self.assertEqual((destination / "diagnostics/inventory.sh").read_bytes(), content)
        self.assertEqual(json.loads((destination / "manifest.json").read_text())["files"][0]["sha256"],
                         hashlib.sha256(content).hexdigest())

    def test_builder_rejects_hash_mismatch_and_preserves_no_bundle(self):
        source = self.root / "inventory.sh"
        source.write_bytes(b"echo safe\n")
        destination = self.root / "bundle"
        with self.assertRaises(DiagnosticBundleError):
            build_bundle(destination,
                         (ScriptAllowlistEntry("inventory.sh", source, "0" * 64),))
        self.assertFalse(destination.exists())

    def test_builder_rejects_traversal_duplicate_and_oversized_script(self):
        source = self.root / "inventory.sh"
        source.write_bytes(b"12345")
        pinned = hashlib.sha256(source.read_bytes()).hexdigest()
        for name in ("../escape.sh", "nested/../../escape.sh", "Inventory.sh"):
            with self.subTest(name=name), self.assertRaises(DiagnosticBundleError):
                build_bundle(self.root / ("bundle-" + str(len(name))),
                             (ScriptAllowlistEntry(name, source, pinned),
                              ScriptAllowlistEntry("inventory.sh", source, pinned)),
                             max_file_bytes=4)

    def test_output_with_matching_manifest_and_hash_gets_receipt(self):
        output = self.make_output()
        receipt = self.validate(output)
        self.assertEqual(receipt["status"], "validated")
        self.assertEqual(receipt["verified_artifacts"][0]["path"], "result.txt")
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "validated")

    def test_rejects_wrong_run_id_and_records_failure(self):
        output = self.make_output(run_id=uuid.uuid4().hex)
        receipt = self.validate(output)
        self.assertEqual(receipt["status"], "rejected")
        self.assertIn("run ID", receipt["failures"][0])
        self.assertEqual(json.loads(self.receipt.read_text())["verified_artifacts"], [])

    def test_rejects_malformed_json_duplicate_json_keys_and_duplicate_paths(self):
        malformed = self.make_output(manifest_bytes=b"{")
        self.assertEqual(self.validate(malformed)["status"], "rejected")
        (self.root / "output").rename(self.root / "malformed")

        duplicate_keys = (b'{"schema_version":1,"run_id":"' + self.run_id.encode()
                          + b'","run_id":"' + self.run_id.encode()
                          + b'","files":[]}')
        output = self.make_output(manifest_bytes=duplicate_keys)
        self.assertIn("duplicate JSON", self.validate(output)["failures"][0])

        rows = [{"path": "result.txt", "size": 1, "sha256": "0" * 64}] * 2
        duplicate_paths = self.canonical_json({"schema_version": 1, "run_id": self.run_id,
                                               "files": rows})
        (self.root / "output").rename(self.root / "duplicate-keys")
        output = self.make_output(manifest_bytes=duplicate_paths)
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
                    "files": [{"path": "../escape", "size": 1, "sha256": "0" * 64}]}
        (traversal / "output-manifest.json").write_bytes(self.canonical_json(manifest))
        self.assertEqual(self.validate(traversal)["status"], "rejected")
        (self.root / "output").rename(self.root / "traversal")

        undeclared = self.make_output(files={"result.txt": b"x", "extra.txt": b"y"})
        (undeclared / "output-manifest.json").write_bytes(self.canonical_json({
            "schema_version": 1,
            "run_id": self.run_id,
            "files": [{"path": "result.txt", "size": 1,
                       "sha256": hashlib.sha256(b"x").hexdigest()}],
        }))
        self.assertIn("undeclared", self.validate(undeclared, allowed=("result.txt", "extra.txt"))["failures"][0])
        (self.root / "output").rename(self.root / "undeclared")

        hash_mismatch = self.make_output(files={"result.txt": b"actual"})
        rows = [{"path": "result.txt", "size": 6, "sha256": "0" * 64}]
        (hash_mismatch / "output-manifest.json").write_bytes(
            self.canonical_json({"schema_version": 1, "run_id": self.run_id, "files": rows}))
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

        hardlink = output / "hardlink.txt"
        try:
            os.link(output / "result.txt", hardlink)
        except OSError:
            self.skipTest("Hard links are unavailable on this filesystem.")
        self.assertEqual(self.validate(output, allowed=("result.txt", "hardlink.txt"))["status"], "rejected")
        hardlink.unlink()

        unexpected = output / "folder"
        unexpected.mkdir()
        self.assertEqual(self.validate(output)["status"], "rejected")

    def test_enforces_file_count_individual_and_total_size_limits(self):
        output = self.make_output(files={"result.txt": b"12345", "second.txt": b"678"})
        too_many_files = self.validate(output, allowed=("result.txt",), max_files=1)
        self.assertEqual(too_many_files["status"], "rejected")
        (self.root / "output").rename(self.root / "too-many")
        output = self.make_output(files={"result.txt": b"12345", "second.txt": b"678"})
        too_big_file = self.validate(output, allowed=("result.txt", "second.txt"),
                                     max_file_bytes=4)
        self.assertEqual(too_big_file["status"], "rejected")
        (self.root / "output").rename(self.root / "oversized")
        output = self.make_output(files={"result.txt": b"123", "second.txt": b"456"})
        too_big_total = self.validate(output, allowed=("result.txt", "second.txt"),
                                      max_file_bytes=5, max_total_bytes=5)
        self.assertIn("total size", too_big_total["failures"][0])


if __name__ == "__main__":
    unittest.main()
