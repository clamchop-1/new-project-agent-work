"""Safely push the current LocalCodingAgent branch to its configured origin.

This helper never stages, commits, resets, cleans, stashes, or force-pushes.
Uncommitted files are reported but are not included in the push.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent


class SyncError(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SyncError(detail or f"git {' '.join(args)} failed ({result.returncode})")
    return result


def preflight() -> tuple[str, str, list[str]]:
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != REPOSITORY_ROOT:
        raise SyncError(f"Repository root mismatch: expected {REPOSITORY_ROOT}, found {root}")

    remote = git("remote", "get-url", "origin", check=False)
    if remote.returncode:
        raise SyncError("No 'origin' remote is configured. Add the intended repository URL, then rerun.")

    branch_result = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch_result.returncode:
        raise SyncError("HEAD is detached; switch to the intended branch before syncing.")
    branch = branch_result.stdout.strip()
    if not branch or git("check-ref-format", "--branch", branch, check=False).returncode:
        raise SyncError("Could not validate the current branch name.")

    status = git("status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return branch, git("rev-parse", "--short", "HEAD").stdout.strip(), status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review and safely push committed LocalCodingAgent work to origin."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run Git's push preflight but do not update the remote",
    )
    parser.add_argument("--yes", action="store_true", help="skip the final interactive confirmation")
    args = parser.parse_args()

    try:
        branch, commit, status = preflight()
        print(f"Repository: {REPOSITORY_ROOT}")
        print(f"Remote: origin (URL hidden)")
        print(f"Branch: {branch}")
        print(f"Local HEAD: {commit}")
        if status:
            print(f"Working tree has {len(status)} uncommitted path(s); they will not be uploaded:")
            for line in status:
                print(f"  {line}")

        destination = f"HEAD:refs/heads/{branch}"
        print("Checking whether a normal, non-forced push is accepted...")
        git("push", "--dry-run", "origin", destination)
        if args.dry_run:
            print("Dry run succeeded. No remote refs were changed.")
            return 0

        if not args.yes:
            answer = input(f"Push committed work to origin/{branch}? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Push cancelled; no remote refs were changed.")
                return 0

        git("push", "origin", destination)
        print(f"Uploaded committed work to origin/{branch}.")
        return 0
    except (OSError, SyncError) as exc:
        print(f"Sync stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
