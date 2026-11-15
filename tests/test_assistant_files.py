import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentError
from assistant_files import AssistantFiles


class AssistantFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = AssistantFiles(self.root)

    def test_lists_reads_and_finds_files_inside_selected_root(self):
        (self.root / 'notes').mkdir()
        (self.root / 'notes' / 'plan.md').write_text('Remember the telescope.\n', encoding='utf-8')

        listing = self.files.list_directory()
        found = self.files.find_files('plan')
        read = self.files.read_text('notes/plan.md')
        searched = self.files.search_text('telescope')

        self.assertEqual(listing['entries'][0]['name'], 'notes')
        self.assertEqual(found['matches'][0]['path'], 'notes/plan.md')
        self.assertIn('Remember the telescope.', read['text'])
        self.assertEqual(searched['matches'][0]['path'], 'notes/plan.md')

    def test_rejects_parent_and_absolute_path_escape(self):
        outside = self.root.parent / 'outside.txt'
        for path in ('../outside.txt', str(outside)):
            with self.subTest(path=path), self.assertRaises(AgentError):
                self.files.read_text(path)

    def test_hides_private_paths_and_secret_file_names(self):
        (self.root / '.private').mkdir()
        (self.root / '.private' / 'notes.txt').write_text('hidden', encoding='utf-8')
        (self.root / 'credentials.json').write_text('{"token":"hidden"}', encoding='utf-8')
        (self.root / 'visible.txt').write_text('visible', encoding='utf-8')

        found = self.files.find_files('visible')
        listing = self.files.list_directory()

        self.assertEqual([row['path'] for row in found['matches']], ['visible.txt'])
        names = {row['name'] for row in listing['entries']}
        self.assertEqual(names, {'visible.txt'})

    def test_rejects_secret_like_text_content(self):
        (self.root / 'notes.txt').write_text('api_key = "abcdefghijklmnopqrstuvwxyz"', encoding='utf-8')
        with self.assertRaises(AgentError):
            self.files.read_text('notes.txt')

    def test_pdf_read_is_explicitly_unavailable(self):
        (self.root / 'reference.pdf').write_bytes(b'%PDF-1.4')
        with patch.dict('sys.modules', {'pypdf': None}):
            with self.assertRaisesRegex(AgentError, 'local pypdf package'):
                self.files.read_pdf('reference.pdf')

    def test_pdf_reader_reports_embedded_text_availability_and_bounds_pages(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('Optional pypdf dependency is not installed in this interpreter.')
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with (self.root / 'blank.pdf').open('wb') as stream:
            writer.write(stream)

        result = self.files.read_pdf('blank.pdf')

        self.assertEqual(result['total_pages'], 1)
        self.assertFalse(result['embedded_text_found'])
        self.assertFalse(result['ocr_available'])
        with self.assertRaisesRegex(AgentError, '1 to 10 pages'):
            self.files.read_pdf('blank.pdf', page_count=11)

    def test_exact_text_writes_use_sha_and_create_never_overwrites(self):
        target = self.root / 'note.txt'
        target.write_text('before\n', encoding='utf-8')
        original = self.files.read_text('note.txt')['sha256']

        receipt = self.files.write_text('note.txt', 'after\n', original)
        self.assertEqual(target.read_text(encoding='utf-8'), 'after\n')
        self.assertNotEqual(receipt['before_sha256'], receipt['after_sha256'])
        with self.assertRaisesRegex(AgentError, 'changed since inspection'):
            self.files.write_text('note.txt', 'stale\n', original)

        created = self.files.create_text('new.txt', 'new content\n')
        self.assertIsNone(created['before_sha256'])
        with self.assertRaisesRegex(AgentError, 'cannot overwrite'):
            self.files.create_text('new.txt', 'replace')

    def test_instruction_files_cannot_be_edited(self):
        (self.root / 'AGENTS.md').write_text('Instructions', encoding='utf-8')
        sha = self.files.read_text('AGENTS.md')['sha256']
        with self.assertRaisesRegex(AgentError, 'instruction files are protected'):
            self.files.write_text('AGENTS.md', 'changed', sha)

    def test_symlink_is_not_traversed(self):
        outside_temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        (outside / 'secret.txt').write_text('not in scope', encoding='utf-8')
        link = self.root / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'Host does not permit directory symlinks: {exc}')

        self.assertEqual(self.files.find_files('secret')['matches'], [])
        with self.assertRaises(AgentError):
            self.files.list_directory('linked')


if __name__ == '__main__':
    unittest.main()
