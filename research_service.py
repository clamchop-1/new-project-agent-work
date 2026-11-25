"""Task-scoped public research over the existing bounded web retrieval adapter."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Protocol
from urllib.parse import urlsplit

from agent_core import AgentError, atomic_write, exclusive, no_secrets

MAX_OBJECTIVE_CHARS = 300
MAX_SEARCH_REQUESTS = 1
MAX_PAGE_REQUESTS = 3
MAX_ELAPSED_SECONDS = 55
MAX_TOTAL_CONTENT_CHARS = 36_000
MAX_PAGE_CHARS = 16_000
MAX_CONTEXT_CHARS = 6_000
MAX_AUDIT_BYTES = 2_000_000
PROPOSAL_TTL_SECONDS = 180
RESUME_TTL_SECONDS = 60
SOURCE_TTL_SECONDS = 1800
CACHE_TTL_SECONDS = 300
MIN_REQUEST_INTERVAL_SECONDS = 2
MAX_RESEARCH_SESSIONS_PER_HOUR = 30


class ResearchProvider(Protocol):
    provider_id: str

    def search(self, query: str, limit: int) -> dict: ...
    def read(self, url: str) -> dict: ...


class DuckDuckGoHTMLProvider:
    """Adapter for the existing public DuckDuckGo HTML search and HTTPS reader."""
    provider_id = 'duckduckgo_html'

    def search(self, query: str, limit: int) -> dict:
        from web_retrieval import search_web
        return search_web(query, limit=limit)

    def read(self, url: str) -> dict:
        from web_retrieval import read_web_page
        return read_web_page(url)


class FirecrawlKeylessProvider:
    """Official keyless Firecrawl Search API; page fetching stays local and pinned."""
    provider_id = 'firecrawl_keyless'
    endpoint = 'https://api.firecrawl.dev/v2/search'

    def search(self, query: str, limit: int) -> dict:
        from web_retrieval import _PinnedHTTPSConnection, _safe_url, USER_AGENT
        if type(limit) is not int or not 1 <= limit <= 5:
            raise AgentError('Research search result limit is invalid.')
        host,address,target,canonical=_safe_url(self.endpoint)
        if canonical != self.endpoint or host != 'api.firecrawl.dev':
            raise AgentError('Configured search provider endpoint failed its fixed-host check.')
        body=json.dumps({'query':query,'limit':limit,'sources':['web']},
            ensure_ascii=False,separators=(',',':')).encode('utf-8')
        connection=_PinnedHTTPSConnection(host,address,timeout=12)
        try:
            connection.request('POST',target,body=body,headers={
                'Host':host,'User-Agent':USER_AGENT,'Accept':'application/json',
                'Content-Type':'application/json','Content-Length':str(len(body)),
                'Accept-Encoding':'identity','Connection':'close'})
            response=connection.getresponse()
            if 300 <= response.status < 400:
                raise AgentError('Search provider redirect refused.')
            if response.status < 200 or response.status >= 300:
                raise AgentError(f'Search provider returned HTTP {response.status}.')
            content_type=response.getheader('Content-Type','').casefold()
            if 'application/json' not in content_type:
                raise AgentError('Search provider returned an unsupported response type.')
            length=response.getheader('Content-Length')
            if length and int(length)>256_000:
                raise AgentError('Search provider response exceeds its size limit.')
            raw=response.read(256_001)
            if len(raw)>256_000:
                raise AgentError('Search provider response exceeds its size limit.')
            result=json.loads(raw.decode('utf-8'))
            if not isinstance(result,dict) or result.get('success') is not True:
                raise AgentError('Search provider did not return a successful result set.')
            data=result.get('data')
            if not isinstance(data,dict):
                raise AgentError('Search provider result schema is invalid.')
            rows=data.get('web',[])
            if not isinstance(rows,list):
                raise AgentError('Search provider result schema is invalid.')
            cleaned=[]
            for row in rows[:limit]:
                if not isinstance(row,dict) or not isinstance(row.get('url'),str):
                    continue
                cleaned.append({'url':row['url'],'title':str(row.get('title') or '')[:300],
                    'snippet':str(row.get('description') or '')[:500]})
            return {'results':cleaned,'provider':self.provider_id,'untrusted_content':True}
        except AgentError:
            raise
        except (OSError,ValueError,UnicodeError,json.JSONDecodeError) as exc:
            raise AgentError(f'Search provider request failed: {type(exc).__name__}.') from exc
        finally:
            connection.close()

    def read(self, url: str) -> dict:
        from web_retrieval import read_web_page
        return read_web_page(url)


def _objective_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def sanitize_research_query(value: str) -> str:
    """Keep public search limited to the user question; discard obvious local data."""
    if not isinstance(value, str) or not value.strip() or len(value) > 12_000:
        raise AgentError('Research question must contain 1 to 12,000 characters.')
    no_secrets(value)
    query = re.sub(r'```[\s\S]*?```', ' ', value)
    query = re.sub(r'`[^`\n]{1,500}`', ' ', query)
    query = re.sub(r'(?i)\b[A-Z]:\\(?:[^\s<>|"?*]+\\?)+', ' ', query)
    query = re.sub(r'(?<!\w)/(?:Users|home|private|var|tmp|mnt|workspaces)/[^\s,;]+', ' ', query)
    query = re.sub(r'(?m)^\s*(?:Traceback \(most recent call last\):|File ".*", line \d+.*)$', ' ', query)
    query = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', ' ', query)
    query = re.sub(r'\s+', ' ', query).strip()
    if not query or len(query) > MAX_OBJECTIVE_CHARS:
        raise AgentError('The question contains local code or paths; rewrite it as a short public research topic.')
    no_secrets(query)
    return query


def _relevant_excerpt(content, query, maximum=2000):
    terms={term.casefold() for term in re.findall(r'[A-Za-z0-9_]{3,}',query)}
    sentences=[part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+',content) if part.strip()]
    ranked=sorted(enumerate(sentences),key=lambda item:(-sum(
        term in set(re.findall(r'[A-Za-z0-9_]{3,}',item[1].casefold())) for term in terms),item[0]))
    selected=[];size=0
    for _,sentence in ranked:
        piece=sentence[:maximum-size]
        if piece:
            selected.append(piece);size+=len(piece)+1
        if size>=maximum:break
    return '\n'.join(selected)[:maximum]


class ResearchAuthorizationService:
    """One-use approval grants bound to one conversation, model and objective."""

    def __init__(self, audit_root=None, provider: ResearchProvider | None = None, *, clock=time.monotonic):
        self.provider = provider or FirecrawlKeylessProvider()
        self._clock = clock
        self._lock = threading.RLock()
        self._proposals = {}
        self._resumes = {}
        self._sources = {}
        self._cache = {}
        self._last_research_at = None
        self._research_times = []
        self.audit_path = None
        self.audit_lock = None
        if audit_root is not None:
            from pathlib import Path
            root = Path(audit_root)
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise AgentError('Research audit storage cannot be linked.')
            self.audit_path = root / 'research-audit.jsonl'
            self.audit_lock = root / 'research-audit.lock'

    @staticmethod
    def _scope(conversation_id, objective, model_key):
        if (not isinstance(conversation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', conversation_id)
                or not isinstance(model_key, str) or not model_key or len(model_key) > 160):
            raise AgentError('Research authorization scope is invalid.')
        query = sanitize_research_query(objective)
        return query, _objective_digest(query)

    def _audit(self, record):
        if self.audit_path is None:
            return
        encoded = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        owner = uuid.uuid4().hex
        with exclusive(self.audit_lock, owner):
            if self.audit_path.exists():
                if self.audit_path.is_symlink() or self.audit_path.stat().st_size > MAX_AUDIT_BYTES:
                    raise AgentError('Research audit is linked or exceeds its storage limit.')
                prior = self.audit_path.read_bytes()
            else:
                prior = b''
            if len(prior) + len(encoded) > MAX_AUDIT_BYTES:
                raise AgentError('Research audit reached its storage limit; no online request was made.')
            atomic_write(self.audit_path, prior + encoded)

    def _audit_row(self, state, conversation_id, digest, *, session_id=None, search_requests=0,
                   page_requests=0, error_code=None):
        return {'schema_version': 1, 'at': datetime.now(timezone.utc).isoformat(),
            'session_id': session_id or uuid.uuid4().hex,
            'conversation_sha256': _objective_digest(conversation_id),
            'objective_sha256': digest, 'provider_id': self.provider.provider_id,
            'state': state, 'search_requests': search_requests,
            'page_requests': page_requests, 'error_code': error_code}

    def propose(self, conversation_id, objective, model_key):
        query, digest = self._scope(conversation_id, objective, model_key)
        proposal_id = secrets.token_urlsafe(32)
        expires = self._clock() + PROPOSAL_TTL_SECONDS
        self._audit(self._audit_row('approval_requested', conversation_id, digest))
        with self._lock:
            self._expire()
            if len(self._proposals) >= 128:
                raise AgentError('Too many pending research approvals; finish or cancel one first.')
            self._proposals[proposal_id] = {'conversation_id': conversation_id,
                'objective_sha256': digest, 'model_key': model_key, 'query': query,
                'provider_id':self.provider.provider_id,
                'expires': expires}
        return {'proposal_id': proposal_id,
            'objective': query,
            'provider_id':self.provider.provider_id,
            'third_party_query':True,
            'limits': {'search_requests': MAX_SEARCH_REQUESTS, 'pages': MAX_PAGE_REQUESTS,
                'time_seconds': MAX_ELAPSED_SECONDS, 'page_chars': MAX_PAGE_CHARS,
                'expires_seconds': PROPOSAL_TTL_SECONDS}}

    def decide(self, proposal_id, conversation_id, objective, model_key, decision):
        if decision not in {'approved', 'offline', 'cancel'}:
            raise AgentError('Research decision is invalid.')
        query, digest = self._scope(conversation_id, objective, model_key)
        with self._lock:
            self._expire()
            pending = self._proposals.pop(proposal_id, None)
            if (pending is None or pending['conversation_id'] != conversation_id
                    or pending['objective_sha256'] != digest or pending['model_key'] != model_key
                    or pending['provider_id'] != self.provider.provider_id):
                raise AgentError('Research approval expired or does not match this conversation and question.')
            if decision == 'cancel':
                state = 'cancelled'
                resume_id = None
            else:
                resume_id = secrets.token_urlsafe(32)
                self._resumes[resume_id] = {**pending, 'decision': decision,
                    'expires': self._clock() + RESUME_TTL_SECONDS}
                state = 'approved' if decision == 'approved' else 'continued_offline'
        self._audit(self._audit_row(state, conversation_id, digest))
        return {'decision': decision, 'resume_id': resume_id, 'objective': query}

    def consume_resume(self, resume_id, conversation_id, objective, model_key, decision):
        query, digest = self._scope(conversation_id, objective, model_key)
        with self._lock:
            self._expire()
            row = self._resumes.pop(resume_id, None)
        if (row is None or row['conversation_id'] != conversation_id
                or row['objective_sha256'] != digest or row['model_key'] != model_key
                or row['decision'] != decision):
            raise AgentError('Research authorization expired, was already used, or does not match this task.')
        session = {'session_id': uuid.uuid4().hex, 'conversation_id': conversation_id,
            'objective_sha256': digest, 'query': query, 'provider_id': self.provider.provider_id,
            'search_requests': MAX_SEARCH_REQUESTS, 'page_requests': MAX_PAGE_REQUESTS,
            'max_elapsed_seconds': MAX_ELAPSED_SECONDS, 'decision': decision}
        if decision == 'approved':
            self._audit(self._audit_row('research_started', conversation_id, digest,
                                         session_id=session['session_id']))
        return session

    def authorize_explicit(self, conversation_id, objective, model_key):
        query, digest = self._scope(conversation_id, objective, model_key)
        session = {'session_id': uuid.uuid4().hex, 'conversation_id': conversation_id,
            'objective_sha256': digest, 'query': query, 'provider_id': self.provider.provider_id,
            'search_requests': MAX_SEARCH_REQUESTS, 'page_requests': MAX_PAGE_REQUESTS,
            'max_elapsed_seconds': MAX_ELAPSED_SECONDS, 'decision': 'explicit_user_request'}
        self._audit(self._audit_row('explicit_research_started', conversation_id, digest,
                                     session_id=session['session_id']))
        return session

    def execute(self, session, cancel_event, progress):
        if (session.get('provider_id') != self.provider.provider_id
                or session.get('decision') not in {'approved', 'explicit_user_request'}):
            raise AgentError('Research session is not authorized for this provider.')
        query = sanitize_research_query(session['query'])
        digest = _objective_digest(query)
        if digest != session.get('objective_sha256'):
            raise AgentError('Research objective changed after authorization.')
        cache_key = (session['conversation_id'], digest, session['provider_id'])
        started = self._clock()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached['expires'] > started:
                self._audit(self._audit_row('research_cache_hit',session['conversation_id'],digest,
                    session_id=session['session_id']))
                return {'sources': [{**row, 'cached':True} for row in cached['sources']], 'cached': True,
                    'context':[{**row, 'cached':True} for row in cached['context']],
                    'session_id': session['session_id'], 'search_requests': 0, 'page_requests': 0,
                    'search_results_count':cached.get('search_results_count',len(cached['sources']))}
            self._research_times = [stamp for stamp in self._research_times if started-stamp < 3600]
            if (self._last_research_at is not None
                    and started-self._last_research_at < MIN_REQUEST_INTERVAL_SECONDS):
                raise AgentError('Online research is briefly rate limited. Wait a moment and try again.')
            if len(self._research_times) >= MAX_RESEARCH_SESSIONS_PER_HOUR:
                raise AgentError('The local research limit is 30 sessions per hour. Try again later.')
            self._last_research_at = started
            self._research_times.append(started)
        search_count = page_count = 0
        sources = []
        seen_urls = set()
        error_code = None
        try:
            if cancel_event.is_set():
                raise AgentError('Research was cancelled before the first request.')
            progress('Searching online…')
            search = self.provider.search(query, limit=5)
            search_count += 1
            rows = search.get('results', []) if isinstance(search, dict) else []
            search_results_count = min(len(rows),5) if isinstance(rows,list) else 0
            progress(f'Retrieving up to {min(MAX_PAGE_REQUESTS, len(rows))} public sources…')
            total_chars = 0
            for row in rows:
                if cancel_event.is_set():
                    raise AgentError('Research was cancelled; no further sources were requested.')
                if self._clock() - started > MAX_ELAPSED_SECONDS:
                    raise AgentError('Research reached its task time limit.')
                url = row.get('url') if isinstance(row, dict) else None
                if not isinstance(url, str) or url in seen_urls:
                    continue
                try:
                    from web_retrieval import _safe_url
                    _, _, _, url = _safe_url(url)
                except AgentError:
                    continue
                seen_urls.add(url)
                if page_count >= MAX_PAGE_REQUESTS:
                    break
                page_count += 1
                try:
                    source = self.provider.read(url)
                except AgentError:
                    # A blocked or unavailable source is not treated as retrieved evidence.
                    continue
                if (not isinstance(source, dict) or source.get('untrusted_content') is not True
                        or not isinstance(source.get('content'), str)
                        or source.get('url') != url
                        or not re.fullmatch(r'[0-9a-f]{64}', source.get('content_sha256', ''))
                        or hashlib.sha256(source['content'].encode('utf-8')).hexdigest() != source['content_sha256']):
                    continue
                content = source['content'][:MAX_PAGE_CHARS]
                if total_chars + len(content) > MAX_TOTAL_CONTENT_CHARS:
                    break
                total_chars += len(content)
                sources.append({**source, 'content': content,
                    'provider_id': self.provider.provider_id, 'publication_date':source.get('publication_date'),
                    'research_excerpt':_relevant_excerpt(content,query),
                    'research_objective_sha256':digest,'cached': False})
            if cancel_event.is_set():
                raise AgentError('Research was cancelled; completed sources were not sent for generation.')
            context = []
            remaining = MAX_CONTEXT_CHARS
            for source in sources:
                excerpt = source['research_excerpt'][:min(2000, remaining)]
                if not excerpt:
                    break
                context.append({'title': source['title'], 'url': source['url'],
                    'retrieved_at': source['retrieved_at'], 'publication_date':source.get('publication_date'),
                    'content_sha256':source['content_sha256'], 'excerpt':excerpt,
                    'source_id':source['source_id'], 'provider_id':self.provider.provider_id,
                    'untrusted_content':True})
                remaining -= len(excerpt)
            with self._lock:
                self._cache[cache_key] = {'expires': self._clock() + CACHE_TTL_SECONDS,
                    'sources': [dict(row) for row in sources], 'context':[dict(row) for row in context],
                    'search_results_count':search_results_count}
                self._sources = {key:value for key,value in self._sources.items()
                                 if value['expires'] > self._clock()}
                for source in sources:
                    self._sources[source['source_id']] = {'conversation_id':session['conversation_id'],
                        'source':dict(source), 'expires':self._clock()+SOURCE_TTL_SECONDS}
            self._audit(self._audit_row('research_completed', session['conversation_id'], digest,
                session_id=session['session_id'], search_requests=search_count, page_requests=page_count))
            return {'sources':sources,'context':context,'cached':False,
                'search_results_count':search_results_count,
                'session_id':session['session_id'],'search_requests':search_count,'page_requests':page_count}
        except BaseException as exc:
            error_code = 'cancelled' if cancel_event.is_set() else type(exc).__name__
            self._audit(self._audit_row('research_cancelled' if cancel_event.is_set() else 'research_failed',
                session['conversation_id'], digest, session_id=session['session_id'],
                search_requests=search_count, page_requests=page_count, error_code=error_code))
            raise

    def source_for_retention(self, source_id, conversation_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
            raise AgentError('Research source identity is invalid.')
        with self._lock:
            self._expire()
            row = self._sources.get(source_id)
            if row is None or row['conversation_id'] != conversation_id:
                raise AgentError('Research source is unavailable or belongs to another conversation.')
            return dict(row['source'])

    def _expire(self):
        now = self._clock()
        self._proposals = {key:value for key,value in self._proposals.items() if value['expires'] > now}
        self._resumes = {key:value for key,value in self._resumes.items() if value['expires'] > now}
        self._sources = {key:value for key,value in self._sources.items() if value['expires'] > now}
        self._cache = {key:value for key,value in self._cache.items() if value['expires'] > now}
