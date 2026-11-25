"""Persistent, bounded conversation records for the local chat interface."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from agent_core import atomic_write


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

    def create(self, project_id: str | None = None) -> dict:
        if project_id is not None and not re.fullmatch(r'[a-f0-9]{64}', project_id):
            raise ValueError('Project identity is invalid.')
        now = time.time()
        row = {"id": uuid.uuid4().hex, "title": "New chat", "created_at": now,
               "updated_at": now, "messages": [], "project_id": project_id}
        with self._lock:
            self._save(row)
        return row

    def _save(self, row: dict) -> None:
        path = self._path(row["id"])
        data = {"id": row["id"], "title": row["title"][:MAX_TITLE],
                "created_at": row["created_at"], "updated_at": time.time(),
                "messages": row["messages"][-MAX_MESSAGES:],
                "project_id": row.get('project_id')}
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        atomic_write(path, payload)
        row["updated_at"] = data["updated_at"]

    @staticmethod
    def _source_refs(value):
        if value is None:
            return []
        if not isinstance(value,list) or len(value)>8:
            raise ValueError('Conversation source list is invalid.')
        output=[]
        for source in value:
            if (not isinstance(source,dict)
                    or not re.fullmatch(r'[0-9a-f]{32}',str(source.get('source_id','')))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',str(source.get('provider_id','')))
                    or not all(isinstance(source.get(key),str) and source[key]
                        for key in ('title','url','retrieved_at','content_sha256'))
                    or not re.fullmatch(r'[0-9a-f]{64}',source['content_sha256'])):
                raise ValueError('Conversation source reference is invalid.')
            try:
                parts=urlsplit(source['url'])
                safe_url=(parts.scheme=='https' and bool(parts.hostname) and not parts.username
                    and not parts.password and parts.port in (None,443))
            except ValueError:
                safe_url=False
            if not safe_url:
                raise ValueError('Conversation source URL is invalid.')
            published=source.get('publication_date')
            if published is not None and (not isinstance(published,str) or len(published)>80):
                published=None
            cached=source.get('cached',False)
            if type(cached) is not bool:
                raise ValueError('Conversation source cache state is invalid.')
            output.append({'source_id':source['source_id'],'provider_id':source['provider_id'],
                'title':source['title'][:300],'url':source['url'][:4096],
                'retrieved_at':source['retrieved_at'][:80],'publication_date':published,
                'content_sha256':source['content_sha256'],'cached':cached})
        return output

    def load(self, conversation_id: str) -> dict:
        path = self._path(conversation_id)
        with self._lock:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise ValueError("Conversation was not found or exceeds the local size limit.")
            row = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(row, dict) or row.get("id") != conversation_id
                or not isinstance(row.get("messages"), list)):
            raise ValueError("Conversation record is invalid.")
        project_id = row.get('project_id')
        if project_id is not None and (not isinstance(project_id, str)
                or not re.fullmatch(r'[a-f0-9]{64}', project_id)):
            raise ValueError('Conversation project identity is invalid.')
        messages = []
        for message in row["messages"][-MAX_MESSAGES:]:
            if (isinstance(message, dict) and message.get("role") in {"user", "assistant"}
                    and isinstance(message.get("content"), str)
                    and len(message["content"]) <= MAX_MESSAGE):
                item={"role": message["role"], "content": message["content"]}
                try:
                    refs=self._source_refs(message.get('sources'))
                except (ValueError,TypeError):
                    refs=[]
                if refs and message['role']=='assistant':
                    item['sources']=refs
            stream_id=message.get('stream_id')
            stream_status=message.get('stream_status')
            if (message['role']=='assistant' and isinstance(stream_id,str)
                    and CONVERSATION_ID.fullmatch(stream_id)
                    and stream_status in {'generating','complete','cancelled','failed'}):
                item['stream_id']=stream_id
                item['stream_status']=stream_status
            messages.append(item)
        row["messages"] = messages
        row['project_id'] = project_id
        return row

    def set_project(self, conversation_id: str, project_id: str | None) -> dict:
        if project_id is not None and (not isinstance(project_id, str)
                or not re.fullmatch(r'[a-f0-9]{64}', project_id)):
            raise ValueError('Project identity is invalid.')
        with self._lock:
            row = self.load(conversation_id)
            row['project_id'] = project_id
            self._save(row)
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

    def add_assistant(self, conversation_id: str, content: str, *, sources=None) -> dict:
        if not isinstance(content, str) or len(content) > MAX_MESSAGE:
            raise ValueError("Assistant response exceeds the local conversation limit.")
        with self._lock:
            row = self.load(conversation_id)
            message={"role": "assistant", "content": content}
            refs=self._source_refs(sources)
            if refs:
                message['sources']=refs
            row["messages"].append(message)
            self._save(row)
            return row

    def start_assistant_stream(self, conversation_id: str, stream_id: str) -> dict:
        if not isinstance(stream_id,str) or not CONVERSATION_ID.fullmatch(stream_id):
            raise ValueError('Assistant stream identity is invalid.')
        with self._lock:
            row=self.load(conversation_id)
            if any(message.get('stream_status')=='generating' for message in row['messages']):
                raise ValueError('This conversation already has a generating response.')
            row['messages'].append({'role':'assistant','content':'','stream_id':stream_id,
                                    'stream_status':'generating'})
            self._save(row)
            return row

    def update_assistant_stream(self, conversation_id: str, stream_id: str, content: str, *,
                                status='generating', sources=None) -> dict:
        if (not isinstance(stream_id,str) or not CONVERSATION_ID.fullmatch(stream_id)
                or not isinstance(content,str) or len(content)>MAX_MESSAGE
                or status not in {'generating','complete','cancelled','failed'}):
            raise ValueError('Assistant stream update is invalid.')
        refs=self._source_refs(sources)
        with self._lock:
            row=self.load(conversation_id)
            message=next((item for item in reversed(row['messages'])
                          if item.get('role')=='assistant' and item.get('stream_id')==stream_id),None)
            if message is None:
                raise ValueError('Assistant stream was not found.')
            message['content']=content
            message['stream_status']=status
            if refs:
                message['sources']=refs
            else:
                message.pop('sources',None)
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
