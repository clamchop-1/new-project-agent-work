"""Start an authenticated disposable chat page for a local browser acceptance pass."""
from pathlib import Path
import tempfile
import threading

from chat_conversations import ConversationStore
from coding_dashboard import CodingDashboard
from fixture_repo import make_fixture
from knowledge_documents import DocumentKnowledgeLibrary
from local_agent import HUB


root = Path(tempfile.mkdtemp(prefix='knowledge-browser-', dir=HUB / '.local' / 'tests'))
project, _ = make_fixture(root / 'repo')
upload = root / 'browser-manual.md'
upload.write_text('# Retry Budget\n\nThe reference sets the bounded retry budget to 7 attempts.\n', encoding='utf-8')
library = DocumentKnowledgeLibrary(project)
history = ConversationStore(root / 'history')


def runner(messages, model, cancel, on_token):
    on_token('The document specifies a retry budget of 7 attempts. ')


dashboard = CodingDashboard((), allowed_models=('qwen3:8b',), chat_store=history,
    chat_runner=runner, chat_models=('qwen3:8b',), knowledge_store=library)
server = dashboard.start()
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
print('BROWSER_URL=http://127.0.0.1:' + str(server.server_port), flush=True)
print('BROWSER_TOKEN=' + dashboard.token, flush=True)
print('UPLOAD_FILE=' + str(upload), flush=True)
print('DISPOSABLE_ROOT=' + str(root), flush=True)
try:
    thread.join()
except KeyboardInterrupt:
    server.shutdown(); server.server_close(); thread.join(timeout=5)
