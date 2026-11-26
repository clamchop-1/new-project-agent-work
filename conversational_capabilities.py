"""Trusted, bounded conversational actions over registered project intelligence.

The model never chooses Python handlers or executes commands. A deterministic
intent router creates a proposal, and this module revalidates it against a
fixed registry before calling existing read-only project APIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from typing import Any, Callable

from agent_core import AgentError, atomic_write, encoded, exclusive


MAX_QUERY = 240
MAX_RESULT_CHARS = 8_000
MAX_SOURCE_BYTES = 7_000


@dataclass(frozen=True)
class Capability:
    capability_id: str
    purpose: str
    input_schema: dict[str, str]
    output_schema: str
    handler: str
    host: str
    availability: str
    permissions: tuple[str, ...]
    approval: str
    risk: str
    execution_limit: str
    validation: str
    recovery: str
    verification: str


CAPABILITIES = (
    Capability('projects.list', 'List explicitly registered projects', {}, 'project summaries',
        'list_projects', 'Windows dashboard', 'available when project registry is configured',
        ('registered-project metadata',), 'none', 'low', '24 projects; read-only local Git inspection',
        'project identity revalidated; filesystem roots omitted from model context',
        'report unavailable projects without changing registration', 'implemented; unit tested'),
    Capability('project.select', 'Bind a conversation to one registered project',
        {'name': 'exact registered display name'}, 'selected project summary', 'select_project',
        'Windows dashboard', 'available when project registry is configured',
        ('select registered project for this conversation',), 'conversation-scoped user request',
        'low', 'one project per conversation', 'unambiguous registered name match; identity revalidated',
        'conversation may be unbound using the Projects control', 'implemented; unit tested'),
    Capability('project.status', 'Inspect local Git status and cached remote references',
        {}, 'bounded Git snapshot', 'project_status', 'Windows dashboard',
        'available for a selected registered project', ('read-only registered project metadata',),
        'none', 'low', 'existing Git output and timeout bounds', 'registered identity and local root revalidated',
        'return bounded error; no Git mutation or fetch', 'implemented; unit tested'),
    Capability('project.files', 'List eligible source, documentation, and test paths',
        {}, 'bounded relative path list', 'project_files', 'Windows dashboard',
        'available for a selected registered project', ('read-only registered project inventory',),
        'none', 'low', '120 paths per response', 'existing Git inventory and source exclusion policy',
        'return bounded error without walking outside repository', 'implemented; unit tested'),
    Capability('project.read', 'Read one explicitly named eligible source or documentation file',
        {'path': 'repository-relative eligible path'}, 'bounded file text and hash-bound citation',
        'read_project_file', 'Windows dashboard', 'available for a selected registered project',
        ('read-only registered project source',), 'none', 'low', 'one UTF-8 file; 7,000 bytes',
        'inventory membership, source exclusions, path containment, secret scan, content hash',
        'reject linked, oversized, binary, or secret-bearing files', 'implemented; unit tested'),
    Capability('project.search', 'Search indexed project passages and return validated citations',
        {'query': 'nonempty search text, max 240 characters'}, 'source passages with citations',
        'project_search', 'Windows dashboard', 'available for a selected indexed project',
        ('read-only registered project source',), 'none', 'low', '4 passages; 8,000 context characters',
        'citation hashes and current file bytes revalidated', 'fall back to a clear retrieval error; no source writes',
        'implemented; unit tested'),
    Capability('knowledge.inventory', 'List accessible uploaded documents and indexed coverage',
        {'name_filter': 'optional exact title or filename substring'}, 'bounded document inventory and coverage totals',
        'knowledge_inventory', 'Windows dashboard', 'available in the active project library and current conversation scope',
        ('read-only project document metadata',), 'none', 'low', '20 documents per response; no document text',
        'project identity and global/current-conversation scope enforced by library',
        'return a bounded inventory error; no index changes', 'implemented; unit tested'),
    Capability('knowledge.latest', 'Identify and inspect the most recent accessible upload', {},
        'latest document metadata and ingestion state', 'knowledge_latest', 'Windows dashboard',
        'available in the active project library and current conversation scope',
        ('read-only project document metadata',), 'none', 'low', 'one document',
        'ordered by trusted upload timestamp; no semantic reference resolution',
        'return unavailable when no accessible upload exists', 'implemented; unit tested'),
    Capability('knowledge.inspect', 'Inspect one accessible document and its processing coverage',
        {'reference': 'exact or unambiguous title, filename, or document identity'},
        'document metadata, processing state and coverage', 'knowledge_inspect', 'Windows dashboard',
        'available in the active project library and current conversation scope',
        ('read-only project document metadata',), 'none', 'low', 'one document',
        'exact identity or unambiguous title/filename match only',
        'return ambiguity rather than guessing a document', 'implemented; unit tested'),
    Capability('knowledge.sections', 'List extracted chapter, section, or page navigation labels',
        {'reference': 'exact or unambiguous title, filename, or document identity'},
        'bounded section list and source page ranges', 'knowledge_sections', 'Windows dashboard',
        'available for an accessible indexed document', ('read-only indexed passages',), 'none', 'low',
        '40 sections per response', 'document scope and stored passage hashes validated',
        'report missing heading metadata without inventing a chapter list', 'implemented; unit tested'),
    Capability('knowledge.chapter', 'Read a selected extracted chapter or section progressively',
        {'reference': 'exact or unambiguous title, filename, or document identity',
         'section': 'chapter or heading label'}, 'bounded section text, coverage and source page range',
        'knowledge_chapter', 'Windows dashboard', 'available when indexed headings are present',
        ('read-only indexed passages',), 'none', 'low', '5,000 characters per operation',
        'selected document and each passage hash verified; truncation is explicit',
        'report absent heading or remaining content; no guessed pages', 'implemented; unit tested'),
    Capability('knowledge.pages', 'Read a verified PDF page range from original stored bytes',
        {'reference': 'exact or unambiguous title, filename, or document identity',
         'start_page': 'integer, 1-based', 'end_page': 'integer, at most 20 pages from start'},
        'page text and verified document/page references', 'knowledge_pages', 'Windows dashboard',
        'available for an accessible stored PDF', ('read-only original document bytes',), 'none', 'low',
        '20 pages and 6,000 extracted characters', 'original SHA-256 and page count rechecked',
        'report empty or failed pages explicitly; no OCR fallback', 'implemented; unit tested'),
    Capability('knowledge.search', 'Search the library or one named document with bounded cited passages',
        {'query': 'nonempty search text', 'reference': 'empty for all documents or exact/unambiguous document reference'},
        'relevant indexed passages with page and chunk citations', 'knowledge_search', 'Windows dashboard',
        'available in the active project library and current conversation scope',
        ('read-only indexed passages',), 'none', 'low', '4 passages per response',
        'document filters and conversation scope enforced; stored passage hashes checked',
        'distinguish no matching excerpt from unavailable document', 'implemented; unit tested'),
    Capability('knowledge.adjacent', 'Read neighboring chunks for a cited passage in the same document',
        {'reference': 'exact or unambiguous document reference', 'chunk_id': '64-character stored chunk identity'},
        'bounded adjacent excerpts with verified citations', 'knowledge_adjacent', 'Windows dashboard',
        'available for an accessible indexed document', ('read-only indexed passages',), 'none', 'low',
        'at most three adjacent passages', 'chunk identity must belong to selected document; hashes checked',
        'report stale or mismatched chunk identity; no other documents read', 'implemented; unit tested'),
)


class CapabilityRegistry:
    """Code-owned registry; external content cannot add executable handlers."""
    def __init__(self, capabilities=CAPABILITIES):
        self._items = {item.capability_id: item for item in capabilities}
        if len(self._items) != len(capabilities):
            raise ValueError('Capability identifiers must be unique.')

    def get(self, capability_id: str) -> Capability:
        item = self._items.get(capability_id)
        if item is None:
            raise AgentError('Requested capability is not registered.')
        return item

    def describe(self) -> list[dict[str, Any]]:
        return [item.__dict__.copy() for item in self._items.values()]

    def chat_summary(self, project_selected: bool) -> str:
        items = []
        for item in self._items.values():
            if item.capability_id.startswith('project.') and not project_selected:
                continue
            items.append(f"- {item.capability_id}: {item.purpose}; {item.availability}; "
                         f"approval: {item.approval}.")
        items.extend((
            '- Repository registration is available in chat after a one-use, expiring, conversation-bound application confirmation; it grants local read-only indexing.',
            '- A selected project can offer only its matching code-owned pinned test profile through the Windows Broker after an application approval. Results are untrusted evidence and must cite a verified receipt.',
            '- Source changes, arbitrary commands, builds, and dependency installation are not available as conversational capabilities.',
            '- Models cannot grant themselves permissions or call unregistered capabilities.'
        ))
        return '\n'.join(items)


@dataclass(frozen=True)
class ActionProposal:
    capability_id: str
    arguments: dict[str, Any]


class IntentRouter:
    """Only recognizes clear user-directed read-only project operations."""
    def propose(self, text: str, *, selected_project: bool) -> ActionProposal | None:
        if not isinstance(text, str):
            return None
        # Natural chat commonly ends requests with sentence punctuation. Strip
        # only terminal punctuation before applying the strict intent grammar;
        # keep punctuation inside a quoted project name intact.
        value = text.strip().rstrip('?.!').rstrip()
        folded = value.casefold()
        if re.fullmatch(r'(what )?(projects|registered projects)( do i have)?( registered)?\??', folded):
            return ActionProposal('projects.list', {})
        select = re.fullmatch(
            r'(open|select|use) (?:the )?(?:registered )?project ["\']?(.+?)["\']?\s*',
            value, re.I)
        if select:
            return ActionProposal('project.select', {'name': select.group(2).strip()})
        if re.fullmatch(r'(what did i just upload|what did i upload|can you read (the )?(latest|most recent) document|read (the )?(latest|most recent) document)',folded):
            return ActionProposal('knowledge.latest',{})
        if re.fullmatch(r'(how many pages are indexed|how many documents are indexed|what is the knowledge library status)',folded):
            return ActionProposal('knowledge.inventory',{'name_filter':''})
        if re.fullmatch(r'(what documents (are|do i have)( available)?|what programming documentation do i have|which documents did i upload)',folded):
            return ActionProposal('knowledge.inventory',{'name_filter':''})
        inventory=re.fullmatch(r'(list|show)( me)?( all)?( my| available| accessible| uploaded)? (documents|uploads|knowledge library)',folded)
        if inventory:
            return ActionProposal('knowledge.inventory',{'name_filter':''})
        named_inventory=re.fullmatch(r'(show|list)(?: me)? all my (.+?) (?:docs?|documents|documentation)',value,re.I)
        if named_inventory:
            return ActionProposal('knowledge.inventory',{'name_filter':named_inventory.group(2).strip()[:80]})
        if re.fullmatch(r'(status|progress) of (the )?(knowledge library|my documents)',folded):
            return ActionProposal('knowledge.inventory',{'name_filter':''})
        chapter=re.fullmatch(r'(read|summarize) (?:chapter|section) (.+?)(?: of (.+))?',value,re.I)
        if chapter:
            return ActionProposal('knowledge.chapter',{'reference':(chapter.group(3) or 'latest').strip(),
                'section':(chapter.group(2) or '').strip()})
        pages=re.fullmatch(r'read pages? (\d+)(?:\s*[-–]\s*(\d+))?(?: of (.+))?',value,re.I)
        if pages:
            first=int(pages.group(1));last=int(pages.group(2) or first)
            return ActionProposal('knowledge.pages',{'reference':(pages.group(3) or 'latest').strip(),
                'start_page':first,'end_page':last})
        inspect=re.fullmatch(r'(inspect|describe|check the status of) (?:the )?document (.+)',value,re.I)
        if inspect:
            return ActionProposal('knowledge.inspect',{'reference':inspect.group(2).strip()})
        scoped_search=re.fullmatch(r'(?:search|find) (?:in )?(?:the )?(.+?) (?:documentation|document|manual) for (.+)',value,re.I)
        if scoped_search:
            return ActionProposal('knowledge.search',{'query':scoped_search.group(2).strip()[:240],
                'reference':scoped_search.group(1).strip()})
        search_examples=re.fullmatch(r'(?:find|search for) (?:examples? of )?(.+)',value,re.I)
        if search_examples and re.search(r'\b(examples?|decorators?|api|selenium|python|documentation|docs?)\b',folded):
            return ActionProposal('knowledge.search',{'query':search_examples.group(1).strip()[:240],'reference':''})
        search_in=re.fullmatch(r'(?:search|find) (.+) in (?:the )?(.+?) (?:documentation|document|manual)',value,re.I)
        if search_in:
            return ActionProposal('knowledge.search',{'query':search_in.group(1).strip()[:240],
                'reference':search_in.group(2).strip()})
        if not selected_project:
            return None
        if re.fullmatch(r'(show|get|inspect|check) (the )?(git )?status\??', folded):
            return ActionProposal('project.status', {})
        if re.fullmatch(r'(list|show) (the )?(project )?(source )?files\??', folded):
            return ActionProposal('project.files', {})
        match = re.fullmatch(r'(search|find)(?: in (?:the )?project)?(?: for)?\s+(.+)', value, re.I)
        if match and match.group(2).strip():
            return ActionProposal('project.search', {'query': match.group(2).strip()[:MAX_QUERY]})
        if re.fullmatch(r'(where is|where are|find) .{1,180}(implemented|defined)\??', value, re.I):
            return ActionProposal('project.search', {'query': value[:MAX_QUERY]})
        read = re.fullmatch(r'(read|show|open) (?:the )?file ["\']?(.+?)["\']?\s*', value, re.I)
        if read:
            return ActionProposal('project.read', {'path': read.group(2).strip().replace('\\', '/')})
        return None


class CapabilityDispatcher:
    def __init__(self, project_intelligence, chat_store, *, knowledge_store=None,
                 registry=None, router=None):
        self.projects = project_intelligence
        self.conversations = chat_store
        self.knowledge = knowledge_store
        self.registry = registry or CapabilityRegistry()
        self.router = router or IntentRouter()
        self._audit_lock = threading.Lock()
        self.audit_path = (self.conversations.root / 'capability-audit.jsonl'
                           if self.conversations is not None else None)

    def propose(self, text: str, *, project_id: str | None) -> ActionProposal | None:
        return self.router.propose(text, selected_project=project_id is not None)

    def execute(self, proposal: ActionProposal, *, conversation_id: str,
                current_project_id: str | None, cancel_event=None) -> dict[str, Any]:
        if not isinstance(proposal, ActionProposal) or not isinstance(proposal.arguments, dict):
            raise AgentError('Action proposal is malformed.')
        capability = self.registry.get(proposal.capability_id)
        if set(proposal.arguments) != set(capability.input_schema):
            raise AgentError('Action arguments do not match the registered schema.')
        if cancel_event is not None and cancel_event.is_set():
            raise AgentError('Action was cancelled before execution.')
        if self.projects is None and not proposal.capability_id.startswith('knowledge.'):
            raise AgentError('Project registry is unavailable.')
        if capability.capability_id.startswith('knowledge.'):
            if self.knowledge is None:
                raise AgentError('Knowledge Library is unavailable.')
            audit_project_id=self.knowledge.project.identity
        else:
            audit_project_id=current_project_id
        action_id = __import__('uuid').uuid4().hex
        self._write_audit(action_id, 'started', conversation_id, capability.capability_id,
                          audit_project_id, proposal.arguments)
        try:
            if capability.capability_id == 'projects.list':
                result = self._list_projects()
            elif capability.capability_id == 'project.select':
                result = self._select(proposal.arguments['name'], conversation_id)
            elif capability.capability_id.startswith('knowledge.'):
                result = self._execute_knowledge(capability.capability_id,
                    proposal.arguments, conversation_id, cancel_event)
            else:
                if current_project_id is None:
                    raise AgentError('Select a registered project before this action.')
                self.projects._record(current_project_id)
                if capability.capability_id == 'project.status':
                    result = self._status(current_project_id)
                elif capability.capability_id == 'project.files':
                    result = self._files(current_project_id)
                elif capability.capability_id == 'project.read':
                    result = self._read(current_project_id, proposal.arguments['path'])
                elif capability.capability_id == 'project.search':
                    result = self._search(current_project_id, proposal.arguments['query'], cancel_event)
                else:
                    raise AgentError('Capability handler is unavailable.')
            result_bytes = encoded(result)
            if len(result_bytes) > MAX_RESULT_CHARS:
                raise AgentError('Capability result exceeded its response budget.')
            self._write_audit(action_id, 'completed', conversation_id, capability.capability_id,
                              audit_project_id, proposal.arguments, result_bytes)
            return {'capability_id': capability.capability_id, 'result': result}
        except Exception:
            self._write_audit(action_id, 'failed', conversation_id, capability.capability_id,
                              audit_project_id, proposal.arguments)
            raise

    def _execute_knowledge(self, capability_id, arguments, conversation_id, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise AgentError('Knowledge request was cancelled.')
        if capability_id=='knowledge.inventory':
            return self.knowledge.document_inventory(conversation_id=conversation_id,
                name_filter=arguments['name_filter'] or None,limit=20)
        if capability_id=='knowledge.latest':
            return self.knowledge.inspect_document(None,conversation_id=conversation_id)
        if capability_id=='knowledge.inspect':
            return self.knowledge.inspect_document(arguments['reference'],conversation_id=conversation_id)
        if capability_id=='knowledge.sections':
            return self.knowledge.list_sections(arguments['reference'],conversation_id=conversation_id,limit=40)
        if capability_id=='knowledge.chapter':
            return self.knowledge.read_section(arguments['reference'],arguments['section'],
                conversation_id=conversation_id)
        if capability_id=='knowledge.pages':
            return self.knowledge.read_pages(arguments['reference'],arguments['start_page'],
                arguments['end_page'],conversation_id=conversation_id)
        if capability_id=='knowledge.search':
            reference=arguments['reference'].strip()
            document_id=(self.knowledge._resolve_document(reference,conversation_id=conversation_id)['doc_id']
                         if reference else None)
            return self.knowledge.search(arguments['query'],conversation_id=conversation_id,limit=4,
                provider=getattr(self.knowledge,'_embedding_provider',None),cancel_event=cancel_event,
                document_id=document_id)
        if capability_id=='knowledge.adjacent':
            return self.knowledge.adjacent_chunks(arguments['reference'],arguments['chunk_id'],
                conversation_id=conversation_id,before=1,after=1)
        raise AgentError('Knowledge capability handler is unavailable.')

    def _write_audit(self, action_id, state, conversation_id, capability_id,
                     project_id, arguments, result_bytes=None):
        if self.audit_path is None:
            raise AgentError('Capability audit storage is unavailable; action was not run.')
        root = self.audit_path.parent
        with self._audit_lock, exclusive(
                self.audit_path.with_name(self.audit_path.name + '.lock'),
                'chat-capability-audit'):
            if (root.is_symlink() or not root.is_dir()
                    or (self.audit_path.exists() and
                        (self.audit_path.is_symlink() or not self.audit_path.is_file()
                         or self.audit_path.stat().st_nlink != 1))):
                raise AgentError('Capability audit storage failed its integrity check.')
            try:
                existing = self.audit_path.read_bytes() if self.audit_path.exists() else b''
            except OSError as exc:
                raise AgentError('Capability audit storage could not be read safely.') from exc
            if len(existing) > 1_000_000:
                raise AgentError('Capability audit storage reached its fixed size limit.')
            for line in existing.splitlines():
                try:
                    if not isinstance(json.loads(line), dict):
                        raise ValueError
                except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                    raise AgentError('Capability audit storage is malformed; preserve it for review.') from exc
            record = {'action_id': action_id, 'state': state,
                'conversation_id': conversation_id, 'capability_id': capability_id,
                'project_id': project_id, 'argument_sha256': hashlib.sha256(
                    encoded(arguments)).hexdigest(),
                'result_sha256': hashlib.sha256(result_bytes).hexdigest() if result_bytes is not None else None,
                'observed_at': datetime.now(timezone.utc).isoformat()}
            raw = existing + encoded(record) + b'\n'
            if len(raw) > 1_000_000:
                raise AgentError('Capability audit storage reached its fixed size limit.')
            atomic_write(self.audit_path, raw)

    def _list_projects(self):
        result = []
        for row in self.projects._load()['projects']:
            result.append({'name': row.get('display_name'), 'project_id': row.get('project_id'),
                'availability': 'registered; not checked',
                'indexing_status': row.get('indexing_status'),
                'last_refreshed_at': row.get('last_refreshed_at'),
                'scope': row.get('authorization_scope')})
        return {'projects': result}

    def _select(self, name, conversation_id):
        if not isinstance(name, str) or not name.strip() or len(name) > 180:
            raise AgentError('Project name is invalid.')
        matches = [row for row in self.projects._load()['projects']
                   if row.get('display_name', '').casefold() == name.strip().casefold()
                   or row.get('project_id', '').startswith(name.strip().casefold())]
        if len(matches) != 1:
            raise AgentError('Project name did not identify exactly one registered project.')
        project_id = matches[0]['project_id']
        self.projects._record(project_id)
        self.conversations.set_project(conversation_id, project_id)
        return {'selected': matches[0]['display_name'], 'project_id': project_id,
                'scope': matches[0].get('authorization_scope')}

    def _status(self, project_id):
        _, project = self.projects._record(project_id)
        from project_intelligence import ProjectIntelligence
        snapshot = ProjectIntelligence.snapshot(project)
        snapshot.pop('root', None)
        snapshot['fresh_remote_fetch'] = False
        snapshot['remote_relationship_note'] = 'Local cached Git references only; no network fetch was performed.'
        return snapshot

    def _files(self, project_id):
        _, project = self.projects._record(project_id)
        from project_intelligence import ProjectIntelligence, _source_allowed
        paths = [path.replace('\\', '/') for path in ProjectIntelligence._file_inventory(project)
                 if path and _source_allowed(path)]
        return {'paths': paths[:120], 'count_returned': min(120, len(paths)),
                'truncated': len(paths) > 120}

    def _read(self, project_id, relative):
        from project_intelligence import ProjectIntelligence, _source_allowed, _now
        from agent_core import file_hash, no_secrets
        if (not isinstance(relative, str) or not relative or len(relative) > 300
                or relative.startswith('/') or ':' in relative
                or any(part in {'', '.', '..'} for part in relative.split('/'))
                or not _source_allowed(relative)):
            raise AgentError('Choose an eligible repository-relative source path.')
        row, project = self.projects._record(project_id)
        names = {name.replace('\\', '/') for name in ProjectIntelligence._file_inventory(project)
                 if name}
        if relative not in names:
            raise AgentError('The file is not present in the registered repository inventory.')
        path = project.path(relative, allow_instructions=True)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES or path.stat().st_nlink != 1:
            raise AgentError('The selected file is not a bounded regular source file.')
        raw = path.read_bytes()
        document_sha = hashlib.sha256(raw).hexdigest()
        if hashlib.sha256(path.read_bytes()).hexdigest() != document_sha:
            raise AgentError('The file changed during its bounded read; retry after project refresh.')
        content = raw.decode('utf-8', errors='strict')
        no_secrets(content)
        commit = project.git('rev-parse', 'HEAD').strip()
        lines = content.splitlines()
        citation = {'project': row['display_name'], 'path': relative,
            'line_start': 1 if lines else 0, 'line_end': len(lines), 'section': None,
            'commit': commit, 'document_sha256': document_sha,
            'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            'retrieved_at': _now(), 'validated': True}
        return {'content': content, 'citation': citation}

    def validate_read_result(self, project_id, result):
        from project_intelligence import _source_allowed, _now
        from agent_core import file_hash
        try:
            _, project = self.projects._record(project_id)
            citation = result['citation']
            relative = citation['path']
            if not _source_allowed(relative):
                return None
            path = project.path(relative, allow_instructions=True)
            if (not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES
                    or path.stat().st_nlink != 1
                    or file_hash(path) != citation['document_sha256']
                    or project.git('rev-parse', 'HEAD').strip() != citation['commit']):
                return None
            return {**citation, 'validated': True, 'retrieved_at': _now()}
        except (AgentError, KeyError, OSError, TypeError, ValueError):
            return None

    def _search(self, project_id, query, cancel_event):
        if not isinstance(query, str) or not query.strip() or len(query.encode('utf-8')) > MAX_QUERY:
            raise AgentError('Project search query is empty or exceeds 240 characters.')
        if cancel_event is not None and cancel_event.is_set():
            raise AgentError('Action was cancelled before source retrieval.')
        result = self.projects.search(project_id, query.strip(), limit=4,
            embedding_provider=getattr(self.projects, 'embedding_provider', None))
        validated = self.projects.validate_results(project_id, result.get('results', []))
        passages = []
        used = 0
        for item in validated:
            content = item.get('content', '')[:min(2000, MAX_RESULT_CHARS - used)]
            if not content:
                break
            used += len(content)
            passages.append({key: item[key] for key in ('chunk_id', 'path', 'document_sha256',
                'content_sha256', 'line_start', 'line_end', 'content', 'citation')
                if key in item})
            if used >= MAX_RESULT_CHARS:
                break
        return {'project': self.projects._record(project_id)[0]['display_name'],
                'passages': passages, 'validated_count': len(passages),
                'semantic_available': bool(result.get('semantic_available', False))}
