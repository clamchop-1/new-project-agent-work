"""Project-scoped, nonprivileged journal for diagnostic media runs.

The ledger records intent and evidence only. It does not authorize or perform
Hyper-V operations, launch guest commands, or grant Broker execution authority.
Run IDs are reserved once and never released, including after failed runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 1024 * 1024
MAX_JOURNAL_EVENTS = 256
RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class DiagnosticRunError(ValueError):
    """A run ledger operation violated its project or lifecycle contract."""


class RunBusyError(DiagnosticRunError):
    """Another process currently holds the run's journal lock."""


class RunInterruptedError(DiagnosticRunError):
    """A run has incomplete, pending, or otherwise untrusted journal state."""


class RunJournalCorruptError(DiagnosticRunError):
    """A run journal or project owner record failed integrity checks."""


STATES = (
    "reserved",
    "preparing",
    "prepared",
    "awaiting_approval",
    "approved",
    "attached",
    "running",
    "stopped",
    "detached",
    "collected",
    "validated",
    "rejected",
    "incomplete",
)
TERMINAL_STATES = frozenset({"validated", "rejected", "incomplete"})
_TRANSITIONS = {
    "reserved": {"preparing"},
    "preparing": {"prepared"},
    "prepared": {"awaiting_approval"},
    "awaiting_approval": {"approved"},
    "approved": {"attached"},
    "attached": {"running"},
    "running": {"stopped"},
    "stopped": {"detached"},
    "detached": {"collected"},
    "collected": {"validated", "rejected"},
}
_ALLOWED_RUN_ENTRIES = frozenset({
    ".lock", "journal.json", "input", "collected", "validation-receipt.json",
})


class DiagnosticRunLedger:
    """Durable run-ID reservations and hash-chained lifecycle records.

    ``storage_dir`` must be a project-local directory. The owner record binds it
    to both ``project_id`` and the canonical project root. A run directory is
    never deleted by this class; failed and interrupted IDs remain consumed.
    """

    def __init__(self, project_root: Path, storage_dir: Path, *, project_id: str):
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise DiagnosticRunError("Project ID must be a lowercase scoped identifier.")
        supplied_root = Path(project_root).absolute()
        _require_directory(supplied_root)
        self.project_root = supplied_root.resolve(strict=True)
        self.project_id = project_id
        supplied_store = Path(storage_dir)
        if not supplied_store.is_absolute():
            supplied_store = self.project_root / supplied_store
        self.storage_dir = Path(os.path.abspath(supplied_store))
        _require_scoped_path(self.project_root, self.storage_dir, allow_missing=True)
        self._initialize_owner()
        self.reservations_dir = self.storage_dir / "reservations"
        self.runs_dir = self.storage_dir / "runs"
        for directory in (self.reservations_dir, self.runs_dir):
            if directory.exists() or directory.is_symlink():
                _require_directory(directory)
            else:
                directory.mkdir()

    def reserve(self, run_id: str | None = None) -> dict:
        """Consume a fresh run ID before any bundle or media work begins."""
        if run_id is None:
            run_id = uuid.uuid4().hex
        _validate_run_id(run_id)
        reservation = self.reservation_path(run_id)
        run_directory = self.run_directory(run_id)
        if reservation.exists() or reservation.is_symlink() or run_directory.exists() or run_directory.is_symlink():
            raise DiagnosticRunError("Run ID or run directory has already been reserved.")
        if list(self.reservations_dir.glob("." + run_id + ".json.pending-*")):
            raise RunInterruptedError("Run ID has an interrupted reservation write and cannot be reused.")

        reserved_at = _now()
        run_nonce = uuid.uuid4().hex
        reservation_record = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "project_id": self.project_id,
            "project_root": str(self.project_root),
            "run_id": run_id,
            "run_nonce": run_nonce,
            "reserved_at": reserved_at,
        }
        _atomic_create_new(reservation, _canonical_json(reservation_record))
        try:
            run_directory.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise DiagnosticRunError("Run directory appeared during reservation; ID remains consumed.") from exc
        except OSError as exc:
            # The reservation marker is intentionally retained. An operator can
            # explicitly recover it as incomplete, but the ID cannot be reused.
            raise DiagnosticRunError("Run ID was reserved but its directory could not be created.") from exc

        first_event = _make_event(0, "reserved", None, {
            "reservation": "one-time",
            "reserved_at": reserved_at,
            "run_nonce_sha256": hashlib.sha256(run_nonce.encode("ascii")).hexdigest(),
        }, recorded_at=reserved_at)
        _atomic_create_new(run_directory / "journal.json", _canonical_json(
            _make_journal(self, run_id, [first_event])))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        """Read and verify a run. Pending or anomalous runs fail closed."""
        _validate_run_id(run_id)
        run_directory = self.run_directory(run_id)
        if not run_directory.exists():
            if self.reservation_path(run_id).exists():
                raise RunInterruptedError("Reserved run directory is missing; explicit recovery is required.")
            raise DiagnosticRunError("Run ID has not been reserved in this project.")
        _require_directory(run_directory)
        with self._run_lock(run_directory):
            journal = self._read_journal(run_id, run_directory)
            anomalies = self._run_anomalies(run_directory)
            state = journal["events"][-1]["state"]
            if ((run_directory / "validation-receipt.json").exists()
                    and state not in TERMINAL_STATES):
                anomalies.append("unbound:validation-receipt.json")
            if anomalies and state != "incomplete":
                raise RunInterruptedError("Run has pending or unexpected artifacts; explicit recovery is required.")
            journal["integrity_warnings"] = anomalies
            journal["run_nonce"] = self._read_reservation(run_id)["run_nonce"]
            return journal

    def transition(self, run_id: str, *, expected_state: str, new_state: str,
                   evidence: dict) -> dict:
        """Append one compare-and-swap lifecycle event; perform no side effects."""
        _validate_run_id(run_id)
        if expected_state not in STATES or new_state not in STATES:
            raise DiagnosticRunError("Unknown diagnostic run state.")
        normalized_evidence = _validate_evidence(evidence)
        run_directory = self.run_directory(run_id)
        if not run_directory.exists():
            raise RunInterruptedError("Run directory is missing; explicit recovery is required.")
        _require_directory(run_directory)
        with self._run_lock(run_directory):
            journal = self._read_journal(run_id, run_directory)
            anomalies = self._run_anomalies(run_directory)
            state_before = journal["events"][-1]["state"]
            if ((run_directory / "validation-receipt.json").exists()
                    and state_before not in TERMINAL_STATES):
                anomalies.append("unbound:validation-receipt.json")
            receipt_commit = (new_state in {"validated", "rejected"}
                              and anomalies == ["unbound:validation-receipt.json"])
            if anomalies and new_state != "incomplete" and not receipt_commit:
                raise RunInterruptedError("Run has pending or unexpected artifacts; explicit recovery is required.")
            events = journal["events"]
            current_state = events[-1]["state"]
            if current_state != expected_state:
                raise DiagnosticRunError(
                    f"Run state changed: expected {expected_state}, found {current_state}.")
            if new_state == "incomplete":
                pass
            elif new_state not in _TRANSITIONS.get(current_state, set()):
                raise DiagnosticRunError(f"Invalid run transition: {current_state} -> {new_state}.")
            if current_state in TERMINAL_STATES:
                raise DiagnosticRunError("Terminal diagnostic runs cannot transition.")
            if len(events) >= MAX_JOURNAL_EVENTS:
                raise DiagnosticRunError("Run journal reached its event limit.")
            if anomalies:
                normalized_evidence["observed_anomalies"] = anomalies
            previous_hash = events[-1]["event_hash"]
            event = _make_event(len(events), new_state, previous_hash, normalized_evidence)
            if new_state in {"validated", "rejected"}:
                receipt = _read_canonical_json(run_directory / "validation-receipt.json", max_bytes=64 * 1024)
                _validate_receipt_binding(receipt, self, run_id, event)
            events.append(event)
            _atomic_replace(run_directory / "journal.json", _canonical_json(
                _make_journal(self, run_id, events)))
        return self.get_run(run_id)

    def recover_incomplete(self, run_id: str, *, operator_id: str, reason: str) -> dict:
        """Explicitly close interrupted work as incomplete; never resume it."""
        _validate_run_id(run_id)
        operator_id = _required_text(operator_id, "Operator ID", max_length=128)
        reason = _required_text(reason, "Recovery reason", max_length=1024)
        reservation = self.reservation_path(run_id)
        if not reservation.exists():
            pending = list(self.reservations_dir.glob("." + run_id + ".json.pending-*"))
            if not pending:
                raise DiagnosticRunError("Cannot recover an ID without a reservation or pending marker.")
            recovered_reservation = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "project_id": self.project_id,
                "project_root": str(self.project_root),
                "run_id": run_id,
                "run_nonce": uuid.uuid4().hex,
                "reserved_at": _now(),
            }
            _atomic_create_new(reservation, _canonical_json(recovered_reservation))
        self._read_reservation(run_id)
        run_directory = self.run_directory(run_id)
        if not run_directory.exists():
            run_directory.mkdir(exist_ok=False)
        _require_directory(run_directory)

        with self._run_lock(run_directory):
            anomalies = self._run_anomalies(run_directory)
            journal_path = run_directory / "journal.json"
            if journal_path.exists():
                journal = self._read_journal(run_id, run_directory)
                events = journal["events"]
                if events[-1]["state"] in TERMINAL_STATES:
                    pass
                else:
                    previous_hash = events[-1]["event_hash"]
                    sequence = len(events)
            else:
                events = []
                previous_hash = None
                sequence = 0
            if not journal_path.exists() or events[-1]["state"] not in TERMINAL_STATES:
                if len(events) >= MAX_JOURNAL_EVENTS:
                    raise RunJournalCorruptError("Run journal reached its event limit; cannot recover safely.")
                event = _make_event(sequence, "incomplete", previous_hash, {
                    "operator_id": operator_id,
                    "reason": reason,
                    "recovery": True,
                    "observed_anomalies": anomalies,
                })
                events.append(event)
                data = _canonical_json(_make_journal(self, run_id, events))
                if journal_path.exists():
                    _atomic_replace(journal_path, data)
                else:
                    _atomic_create_new(journal_path, data)
        return self.get_run(run_id)

    def run_directory(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        return self.runs_dir / run_id

    def reservation_path(self, run_id: str) -> Path:
        _validate_run_id(run_id)
        return self.reservations_dir / (run_id + ".json")

    def receipt_path(self, run_id: str) -> Path:
        return self.run_directory(run_id) / "validation-receipt.json"

    def _initialize_owner(self) -> None:
        if self.storage_dir.exists() or self.storage_dir.is_symlink():
            _require_directory(self.storage_dir)
        else:
            self.storage_dir.mkdir(parents=True)
        owner_path = self.storage_dir / "owner.json"
        pending_owner = list(self.storage_dir.glob(".owner.json.pending-*"))
        if pending_owner:
            raise RunInterruptedError("Project owner record has a pending write; explicit review is required.")
        expected = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "project_id": self.project_id,
            "project_root": str(self.project_root),
        }
        if owner_path.exists():
            actual = _read_canonical_json(owner_path, max_bytes=4096)
            if actual != expected:
                raise DiagnosticRunError("Ledger directory belongs to a different project identity.")
            return
        if any(self.storage_dir.iterdir()):
            raise RunInterruptedError("Unowned ledger directory is not empty; refusing to adopt its contents.")
        try:
            _atomic_create_new(owner_path, _canonical_json(expected), temp_prefix=".owner.json.pending-")
        except FileExistsError:
            actual = _read_canonical_json(owner_path, max_bytes=4096)
            if actual != expected:
                raise DiagnosticRunError("Ledger directory belongs to a different project identity.")

    def _read_reservation(self, run_id: str) -> dict:
        record = _read_canonical_json(self.reservation_path(run_id), max_bytes=4096)
        expected = {"schema_version", "project_id", "project_root", "run_id", "run_nonce", "reserved_at"}
        if set(record) != expected:
            raise RunJournalCorruptError("Run reservation fields are invalid.")
        if (record["schema_version"] != JOURNAL_SCHEMA_VERSION
                or record["project_id"] != self.project_id
                or record["project_root"] != str(self.project_root)
                or record["run_id"] != run_id
                or not isinstance(record["run_nonce"], str)
                or not RUN_ID_PATTERN.fullmatch(record["run_nonce"])):
            raise RunJournalCorruptError("Run reservation does not match this project and run ID.")
        return record

    def _read_journal(self, run_id: str, run_directory: Path) -> dict:
        reservation = self._read_reservation(run_id)
        journal_path = run_directory / "journal.json"
        if not journal_path.exists():
            raise RunInterruptedError("Run journal is missing; explicit recovery is required.")
        journal = _read_canonical_json(journal_path, max_bytes=MAX_JOURNAL_BYTES)
        _validate_journal(journal, self, run_id, reservation)
        latest = journal["events"][-1]
        if latest["state"] in {"validated", "rejected"}:
            receipt_path = run_directory / "validation-receipt.json"
            receipt = _read_canonical_json(receipt_path, max_bytes=64 * 1024)
            _validate_receipt_binding(receipt, self, run_id, latest)
        return journal

    def _run_anomalies(self, run_directory: Path) -> list[str]:
        anomalies = []
        if not (run_directory / "journal.json").exists():
            anomalies.append("missing:journal.json")
        for path in sorted(run_directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith("journal.json.pending-"):
                anomalies.append("pending:" + path.name)
            elif path.name not in _ALLOWED_RUN_ENTRIES:
                anomalies.append("unexpected:" + path.name)
                continue
            elif path.name in {"input", "collected"} and (path.exists() or path.is_symlink()):
                try:
                    _require_directory(path)
                except (OSError, DiagnosticRunError):
                    anomalies.append("unsafe-entry:" + path.name)
            elif path.name in {"journal.json", "validation-receipt.json"} and path.exists():
                try:
                    _require_regular_unlinked_file(path)
                except (OSError, DiagnosticRunError):
                    anomalies.append("unsafe-entry:" + path.name)
        return anomalies

    @contextmanager
    def _run_lock(self, run_directory: Path):
        lock_path = run_directory / ".lock"
        if lock_path.exists() or lock_path.is_symlink():
            _require_regular_unlinked_file(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _is_reparse_point(opened)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            os.close(descriptor)
            raise RunInterruptedError("Run lock changed or is not a regular unlinked file.")
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                os.fsync(stream.fileno())
            stream.seek(0)
            _lock_file(stream)
            try:
                yield
            finally:
                _unlock_file(stream)
        finally:
            stream.close()


def _make_journal(ledger: DiagnosticRunLedger, run_id: str, events: list[dict]) -> dict:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "project_id": ledger.project_id,
        "project_root": str(ledger.project_root),
        "run_id": run_id,
        "events": events,
    }


def _make_event(sequence: int, state: str, previous_hash: str | None, evidence: dict,
                *, recorded_at: str | None = None) -> dict:
    event = {
        "sequence": sequence,
        "state": state,
        "recorded_at": recorded_at or _now(),
        "previous_hash": previous_hash,
        "evidence": evidence,
    }
    event["event_hash"] = hashlib.sha256(_canonical_json(event)).hexdigest()
    return event


def _validate_journal(journal: dict, ledger: DiagnosticRunLedger, run_id: str,
                      reservation: dict) -> None:
    if set(journal) != {"schema_version", "project_id", "project_root", "run_id", "events"}:
        raise RunJournalCorruptError("Run journal fields are invalid.")
    if (journal["schema_version"] != JOURNAL_SCHEMA_VERSION
            or journal["project_id"] != ledger.project_id
            or journal["project_root"] != str(ledger.project_root)
            or journal["run_id"] != run_id):
        raise RunJournalCorruptError("Run journal identity does not match its project reservation.")
    events = journal["events"]
    if not isinstance(events, list) or not events or len(events) > MAX_JOURNAL_EVENTS:
        raise RunJournalCorruptError("Run journal event inventory is invalid.")
    previous_hash = None
    previous_state = None
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {
            "sequence", "state", "recorded_at", "previous_hash", "evidence", "event_hash",
        }:
            raise RunJournalCorruptError("Run journal event fields are invalid.")
        state = event["state"]
        if (type(event["sequence"]) is not int or event["sequence"] != index
                or state not in STATES or event["previous_hash"] != previous_hash
                or not isinstance(event["evidence"], dict)):
            raise RunJournalCorruptError("Run journal event order or state is invalid.")
        if index == 0:
            if state not in {"reserved", "incomplete"} or previous_hash is not None:
                raise RunJournalCorruptError("Run journal has an invalid initial event.")
            if state == "reserved" and event["recorded_at"] != reservation["reserved_at"]:
                raise RunJournalCorruptError("Initial journal event does not match reservation time.")
            if state == "reserved" and event["evidence"].get("run_nonce_sha256") != hashlib.sha256(
                    reservation["run_nonce"].encode("ascii")).hexdigest():
                raise RunJournalCorruptError("Initial event does not match the reserved run nonce.")
        else:
            if state == "incomplete":
                if previous_state in TERMINAL_STATES:
                    raise RunJournalCorruptError("A terminal run has an appended event.")
            elif state not in _TRANSITIONS.get(previous_state, set()):
                raise RunJournalCorruptError("Run journal contains an invalid lifecycle transition.")
        payload = {key: value for key, value in event.items() if key != "event_hash"}
        expected_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if event["event_hash"] != expected_hash:
            raise RunJournalCorruptError("Run journal event hash does not match.")
        try:
            datetime.fromisoformat(event["recorded_at"])
        except (TypeError, ValueError) as exc:
            raise RunJournalCorruptError("Run journal event timestamp is malformed.") from exc
        _validate_evidence(event["evidence"])
        previous_hash = expected_hash
        previous_state = state


def _validate_receipt_binding(receipt: dict, ledger: DiagnosticRunLedger,
                              run_id: str, terminal_event: dict) -> None:
    required = {
        "schema_version", "project_id", "run_id", "run_nonce_sha256", "outcome", "status",
        "policy_version", "validated_at", "artifact_hashes", "verified_artifacts",
        "observed_artifacts", "failures",
    }
    if set(receipt) != required:
        raise RunJournalCorruptError("Validation receipt fields are invalid.")
    state = terminal_event["state"]
    if (receipt["schema_version"] != JOURNAL_SCHEMA_VERSION
            or receipt["project_id"] != ledger.project_id
            or receipt["run_id"] != run_id
            or receipt["run_nonce_sha256"] != hashlib.sha256(
                ledger._read_reservation(run_id)["run_nonce"].encode("ascii")).hexdigest()
            or receipt["outcome"] != state or receipt["status"] != state
            or type(receipt["policy_version"]) is not int or receipt["policy_version"] < 1
            or not isinstance(receipt["artifact_hashes"], dict)):
        raise RunJournalCorruptError("Validation receipt identity or outcome does not match its journal.")
    expected_hash = terminal_event["evidence"].get("receipt_sha256")
    actual_hash = hashlib.sha256(_canonical_json(receipt)).hexdigest()
    if expected_hash != actual_hash:
        raise RunJournalCorruptError("Validation receipt hash does not match its terminal event.")


def _validate_evidence(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise DiagnosticRunError("Journal evidence must be a JSON object.")
    try:
        encoded = _canonical_json(evidence)
    except (TypeError, ValueError) as exc:
        raise DiagnosticRunError("Journal evidence must contain finite canonical JSON values.") from exc
    if len(encoded) > 16 * 1024:
        raise DiagnosticRunError("Journal evidence exceeds its 16 KiB limit.")
    return json.loads(encoded.decode("utf-8"))


def _read_canonical_json(path: Path, *, max_bytes: int) -> dict:
    try:
        raw = _read_regular_file(path, max_bytes)
    except FileNotFoundError as exc:
        raise RunInterruptedError(f"Required record is missing: {path.name}.") from exc
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunJournalCorruptError(f"Record is not strict UTF-8 JSON: {path.name}.") from exc
    if not isinstance(value, dict) or _canonical_json(value) != raw:
        raise RunJournalCorruptError(f"Record is not a canonical JSON object: {path.name}.")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            written = stream.write(data)
            if written != len(data):
                raise OSError("Short write while recording diagnostic evidence.")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _atomic_create_new(path: Path, data: bytes, *, temp_prefix: str | None = None) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = temp_prefix or ("." + path.name + ".pending-")
    temporary = path.parent / (prefix + uuid.uuid4().hex)
    _write_new_file(temporary, data)
    try:
        os.link(temporary, path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        # Preserve the temp file so recovery can see that publication did not
        # complete. Never fall back to a potentially overwriting rename.
        raise RunInterruptedError(f"Atomic create-new publication failed for {path.name}.") from exc
    temporary.unlink()
    _sync_directory(path.parent)


def atomic_create_new_file(path: Path, data: bytes) -> None:
    """Atomically publish a new file without replacing an existing record."""
    _atomic_create_new(Path(path), data)


def _atomic_replace(path: Path, data: bytes) -> None:
    if not path.exists():
        raise RunInterruptedError("Journal disappeared before atomic replacement.")
    _require_regular_unlinked_file(path)
    temporary = path.parent / ("journal.json.pending-" + uuid.uuid4().hex)
    _write_new_file(temporary, data)
    os.replace(temporary, path)
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        # Python does not expose a portable Windows directory FlushFileBuffers
        # handle. The file itself is flushed before publication.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or _is_reparse_point(before)):
        raise DiagnosticRunError("Evidence record must be a regular unlinked file.")
    if before.st_size > max_bytes:
        raise DiagnosticRunError("Evidence record exceeds its size limit.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise DiagnosticRunError("Evidence record changed while being opened.")
        chunks = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks))):
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise DiagnosticRunError("Evidence record exceeds its size limit.")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise DiagnosticRunError("Evidence record changed while being read.")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
        raise DiagnosticRunError("Project-scoped paths cannot contain links or non-directories.")


def _require_regular_unlinked_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _is_reparse_point(info)):
        raise DiagnosticRunError("Journal artifacts must be regular unlinked files.")


def _require_scoped_path(project_root: Path, target: Path, *, allow_missing: bool) -> None:
    try:
        relative = target.relative_to(project_root)
    except ValueError as exc:
        raise DiagnosticRunError("Diagnostic state and artifacts must remain inside this project.") from exc
    cursor = project_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            _require_directory(cursor)
        elif not allow_missing:
            raise DiagnosticRunError("A required project-scoped directory is missing.")


def _is_reparse_point(info) -> bool:
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DiagnosticRunError("Run ID must be 32 lowercase hexadecimal characters.")


def _required_text(value: str, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise DiagnosticRunError(f"{label} must be nonempty and at most {max_length} characters.")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_file(stream) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RunBusyError("Another process holds the diagnostic run lock.") from exc


def _unlock_file(stream) -> None:
    if os.name == "nt":
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
