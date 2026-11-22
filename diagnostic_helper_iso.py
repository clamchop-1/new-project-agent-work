"""Read-only verification for the Phase B.6C helper staging ISO."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile


EXPECTED_VOLUME_ID = "LCAH5D931032"
EXPECTED_FILES = {
    "lca_offline_readiness.py": (
        "5f63c8db6a3050fc48a462c197a51cb030de7478b124dbc22c6084268a84353a",
        5325,
    ),
    "lca_offline_execute.py": (
        "0256e96a20e91417429b7efebee0ca9d99828d26253011f5f0df80bd2558b31e",
        4955,
    ),
}
EXPECTED_STAGING_SHA256 = (
    "b196fead753e2412aed5168eb45a61604a97808ddef2883c758ecada325f547d"
)
EXPECTED_STAGING_SIZE = 75776


class HelperIsoVerificationError(ValueError):
    """The staging ISO or one of its pinned source artifacts did not validate."""


def verify_helper_staging_iso(iso_path: Path, source_root: Path) -> dict:
    """Independently reopen and verify the existing staging ISO; never writes it."""
    iso_path = Path(iso_path)
    source_root = Path(source_root)
    before = _regular_file_identity(iso_path)
    if before[2] != EXPECTED_STAGING_SIZE or _sha256(iso_path) != EXPECTED_STAGING_SHA256:
        raise HelperIsoVerificationError("Staging ISO size or SHA-256 does not match its pin.")

    expected_sources = {}
    for name, (expected_hash, expected_size) in EXPECTED_FILES.items():
        source = source_root / name
        _regular_file_identity(source)
        if source.stat().st_size != expected_size or _sha256(source) != expected_hash:
            raise HelperIsoVerificationError(f"Pinned helper source mismatch: {name}")
        expected_sources[name] = (expected_hash, expected_size)

    try:
        import pycdlib
    except ImportError as exc:
        raise HelperIsoVerificationError("pycdlib is unavailable for ISO verification.") from exc

    image = pycdlib.PyCdlib()
    try:
        image.open(str(iso_path))
        # pycdlib 1.22.0 exposes the PVD through `pvd`; the field is
        # `volume_identifier` (bytes), not `vol_ident`.
        volume_id = image.pvd.volume_identifier
        if isinstance(volume_id, bytes):
            volume_id = volume_id.decode("ascii", errors="strict").rstrip(" ")
        if volume_id != EXPECTED_VOLUME_ID:
            raise HelperIsoVerificationError(f"Unexpected ISO volume ID: {volume_id!r}")

        inventory = set()
        for root, directories, files in image.walk(rr_path="/"):
            if directories:
                raise HelperIsoVerificationError("ISO contains an unexpected directory.")
            for filename in files:
                if root != "/":
                    raise HelperIsoVerificationError("ISO contains a nested payload.")
                inventory.add(filename)
        if inventory != set(EXPECTED_FILES):
            raise HelperIsoVerificationError(
                "ISO root inventory differs from the exact two-file allowlist."
            )

        extracted = {}
        with tempfile.TemporaryDirectory(prefix="lca-helper-iso-verify-") as temp_dir:
            for name, (expected_hash, expected_size) in expected_sources.items():
                target = Path(temp_dir) / name
                image.get_file_from_iso(local_path=str(target), rr_path="/" + name)
                data = target.read_bytes()
                actual_hash = hashlib.sha256(data).hexdigest()
                if len(data) != expected_size or actual_hash != expected_hash:
                    raise HelperIsoVerificationError(f"Extracted helper mismatch: {name}")
                extracted[name] = {"size_bytes": len(data), "sha256": actual_hash}
    finally:
        image.close()

    after = _regular_file_identity(iso_path)
    if after != before or _sha256(iso_path) != EXPECTED_STAGING_SHA256:
        raise HelperIsoVerificationError("Staging ISO changed during read-only verification.")
    return {
        "path": str(iso_path),
        "size_bytes": before[2],
        "sha256": EXPECTED_STAGING_SHA256,
        "volume_id": EXPECTED_VOLUME_ID,
        "inventory": extracted,
        "directories": [],
        "status": "verified",
    }


def _regular_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise HelperIsoVerificationError(f"Required file is unavailable: {path}") from exc
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise HelperIsoVerificationError(f"Expected an ordinary unlinked file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
