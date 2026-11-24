"""Persistent, bounded conversation records for the local chat interface."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid


MAX_TITLE = 120
MAX_MESSAGE = 12_000
MAX_MESSAGES = 80
MAX_CONTEXT_MESSAGES = 12
MAX_CONTEXT_CHARS = 24_000
CONVERSATION_ID = re.compile(r"^[a-f0-9]{32}$")


class ConversationStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve() / ".local" / "chat" / "conversations"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, conversation_id: str) -> Path:
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("Conversation was not found.")
        path = self.root / (conversation_id + ".json")
        if path.is_symlink() or path.parent.resolve() != self.root:
            raise ValueError("Conversation path is not a regular local record.")
        return path

    def create(self) -> dict:
        now = time.time()
        row = {"id": uuid.uuid4().hex, "title": "New chat", "created_at": now,
               "updated_at": now, "messages": []}
        with self._lock:
            self._save(row)
        return row

    def _save(self, row: dict) -> None:
        path = self._path(row["id"])
        data = {"id": row["id"], "title": row["title"][:MAX_TITLE],
                "created_at": row["created_at"], "updated_at": time.time(),
                "messages": row["messages"][-MAX_MESSAGES:]}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.root,
                    prefix=path.name + ".new-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        row["updated_at"] = data["updated_at"]

    def load(self, conversation_id: str) -> dict:
        path = self._path(conversation_id)
        with self._lock:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise ValueError("Conversation was not found or exceeds the local size limit.")
            row = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(row, dict) or row.get("id") != conversation_id
                or not isinstance(row.get("messages"), list)):
            raise ValueError("Conversation record is invalid.")
        messages = []
        for message in row["messages"][-MAX_MESSAGES:]:
            if (isinstance(message, dict) and message.get("role") in {"user", "assistant"}
                    and isinstance(message.get("content"), str)
                    and len(message["content"]) <= MAX_MESSAGE):
                messages.append({"role": message["role"], "content": message["content"]})
        row["messages"] = messages
        return row

    def add_user(self, conversation_id: str, content: str) -> dict:
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > MAX_MESSAGE:
            raise ValueError("Message must be nonempty and within the 12,000 character limit.")
        with self._lock:
            row = self.load(conversation_id)
            content = content.strip()
            row["messages"].append({"role": "user", "content": content})
            if row["title"] == "New chat":
                row["title"] = content[:MAX_TITLE].splitlines()[0]
            self._save(row)
            return row

    def add_assistant(self, conversation_id: str, content: str) -> dict:
        if not isinstance(content, str) or len(content) > MAX_MESSAGE:
            raise ValueError("Assistant response exceeds the local conversation limit.")
        with self._lock:
            row = self.load(conversation_id)
            row["messages"].append({"role": "assistant", "content": content})
            self._save(row)
            return row

    def context(self, row: dict) -> list[dict]:
        selected = row["messages"][-MAX_CONTEXT_MESSAGES:]
        total = 0
        bounded = []
        for message in reversed(selected):
            size = len(message["content"])
            if total + size > MAX_CONTEXT_CHARS:
                break
            bounded.append({"role": message["role"], "content": message["content"]})
            total += size
        return list(reversed(bounded))

    def list(self) -> list[dict]:
        results = []
        with self._lock:
            paths = list(self.root.glob("[0-9a-f]" * 32 + ".json"))
        for path in paths:
            if path.is_symlink():
                continue
            try:
                row = self.load(path.stem)
                results.append({"id": row["id"], "title": row["title"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"]})
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return sorted(results, key=lambda item: item["updated_at"], reverse=True)[:200]
