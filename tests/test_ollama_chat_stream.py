from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from local_agent import Ollama


class OllamaChatStreamTests(unittest.TestCase):
    def setUp(self):
        self.payload = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_POST(self):
                owner.payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body = (json.dumps({'message': {'role': 'assistant', 'content': 'Hello'}, 'done': False}) + '\n'
                    + json.dumps({'message': {'role': 'assistant', 'content': ' there'}, 'done': False}) + '\n'
                    + json.dumps({'message': {'role': 'assistant', 'content': ''}, 'done': True}) + '\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                for part in body.splitlines(keepends=True):
                    self.wfile.write(part)
                    self.wfile.flush()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def test_chat_stream_yields_incremental_text_with_no_tools(self):
        endpoint = f'http://127.0.0.1:{self.server.server_port}'
        tokens = []
        with patch('local_agent.validate_endpoint', return_value=endpoint):
            provider = Ollama(endpoint)
        result = provider.chat_stream('qwen3-coder:30b',
            [{'role': 'system', 'content': 'safe'}, {'role': 'user', 'content': 'Hi'}],
            8192, {}, tokens.append, timeout=5)
        self.assertEqual(tokens, ['Hello', ' there'])
        self.assertTrue(result['done'])
        self.assertTrue(self.payload['stream'])
        self.assertEqual(self.payload['tools'], [])


if __name__ == '__main__':
    unittest.main()
