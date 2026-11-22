"""Host-side builder for the fixed Ubuntu sentinel launcher input contract.

This stays separate from the legacy schema-v1 diagnostic_bundle builder. It
creates only a one-file inert sentinel bundle for the fixed schema-v2 launcher.
It has no media, VM, Broker, or guest side effects.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Any

from diagnostic_runs import DiagnosticRunError, DiagnosticRunLedger
import ubuntu_diagnostic_launcher as launcher


PINNED_LAUNCHER_SHA256 = "506b39ae8cec3464c1592a75d5e975240793739b9811b00ffb598bb50236efbc"
MAX_MANIFEST_BYTES = launcher.MAX_MANIFEST_BYTES
MAX_SENTINEL_BYTES = launcher.MAX_INPUT_BYTES
MAX_TOTAL_BYTES = launcher.MAX_TOTAL_INPUT_BYTES
STAGING_PREFIX = ".input-v2.partial-"


class LauncherBundleError(ValueError):
    """A schema-v2 input source or published bundle failed validation."""


def build_sentinel_bundle(source_directory: Path, destination: Path, *,
                          ledger: DiagnosticRunLedger,
                          run_id: str | None = None) -> dict[str, Any]:
    """Build and verify a fresh run-scoped launcher bundle.

    The destination must be exactly `<run>/input`. The run must be freshly
    reserved. Any failure after the run enters `preparing` closes it as
    `incomplete`; partial staging/publication is preserved for review.
    """
    _require_ledger(ledger)
    if run_id is None:
        raise LauncherBundleError("A previously reserved, explicit run ID is required.")
    if not isinstance(run_id, str) or len(run_id) != 32 \
            or any(char not in "0123456789abcdef" for char in run_id):
        raise LauncherBundleError("Run ID must be 32 lowercase hexadecimal characters.")
    run = ledger.get_run(run_id)
    if run["events"][-1]["state"] != "reserved":
        raise LauncherBundleError("Schema-v2 input can only be built for a fresh reserved run.")
    if ledger.project_id != launcher.PROJECT_ID:
        raise LauncherBundleError("Schema-v2 launcher bundle is scoped to LocalCodingAgent only.")

    run_directory = ledger.run_directory(run_id)
    expected_destination = run_directory / "input"
    _require_project_path(ledger.project_root, run_directory, allow_missing=False)
    destination = _canonical_requested_path(destination)
    if destination != expected_destination:
        raise LauncherBundleError("Bundle destination must be this run's fixed input directory.")
    if destination.exists() or destination.is_symlink():
        raise LauncherBundleError("Bundle destination already exists; overwrite is forbidden.")

    source_root = _canonical_requested_path(source_directory)
    _require_project_path(ledger.project_root, source_root, allow_missing=False)
    _require_directory(source_root)
    sentinel_source = _require_exact_source_inventory(source_root)
    sentinel_bytes = _read_regular_file(sentinel_source, MAX_SENTINEL_BYTES)
    launcher_bytes = _read_regular_file(
        Path(launcher.__file__), max_bytes=1024 * 1024)
    launcher_hash = hashlib.sha256(launcher_bytes).hexdigest()
    if launcher_hash != PINNED_LAUNCHER_SHA256:
        raise LauncherBundleError("Launcher source differs from the host-pinned implementation.")

    input_row = {"path": "sentinel.txt", "size": len(sentinel_bytes),
                 "sha256": hashlib.sha256(sentinel_bytes).hexdigest()}
    manifest = {
        "schema": launcher.INPUT_SCHEMA,
        "schema_version": launcher.INPUT_SCHEMA_VERSION,
        "project_id": ledger.project_id,
        "run_id": run_id,
        "run_nonce": run["run_nonce"],
        "operation_id": launcher.OPERATION_ID,
        "launcher_version": launcher.LAUNCHER_VERSION,
        "launcher_sha256": launcher_hash,
        "limits": {"max_files": launcher.MAX_INPUT_FILES,
                    "max_file_bytes": launcher.MAX_INPUT_BYTES,
                    "max_total_bytes": launcher.MAX_TOTAL_INPUT_BYTES},
        "files": [input_row],
    }
    manifest_bytes = launcher.canonical_json(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise LauncherBundleError("Schema-v2 input manifest exceeds its byte limit.")

    ledger.transition(run_id, expected_state="reserved", new_state="preparing",
                      evidence={"input_schema_version": launcher.INPUT_SCHEMA_VERSION,
                                "operation_id": launcher.OPERATION_ID,
                                "input_file_count": 1})
    staging = run_directory / (STAGING_PREFIX + uuid.uuid4().hex)
    try:
        staging.mkdir(mode=0o700, exist_ok=False)
        _write_new_file(staging / "sentinel.txt", sentinel_bytes)
        _write_new_file(staging / launcher.MANIFEST_NAME, manifest_bytes)
        _verify_bundle_tree(staging, ledger=ledger, run_id=run_id,
                            run_nonce=run["run_nonce"], expected_launcher_hash=launcher_hash)

        # Reserve the final directory with create-new semantics, then create
        # each file exclusively. A partial publication remains untrusted and
        # the run is terminally marked incomplete below.
        destination.mkdir(mode=0o700, exist_ok=False)
        _write_new_file(destination / "sentinel.txt", sentinel_bytes)
        _write_new_file(destination / launcher.MANIFEST_NAME, manifest_bytes)
        report = _verify_bundle_tree(
            destination, ledger=ledger, run_id=run_id,
            run_nonce=run["run_nonce"], expected_launcher_hash=launcher_hash)
        _remove_owned_staging(staging)
        ledger.transition(
            run_id, expected_state="preparing", new_state="prepared",
            evidence={"manifest_sha256": report["manifest_sha256"],
                      "input_bundle_sha256": report["bundle_sha256"],
                      "artifact_hashes": {row["path"]: row["sha256"]
                                          for row in report["files"]}},
        )
        return report
    except Exception:
        try:
            # get_run intentionally refuses unknown staging artifacts while
            # preparing. The ledger's incomplete transition is the explicit
            # recovery path and records those anomalies in its journal.
            ledger.transition(run_id, expected_state="preparing", new_state="incomplete",
                              evidence={"reason": "schema_v2_bundle_build_failed"})
        except (DiagnosticRunError, OSError):
            # Preserve all artifacts and let journal recovery handle ambiguity.
            pass
        raise


def validate_sentinel_bundle(bundle_root: Path, *, ledger: DiagnosticRunLedger,
                             expected_run_id: str) -> dict[str, Any]:
    """Reopen a published schema-v2 bundle and verify exact run-bound bytes."""
    _require_ledger(ledger)
    run = ledger.get_run(expected_run_id)
    if run["events"][-1]["state"] not in {
            "prepared", "awaiting_approval", "approved", "attached", "running",
            "stopped", "detached", "collected"}:
        raise LauncherBundleError("Input bundle is not in a verifiable prepared run state.")
    expected_root = ledger.run_directory(expected_run_id) / "input"
    root = _canonical_requested_path(bundle_root)
    if root != expected_root:
        raise LauncherBundleError("Input bundle path is outside the fixed run directory.")
    prepared = next((event for event in run["events"] if event["state"] == "prepared"), None)
    if prepared is None:
        raise LauncherBundleError("Run has no prepared input evidence.")
    report = _verify_bundle_tree(
        root, ledger=ledger, run_id=expected_run_id,
        run_nonce=run["run_nonce"], expected_launcher_hash=PINNED_LAUNCHER_SHA256)
    evidence = prepared["evidence"]
    if (evidence.get("manifest_sha256") != report["manifest_sha256"]
            or evidence.get("input_bundle_sha256") != report["bundle_sha256"]):
        raise LauncherBundleError("Published schema-v2 input changed after preparation.")
    return report


def _verify_bundle_tree(root: Path, *, ledger: DiagnosticRunLedger, run_id: str,
                        run_nonce: str, expected_launcher_hash: str) -> dict[str, Any]:
    _require_project_path(ledger.project_root, root, allow_missing=False)
    _require_directory(root)
    entries = list(root.iterdir())
    names = [path.name for path in entries]
    if len(names) != 2 or len({name.casefold() for name in names}) != 2 \
            or set(names) != {launcher.MANIFEST_NAME, "sentinel.txt"}:
        raise LauncherBundleError("Bundle inventory must contain exactly manifest.json and sentinel.txt.")
    manifest_raw = _read_regular_file(root / launcher.MANIFEST_NAME, MAX_MANIFEST_BYTES)
    manifest = _parse_manifest(manifest_raw)
    expected_fields = {
        "schema", "schema_version", "project_id", "run_id", "run_nonce",
        "operation_id", "launcher_version", "launcher_sha256", "limits", "files",
    }
    if set(manifest) != expected_fields:
        raise LauncherBundleError("Schema-v2 input manifest fields are invalid.")
    if (manifest["schema"] != launcher.INPUT_SCHEMA
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != launcher.INPUT_SCHEMA_VERSION):
        raise LauncherBundleError("Input manifest schema is unsupported.")
    if (manifest["project_id"] != ledger.project_id
            or manifest["project_id"] != launcher.PROJECT_ID
            or manifest["run_id"] != run_id or manifest["run_nonce"] != run_nonce
            or manifest["operation_id"] != launcher.OPERATION_ID
            or manifest["launcher_version"] != launcher.LAUNCHER_VERSION
            or manifest["launcher_sha256"] != expected_launcher_hash):
        raise LauncherBundleError("Input manifest identity or launcher binding does not match.")
    if manifest["limits"] != {
            "max_files": launcher.MAX_INPUT_FILES,
            "max_file_bytes": launcher.MAX_INPUT_BYTES,
            "max_total_bytes": launcher.MAX_TOTAL_INPUT_BYTES}:
        raise LauncherBundleError("Input manifest limits do not match the fixed launcher policy.")
    files = manifest["files"]
    if (not isinstance(files, list) or len(files) != 1
            or not isinstance(files[0], dict)
            or set(files[0]) != {"path", "size", "sha256"}
            or files[0]["path"] != "sentinel.txt"):
        raise LauncherBundleError("Input manifest must declare exactly sentinel.txt.")
    sentinel = _read_regular_file(root / "sentinel.txt", MAX_SENTINEL_BYTES)
    row = files[0]
    actual_hash = hashlib.sha256(sentinel).hexdigest()
    if (type(row["size"]) is not int or row["size"] != len(sentinel)
            or not isinstance(row["sha256"], str) or row["sha256"] != actual_hash):
        raise LauncherBundleError("Sentinel size or SHA-256 does not match the manifest.")
    rows = []
    for name in (launcher.MANIFEST_NAME, "sentinel.txt"):
        data = manifest_raw if name == launcher.MANIFEST_NAME else sentinel
        rows.append({"path": name, "size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})
    inventory_bytes = launcher.canonical_json({"files": rows})
    return {
        "schema_version": launcher.INPUT_SCHEMA_VERSION,
        "project_id": ledger.project_id,
        "run_id": run_id,
        "launcher_version": launcher.LAUNCHER_VERSION,
        "launcher_sha256": expected_launcher_hash,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "bundle_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "files": rows,
    }


def _require_exact_source_inventory(root: Path) -> Path:
    children = list(root.iterdir())
    if (len(children) != 1 or children[0].name != "sentinel.txt"
            or children[0].name.casefold() != "sentinel.txt"):
        raise LauncherBundleError("Source directory must contain exactly sentinel.txt.")
    _require_regular_unlinked_file(children[0])
    return children[0]


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LauncherBundleError("Manifest contains duplicate JSON keys.")
            result[key] = value
        return result

    try:
        manifest = json.loads(raw.decode("utf-8", errors="strict"),
                              object_pairs_hook=unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(
                                  LauncherBundleError("Manifest contains invalid numeric values.")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LauncherBundleError("Manifest is not strict UTF-8 JSON.") from exc
    if not isinstance(manifest, dict) or launcher.canonical_json(manifest) != raw:
        raise LauncherBundleError("Manifest is not canonically encoded.")
    return manifest


def _canonical_requested_path(path: Path) -> Path:
    candidate = Path(path)
    if ".." in candidate.parts:
        raise LauncherBundleError("Path traversal is not accepted.")
    try:
        absolute = Path(os.path.abspath(candidate))
    except (OSError, ValueError) as exc:
        raise LauncherBundleError("Path is invalid.") from exc
    return absolute


def _require_project_path(project_root: Path, target: Path, *, allow_missing: bool) -> None:
    root = Path(os.path.abspath(project_root))
    candidate = _canonical_requested_path(target)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LauncherBundleError("Bundle and source must remain inside the active project.") from exc
    cursor = root
    paths = [root]
    for part in relative.parts:
        paths.append(paths[-1] / part)
    for index, current in enumerate(paths):
        cursor = current
        if not (cursor.exists() or cursor.is_symlink()):
            if allow_missing:
                continue
            raise LauncherBundleError("Required project-scoped path is missing.")
        info = cursor.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise LauncherBundleError("Project paths cannot traverse links or reparse points.")
        if index < len(paths) - 1 and not stat.S_ISDIR(info.st_mode):
            raise LauncherBundleError("Project path parent is not a directory.")


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise LauncherBundleError("Path must be a real directory, not a link or reparse point.")


def _require_regular_unlinked_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise LauncherBundleError("Input must be a regular file without links or reparse points.")


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    _require_regular_unlinked_file(path)
    before = path.lstat()
    if before.st_size > max_bytes:
        raise LauncherBundleError("Input file exceeds its fixed size limit.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
                or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode)):
            raise LauncherBundleError("Input file changed while being opened.")
        result = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(result))):
            result.extend(chunk)
            if len(result) > max_bytes:
                raise LauncherBundleError("Input file exceeds its fixed size limit.")
        after = os.fstat(descriptor)
        current = path.lstat()
        expected_identity = (before.st_dev, before.st_ino, before.st_size)
        if ((after.st_dev, after.st_ino, after.st_size) != expected_identity
                or (current.st_dev, current.st_ino, current.st_size) != expected_identity):
            raise LauncherBundleError("Input file changed while being read.")
        return bytes(result)
    finally:
        os.close(descriptor)


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            written = stream.write(data)
            if written != len(data):
                raise OSError("Short bundle write.")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _remove_owned_staging(staging: Path) -> None:
    _require_directory(staging)
    for name in ("sentinel.txt", launcher.MANIFEST_NAME):
        path = staging / name
        if path.exists():
            _require_regular_unlinked_file(path)
            path.unlink()
    staging.rmdir()


def _require_ledger(ledger: DiagnosticRunLedger) -> None:
    if not isinstance(ledger, DiagnosticRunLedger):
        raise LauncherBundleError("Operation requires the project-scoped diagnostic run ledger.")
