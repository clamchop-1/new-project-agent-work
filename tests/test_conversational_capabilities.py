import subprocess
from pathlib import Path
import tempfile
import unittest

from agent_core import AgentError, Project, exclusive
from chat_conversations import ConversationStore
from conversational_capabilities import CapabilityDispatcher, CapabilityRegistry, IntentRouter
from project_intelligence import ProjectIntelligence


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class ConversationalCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lca-capabilities-')
        self.base = Path(self.temp.name)
        self.app = self.base / 'app'
        self.repo = self.base / 'audio-demo'
        for root in (self.app, self.repo):
            root.mkdir()
            git(root, 'init', '-q')
            git(root, 'config', 'user.email', 'tests@example.invalid')
            git(root, 'config', 'user.name', 'Test User')
            (root / '.gitignore').write_text('.local/\n', encoding='utf-8')
            git(root, 'add', '.gitignore')
            git(root, 'commit', '-qm', 'initial')
        source = self.repo / 'audio.py'
        source.write_text('def classify_note(samples):\n    """Compute a bounded spectrum."""\n    return fft(samples)\n', encoding='utf-8')
        (self.repo / '.env').write_text('TOKEN=secret-not-to-return', encoding='utf-8')
        git(self.repo, 'add', 'audio.py', '.env')
        git(self.repo, 'commit', '-qm', 'add audio classification')
        self.store = ConversationStore(self.app)
        self.projects = ProjectIntelligence(Project(self.app))
        self.row = self.projects.register(str(self.repo), confirmed=True)
        self.projects.refresh(self.row['project_id'])
        self.dispatcher = CapabilityDispatcher(self.projects, self.store)
        self.conversation = self.store.create()

    def tearDown(self):
        self.temp.cleanup()

    def test_registry_is_code_owned_and_explains_real_boundaries(self):
        registry = CapabilityRegistry()
        self.assertIn('project.search', [item['capability_id'] for item in registry.describe()])
        summary = registry.chat_summary(True)
        self.assertIn('project.search', summary)
        self.assertIn('not available as conversational capabilities', summary)
        with self.assertRaises(AgentError):
            registry.get('shell.execute')

    def test_router_only_proposes_clear_registered_operations(self):
        router = IntentRouter()
        self.assertEqual(router.propose('What projects do I have registered?', selected_project=False).capability_id,
                         'projects.list')
        self.assertEqual(router.propose('Open project audio-demo', selected_project=False).arguments['name'],
                         'audio-demo')
        self.assertIsNone(router.propose('Tell me about audio pipelines', selected_project=False))
        self.assertIsNone(router.propose('Run git reset --hard', selected_project=True))

    def test_project_list_select_status_files_and_cited_search_are_read_only(self):
        before = git(self.repo, 'status', '--porcelain=v1')
        proposal = self.dispatcher.propose('What projects do I have registered?', project_id=None)
        listed = self.dispatcher.execute(proposal, conversation_id=self.conversation['id'], current_project_id=None)
        self.assertEqual(listed['result']['projects'][0]['name'], 'audio-demo')
        self.assertNotIn('root', listed['result']['projects'][0])

        proposal = self.dispatcher.propose('Open project audio-demo', project_id=None)
        selected = self.dispatcher.execute(proposal, conversation_id=self.conversation['id'], current_project_id=None)
        project_id = selected['result']['project_id']
        self.assertEqual(self.store.load(self.conversation['id'])['project_id'], project_id)

        for message, expected in (('Show git status', 'project.status'),
                                  ('List source files', 'project.files'),
                                  ('Search project for classify_note', 'project.search')):
            proposal = self.dispatcher.propose(message, project_id=project_id)
            result = self.dispatcher.execute(proposal, conversation_id=self.conversation['id'],
                                             current_project_id=project_id)
            self.assertEqual(result['capability_id'], expected)
            if expected == 'project.files':
                self.assertIn('audio.py', result['result']['paths'])
                self.assertNotIn('.env', result['result']['paths'])
            if expected == 'project.status':
                self.assertNotIn('root', result['result'])
                self.assertFalse(result['result']['fresh_remote_fetch'])
            if expected == 'project.search':
                hit = result['result']['passages'][0]
                self.assertEqual(hit['citation']['path'], 'audio.py')
                self.assertTrue(hit['citation']['validated'])
                self.assertIn('classify_note', hit['content'])
        self.assertEqual(before, git(self.repo, 'status', '--porcelain=v1'))
        audit = self.dispatcher.audit_path.read_text(encoding='utf-8')
        states = [__import__('json').loads(line)['state'] for line in audit.splitlines()]
        self.assertIn('started', states)
        self.assertIn('completed', states)
        self.assertNotIn('fixture-secret-value', audit)
        self.assertNotIn('bounded spectrum', audit)

    def test_conversational_file_read_is_allowlisted_bounded_and_cited(self):
        proposal = self.dispatcher.propose('Read file audio.py', project_id=self.row['project_id'])
        self.assertEqual(proposal.capability_id, 'project.read')
        result = self.dispatcher.execute(proposal, conversation_id=self.conversation['id'],
                                         current_project_id=self.row['project_id'])
        self.assertIn('classify_note', result['result']['content'])
        self.assertEqual(result['result']['citation']['path'], 'audio.py')
        self.assertTrue(self.dispatcher.validate_read_result(
            self.row['project_id'], result['result'])['validated'])
        secret = self.dispatcher.propose('Read file .env', project_id=self.row['project_id'])
        with self.assertRaises(AgentError):
            self.dispatcher.execute(secret, conversation_id=self.conversation['id'],
                                    current_project_id=self.row['project_id'])
        with self.assertRaises(AgentError):
            self.dispatcher.execute(type('Proposal', (), {'capability_id':'project.read',
                'arguments':{'path':'..\\.env'}})(), conversation_id=self.conversation['id'],
                current_project_id=self.row['project_id'])

    def test_dispatch_rejects_invented_capability_bad_schema_and_unbound_scope(self):
        with self.assertRaises(AgentError):
            self.dispatcher.execute(type('Proposal', (), {'capability_id':'shell.execute', 'arguments':{}})(),
                conversation_id=self.conversation['id'], current_project_id=self.row['project_id'])
        with self.assertRaises(AgentError):
            self.dispatcher.execute(type('Proposal', (), {'capability_id':'project.search',
                'arguments':{'query':'classify_note','path':'..\\.env'}})(),
                conversation_id=self.conversation['id'], current_project_id=self.row['project_id'])
        with self.assertRaises(AgentError):
            proposal = self.dispatcher.propose('Show git status', project_id=None)
            self.dispatcher.execute(proposal, conversation_id=self.conversation['id'], current_project_id=None)

    def test_audit_lock_conflict_blocks_action_before_execution(self):
        proposal = self.dispatcher.propose('What projects do I have registered?', project_id=None)
        lock = self.dispatcher.audit_path.with_name(self.dispatcher.audit_path.name + '.lock')
        with exclusive(lock, 'test-audit-owner'):
            with self.assertRaises(AgentError):
                self.dispatcher.execute(proposal, conversation_id=self.conversation['id'],
                                        current_project_id=None)
        self.assertFalse(self.dispatcher.audit_path.exists())

    def test_search_citations_are_withheld_after_source_change(self):
        proposal = self.dispatcher.propose('Search project for classify_note', project_id=self.row['project_id'])
        source = self.repo / 'audio.py'
        source.write_text('def changed():\n    return None\n', encoding='utf-8')
        result = self.dispatcher.execute(proposal, conversation_id=self.conversation['id'],
                                         current_project_id=self.row['project_id'])
        self.assertEqual(result['result']['passages'], [])


if __name__ == '__main__':
    unittest.main()
