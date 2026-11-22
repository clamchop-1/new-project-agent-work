"""Offline diagnostic bundle creation and untrusted result validation.

This module is intentionally not imported by the Broker or any execution entry
point. Callers must supply a host-owned script allowlist and expected output
policy. Bundle contents are data and are never executed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import uuid

from diagnostic_runs import (
    DiagnosticRunError,
    DiagnosticRunLedger,
    RunInterruptedError,
    atomic_create_new_file,
)

SCHEMA_VERSION = 1
OUTPUT_POLICY_VERSION = 1
MANIFEST_NAME = "manifest.json"
OUTPUT_MANIFEST_NAME = "output-manifest.json"
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_FILES = 32
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024


class DiagnosticBundleError(ValueError):
    """A bundle or diagnostic result violated its bounded contract."""


@dataclass(frozen=True)
class ScriptAllowlistEntry:
    """One operator-authored script pinned to a known content hash."""

    name: str
    source: Path
    sha256: str


@dataclass(frozen=True)
class OutputPolicy:
    """Host-owned limits and names allowed in one diagnostic result."""

    allowed_files: frozenset[str]
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    policy_version: int = OUTPUT_POLICY_VERSION
    manifest_schema_version: int = SCHEMA_VERSION
    expected_operation_id: str | None = None
    expected_launcher_version: str | None = None
    expected_launcher_sha256: str | None = None
    expected_input_manifest_sha256: str | None = None
    expected_input_hashes: tuple[tuple[str, int, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_files:
            raise DiagnosticBundleError("Output policy must allow at least one file.")
        if len(self.allowed_files) > self.max_files:
            raise DiagnosticBundleError("Output policy exceeds its file-count limit.")
        for name in self.allowed_files:
            _safe_relative_path(name)
            if name.casefold() == OUTPUT_MANIFEST_NAME.casefold():
                raise DiagnosticBundleError("Output policy uses the reserved manifest filename.")
        if len({name.casefold() for name in self.allowed_files}) != len(self.allowed_files):
            raise DiagnosticBundleError("Output policy contains duplicate normalized paths.")
        _validate_limits(self.max_file_bytes, self.max_files, self.max_total_bytes)
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise DiagnosticBundleError("Output policy version must be a positive integer.")
        if type(self.manifest_schema_version) is not int or self.manifest_schema_version not in {1, 2}:
            raise DiagnosticBundleError("Output manifest schema version is unsupported.")
        if self.manifest_schema_version == 2 and self.policy_version < 2:
            raise DiagnosticBundleError("Output manifest schema v2 requires output policy version 2 or later.")
        expected_fields = (self.expected_operation_id, self.expected_launcher_version,
                           self.expected_launcher_sha256, self.expected_input_manifest_sha256,
                           self.expected_input_hashes)
        if self.manifest_schema_version == 1:
            if any(value not in (None, ()) for value in expected_fields):
                raise DiagnosticBundleError("Schema-v1 output policy cannot bind schema-v2 fields.")
        else:
            if (not isinstance(self.expected_operation_id, str)
                    or self.expected_operation_id not in {
                        "diagnostic-noop-v1", "inventory-report-v1", "offline-tests-v1",
                        "sentinel-hash-v1"}
                    or not isinstance(self.expected_launcher_version, str)
                    or not self.expected_launcher_version
                    or not isinstance(self.expected_launcher_sha256, str)
                    or not SHA256_PATTERN.fullmatch(self.expected_launcher_sha256)
                    or not isinstance(self.expected_input_manifest_sha256, str)
                    or not SHA256_PATTERN.fullmatch(self.expected_input_manifest_sha256)
                    or not self.expected_input_hashes):
                raise DiagnosticBundleError("Schema-v2 policy must pin launcher, operation, and inputs.")
            seen_inputs = set()
            for row in self.expected_input_hashes:
                if (not isinstance(row, tuple) or len(row) != 3
                        or type(row[1]) is not int or row[1] < 0
                        or not isinstance(row[2], str) or not SHA256_PATTERN.fullmatch(row[2])):
                    raise DiagnosticBundleError("Expected input inventory is malformed.")
                name = _safe_relative_path(row[0])
                if name in seen_inputs:
                    raise DiagnosticBundleError("Expected input inventory has duplicate paths.")
                seen_inputs.add(name)
            if tuple(sorted(self.expected_input_hashes)) != self.expected_input_hashes:
                raise DiagnosticBundleError("Expected input inventory must be sorted.")


def build_bundle(destination: Path, scripts: tuple[ScriptAllowlistEntry, ...], *,
                 ledger: DiagnosticRunLedger, run_id: str | None = None,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                 max_files: int = DEFAULT_MAX_FILES,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """Create a project-local input bundle for one reserved run.

    The destination is fixed to ``<run>/input``. A failed write consumes the
    run and is journaled incomplete; callers must reserve a new run ID.
    """
    _validate_limits(max_file_bytes, max_files, max_total_bytes)
    if not isinstance(ledger, DiagnosticRunLedger):
        raise DiagnosticBundleError("Bundle creation requires a project-scoped run ledger.")
    if run_id is None:
        run_id = ledger.reserve()["run_id"]
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DiagnosticBundleError("Run ID must be a lowercase UUID hex string.")
    run_record = ledger.get_run(run_id)
    if run_record["events"][-1]["state"] != "reserved":
        raise DiagnosticBundleError("Input bundle can only be built for a newly reserved run.")
    if not scripts or len(scripts) > max_files:
        raise DiagnosticBundleError("Script inventory is empty or exceeds its file limit.")
    destination = Path(os.path.abspath(destination))
    expected_destination = ledger.run_directory(run_id) / "input"
    if destination != expected_destination:
        raise DiagnosticBundleError("Input bundle destination must be this run's project-local input directory.")
    _require_project_path(ledger.project_root, destination, allow_missing=True)
    if destination.exists() or destination.is_symlink():
        raise DiagnosticBundleError("Bundle destination must be a new path.")

    entries = []
    normalized_names = set()
    total_bytes = 0
    for entry in scripts:
        name = _safe_relative_path(entry.name)
        folded_name = name.casefold()
        if folded_name in normalized_names or folded_name == MANIFEST_NAME.casefold():
            raise DiagnosticBundleError("Script inventory contains a duplicate or reserved path.")
        normalized_names.add(folded_name)
        if not SHA256_PATTERN.fullmatch(entry.sha256):
            raise DiagnosticBundleError("Allowlisted script hash is malformed.")
        source = Path(os.path.abspath(entry.source))
        _require_project_path(ledger.project_root, source, allow_missing=False)
        _require_regular_unlinked_file(source)
        data = _read_regular_file(source, max_file_bytes)
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != entry.sha256:
            raise DiagnosticBundleError("Allowlisted script hash does not match its content.")
        if len(data) > max_file_bytes:
            raise DiagnosticBundleError("Script exceeds the per-file size limit.")
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise DiagnosticBundleError("Script inventory exceeds the total size limit.")
        entries.append((name, data, actual_hash))
    entries.sort(key=lambda row: row[0])

    ledger.transition(run_id, expected_state="reserved", new_state="preparing",
                      evidence={"input_file_count": len(entries), "source_project_id": ledger.project_id})
    run_directory = ledger.run_directory(run_id)
    staging = run_directory / (".input.partial-" + uuid.uuid4().hex)
    try:
        staging.mkdir(exist_ok=False)
        for name, data, _ in entries:
            output = staging.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            _write_new_file(output, data)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "run_nonce": run_record["run_nonce"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "limits": {"max_file_bytes": max_file_bytes, "max_files": max_files,
                       "max_total_bytes": max_total_bytes},
            "files": [{"path": name, "size": len(data), "sha256": file_hash}
                      for name, data, file_hash in entries],
        }
        _write_new_file(staging / MANIFEST_NAME, _canonical_json(manifest))
        checked = _validate_bundle_tree(staging, ledger=ledger, expected_run_id=run_id,
                                       expected_run_nonce=run_record["run_nonce"],
                                       max_file_bytes=max_file_bytes, max_files=max_files,
                                       max_total_bytes=max_total_bytes, require_run_path=False)
        if destination.exists() or destination.is_symlink():
            raise DiagnosticBundleError("Bundle destination appeared during construction.")
        os.rename(staging, destination)
        ledger.transition(run_id, expected_state="preparing", new_state="prepared",
                          evidence={"manifest_sha256": checked["manifest_sha256"],
                                    "artifact_hashes": {row["path"]: row["sha256"]
                                                        for row in checked["artifacts"]}})
    except Exception:
        try:
            ledger.transition(run_id, expected_state="preparing", new_state="incomplete",
                              evidence={"reason": "input_bundle_build_failed"})
        except DiagnosticRunError:
            # Preserve all partial artifacts; recovery must be explicit.
            pass
        raise
    return {**manifest, "manifest_sha256": checked["manifest_sha256"]}


def validate_bundle(bundle_root: Path, *, ledger: DiagnosticRunLedger,
                    expected_run_id: str) -> dict:
    """Revalidate exact input inventory and hashes for a prepared project run."""
    _require_ledger(ledger)
    run = ledger.get_run(expected_run_id)
    if run["events"][-1]["state"] not in {
        "prepared", "awaiting_approval", "approved", "attached", "running",
        "stopped", "detached", "collected", "validated",
    }:
        raise DiagnosticBundleError("Input bundle is not attached to a trusted prepared run state.")
    expected_root = ledger.run_directory(expected_run_id) / "input"
    if Path(os.path.abspath(bundle_root)) != expected_root:
        raise DiagnosticBundleError("Input bundle is outside this run's project-scoped path.")
    checked = _validate_bundle_tree(expected_root, ledger=ledger,
                                    expected_run_id=expected_run_id,
                                    expected_run_nonce=run["run_nonce"],
                                    max_file_bytes=DEFAULT_MAX_FILE_BYTES,
                                    max_files=DEFAULT_MAX_FILES,
                                    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
                                    require_run_path=True)
    prepared_event = next((event for event in run["events"] if event["state"] == "prepared"), None)
    if prepared_event and prepared_event["evidence"].get("manifest_sha256") != checked["manifest_sha256"]:
        raise DiagnosticBundleError("Input manifest changed after preparation.")
    return checked


def validate_output(output_root: Path, *, expected_run_id: str, policy: OutputPolicy,
                    receipt_path: Path, ledger: DiagnosticRunLedger) -> dict:
    """Validate one collected run and write a create-new host audit receipt.

    The run must be in ``collected`` state, and both paths must be its fixed
    project-local paths. A successful or rejected receipt closes the run so an
    output bundle cannot be replayed through this API.
    """
    _require_ledger(ledger)
    run = ledger.get_run(expected_run_id)
    if run["events"][-1]["state"] != "collected":
        raise DiagnosticBundleError("Only a collected, nonterminal run can be validated.")
    expected_output = ledger.run_directory(expected_run_id) / "collected"
    expected_receipt = ledger.receipt_path(expected_run_id)
    output_root = Path(os.path.abspath(output_root))
    receipt_path = Path(os.path.abspath(receipt_path))
    if output_root != expected_output or receipt_path != expected_receipt:
        raise DiagnosticBundleError("Output and receipt paths must be fixed inside this project run.")
    _require_project_path(ledger.project_root, output_root, allow_missing=True)
    _require_project_path(ledger.project_root, receipt_path, allow_missing=True)

    failures: list[str] = []
    observed: list[dict] = []
    try:
        verified: list[dict] = []
        _validate_output_tree(output_root, expected_run_id, run["run_nonce"],
                              ledger.project_id, policy,
                              verified, observed)
        outcome = "validated"
    except (DiagnosticBundleError, DiagnosticRunError, OSError, UnicodeError,
            json.JSONDecodeError) as exc:
        failures.append(str(exc))
        outcome = "rejected"
    artifact_hashes = {row["path"]: row["sha256"] for row in observed}
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "project_id": ledger.project_id,
        "run_id": expected_run_id,
        "run_nonce_sha256": hashlib.sha256(run["run_nonce"].encode("ascii")).hexdigest(),
        "outcome": outcome,
        "status": outcome,
        "policy_version": policy.policy_version,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_hashes": artifact_hashes,
        "verified_artifacts": verified if outcome == "validated" else [],
        "observed_artifacts": observed,
        "failures": failures,
    }
    receipt_bytes = _canonical_json(receipt)
    atomic_create_new_file(receipt_path, receipt_bytes)
    next_state = "validated" if outcome == "validated" else "rejected"
    ledger.transition(expected_run_id, expected_state="collected", new_state=next_state,
                      evidence={"receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                                "policy_version": policy.policy_version,
                                "artifact_hashes": artifact_hashes})
    return receipt


def _validate_output_tree(root: Path, expected_run_id: str, expected_run_nonce: str,
                          expected_project_id: str, policy: OutputPolicy,
                          verified: list[dict], observed: list[dict]) -> None:
    if not isinstance(expected_run_id, str) or not RUN_ID_PATTERN.fullmatch(expected_run_id):
        raise DiagnosticBundleError("Expected run ID is malformed.")
    _require_directory(root)
    files, directories_seen = _collect_inventory(root)
    if len(files) > policy.max_files + 1:
        raise DiagnosticBundleError("Output exceeds the file-count limit.")

    if OUTPUT_MANIFEST_NAME not in files:
        raise DiagnosticBundleError("Output manifest is missing.")
    manifest_raw = _read_regular_file(files.pop(OUTPUT_MANIFEST_NAME), 64 * 1024)
    manifest_artifact = {"path": OUTPUT_MANIFEST_NAME, "size": len(manifest_raw),
                         "sha256": hashlib.sha256(manifest_raw).hexdigest()}
    observed.append(manifest_artifact)
    manifest = _strict_json_object(manifest_raw)
    if _canonical_json(manifest) != manifest_raw:
        raise DiagnosticBundleError("Output manifest is not canonically encoded.")
    v1_fields = {"schema_version", "run_id", "run_nonce", "files"}
    v2_fields = {"schema_version", "run_id", "run_nonce", "project_id", "operation_id",
                 "launcher_version", "launcher_sha256", "input_manifest_sha256",
                 "input_hashes", "exit_status", "diagnostic_evidence", "files"}
    expected_fields = v1_fields if policy.manifest_schema_version == 1 else v2_fields
    if set(manifest) != expected_fields:
        raise DiagnosticBundleError("Output manifest fields are invalid.")
    if (type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != policy.manifest_schema_version):
        raise DiagnosticBundleError("Output manifest schema version is unsupported.")
    if manifest["run_id"] != expected_run_id:
        raise DiagnosticBundleError("Output manifest run ID does not match.")
    if manifest["run_nonce"] != expected_run_nonce:
        raise DiagnosticBundleError("Output manifest is stale or belongs to another run.")
    if policy.manifest_schema_version == 2:
        if manifest["project_id"] != expected_project_id:
            raise DiagnosticBundleError("Output manifest project identity does not match.")
        if manifest["operation_id"] != policy.expected_operation_id:
            raise DiagnosticBundleError("Output operation does not match the host policy.")
        if manifest["launcher_version"] != policy.expected_launcher_version:
            raise DiagnosticBundleError("Output launcher version does not match the host policy.")
        if manifest["launcher_sha256"] != policy.expected_launcher_sha256:
            raise DiagnosticBundleError("Output launcher hash does not match the host policy.")
        if manifest["input_manifest_sha256"] != policy.expected_input_manifest_sha256:
            raise DiagnosticBundleError("Output input-manifest hash does not match the host policy.")
        if type(manifest["exit_status"]) is not int or manifest["exit_status"] != 0:
            raise DiagnosticBundleError("Output operation did not report a successful exit status.")
        rows = manifest["input_hashes"]
        if not isinstance(rows, list):
            raise DiagnosticBundleError("Output input hashes are malformed.")
        actual_inputs = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
                raise DiagnosticBundleError("Output input hash entry is malformed.")
            name = _safe_relative_path(row["path"])
            if (type(row["size"]) is not int or row["size"] < 0
                    or not isinstance(row["sha256"], str)
                    or not SHA256_PATTERN.fullmatch(row["sha256"])):
                raise DiagnosticBundleError("Output input hash values are malformed.")
            actual_inputs.append((name, row["size"], row["sha256"]))
        if tuple(sorted(actual_inputs)) != policy.expected_input_hashes:
            raise DiagnosticBundleError("Output input hashes do not match the host inventory.")
        evidence = manifest["diagnostic_evidence"]
        if (not isinstance(evidence, dict)
                or set(evidence) != {"files_checked", "total_input_bytes", "sentinel_sha256"}
                or type(evidence["files_checked"]) is not int
                or type(evidence["total_input_bytes"]) is not int
                or not isinstance(evidence["sentinel_sha256"], str)
                or not SHA256_PATTERN.fullmatch(evidence["sentinel_sha256"])):
            raise DiagnosticBundleError("Diagnostic evidence fields are malformed.")
        sentinel_expected = next((row for row in policy.expected_input_hashes
                                  if row[0] == "sentinel.txt"), None)
        if (sentinel_expected is None
                or evidence["files_checked"] != len(policy.expected_input_hashes)
                or evidence["total_input_bytes"] != sum(row[1] for row in policy.expected_input_hashes)
                or evidence["sentinel_sha256"] != sentinel_expected[2]):
            raise DiagnosticBundleError("Diagnostic evidence does not match host-pinned input data.")
    verified.append(manifest_artifact)
    declared = manifest["files"]
    if not isinstance(declared, list) or len(declared) > policy.max_files:
        raise DiagnosticBundleError("Output file inventory is malformed or too large.")

    expected: dict[str, dict] = {}
    for row in declared:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise DiagnosticBundleError("Output manifest entry fields are invalid.")
        name = _safe_relative_path(row["path"])
        folded = name.casefold()
        if folded in expected:
            raise DiagnosticBundleError("Output manifest contains duplicate paths.")
        if name not in policy.allowed_files:
            raise DiagnosticBundleError("Output manifest declares a disallowed file.")
        if type(row["size"]) is not int or row["size"] < 0:
            raise DiagnosticBundleError("Output manifest file size is malformed.")
        if not isinstance(row["sha256"], str) or not SHA256_PATTERN.fullmatch(row["sha256"]):
            raise DiagnosticBundleError("Output manifest hash is malformed.")
        expected[name] = row

    if set(files) != set(expected):
        raise DiagnosticBundleError("Output contains missing or undeclared files.")
    if set(expected) != set(policy.allowed_files):
        raise DiagnosticBundleError("Output manifest does not match the exact host policy inventory.")
    expected_directories = {
        parent.as_posix()
        for name in expected
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    if directories_seen != expected_directories:
        raise DiagnosticBundleError("Output contains unexpected or empty directories.")
    total = 0
    for name, path in files.items():
        size = path.stat(follow_symlinks=False).st_size
        if size > policy.max_file_bytes or size != expected[name]["size"]:
            raise DiagnosticBundleError("Output file size is invalid.")
        total += size
        if total > policy.max_total_bytes:
            raise DiagnosticBundleError("Output exceeds the total size limit.")
        data = _read_regular_file(path, policy.max_file_bytes)
        actual_hash = hashlib.sha256(data).hexdigest()
        artifact = {"path": name, "size": size, "sha256": actual_hash}
        observed.append(artifact)
        if actual_hash != expected[name]["sha256"]:
            raise DiagnosticBundleError("Output file hash does not match its manifest.")
        verified.append(artifact)


def _validate_bundle_tree(root: Path, *, ledger: DiagnosticRunLedger,
                          expected_run_id: str, expected_run_nonce: str,
                          max_file_bytes: int,
                          max_files: int, max_total_bytes: int,
                          require_run_path: bool) -> dict:
    if not RUN_ID_PATTERN.fullmatch(expected_run_id):
        raise DiagnosticBundleError("Expected run ID is malformed.")
    _require_directory(root)
    _require_project_path(ledger.project_root, root, allow_missing=False)
    files, directories = _collect_inventory(root)
    if MANIFEST_NAME not in files:
        raise DiagnosticBundleError("Input bundle manifest is missing.")
    raw_manifest = _read_regular_file(files.pop(MANIFEST_NAME), 64 * 1024)
    manifest = _strict_json_object(raw_manifest)
    if _canonical_json(manifest) != raw_manifest:
        raise DiagnosticBundleError("Input manifest is not canonically encoded.")
    if set(manifest) != {"schema_version", "run_id", "run_nonce", "created_at", "limits", "files"}:
        raise DiagnosticBundleError("Input manifest fields are invalid.")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise DiagnosticBundleError("Input manifest schema version is unsupported.")
    if manifest["run_id"] != expected_run_id:
        raise DiagnosticBundleError("Input manifest run ID does not match.")
    if manifest["run_nonce"] != expected_run_nonce:
        raise DiagnosticBundleError("Input manifest run nonce does not match.")
    limits = manifest["limits"]
    if (not isinstance(limits, dict)
            or set(limits) != {"max_file_bytes", "max_files", "max_total_bytes"}):
        raise DiagnosticBundleError("Input manifest limits are malformed.")
    _validate_limits(limits["max_file_bytes"], limits["max_files"], limits["max_total_bytes"])
    if (limits["max_file_bytes"] > max_file_bytes or limits["max_files"] > max_files
            or limits["max_total_bytes"] > max_total_bytes):
        raise DiagnosticBundleError("Input manifest limits exceed the host policy.")
    if not isinstance(manifest["created_at"], str):
        raise DiagnosticBundleError("Input manifest timestamp is malformed.")
    try:
        datetime.fromisoformat(manifest["created_at"])
    except ValueError as exc:
        raise DiagnosticBundleError("Input manifest timestamp is malformed.") from exc
    declared = manifest["files"]
    if not isinstance(declared, list) or not declared or len(declared) > max_files:
        raise DiagnosticBundleError("Input manifest inventory is empty or too large.")

    expected: dict[str, dict] = {}
    folded_names: set[str] = set()
    for row in declared:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise DiagnosticBundleError("Input manifest entry fields are invalid.")
        name = _safe_relative_path(row["path"])
        folded = name.casefold()
        if folded in folded_names or folded == MANIFEST_NAME.casefold():
            raise DiagnosticBundleError("Input manifest contains a duplicate or reserved path.")
        folded_names.add(folded)
        if type(row["size"]) is not int or row["size"] < 0:
            raise DiagnosticBundleError("Input manifest file size is malformed.")
        if not isinstance(row["sha256"], str) or not SHA256_PATTERN.fullmatch(row["sha256"]):
            raise DiagnosticBundleError("Input manifest hash is malformed.")
        expected[name] = row
    if set(files) != set(expected):
        raise DiagnosticBundleError("Input bundle contains missing or undeclared files.")
    expected_directories = {
        parent.as_posix()
        for name in expected
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    if directories != expected_directories:
        raise DiagnosticBundleError("Input bundle contains unexpected or empty directories.")
    total = 0
    artifacts = []
    for name in sorted(files):
        path = files[name]
        data = _read_regular_file(path, max_file_bytes)
        size = len(data)
        total += size
        if size != expected[name]["size"] or total > max_total_bytes:
            raise DiagnosticBundleError("Input bundle size does not match its declared limits.")
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected[name]["sha256"]:
            raise DiagnosticBundleError("Input bundle file hash does not match its manifest.")
        artifacts.append({"path": name, "size": size, "sha256": actual_hash})
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    if require_run_path:
        run = ledger.get_run(expected_run_id)
        prepared = next((event for event in run["events"] if event["state"] == "prepared"), None)
        if prepared and prepared["evidence"].get("manifest_sha256") != manifest_hash:
            raise DiagnosticBundleError("Input manifest changed after preparation.")
    return {
        "run_id": expected_run_id,
        "manifest_sha256": manifest_hash,
        "artifacts": artifacts,
        "status": "validated",
    }


def _collect_inventory(root: Path) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    folded_names: set[str] = set()

    def fail_walk(error):
        raise error

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail_walk):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            _require_directory(path)
            relative = _safe_relative_path(path.relative_to(root).as_posix())
            folded = relative.casefold()
            if folded in folded_names:
                raise DiagnosticBundleError("Artifact inventory contains duplicate normalized paths.")
            folded_names.add(folded)
            directories.add(relative)
        for name in filenames:
            path = current_path / name
            relative = _safe_relative_path(path.relative_to(root).as_posix())
            folded = relative.casefold()
            if folded in folded_names:
                raise DiagnosticBundleError("Artifact inventory contains duplicate normalized paths.")
            folded_names.add(folded)
            _require_regular_unlinked_file(path)
            files[relative] = path
    return files, directories


def _strict_json_object(raw: bytes) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DiagnosticBundleError("Manifest contains duplicate JSON keys.")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               DiagnosticBundleError("Manifest contains invalid numeric values.")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DiagnosticBundleError("Manifest is not strict UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise DiagnosticBundleError("Manifest root must be an object.")
    return value


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DiagnosticBundleError("Artifact path is invalid.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise DiagnosticBundleError("Artifact path must be normalized and relative.")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DiagnosticBundleError("Artifact path contains invalid Unicode.") from exc
    if path.as_posix() != value or encoded_length > 240:
        raise DiagnosticBundleError("Artifact path is not normalized or is too long.")
    if unicodedata.normalize("NFC", value) != value:
        raise DiagnosticBundleError("Artifact path must use NFC Unicode normalization.")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    for part in path.parts:
        stem = part.split(".", 1)[0].upper()
        if (any(ord(character) < 32 or character in '<>:"|?*' for character in part)
                or part.endswith((" ", ".")) or stem in reserved
                or len(part.encode("utf-8")) > 255):
            raise DiagnosticBundleError("Artifact path contains a platform-reserved component.")
    return value


def _require_ledger(ledger: DiagnosticRunLedger) -> None:
    if not isinstance(ledger, DiagnosticRunLedger):
        raise DiagnosticBundleError("Operation requires a project-scoped diagnostic run ledger.")


def _require_project_path(project_root: Path, target: Path, *, allow_missing: bool) -> None:
    root = Path(project_root)
    candidate = Path(os.path.abspath(target))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise DiagnosticBundleError("Diagnostic artifacts must remain inside the active project.") from exc
    cursor = root
    for index, part in enumerate(relative.parts):
        cursor = cursor / part
        if not (cursor.exists() or cursor.is_symlink()):
            if allow_missing:
                continue
            raise DiagnosticBundleError("Required project-scoped artifact path is missing.")
        info = cursor.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise DiagnosticBundleError("Project-scoped artifacts cannot traverse links or reparse points.")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise DiagnosticBundleError("Project-scoped artifact path contains a non-directory parent.")


def _require_regular_unlinked_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DiagnosticBundleError("Artifact must be a regular file without links.")
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DiagnosticBundleError("Reparse-point artifacts are not accepted.")


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise DiagnosticBundleError("Output path contains a link or non-directory entry.")
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DiagnosticBundleError("Reparse-point directories are not accepted.")


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise DiagnosticBundleError("Artifact must be a regular file without links.")
    if getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DiagnosticBundleError("Reparse-point artifacts are not accepted.")
    if before.st_size > max_bytes:
        raise DiagnosticBundleError("Artifact exceeds its size limit.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise DiagnosticBundleError("Artifact changed type while being validated.")
        chunks = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks))):
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise DiagnosticBundleError("Artifact exceeds its size limit.")
        after_open = os.fstat(descriptor)
        after_path = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size)
        if ((after_open.st_dev, after_open.st_ino, after_open.st_size) != identity
                or (after_path.st_dev, after_path.st_ino, after_path.st_size) != identity):
            raise DiagnosticBundleError("Artifact changed during validation.")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _validate_limits(max_file_bytes: int, max_files: int, max_total_bytes: int) -> None:
    values = (max_file_bytes, max_files, max_total_bytes)
    if any(type(value) is not int or value < 1 for value in values):
        raise DiagnosticBundleError("All size and count limits must be positive integers.")
    if (max_file_bytes > DEFAULT_MAX_FILE_BYTES or max_files > DEFAULT_MAX_FILES
            or max_total_bytes > DEFAULT_MAX_TOTAL_BYTES):
        raise DiagnosticBundleError("Requested limits exceed the diagnostic bundle hard caps.")


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
