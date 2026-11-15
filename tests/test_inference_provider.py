import unittest
from unittest.mock import patch

from inference_provider import InferenceProvider
from agent_core import AgentError
from local_agent import Ollama, create_inference_provider
from agent_config import DEFAULT_ENDPOINT


class InferenceProviderContractTests(unittest.TestCase):
    def test_local_ollama_implements_model_level_contract(self):
        self.assertIsInstance(Ollama(DEFAULT_ENDPOINT), InferenceProvider)

    def test_models_uses_only_the_installed_local_tags_endpoint(self):
        client = Ollama(DEFAULT_ENDPOINT)
        with patch.object(client, 'request', return_value={
            'models': [{'name': 'qwen3.5:4b'}],
        }) as request:
            self.assertEqual(client.models(), [{'name': 'qwen3.5:4b'}])
        request.assert_called_once_with('/api/tags')

    def test_factory_builds_configured_local_provider_and_rejects_unknown(self):
        provider = create_inference_provider('ollama', DEFAULT_ENDPOINT)
        self.assertIsInstance(provider, InferenceProvider)
        self.assertEqual(provider.base, DEFAULT_ENDPOINT)
        with self.assertRaises(AgentError):
            create_inference_provider('remote_ollama', DEFAULT_ENDPOINT)

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
