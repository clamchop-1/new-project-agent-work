"""Short-lived, protected registration for one disposable Git worktree."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from agent_core import AgentError, Project, encoded, exclusive
from protocol_diagnostics import (
    _atomic_create, _ensure_private_permissions, _reject_reparse_chain,
    _validate_private_directory, _validate_private_file,
)

DEFAULT_TTL_SECONDS = 4 * 60 * 60
MAX_TTL_SECONDS = 24 * 60 * 60
SOURCE_PATH = "calculator.py"
REGISTRATION_ID = re.compile(r"[0-9a-f]{32}\Z")


def store_root():
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_STATE_HOME")
    if not base:
        if os.name == "nt":
            raise AgentError("LOCALAPPDATA is required for temporary project registrations.")
        base = str(Path.home() / ".local" / "state")
    return Path(base) / "LocalCodingAgent" / "TemporaryProjects"


def _prepare_store(root, *, create=True):
    root = Path(root)
    _reject_reparse_chain(root)
    if root.exists():
        _validate_private_directory(root)
    else:
        if not create:
            raise AgentError("Temporary project registration store is unavailable.")
        root.mkdir(parents=True, exist_ok=False)
        _ensure_private_permissions(root)
    _validate_private_directory(root)
    return root


def _worktree_identity(project):
    top = Path(project.git("rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    git_dir = Path(project.git("rev-parse", "--absolute-git-dir").strip()).resolve(strict=True)
    common_dir_text = project.git("rev-parse", "--git-common-dir").strip()
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = project.root / common_dir
    common_dir = common_dir.resolve(strict=True)
    if top != project.root or not git_dir.is_dir() or not common_dir.is_dir():
        raise AgentError("Temporary project is not the exact registered Git worktree.")
    git_info = git_dir.stat()
    if type(git_info.st_ino) is not int or git_info.st_ino <= 0:
        raise AgentError("Git worktree metadata identity cannot be verified on this filesystem.")
    identity_fields = {
        "root": str(project.root),
        "git_dir": str(git_dir),
        "git_common_dir": str(common_dir),
        "git_dir_device": int(git_info.st_dev),
        "git_dir_file_id": int(git_info.st_ino),
    }
    identity = hashlib.sha256(encoded(identity_fields)).hexdigest()
    return {**identity_fields, "identity": identity}


def canonical_worktree(root, protected_roots=()):
    raw = Path(root)
    raw_text = os.fspath(root)
    if (not raw.is_absolute() or any(part == ".." for part in raw.parts)
            or raw_text.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./"))
            or (os.name == "nt" and raw.drive.startswith("\\\\"))):
        raise AgentError("Temporary project root must be an absolute path.")
    _reject_reparse_chain(raw)
    canonical = raw.resolve(strict=True)
    info = canonical.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise AgentError("Temporary project root must be a regular directory.")
    project = Project(canonical)
    for protected in protected_roots:
        protected_path = Path(protected).resolve(strict=False)
        if canonical == protected_path or canonical.is_relative_to(protected_path) or protected_path.is_relative_to(canonical):
            raise AgentError("Temporary project cannot overlap a configured or production project root.")
    return project, _worktree_identity(project)


def _record_path(registration_id, root):
    if not isinstance(registration_id, str) or not REGISTRATION_ID.fullmatch(registration_id):
        raise AgentError("Temporary project registration ID is invalid.")
    return Path(root) / (registration_id + ".json")


def _reject_duplicate_registration(directory, project_root):
    existing_records = list(directory.iterdir())
    if len(existing_records) > 128:
        raise AgentError("Temporary registration inventory exceeds its fixed bound.")
    for existing in existing_records:
        if existing.suffix.casefold() != ".json":
            continue
        _reject_reparse_chain(existing)
        _validate_private_file(existing)
        if existing.lstat().st_size > 16384:
            raise AgentError("Existing temporary registration record exceeds its bound.")
        try:
            previous = json.loads(existing.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentError("Existing temporary registration inventory is invalid.") from exc
        if not isinstance(previous, dict) or previous.get("format") != "local-agent-temporary-project-v1":
            raise AgentError("Existing temporary registration inventory is invalid.")
        previous_root = previous.get("project_root")
        overlaps = False
        if isinstance(previous_root, str) and previous.get("revoked") is False:
            old_root = Path(previous_root).resolve(strict=False)
            new_root = Path(project_root)
            overlaps = (old_root == new_root or old_root.is_relative_to(new_root)
                        or new_root.is_relative_to(old_root))
        if overlaps:
            try:
                still_active = datetime.fromisoformat(previous["expires_at"]) > datetime.now(timezone.utc)
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError("Existing temporary registration lifetime is invalid.") from exc
            previous_id = previous.get("registration_id")
            if still_active and not is_revoked(previous_id, registry_root=directory):
                raise AgentError("This project already has an active temporary registration.")


def register(root, *, protected_roots=(), ttl_seconds=DEFAULT_TTL_SECONDS,
             confirmed=False, registry_root=None):
    if confirmed is not True:
        raise AgentError("Operator confirmation is required to register a temporary project.")
    if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise AgentError("Temporary registration lifetime is outside its fixed bound.")
    project, worktree = canonical_worktree(root, protected_roots)
    directory = _prepare_store(registry_root or store_root())
    now = datetime.now(timezone.utc)
    registration_id = uuid.uuid4().hex
    record = {
        "format": "local-agent-temporary-project-v1",
        "registration_id": registration_id,
        "project_id": project.identity,
        "project_root": str(project.root),
        "worktree": worktree,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "allowed_source_path": SOURCE_PATH,
        "capabilities": ["restricted_acceptance_evaluation"],
        "write_authorized": False,
        "test_execution_authorized": False,
        "revoked": False,
    }
    with exclusive(directory / ".registration.lock", "temporary-project-registration"):
        _reject_duplicate_registration(directory, str(project.root))
        artifact = _atomic_create(directory, registration_id + ".json", encoded(record))
    _validate_private_file(artifact)
    return record


def inspect(registration_id, *, registry_root=None, allow_expired=False):
    directory = _prepare_store(registry_root or store_root(), create=False)
    artifact = _record_path(registration_id, directory)
    _reject_reparse_chain(artifact)
    info = artifact.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
        raise AgentError("Temporary project registration record is unsafe.")
    _validate_private_file(artifact)
    try:
        record = json.loads(artifact.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError("Temporary project registration record is invalid.") from exc
    expected = {
        "format", "registration_id", "project_id", "project_root", "worktree",
        "created_at", "expires_at", "allowed_source_path", "capabilities",
        "write_authorized", "test_execution_authorized", "revoked",
    }
    if (not isinstance(record, dict) or set(record) != expected
            or record.get("format") != "local-agent-temporary-project-v1"
            or record.get("registration_id") != registration_id
            or record.get("allowed_source_path") != SOURCE_PATH
            or record.get("capabilities") != ["restricted_acceptance_evaluation"]
            or record.get("write_authorized") is not False
            or record.get("test_execution_authorized") is not False
            or type(record.get("revoked")) is not bool):
        raise AgentError("Temporary project registration scope is invalid.")
    if is_revoked(registration_id, registry_root=directory):
        raise AgentError("Temporary project registration has been revoked.")
    if record["revoked"]:
        raise AgentError("Temporary project registration has been revoked.")
    try:
        expires = datetime.fromisoformat(record["expires_at"])
        created = datetime.fromisoformat(record["created_at"])
    except (TypeError, ValueError) as exc:
        raise AgentError("Temporary project registration lifetime is invalid.") from exc
    now = datetime.now(timezone.utc)
    if expires.tzinfo is None or created.tzinfo is None or not created <= now:
        raise AgentError("Temporary project registration is expired or not yet valid.")
    if now >= expires and not allow_expired:
        raise AgentError("Temporary project registration is expired or not yet valid.")
    if expires - created > timedelta(seconds=MAX_TTL_SECONDS):
        raise AgentError("Temporary project registration lifetime exceeds its bound.")
    project, worktree = canonical_worktree(record["project_root"])
    if (project.identity != record["project_id"] or worktree != record["worktree"]):
        raise AgentError("Temporary project identity or Git worktree changed after registration.")
    return record


def revoke(registration_id, *, registry_root=None):
    directory = _prepare_store(registry_root or store_root(), create=False)
    record = inspect(registration_id, registry_root=directory, allow_expired=True)
    marker = directory / (registration_id + ".revoked")
    _atomic_create(directory, marker.name, encoded({
        "format": "local-agent-temporary-project-revocation-v1",
        "registration_id": registration_id,
        "project_id": record["project_id"],
        "revoked_at": datetime.now(timezone.utc).isoformat(),
    }))
    return {"registration_id": registration_id, "revoked": True}


def is_revoked(registration_id, *, registry_root=None):
    if not isinstance(registration_id, str) or not REGISTRATION_ID.fullmatch(registration_id):
        raise AgentError("Temporary project registration ID is invalid.")
    directory = Path(registry_root or store_root())
    marker = directory / (registration_id + ".revoked")
    _reject_reparse_chain(marker)
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    else:
        _validate_private_file(marker)
        return True
