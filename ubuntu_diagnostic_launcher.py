"""Fixed, explicitly invoked Ubuntu sentinel diagnostic launcher.

The launcher reads one inert file and hashes it. It never executes bundle
contents, accepts no device or command selection, and opens no network service.
The CLI uses fixed mount points; the run() API accepts directories for tests.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Mapping


INPUT_SCHEMA = "localcodingagent.diagnostic.input-manifest"
INPUT_SCHEMA_VERSION = 2
OUTPUT_SCHEMA_VERSION = 2
OPERATION_ID = "sentinel-hash-v1"
PROJECT_ID = "localcodingagent"
LAUNCHER_VERSION = "1.0.0"
MANIFEST_NAME = "manifest.json"
OUTPUT_MANIFEST_NAME = "output-manifest.json"
RESULT_NAME = "result.txt"
INPUT_MOUNTPOINT = Path("/mnt/lca-input")
OUTPUT_MOUNTPOINT = Path("/mnt/lca-output")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INPUT_BYTES = 256 * 1024
MAX_TOTAL_INPUT_BYTES = 256 * 1024
MAX_INPUT_FILES = 1
MAX_OUTPUT_BYTES = 8 * 1024
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class LauncherError(ValueError):
    """Input, output, or launcher policy failed closed."""


def launcher_sha256() -> str:
    """Hash the installed launcher source so the host can pin this exact file."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def canonical_json(value: object) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                           separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LauncherError("Record is not canonical JSON data.") from exc


def run(input_root: Path, output_root: Path, *, expected_run_id: str,
        expected_run_nonce: str) -> dict[str, object]:
    """Validate one fixed manifest and emit a bounded sentinel-hash result."""
    _validate_identity(expected_run_id, "Expected run ID")
    _validate_identity(expected_run_nonce, "Expected run nonce")
    input_directory = _canonical_directory(input_root, "Input")
    output_directory = _canonical_directory(output_root, "Output")
    if any(output_directory.iterdir()):
        raise LauncherError("Output directory must be empty; existing data is never overwritten.")

    manifest_bytes = _read_regular(input_directory / MANIFEST_NAME, MAX_MANIFEST_BYTES)
    manifest = _parse_canonical_manifest(manifest_bytes)
    checked = _validate_input(input_directory, manifest, expected_run_id,
                              expected_run_nonce, manifest_bytes)

    sentinel = checked["files"]["sentinel.txt"]
    sentinel_hash = hashlib.sha256(sentinel).hexdigest()
    result_bytes = (
        "LocalCodingAgent inert sentinel diagnostic\n"
        f"run_id={manifest['run_id']}\n"
        f"operation_id={OPERATION_ID}\n"
        f"sentinel_bytes={len(sentinel)}\n"
        f"sentinel_sha256={sentinel_hash}\n"
    ).encode("ascii")
    if len(result_bytes) > MAX_OUTPUT_BYTES:
        raise LauncherError("Diagnostic evidence exceeds the output limit.")

    output_rows = [{"path": RESULT_NAME, "size": len(result_bytes),
                    "sha256": hashlib.sha256(result_bytes).hexdigest()}]
    output_manifest = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "run_nonce": manifest["run_nonce"],
        "project_id": manifest["project_id"],
        "operation_id": OPERATION_ID,
        "launcher_version": LAUNCHER_VERSION,
        "launcher_sha256": checked["launcher_sha256"],
        "input_manifest_sha256": checked["input_manifest_sha256"],
        "input_hashes": checked["input_hashes"],
        "exit_status": 0,
        "diagnostic_evidence": {
            "files_checked": 1,
            "total_input_bytes": len(sentinel),
            "sentinel_sha256": sentinel_hash,
        },
        "files": output_rows,
    }
    manifest_out_bytes = canonical_json(output_manifest)
    if len(manifest_out_bytes) > MAX_MANIFEST_BYTES:
        raise LauncherError("Output manifest exceeds its size limit.")

    _write_new(output_directory / RESULT_NAME, result_bytes)
    _write_new(output_directory / OUTPUT_MANIFEST_NAME, manifest_out_bytes)
    return output_manifest


def main(argv: list[str] | None = None) -> int:
    """CLI accepts only the expected run ID and nonce; paths stay fixed."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (len(arguments) != 4 or arguments[0] != "--run-id"
            or arguments[2] != "--run-nonce"):
        _write_stderr("Usage: ubuntu_diagnostic_launcher.py --run-id HEX32 --run-nonce HEX32")
        return 64
    try:
        result = run(INPUT_MOUNTPOINT, OUTPUT_MOUNTPOINT,
                     expected_run_id=arguments[1], expected_run_nonce=arguments[3])
    except (LauncherError, OSError) as exc:
        _write_stderr("Diagnostic rejected: " + _bounded_message(str(exc)))
        return 2
    _write_stdout("LCA_DIAGNOSTIC_STATUS=PASS " + str(result["run_id"]))
    return 0


def _validate_input(input_root: Path, manifest: dict[str, object],
                    expected_run_id: str, expected_run_nonce: str,
                    manifest_bytes: bytes) -> dict[str, object]:
    required = {
        "schema", "schema_version", "project_id", "run_id", "run_nonce",
        "operation_id", "launcher_version", "launcher_sha256", "limits", "files",
    }
    if set(manifest) != required:
        raise LauncherError("Input manifest fields do not match schema v2.")
    if manifest["schema"] != INPUT_SCHEMA:
        raise LauncherError("Input manifest schema identifier is unsupported.")
    if type(manifest["schema_version"]) is not int \
            or manifest["schema_version"] != INPUT_SCHEMA_VERSION:
        raise LauncherError("Input manifest schema version is unsupported.")
    project_id = manifest["project_id"]
    if (not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id)
            or project_id != PROJECT_ID):
        raise LauncherError("Project identity is malformed or outside this fixed project.")
    _validate_identity(manifest["run_id"], "Manifest run ID")
    _validate_identity(manifest["run_nonce"], "Manifest run nonce")
    if manifest["run_id"] != expected_run_id:
        raise LauncherError("Input run ID does not match the explicit invocation.")
    if manifest["run_nonce"] != expected_run_nonce:
        raise LauncherError("Input run nonce is stale or does not match the explicit invocation.")
    if manifest["operation_id"] != OPERATION_ID:
        raise LauncherError("Operation is not in the fixed launcher allowlist.")
    if manifest["launcher_version"] != LAUNCHER_VERSION:
        raise LauncherError("Input manifest pins another launcher version.")
    launcher_hash = manifest["launcher_sha256"]
    if not isinstance(launcher_hash, str) or not SHA256_RE.fullmatch(launcher_hash):
        raise LauncherError("Launcher hash is malformed.")
    actual_launcher_hash = launcher_sha256()
    if launcher_hash != actual_launcher_hash:
        raise LauncherError("Installed launcher hash differs from the pinned manifest.")

    limits = manifest["limits"]
    if (not isinstance(limits, dict)
            or set(limits) != {"max_files", "max_file_bytes", "max_total_bytes"}):
        raise LauncherError("Input limits are malformed.")
    max_files = _bounded_int(limits["max_files"], 1, MAX_INPUT_FILES, "file count")
    max_file_bytes = _bounded_int(limits["max_file_bytes"], 1, MAX_INPUT_BYTES,
                                  "file size")
    max_total_bytes = _bounded_int(limits["max_total_bytes"], 1,
                                   MAX_TOTAL_INPUT_BYTES, "total input size")

    entries = manifest["files"]
    if (not isinstance(entries, list) or len(entries) != max_files
            or len(entries) != 1):
        raise LauncherError("Input inventory must contain exactly the sentinel file.")
    row = entries[0]
    if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
        raise LauncherError("Input inventory entry is malformed.")
    if row["path"] != "sentinel.txt":
        raise LauncherError("Input inventory path is not the fixed sentinel filename.")
    size = _bounded_int(row["size"], 0, min(max_file_bytes, MAX_INPUT_BYTES), "sentinel size")
    digest = row["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise LauncherError("Sentinel SHA-256 is malformed.")

    names = {entry.name for entry in input_root.iterdir()}
    if names != {MANIFEST_NAME, "sentinel.txt"}:
        raise LauncherError("Input media contains missing or unexpected entries.")
    sentinel = _read_regular(input_root / "sentinel.txt", min(max_file_bytes, MAX_INPUT_BYTES))
    actual_hash = hashlib.sha256(sentinel).hexdigest()
    if len(sentinel) != size or actual_hash != digest:
        raise LauncherError("Sentinel size or SHA-256 does not match the manifest.")
    if len(sentinel) > max_total_bytes:
        raise LauncherError("Sentinel exceeds the declared total-input limit.")
    return {
        "files": {"sentinel.txt": sentinel},
        "launcher_sha256": actual_launcher_hash,
        "input_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "input_hashes": [{"path": "sentinel.txt", "size": len(sentinel),
                           "sha256": actual_hash}],
    }


def _parse_canonical_manifest(raw: bytes) -> dict[str, object]:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LauncherError("Manifest contains duplicate keys.")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               LauncherError("Manifest contains invalid numeric values.")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LauncherError("Manifest is not strict UTF-8 JSON.") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise LauncherError("Manifest is not a canonical JSON object.")
    return value


def _canonical_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if any(part == ".." for part in candidate.parts):
        raise LauncherError(f"{label} directory path is not canonical.")
    absolute = Path(os.path.abspath(candidate))
    info = absolute.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise LauncherError(f"{label} root must be a real directory.")
    if absolute != absolute.resolve(strict=True):
        raise LauncherError(f"{label} directory path traverses a link.")
    return absolute


def _read_regular(path: Path, max_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise LauncherError("Required input file is unavailable.") from exc
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1 or before.st_size > max_bytes):
        raise LauncherError("Input must be a bounded, regular, unlinked file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise LauncherError("Input file changed while being opened.")
        data = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(data))):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise LauncherError("Input file exceeds its hard size limit.")
        after = os.fstat(descriptor)
        current = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size)
        if ((after.st_dev, after.st_ino, after.st_size) != identity
                or (current.st_dev, current.st_ino, current.st_size) != identity):
            raise LauncherError("Input file changed during validation.")
        return bytes(data)
    finally:
        os.close(descriptor)


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _validate_identity(value: object, label: str) -> None:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise LauncherError(f"{label} must be 32 lowercase hexadecimal characters.")


def _bounded_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LauncherError(f"{label} is outside the fixed limit.")
    return value


def _bounded_message(value: str) -> str:
    sanitized = " ".join(value.replace("\x00", " ").split())
    return sanitized[:240] or "input validation failed"


def _write_stderr(message: str) -> None:
    sys.stderr.write(_bounded_message(message) + "\n")


def _write_stdout(message: str) -> None:
    sys.stdout.write(_bounded_message(message) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
