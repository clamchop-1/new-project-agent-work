#!/usr/bin/env python3
"""Read-only readiness check for the fixed Phase B.6C offline run."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

RUN_ID = "5d9310328de54e18925a9d53cb1d0fa2"
NONCE = "0e0c3ecf253d4c4bad21babc0beb1d85"
INPUT = Path("/mnt/lca-input")
LAUNCHER = Path("/mnt/lca-launcher/ubuntu_diagnostic_launcher.py")
OUTPUT = Path("/mnt/lca-output")
PINS = {
    "manifest.json": "f85cd5ff46acd553469179cf8e70b9a95783b6d81be809fb3da58063fa9e9790",
    "sentinel.txt": "0ac6cc7fe44e490c20c21989eef31116b63d636865be373180a54e4d574776b2",
    "ubuntu_diagnostic_launcher.py": "506b39ae8cec3464c1592a75d5e975240793739b9811b00ffb598bb50236efbc",
}


def fail(message):
    print("LCA_HELPER_STATUS=FAIL " + message, file=sys.stderr)
    return 1


def mount_record(path):
    target = str(path)
    try:
        rows = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ValueError("mount table unavailable")
    matches = []
    for row in rows:
        columns = row.split()
        if len(columns) >= 4 and columns[1].replace("\\040", " ") == target:
            matches.append((columns[0], columns[2], set(columns[3].split(","))))
    if len(matches) != 1:
        raise ValueError("required mount point is absent or ambiguous: " + target)
    return matches[0]


def require_device(source, label):
    device_link = Path("/dev/disk/by-label") / label
    if not device_link.is_symlink():
        raise ValueError("expected volume label is unavailable: " + label)
    if os.path.realpath(source) != os.path.realpath(device_link):
        raise ValueError("mounted device does not match volume label " + label)


def regular_file_hash(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ValueError("expected a regular unlinked file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check():
    if sys.argv[1:]:
        raise ValueError("this helper accepts no arguments")
    for directory in (INPUT, Path("/mnt/lca-launcher"), OUTPUT):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("mount point is not a real directory: " + str(directory))
    input_source, input_fs, input_opts = mount_record(INPUT)
    launcher_source, launcher_fs, launcher_opts = mount_record(Path("/mnt/lca-launcher"))
    output_source, output_fs, output_opts = mount_record(OUTPUT)
    require_device(input_source, "LCAIN5D931032")
    require_device(launcher_source, "LCAL5D931032")
    require_device(output_source, "LCAOUT5D931032")
    if input_fs != "iso9660" or "ro" not in input_opts:
        raise ValueError("input media must be read-only ISO9660")
    if launcher_fs != "iso9660" or "ro" not in launcher_opts:
        raise ValueError("launcher media must be read-only ISO9660")
    if output_fs != "ntfs3" or "rw" not in output_opts:
        raise ValueError("output must be mounted read/write with ntfs3")
    if not {"nosuid", "nodev", "noexec"}.issubset(output_opts):
        raise ValueError("output mount lacks required nosuid,nodev,noexec options")
    if {entry.name for entry in INPUT.iterdir()} != {"manifest.json", "sentinel.txt"}:
        raise ValueError("input inventory differs from the pinned two-file set")
    if {entry.name for entry in Path("/mnt/lca-launcher").iterdir()} != {
            "ubuntu_diagnostic_launcher.py"}:
        raise ValueError("launcher media inventory is not exact")
    observed = {
        "manifest.json": regular_file_hash(INPUT / "manifest.json"),
        "sentinel.txt": regular_file_hash(INPUT / "sentinel.txt"),
        "ubuntu_diagnostic_launcher.py": regular_file_hash(LAUNCHER),
    }
    if observed != PINS:
        raise ValueError("one or more pinned SHA-256 values differ")
    manifest_bytes = (INPUT / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    canonical = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")) + "\n").encode("utf-8")
    if manifest_bytes != canonical:
        raise ValueError("input manifest is not canonical JSON")
    if manifest.get("run_id") != RUN_ID or manifest.get("run_nonce") != NONCE:
        raise ValueError("manifest run identity does not match this fixed run")
    if manifest.get("operation_id") != "sentinel-hash-v1":
        raise ValueError("manifest operation is not the fixed sentinel operation")
    if manifest.get("launcher_sha256") != PINS["ubuntu_diagnostic_launcher.py"]:
        raise ValueError("manifest launcher hash does not match the pinned launcher")
    if any(OUTPUT.iterdir()):
        raise ValueError("output volume is not empty; refusing this run")
    return observed


if __name__ == "__main__":
    try:
        hashes = check()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(fail(str(exc)))
    print("LCA_HELPER_STATUS=READY " + RUN_ID)
    for name, digest in sorted(hashes.items()):
        print(f"{name} SHA-256 {digest}")
