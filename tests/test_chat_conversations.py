import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from local_agent import HUB


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


class ChatDashboardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='chat-dashboard-', dir=HUB / '.local' / 'tests'))
        self.store = ConversationStore(self.root)
        self.calls = []
        def runner(messages, model, cancel, on_token):
            self.calls.append((messages, model))
            on_token('Here is a useful answer. ')
            on_token('```python\ndef example():\n    return 1\n```')
        self.dashboard = CodingDashboard((), allowed_models=('qwen3-coder:30b', 'qwen3:8b'),
            chat_store=self.store, chat_runner=runner,
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
        self.assertIn('Mac model connection ready at startup', page)
        self.assertIn('installed on Mac', page)
        self.assertIn('not confirmed on Mac', page)
        status, _ = self.request('/api/chat/conversations')
        self.assertEqual(status, 401)
        status, data = self.request('/api/chat/models', token=self.dashboard.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)['models'], ['qwen3-coder:30b', 'qwen3:8b'])

    def test_streaming_reply_persists_and_followup_receives_prior_turns(self):
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

    def test_model_allowlist_and_origin_protect_message_submission(self):
        conversation = self.store.create()
        status, _ = self.request('/api/chat/conversations/' + conversation['id'] + '/messages',
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


if __name__ == '__main__':
    unittest.main()
