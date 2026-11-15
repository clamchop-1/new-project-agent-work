import unittest
from unittest.mock import patch

from inference_provider import InferenceProvider
from local_agent import Ollama
from agent_config import DEFAULT_ENDPOINT


class InferenceProviderContractTests(unittest.TestCase):
    def test_local_ollama_implements_model_level_contract(self):
        self.assertIsInstance(Ollama(DEFAULT_ENDPOINT), InferenceProvider)

    def test_chat_maps_contract_to_non_streaming_local_ollama_request(self):
        client = Ollama(DEFAULT_ENDPOINT)
        messages = [{'role': 'user', 'content': 'Inspect'}]
        tools = [{'type': 'function', 'function': {'name': 'finish'}}]
        options = {'num_predict': 800, 'temperature': 0}
        expected = {
            'model': 'local-model',
            'messages': messages,
            'tools': tools,
            'stream': False,
            'think': False,
            'keep_alive': '2m',
            'options': {'num_ctx': 4096, **options},
        }
        with patch.object(client, 'request', return_value={'message': {'role': 'assistant'}}) as request:
            result = client.chat(
                'local-model', messages, tools, 4096, options
            )

        self.assertEqual(result, {'message': {'role': 'assistant'}})
        request.assert_called_once_with('/api/chat', expected)


if __name__ == '__main__':
    unittest.main()
