import unittest

from interactive_agent import local_policy_answer


class InteractivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.memory = [{
            'sources': [
                'D:/Workspaces/LocalCodingAgent/.local/PROJECT_CONTEXT.md',
                'D:/Workspaces/LocalCodingAgent/.local/DURABLE_MEMORY.md',
            ],
            'text': (
                'Handoff-ID: handoff-example\n'
                'Objective: Restore the local assistant.\n'
                '### Next Actions\n'
                '- Start PowerShell in the selected folder.\n'
                '- Run agent.\n'
                '### Constraints\n'
                '- Keep memory private.\n'
            ),
        }]

    def test_memory_question_reports_sources_and_current_handoff(self):
        answer = local_policy_answer('load my project handover', 'LocalCodingAgent', self.memory)

        self.assertIn('PROJECT_CONTEXT.md', answer)
        self.assertIn('DURABLE_MEMORY.md', answer)
        self.assertIn('handoff-example', answer)
        self.assertIn('Restore the local assistant.', answer)
        self.assertIn('Run agent.', answer)

    def test_learning_question_explains_local_reference_and_durable_memory(self):
        answer = local_policy_answer(
            'Can I train you further by uploading instruction manuals?',
            'LocalCodingAgent',
            self.memory,
        )

        self.assertIn('cannot accept file attachments', answer)
        self.assertIn('Place a text file or PDF', answer)
        self.assertIn('project-memory adapter', answer)
        self.assertIn('does not currently have a direct memory-save command', answer)

    def test_unrelated_question_uses_qwen(self):
        self.assertIsNone(local_policy_answer('What is the weather?', 'LocalCodingAgent', self.memory))


if __name__ == '__main__':
    unittest.main()
