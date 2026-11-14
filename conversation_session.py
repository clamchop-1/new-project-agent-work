"""Bounded, untrusted conversation history.

Conversation records never grant project permissions or override holds.
"""

import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / ".local" / "sessions"

MAX_TURNS = 8
MAX_CHARS = 12000
MAX_TEXT = 3000


def session_path(project):
    if not isinstance(project, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", project
    ):
        raise ValueError("Invalid project identifier")
    return SESSION_DIR / f"{project}.json"


def validate_turn(turn):
    if not isinstance(turn, dict):
        raise ValueError("Invalid conversation record")

    if set(turn) != {"user", "assistant"}:
        raise ValueError("Unexpected conversation fields")

    for field in ("user", "assistant"):
        value = turn[field]
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise ValueError("Invalid conversation text")

    return {"user": turn["user"], "assistant": turn["assistant"]}


def bound_turns(turns):
    if not isinstance(turns, list):
        raise ValueError("Conversation must be a list")

    validated = [validate_turn(turn) for turn in turns[-MAX_TURNS:]]

    while validated and len(
        json.dumps(validated, ensure_ascii=False).encode("utf-8")
    ) > MAX_CHARS:
        validated.pop(0)

    return validated


def load_session(project):
    path = session_path(project)

    if not path.exists():
        return []

    if path.stat().st_size > MAX_CHARS * 4:
        raise ValueError("Session file exceeds allowed size")

    data = json.loads(path.read_text(encoding="utf-8"))
    return bound_turns(data)


def save_session(project, turns):
    path = session_path(project)
    bounded = bound_turns(turns)

    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        bounded, indent=2, ensure_ascii=False
    ).encode("utf-8")

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=path.name + ".new-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def clear_session(project):
    path = session_path(project)
    path.unlink(missing_ok=True)
