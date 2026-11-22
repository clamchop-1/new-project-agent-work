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


SCHEMA_VERSION = 1
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

    def __post_init__(self) -> None:
        if not self.allowed_files:
            raise DiagnosticBundleError("Output policy must allow at least one file.")
        if len(self.allowed_files) > self.max_files:
            raise DiagnosticBundleError("Output policy exceeds its file-count limit.")
        for name in self.allowed_files:
            _safe_relative_path(name)
        _validate_limits(self.max_file_bytes, self.max_files, self.max_total_bytes)


def build_bundle(destination: Path, scripts: tuple[ScriptAllowlistEntry, ...], *,
                 run_id: str | None = None,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                 max_files: int = DEFAULT_MAX_FILES,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """Create a new bundle directory from explicitly pinned scripts."""
    _validate_limits(max_file_bytes, max_files, max_total_bytes)
    run_id = run_id or uuid.uuid4().hex
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DiagnosticBundleError("Run ID must be a lowercase UUID hex string.")
    if not scripts or len(scripts) > max_files:
        raise DiagnosticBundleError("Script inventory is empty or exceeds its file limit.")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise DiagnosticBundleError("Bundle destination must be a new path.")

    entries = []
    normalized_names = set()
    total_bytes = 0
    for entry in scripts:
        name = _safe_relative_path(entry.name)
        folded_name = name.casefold()
        if folded_name in normalized_names or name == MANIFEST_NAME:
            raise DiagnosticBundleError("Script inventory contains a duplicate or reserved path.")
        normalized_names.add(folded_name)
        if not SHA256_PATTERN.fullmatch(entry.sha256):
            raise DiagnosticBundleError("Allowlisted script hash is malformed.")
        source = Path(entry.source)
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

    destination.mkdir(parents=True, exist_ok=False)
    try:
        for name, data, _ in entries:
            output = destination.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            _write_new_file(output, data)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "limits": {"max_file_bytes": max_file_bytes, "max_files": max_files,
                       "max_total_bytes": max_total_bytes},
            "files": [{"path": name, "size": len(data), "sha256": file_hash}
                      for name, data, file_hash in entries],
        }
        _write_new_file(destination / MANIFEST_NAME, _canonical_json(manifest))
    except Exception:
        # Preserve partial output for inspection instead of recursively deleting it.
        raise
    return manifest


def validate_output(output_root: Path, *, expected_run_id: str,
                    policy: OutputPolicy, receipt_path: Path) -> dict:
    """Validate an untrusted result directory and always write a host receipt."""
    failures: list[str] = []
    verified: list[dict] = []
    try:
        _validate_output_tree(Path(output_root), expected_run_id, policy, verified)
        status = "validated"
    except (DiagnosticBundleError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(str(exc))
        status = "rejected"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "run_id": expected_run_id,
        "status": status,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "verified_artifacts": verified if status == "validated" else [],
        "failures": failures,
    }
    _atomic_write(Path(receipt_path), _canonical_json(receipt))
    return receipt


def _validate_output_tree(root: Path, expected_run_id: str, policy: OutputPolicy,
                          verified: list[dict]) -> None:
    if not isinstance(expected_run_id, str) or not RUN_ID_PATTERN.fullmatch(expected_run_id):
        raise DiagnosticBundleError("Expected run ID is malformed.")
    _require_directory(root)
    files: dict[str, Path] = {}
    directories_seen: set[str] = set()
    folded_names: set[str] = set()
    def fail_walk(error):
        raise error

    for current, directories, filenames in os.walk(root, followlinks=False, onerror=fail_walk):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            _require_directory(path)
            directories_seen.add(path.relative_to(root).as_posix())
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            safe_name = _safe_relative_path(relative)
            folded = safe_name.casefold()
            if folded in folded_names:
                raise DiagnosticBundleError("Output contains duplicate normalized paths.")
            folded_names.add(folded)
            _require_regular_unlinked_file(path)
            files[safe_name] = path
            if len(files) > policy.max_files + 1:
                raise DiagnosticBundleError("Output exceeds the file-count limit.")

    if OUTPUT_MANIFEST_NAME not in files:
        raise DiagnosticBundleError("Output manifest is missing.")
    manifest_raw = _read_regular_file(files.pop(OUTPUT_MANIFEST_NAME), 64 * 1024)
    manifest = _strict_json_object(manifest_raw)
    if _canonical_json(manifest) != manifest_raw:
        raise DiagnosticBundleError("Output manifest is not canonically encoded.")
    if set(manifest) != {"schema_version", "run_id", "files"}:
        raise DiagnosticBundleError("Output manifest fields are invalid.")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise DiagnosticBundleError("Output manifest schema version is unsupported.")
    if manifest["run_id"] != expected_run_id:
        raise DiagnosticBundleError("Output manifest run ID does not match.")
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
        actual_hash = hashlib.sha256(_read_regular_file(path, policy.max_file_bytes)).hexdigest()
        if actual_hash != expected[name]["sha256"]:
            raise DiagnosticBundleError("Output file hash does not match its manifest.")
        verified.append({"path": name, "size": size, "sha256": actual_hash})


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
    if path.as_posix() != value or len(value.encode("utf-8")) > 240:
        raise DiagnosticBundleError("Artifact path is not normalized or is too long.")
    if unicodedata.normalize("NFC", value) != value:
        raise DiagnosticBundleError("Artifact path must use NFC Unicode normalization.")
    return value


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


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    _write_new_file(temporary, data)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
