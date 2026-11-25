"""Bounded, public HTTPS retrieval and a private source library."""

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import threading
import uuid

from agent_core import AgentError, atomic_write, exclusive, no_secrets

MAX_RESPONSE_BYTES = 1_000_000
MAX_TEXT_CHARS = 16_000
USER_AGENT = 'LocalCodingAgent/1.0 (public read-only retrieval)'
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_CACHE = {}


def _safe_url(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise AgentError('URL is missing or exceeds the 4096 character limit.')
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise AgentError('URL contains a control character.')
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise AgentError('Only public HTTPS URLs without embedded credentials are allowed.')
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').casefold()
    if host in ('localhost', 'metadata.google.internal') or host.endswith(('.localhost', '.local', '.internal')):
        raise AgentError('Local and metadata destinations are forbidden.')
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise AgentError('Private and reserved IP destinations are forbidden.')
        addresses = [str(ip)]
    except ValueError:
        try:
            records = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise AgentError('Could not resolve the public HTTPS host.') from exc
        addresses = list(dict.fromkeys(row[4][0] for row in records))
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise AgentError('Host resolves to a private or reserved address.')
    port = parsed.port or 443
    if port != 443:
        raise AgentError('Only standard HTTPS port 443 is allowed.')
    path = parsed.path or '/'
    target = urlunsplit(('', '', path, parsed.query, ''))
    authority = f'[{host}]' if ':' in host else host
    return host, addresses[0], target, urlunsplit(('https', authority, path, parsed.query, ''))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def fetch(url, limit=MAX_RESPONSE_BYTES, timeout=12):
    host, address, target, canonical = _safe_url(url)
    connection = _PinnedHTTPSConnection(host, address, timeout)
    try:
        connection.request('GET', target, headers={
            'Host': f'[{host}]' if ':' in host else host,
            'User-Agent': USER_AGENT, 'Accept': 'text/html,application/json,text/plain,*/*',
            'Accept-Encoding': 'identity', 'Connection': 'close',
        })
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise AgentError('Redirect refused. Open the final public HTTPS URL directly.')
        if response.status < 200 or response.status >= 300:
            raise AgentError(f'Public source returned HTTP {response.status}.')
        length = response.getheader('Content-Length')
        if length and int(length) > limit:
            raise AgentError('Public response exceeds its size limit.')
        body = response.read(limit + 1)
        if len(body) > limit:
            raise AgentError('Public response exceeds its size limit.')
        return canonical, response.getheader('Content-Type', ''), body
    except AgentError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
        raise AgentError(f'Public HTTPS retrieval failed: {type(exc).__name__}.') from exc
    finally:
        connection.close()


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.parts = []
        self.skip = 0
        self.in_title = False
        self.publication_date = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta' and not self.publication_date:
            key = (values.get('property') or values.get('name') or values.get('itemprop') or '').casefold()
            if key in {'article:published_time','datepublished','date','dc.date','pubdate'}:
                candidate = values.get('content') or values.get('datetime')
                if candidate and re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[tT][0-9:.+zZ-]+)?',candidate):
                    self.publication_date = candidate[:80]
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.skip += 1
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.skip:
            self.skip -= 1
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def read_web_page(url):
    _check_robots_policy(url)
    canonical, content_type, body = fetch(url)
    if 'html' not in content_type.casefold() and not body.lstrip().startswith(b'<'):
        raise AgentError('The URL did not return an HTML page.')
    parser = _VisibleText()
    parser.feed(body.decode('utf-8', errors='replace'))
    title = ' '.join(' '.join(parser.title).split())[:300]
    text = re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()[:MAX_TEXT_CHARS]
    if not text:
        raise AgentError('No readable page text was found.')
    return _source('webpage', canonical, title, text,
                   publication_date=parser.publication_date)


def _check_robots_policy(url):
    """Fail closed unless a public host's bounded robots policy permits the page."""
    host, _, _, canonical = _safe_url(url)
    now = datetime.now(timezone.utc).timestamp()
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(host)
        if cached and cached[0] > now:
            allowed = cached[1].can_fetch(USER_AGENT, canonical)
            if not allowed:
                raise AgentError('The source site robots policy disallows this page.')
            return
    robots_url = f'https://{host}/robots.txt'
    _, content_type, body = fetch(robots_url, limit=65_536, timeout=4)
    if content_type and 'text/plain' not in content_type.casefold():
        raise AgentError('Could not verify the source site robots policy.')
    try:
        policy = RobotFileParser(robots_url)
        policy.parse(body.decode('utf-8', errors='replace').splitlines())
    except (UnicodeError, ValueError) as exc:
        raise AgentError('Could not parse the source site robots policy.') from exc
    with _ROBOTS_LOCK:
        _ROBOTS_CACHE[host] = (now + 600, policy)
    if not policy.can_fetch(USER_AGENT, canonical):
        raise AgentError('The source site robots policy disallows this page.')


def _source(kind, url, title, content, *, publication_date=None):
    no_secrets(content)
    return {'source_id': uuid.uuid4().hex, 'kind': kind, 'url': url,
            'title': title[:300], 'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'publication_date':publication_date,
            'content': content[:MAX_TEXT_CHARS], 'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'untrusted_content': True}


class _SearchResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.anchor = None
        self.current = None
        self.snippet_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'a' and 'result__a' in classes:
            self.current = {'url': attrs.get('href', ''), 'title': ''}
            self.anchor = 'title'
        elif 'result__snippet' in classes:
            self.snippet_depth += 1
            self.anchor = 'snippet'

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor == 'title' and self.current:
            self.results.append(self.current)
            self.current = None
            self.anchor = None
        if self.snippet_depth and tag in ('a', 'div', 'span'):
            self.snippet_depth -= 1
            if not self.snippet_depth:
                self.anchor = None

    def handle_data(self, data):
        if self.anchor == 'title' and self.current:
            self.current['title'] += data
        elif self.anchor == 'snippet' and self.results:
            self.results[-1]['snippet'] = self.results[-1].get('snippet', '') + data


def search_web(query, limit=5):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise AgentError('Search query must contain 1 to 300 characters.')
    no_secrets(query)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise AgentError('Search result limit must be between 1 and 8.')
    url = 'https://html.duckduckgo.com/html/?q=' + quote_plus(query.strip())
    _, _, body = fetch(url, limit=500_000)
    parser = _SearchResults()
    parser.feed(body.decode('utf-8', errors='replace'))
    results = []
    for row in parser.results:
        try:
            result_url = urljoin('https://duckduckgo.com/', row['url'])
            result_parts = urlsplit(result_url)
            if result_parts.hostname in ('duckduckgo.com', 'www.duckduckgo.com') and result_parts.path == '/l/':
                result_url = parse_qs(result_parts.query).get('uddg', [''])[0]
            host, _, _, canonical = _safe_url(result_url)
        except (AgentError, ValueError):
            continue
        results.append({'title': re.sub(r'\s+', ' ', row['title']).strip()[:300],
                        'url': canonical, 'host': host,
                        'snippet': re.sub(r'\s+', ' ', row.get('snippet', '')).strip()[:500]})
        if len(results) == limit:
            break
    return {'query': query, 'results': results, 'provider': 'DuckDuckGo HTML',
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'untrusted_content': True}


def _youtube_id(url):
    parts = urlsplit(url)
    host = (parts.hostname or '').casefold()
    video_id = ''
    if host == 'youtu.be':
        video_id = parts.path.strip('/').split('/')[0]
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com'):
        if parts.path == '/watch':
            from urllib.parse import parse_qs
            video_id = parse_qs(parts.query).get('v', [''])[0]
        else:
            match = re.match(r'^/(?:shorts|embed)/([A-Za-z0-9_-]{11})(?:/|$)', parts.path)
            video_id = match.group(1) if match else ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise AgentError('Supply a public YouTube video URL with a valid video ID.')
    return video_id


def read_youtube_transcript(url, language='en'):
    video_id = _youtube_id(url)
    if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z0-9-]{2,20}', language):
        raise AgentError('Language must be a short language tag such as en or fr.')
    canonical = 'https://www.youtube.com/watch?v=' + video_id
    _, _, body = fetch(canonical, limit=1_000_000)
    page = body.decode('utf-8', errors='replace')
    marker = 'ytInitialPlayerResponse = '
    start = page.find(marker)
    if start < 0:
        marker = 'var ytInitialPlayerResponse = '
        start = page.find(marker)
    if start < 0:
        raise AgentError('YouTube did not expose public captions in its page response.')
    start += len(marker)
    try:
        player, _ = json.JSONDecoder().raw_decode(page[start:])
    except json.JSONDecodeError as exc:
        raise AgentError('Could not parse YouTube public caption metadata.') from exc
    details = player.get('videoDetails', {})
    tracks = player.get('captions', {}).get('playerCaptionsTracklistRenderer', {}).get('captionTracks', [])
    if not tracks:
        raise AgentError('This video has no publicly available captions.')
    track = next((row for row in tracks if row.get('languageCode') == language), tracks[0])
    caption_url = track.get('baseUrl', '')
    host, _, _, _ = _safe_url(caption_url)
    if host not in ('www.youtube.com', 'youtube.com', 'www.google.com', 'google.com'):
        raise AgentError('YouTube caption endpoint host was outside the allowlist.')
    _, _, caption_body = fetch(caption_url, limit=500_000)
    try:
        data = json.loads(caption_body)
        segments = [segment.get('utf8', '') for event in data.get('events', [])
                    for segment in event.get('segs', [])]
        transcript = ' '.join(value for value in segments if value)
    except (json.JSONDecodeError, AttributeError):
        parser = _VisibleText()
        parser.feed(caption_body.decode('utf-8', errors='replace'))
        transcript = ' '.join(parser.parts)
    transcript = re.sub(r'\s+', ' ', transcript).strip()[:MAX_TEXT_CHARS]
    if not transcript:
        raise AgentError('YouTube returned an empty public transcript.')
    return _source('youtube_transcript', canonical, details.get('title', 'YouTube video'), transcript)


class SourceLibrary:
    """Small private JSONL library with append-only forget markers."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries = self.root / 'sources.jsonl'
        self.lock = self.root.parent / 'locks' / 'source-library.lock'
        self.lock.parent.mkdir(parents=True, exist_ok=True)

    def save(self, source):
        if (not isinstance(source, dict)
                or not re.fullmatch(r'[0-9a-f]{32}', source.get('source_id', ''))
                or not all(isinstance(source.get(field), str) and source[field]
                           for field in ('kind', 'url', 'title', 'retrieved_at', 'content', 'content_sha256'))
                or source.get('untrusted_content') is not True
                or hashlib.sha256(source['content'].encode('utf-8')).hexdigest() != source['content_sha256']):
            raise AgentError('Save requires a source retrieved during this session.')
        no_secrets(source['content'])
        serialized = (json.dumps(source, ensure_ascii=False) + '\n').encode('utf-8')
        with exclusive(self.lock, uuid.uuid4().hex):
            if self.entries.exists() and self.entries.stat().st_size + len(serialized) > 20_000_000:
                raise AgentError('Saved source library has reached its 20 MB limit.')
            with self.entries.open('ab') as stream:
                stream.write(serialized)
        return {'saved': True, 'source_id': source['source_id'], 'url': source['url'],
                'title': source['title'], 'path': str(self.entries)}

    def _rows(self):
        if not self.entries.exists():
            return []
        if self.entries.stat().st_size > 20_000_000:
            raise AgentError('Saved source library exceeds 20 MB.')
        rows = []
        for line in self.entries.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AgentError('Saved source library contains a malformed record; review it before changing it.') from exc
        return rows

    def search(self, query, limit=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise AgentError('Saved-source query must contain 1 to 300 characters.')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise AgentError('Saved-source result limit must be between 1 and 10.')
        with exclusive(self.lock, uuid.uuid4().hex):
            rows = self._rows()
        query_terms = set(query.casefold().split())
        results = []
        for row in reversed(rows):
            if row.get('operation') == 'forget':
                continue
            haystack = (row.get('title', '') + ' ' + row.get('content', '')).casefold()
            if all(term in haystack for term in query_terms):
                results.append({key: row[key] for key in ('source_id', 'kind', 'url', 'title', 'retrieved_at', 'content')})
                if len(results) == limit:
                    break
        return {'results': results, 'count': len(results)}

    def forget(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{32}', source_id):
            raise AgentError('Specify a saved source ID.')
        with exclusive(self.lock, uuid.uuid4().hex):
            existing = [row for row in self._rows() if row.get('source_id') == source_id and not row.get('operation')]
            if not existing:
                raise AgentError('No saved source has that ID.')
            remaining = [row for row in self._rows() if row.get('source_id') != source_id]
            atomic_write(self.entries, ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                                      for row in remaining).encode('utf-8'))
        return {'forgotten': True, 'source_id': source_id}
