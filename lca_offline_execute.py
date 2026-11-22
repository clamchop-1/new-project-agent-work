#!/usr/bin/env python3
"""Explicit, fixed-purpose entry point for the approved inert sentinel run."""

# Keep prerequisite validation local to this entry point: it must recheck every
# precondition immediately before the fixed launcher is invoked.
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

RUN_ID = "5d9310328de54e18925a9d53cb1d0fa2"
NONCE = "0e0c3ecf253d4c4bad21babc0beb1d85"
INPUT = Path("/mnt/lca-input")
LAUNCHER_ROOT = Path("/mnt/lca-launcher")
LAUNCHER = LAUNCHER_ROOT / "ubuntu_diagnostic_launcher.py"
OUTPUT = Path("/mnt/lca-output")
PINS = {
    "manifest.json": "f85cd5ff46acd553469179cf8e70b9a95783b6d81be809fb3da58063fa9e9790",
    "sentinel.txt": "0ac6cc7fe44e490c20c21989eef31116b63d636865be373180a54e4d574776b2",
    "ubuntu_diagnostic_launcher.py": "506b39ae8cec3464c1592a75d5e975240793739b9811b00ffb598bb50236efbc",
}


def mount_record(path):
    rows = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    matches = []
    for row in rows:
        columns = row.split()
        if len(columns) >= 4 and columns[1].replace("\\040", " ") == str(path):
            matches.append((columns[0], columns[2], set(columns[3].split(","))))
    if len(matches) != 1:
        raise ValueError("required mount point is absent or ambiguous: " + str(path))
    return matches[0]


def require_device(source, label):
    device_link = Path("/dev/disk/by-label") / label
    if not device_link.is_symlink():
        raise ValueError("expected volume label is unavailable: " + label)
    if os.path.realpath(source) != os.path.realpath(device_link):
        raise ValueError("mounted device does not match volume label " + label)


def digest(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ValueError("expected a regular unlinked file: " + str(path))
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_before_exec():
    if sys.argv[1:]:
        raise ValueError("this fixed entry point accepts no arguments")
    for directory in (INPUT, LAUNCHER_ROOT, OUTPUT):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("mount point is not a real directory: " + str(directory))
    for root, label in ((INPUT, "LCAIN5D931032"),
                        (LAUNCHER_ROOT, "LCAL5D931032")):
        source, fs, options = mount_record(root)
        require_device(source, label)
        if fs != "iso9660" or "ro" not in options:
            raise ValueError("input and launcher media must be read-only ISO9660")
    source, fs, options = mount_record(OUTPUT)
    require_device(source, "LCAOUT5D931032")
    if fs != "ntfs3" or "rw" not in options or not {
            "nosuid", "nodev", "noexec"}.issubset(options):
        raise ValueError("output must be ntfs3 rw,nosuid,nodev,noexec")
    if {entry.name for entry in INPUT.iterdir()} != {"manifest.json", "sentinel.txt"}:
        raise ValueError("input inventory differs from the pinned two-file set")
    if {entry.name for entry in LAUNCHER_ROOT.iterdir()} != {"ubuntu_diagnostic_launcher.py"}:
        raise ValueError("launcher media inventory is not exact")
    observed = {
        "manifest.json": digest(INPUT / "manifest.json"),
        "sentinel.txt": digest(INPUT / "sentinel.txt"),
        "ubuntu_diagnostic_launcher.py": digest(LAUNCHER),
    }
    if observed != PINS:
        raise ValueError("one or more pinned SHA-256 values differ")
    raw = (INPUT / "manifest.json").read_bytes()
    manifest = json.loads(raw.decode("utf-8", errors="strict"))
    canonical = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) + "\n").encode("utf-8")
    if raw != canonical or manifest.get("run_id") != RUN_ID or manifest.get("run_nonce") != NONCE:
        raise ValueError("manifest is noncanonical or belongs to another run")
    if (manifest.get("operation_id") != "sentinel-hash-v1"
            or manifest.get("launcher_sha256") != PINS["ubuntu_diagnostic_launcher.py"]):
        raise ValueError("manifest operation or launcher binding is invalid")
    if any(OUTPUT.iterdir()):
        raise ValueError("output volume is not empty; refusing to execute")


if __name__ == "__main__":
    try:
        validate_before_exec()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("LCA_HELPER_STATUS=FAIL " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    # Fixed interpreter, launcher path, operation identity, and arguments.
    # execv avoids a shell and accepts no caller-selected command or path.
    os.execv("/usr/bin/python3", [
        "/usr/bin/python3", str(LAUNCHER), "--run-id", RUN_ID,
        "--run-nonce", NONCE,
    ])
