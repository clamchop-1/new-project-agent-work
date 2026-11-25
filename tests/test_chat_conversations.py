import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from local_agent import HUB
from research_service import ResearchAuthorizationService


class FakeResearchProvider:
    provider_id = 'duckduckgo_html'
    def __init__(self):
        self.searches = 0
        self.pages = 0
    def search(self, query, limit):
        self.searches += 1
        return {'results': [{'url':'https://docs.example.test/guide'}]}
    def read(self, url):
        self.pages += 1
        content = 'Async tasks can be cancelled by calling task.cancel(). The coroutine receives CancelledError.'
        return {'source_id':'d'*32, 'provider_id':self.provider_id, 'title':'Async guide',
            'url':url, 'retrieved_at':'2026-11-24T00:00:00+00:00', 'publication_date':None,
            'content':content, 'content_sha256':hashlib.sha256(content.encode()).hexdigest(),
            'untrusted_content':True}


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='chat-store-', dir=HUB / '.local' / 'tests'))
        self.store = ConversationStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_conversation_persists_bounded_turns_and_reuses_followup_context(self):
        row = self.store.create()
        self.store.add_user(row['id'], 'Write a duplicate filename function')
        self.store.add_assistant(row['id'], 'Use a dictionary to track names.')
        loaded = self.store.load(row['id'])
        self.assertEqual(loaded['title'], 'Write a duplicate filename function')
        self.assertEqual(len(self.store.context(loaded)), 2)
        self.assertEqual(self.store.list()[0]['id'], row['id'])

    def test_invalid_ids_and_oversized_input_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.load('../not-a-conversation')
        row = self.store.create()
        with self.assertRaises(ValueError):
            self.store.add_user(row['id'], 'x' * 12_001)

    def test_only_valid_https_research_source_references_survive_reload(self):
        row=self.store.create()
        self.store.add_user(row['id'],'Research asyncio cancellation')
        source={'source_id':'d'*32,'provider_id':'duckduckgo_html','title':'Asyncio docs',
            'url':'https://docs.python.org/3/library/asyncio-task.html',
            'retrieved_at':'2026-11-24T12:00:00+00:00','publication_date':None,
            'content_sha256':'e'*64,'cached':False}
        self.store.add_assistant(row['id'],'Use Task.cancel().',sources=[source])
        self.assertEqual(self.store.load(row['id'])['messages'][-1]['sources'][0],source)
        path=self.store._path(row['id'])
        record=json.loads(path.read_text(encoding='utf-8'))
        record['messages'][-1]['sources'][0]['url']='http://127.0.0.1/secret'
        path.write_text(json.dumps(record),encoding='utf-8')
        reloaded=self.store.load(row['id'])
        self.assertEqual(reloaded['messages'][-1]['content'],'Use Task.cancel().')
        self.assertNotIn('sources',reloaded['messages'][-1])

    def test_stream_snapshots_resume_and_finish_without_duplicate_assistant_rows(self):
        row=self.store.create();stream_id='a'*32
        self.store.add_user(row['id'],'Explain streaming persistence')
        self.store.start_assistant_stream(row['id'],stream_id)
        self.store.update_assistant_stream(row['id'],stream_id,'First chunk')
        partial=self.store.load(row['id'])['messages'][-1]
        self.assertEqual(partial['stream_status'],'generating')
        self.assertEqual(partial['content'],'First chunk')
        self.store.update_assistant_stream(row['id'],stream_id,'Complete response',status='complete')
        final=self.store.load(row['id'])['messages']
        self.assertEqual([item['role'] for item in final],['user','assistant'])
        self.assertEqual(final[-1]['stream_status'],'complete')
        self.assertEqual(final[-1]['content'],'Complete response')


class ChatDashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='chat-dashboard-', dir=HUB / '.local' / 'tests'))
        self.store = ConversationStore(self.root)
        self.calls = []
        self.research_provider = FakeResearchProvider()
        self.dashboard_research = ResearchAuthorizationService(provider=self.research_provider,
            audit_root=self.root / '.local' / 'research-audit')
        def runner(messages, model, cancel, on_token):
            self.calls.append((messages, model))
            on_token('Here is a useful answer. ')
            on_token('```python\ndef example():\n    return 1\n```')
        self.dashboard = CodingDashboard((), allowed_models=('qwen3-coder:30b', 'qwen3:8b'),
            chat_store=self.store, chat_runner=runner,
            research_service=self.dashboard_research,
            provider_status={'status': 'connected', 'installed_models': ['qwen3-coder:30b']})
        self.server = self.dashboard.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, path, *, method='GET', body=None, token=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        origin = self.base
        if method == 'POST':
            headers['Origin'] = origin
            headers['Content-Type'] = 'application/json'
        raw = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read()
            finally:
                error.close()

    def test_chat_page_authentication_and_no_project_or_write_authority(self):
        with urllib.request.urlopen(self.base + '/') as response:
            page = response.read().decode()
        self.assertIn('What can I help with?', page)
        self.assertNotIn('href="/developer"', page)
        self.assertIn('textContent', page)
        self.assertNotIn('innerHTML', page)
        self.assertIn("setAttribute('inert'", page)
        self.assertIn("removeAttribute('inert'", page)
        self.assertIn('Refresh models', page)
        self.assertIn('compatible authorized models', page)
        status, _ = self.request('/api/chat/conversations')
        self.assertEqual(status, 401)
        status, data = self.request('/api/chat/models', token=self.dashboard.token)
        self.assertEqual(status, 200)
        inventory = json.loads(data)
        self.assertEqual([row['model_id'] for row in inventory['models']],
                         ['qwen3-coder:30b', 'qwen3:8b'])
        self.assertEqual(inventory['installed'], ['qwen3-coder:30b', 'qwen3:8b'])
        self.assertTrue(all(row['authorized'] for row in inventory['models']))

    def test_chat_page_parses_stream_events_and_reports_incomplete_streams(self):
        with urllib.request.urlopen(self.base + '/') as response:
            page = response.read().decode()
        self.assertIn('function parseStreamEvent(line){try{const event=JSON.parse(line)', page)
        self.assertNotIn('const event=parseStreamEvent(line);if(!event', page)
        self.assertIn('connection closed before the response was complete', page)
        self.assertIn("showActivity('Preparing your response…')", page)
        self.assertIn('activity-progress', page)
        self.assertIn('activityTitleNode.textContent=text', page)
        self.assertNotIn("$('activityTitle').textContent=text", page)
        self.assertIn('guardedSendChat', page)
        self.assertIn('appendImmediatePlaceholder',page)
        self.assertIn('requestAnimationFrame',page)
        self.assertIn('Stop generating',page)
        self.assertIn('/status',page)
        self.assertRegex(page,r'id="buildId"[^>]*>[a-f0-9]{12}</span>')

    def test_streaming_reply_persists_and_followup_receives_prior_turns(self):
        # The stream endpoint writes explicit HTTP chunk framing. Browser Fetch
        # only strips that framing when the handler speaks HTTP/1.1.
        self.assertEqual(self.server.RequestHandlerClass.protocol_version, 'HTTP/1.1')
        status, data = self.request('/api/chat/conversations', method='POST', body={}, token=self.dashboard.token)
        self.assertEqual(status, 201)
        conversation_id = json.loads(data)['id']
        status, stream = self.request('/api/chat/conversations/' + conversation_id + '/messages',
            method='POST', body={'message': 'Write a duplicate filename helper.',
                                 'model': 'qwen3-coder:30b'}, token=self.dashboard.token)
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in stream.splitlines() if line]
        tokens = [event['text'] for event in events if event['type'] == 'token']
        self.assertEqual(len(tokens), 2)
        self.assertTrue(all(event.get('request_id') for event in events))
        self.assertEqual([event['sequence'] for event in events],
                         list(range(1,len(events)+1)))
        saved = self.store.load(conversation_id)
        self.assertEqual([message['role'] for message in saved['messages']], ['user', 'assistant'])
        status, stream = self.request('/api/chat/conversations/' + conversation_id + '/messages',
            method='POST', body={'message': 'Now ignore extensions.',
                                 'model': 'qwen3-coder:30b'}, token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertTrue(any(json.loads(line).get('type') == 'done' for line in stream.splitlines() if line))
        prior = self.calls[1][0]
        self.assertEqual(prior[-3]['content'], 'Write a duplicate filename helper.')
        self.assertEqual(prior[-2]['content'], 'Here is a useful answer. ```python\ndef example():\n    return 1\n```')
        self.assertEqual(prior[-1]['content'], 'Now ignore extensions.')

    def test_active_stream_status_and_persisted_partial_can_be_reconciled(self):
        started=threading.Event()
        def slow_runner(messages,model,cancel,on_token):
            on_token('Partial reply')
            started.set()
            cancel.wait(4)
        self.dashboard.chat_runner=slow_runner
        conversation=self.store.create();response={}
        worker=threading.Thread(target=lambda:response.setdefault('result',self.request(
            '/api/chat/conversations/'+conversation['id']+'/messages',method='POST',
            body={'message':'Please stream.','model':'qwen3-coder:30b'},token=self.dashboard.token)))
        worker.start();self.assertTrue(started.wait(3))
        status,data=self.request('/api/chat/conversations/'+conversation['id']+'/status',token=self.dashboard.token)
        self.assertEqual(status,200);self.assertTrue(json.loads(data)['active'])
        saved=self.store.load(conversation['id'])['messages'][-1]
        self.assertEqual(saved['stream_status'],'generating')
        self.assertEqual(saved['content'],'Partial reply')
        status,_=self.request('/api/chat/conversations/'+conversation['id']+'/stop',method='POST',
            body={},token=self.dashboard.token)
        worker.join(5);self.assertFalse(worker.is_alive());self.assertEqual(status,202)
        final=self.store.load(conversation['id'])['messages'][-1]
        self.assertEqual(final['stream_status'],'cancelled')
        status,data=self.request('/api/chat/conversations/'+conversation['id']+'/status',token=self.dashboard.token)
        self.assertFalse(json.loads(data)['active'])

    def test_ordinary_chat_never_uses_network_research(self):
        conversation = self.store.create()
        status, body = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message':'Explain async tasks.', 'model':'qwen3-coder:30b'},
            token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(self.research_provider.searches, 0)
        self.assertEqual(self.research_provider.pages, 0)
        self.assertNotIn('research_proposal', body.decode())

    def test_approval_is_required_before_research_and_sources_are_persisted(self):
        conversation = self.store.create()
        message = 'Find current documentation about cancelling Python asyncio tasks.'
        status, body = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message':message, 'model':'qwen3-coder:30b', 'research_mode':'ask'},
            token=self.dashboard.token)
        self.assertEqual(status, 200)
        proposal_event = next(json.loads(line) for line in body.splitlines()
            if json.loads(line).get('type') == 'research_proposal')
        self.assertEqual(self.research_provider.searches, 0)
        self.assertIn('No network request has been made yet', proposal_event['message'])
        decision_status, decision_body = self.request(
            '/api/chat/conversations/' + conversation['id'] + '/research-decision', method='POST',
            body={'proposal_id':proposal_event['proposal_id'], 'decision':'approved',
                  'message':message, 'model':'qwen3-coder:30b'}, token=self.dashboard.token)
        self.assertEqual(decision_status, 200)
        resume = json.loads(decision_body)['resume_id']
        with patch('web_retrieval._safe_url', return_value=('https','docs.example.test',443,'https://docs.example.test/guide')):
            status, research_body = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
                method='POST', body={'message':message, 'model':'qwen3-coder:30b',
                    'research_mode':'approved', 'resume_id':resume}, token=self.dashboard.token)
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in research_body.splitlines()]
        self.assertEqual(self.research_provider.searches, 1)
        self.assertEqual(self.research_provider.pages, 1)
        self.assertTrue(any(event.get('type') == 'sources' and event['sources'][0]['url'].endswith('/guide')
            for event in events))
        saved = self.store.load(conversation['id'])
        answer = saved['messages'][-1]
        self.assertEqual(answer['sources'][0]['source_id'], 'd'*32)
        self.assertIn('https://docs.example.test/guide', answer['content'])

    def test_denied_approval_does_not_make_network_request(self):
        conversation = self.store.create()
        message = 'Look up current Python asyncio task cancellation guidance.'
        status, body = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message':message, 'model':'qwen3-coder:30b', 'research_mode':'ask'},
            token=self.dashboard.token)
        proposal = next(json.loads(line) for line in body.splitlines()
            if json.loads(line).get('type') == 'research_proposal')
        status, body = self.request('/api/chat/conversations/' + conversation['id'] + '/research-decision',
            method='POST', body={'proposal_id':proposal['proposal_id'], 'decision':'cancel',
                'message':message, 'model':'qwen3-coder:30b'}, token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(self.research_provider.searches, 0)

    def test_model_allowlist_and_origin_protect_message_submission(self):
        conversation = self.store.create()
        status, data = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message': 'Hi', 'model': 'unlisted-model'}, token=self.dashboard.token)
        self.assertEqual(status, 400)

    def test_inference_failure_is_clear_and_does_not_leak_provider_details(self):
        def failed_runner(*args):
            raise RuntimeError('private tunnel detail')
        self.dashboard.chat_runner = failed_runner
        conversation = self.store.create()
        status, body = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message': 'Explain Python.',
                                 'model': 'qwen3-coder:30b'}, token=self.dashboard.token)
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in body.splitlines() if line]
        errors = [event['message'] for event in events if event['type'] == 'error']
        self.assertEqual(len(errors), 1)
        self.assertIn('local model could not complete', errors[0])
        self.assertNotIn('private tunnel detail', errors[0])

    def test_cancellation_stops_generation_and_keeps_partial_reply(self):
        started = threading.Event()
        def slow_runner(messages, model, cancel, on_token):
            on_token('A partial reply')
            started.set()
            while not cancel.wait(.01):
                time.sleep(.01)
            raise RuntimeError('cancelled by client')
        self.dashboard.chat_runner = slow_runner
        conversation = self.store.create()
        response = {}
        def send_message():
            response['result'] = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
                method='POST', body={'message': 'Explain this.', 'model': 'qwen3-coder:30b'},
                token=self.dashboard.token)
        worker = threading.Thread(target=send_message)
        worker.start()
        self.assertTrue(started.wait(3))
        status, _ = self.request('/api/chat/conversations/' + conversation['id'] + '/stop',
            method='POST', body={}, token=self.dashboard.token)
        worker.join(timeout=5)
        self.assertEqual(status, 202)
        self.assertFalse(worker.is_alive())
        body = response['result'][1].decode()
        events = [json.loads(line) for line in body.splitlines() if line]
        self.assertTrue(any(event.get('cancelled') for event in events))
        self.assertEqual(self.store.load(conversation['id'])['messages'][-1]['content'], 'A partial reply')

    def test_refresh_replaces_inventory_and_preserves_host_model_identity(self):
        model = {'model_key': 'a' * 64, 'provider_id': 'ssh_tunnel_ollama',
                 'runtime': 'ollama', 'host_id': 'mac_studio', 'model_id': 'qwen3:8b',
                 'model_digest': 'digest', 'metadata': {'quantization': 'Q4_K_M'},
                 'capabilities': {}, 'capabilities_evidence': 'unknown',
                 'discovered': True, 'installed': True, 'compatible': True,
                 'authorized': True, 'available': True, 'loaded': None,
                 'tested': False, 'recommended': False}
        self.dashboard.chat_model_refresher = lambda: {
            'models': [model], 'providers': [{'provider_id': 'ssh_tunnel_ollama',
                'runtime': 'ollama', 'host_id': 'mac_studio', 'available': True}],
            'errors': [], 'observed_at': '2026-11-24T00:00:00+00:00'}
        status, data = self.request('/api/chat/models/refresh', method='POST', body={},
                                    token=self.dashboard.token)
        self.assertEqual(status, 200)
        inventory = json.loads(data)
        self.assertEqual(inventory['models'][0]['model_key'], 'a' * 64)
        self.assertEqual(inventory['models'][0]['host_id'], 'mac_studio')
        self.assertEqual(inventory['observed_at'], '2026-11-24T00:00:00+00:00')

    def test_chat_invokes_the_selected_provider_specific_model(self):
        model = {'model_key': 'b' * 64, 'provider_id': 'ssh_tunnel_ollama',
                 'runtime': 'ollama', 'host_id': 'mac_studio', 'model_id': 'qwen3:8b',
                 'model_digest': 'digest', 'metadata': {}, 'capabilities': {},
                 'capabilities_evidence': 'unknown', 'discovered': True, 'installed': True,
                 'compatible': True, 'authorized': True, 'available': True, 'loaded': None,
                 'tested': False, 'recommended': False}
        self.dashboard.chat_models = self.dashboard._normalize_chat_models([model])
        conversation = self.store.create()
        status, data = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message': 'Use the selected model.', 'model': 'b' * 64},
            token=self.dashboard.token)
        self.assertEqual(status, 200, data)
        selected_events = [json.loads(line) for line in data.splitlines() if line]
        identity_event = next(event for event in selected_events if event['type'] == 'model')
        self.assertEqual(identity_event['provider_id'], 'ssh_tunnel_ollama')
        self.assertEqual(identity_event['host_id'], 'mac_studio')
        self.assertEqual(identity_event['model_id'], 'qwen3:8b')
        self.assertEqual(self.calls[-1][1]['provider_id'], 'ssh_tunnel_ollama')
        self.assertEqual(self.calls[-1][1]['model_id'], 'qwen3:8b')

    def test_unavailable_or_unauthorized_catalog_models_cannot_be_invoked(self):
        model = {'model_key': 'c' * 64, 'provider_id': 'ollama', 'runtime': 'ollama',
                 'host_id': 'windows', 'model_id': 'qwen3:8b', 'model_digest': 'digest',
                 'metadata': {}, 'capabilities': {}, 'capabilities_evidence': 'unknown',
                 'discovered': True, 'installed': True, 'compatible': True,
                 'authorized': False, 'available': True, 'loaded': None, 'tested': False,
                 'recommended': False}
        self.dashboard.chat_models = self.dashboard._normalize_chat_models([model])
        conversation = self.store.create()
        status, _ = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
            method='POST', body={'message': 'Do not invoke it.', 'model': 'c' * 64},
            token=self.dashboard.token)
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
