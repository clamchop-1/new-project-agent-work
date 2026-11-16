import unittest
from unittest.mock import Mock, patch
import tempfile
import threading
from pathlib import Path

from inference_provider import InferenceProvider, ProviderFailure
from agent_core import AgentError
from local_agent import (MAX_INFERENCE_REQUEST_BYTES, Ollama, create_inference_provider,
                         NoRedirect, run_agent)
from agent_config import DEFAULT_ENDPOINT
from local_agent import _fallback_candidate, _fallback_permitted


class InferenceProviderContractTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, status=200, body=b'{"version":"0.5.0"}'):
            self.status, self.body = status, body
        def read(self, size):
            result, self.body = self.body[:size], self.body[size:]
            return result

    def _http_connection(self, status=200, body=b'{"version":"0.5.0"}'):
        connection = __import__('unittest.mock', fromlist=['Mock']).Mock()
        connection.sock = None
        connection.getresponse.return_value = self.FakeResponse(status, body)
        return connection

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

    def test_remote_factory_requires_explicit_ssh_tunnel_profile(self):
        with self.assertRaisesRegex(AgentError, 'trusted tunnel profile'):
            create_inference_provider('ssh_tunnel_ollama', 'http://127.0.0.1:22001')
        profile = {
            'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
            'ssh_host_alias': 'mac-studio-inference',
            'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
            'credential_ref': 'ssh-agent',
            'known_hosts_file': 'C:/Users/test/.ssh/known_hosts',
            'ssh_config_file': 'C:/Users/test/.ssh/config',
        }
        provider = create_inference_provider(
            'ssh_tunnel_ollama', 'http://127.0.0.1:22001', profile
        )
        self.assertEqual(provider.base, 'http://127.0.0.1:22001')
        self.assertEqual(provider.profile_id, 'mac_studio')
        with self.assertRaises(AgentError):
            create_inference_provider('ssh_tunnel_ollama', DEFAULT_ENDPOINT, profile)

    def test_remote_request_uses_same_bounded_no_redirect_transport(self):
        profile = {
            'enabled': True, 'profile_id': 'mac_studio', 'port': 22001,
            'ssh_host_alias': 'mac-studio-inference',
            'ssh_host_key_sha256': 'SHA256:' + 'A' * 43,
            'credential_ref': 'ssh-agent',
            'known_hosts_file': 'C:/Users/test/.ssh/known_hosts',
            'ssh_config_file': 'C:/Users/test/.ssh/config',
        }
        provider = create_inference_provider(
            'ssh_tunnel_ollama', 'http://127.0.0.1:22001', profile
        )
        with patch.object(provider, 'request', return_value={'models': []}) as request:
            self.assertEqual(provider.models(), [])
        request.assert_called_once_with('/api/tags')

    def test_request_size_and_timeout_are_bounded(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        with patch.object(provider.opener, 'open') as open_request:
            with self.assertRaisesRegex(AgentError, 'request exceeds'):
                provider.request('/api/chat', {'data': 'x' * MAX_INFERENCE_REQUEST_BYTES})
            with self.assertRaisesRegex(AgentError, 'timeout'):
                provider.request('/api/tags', timeout=181)
        open_request.assert_not_called()

    def test_health_check_uses_version_endpoint_and_reports_availability(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        connection = self._http_connection()
        with patch('local_agent.http.client.HTTPConnection', return_value=connection):
            self.assertEqual(provider.health_check(), {'healthy': True, 'version': '0.5.0'})
        connection.request.assert_called_once_with('GET', '/api/version', body=None, headers={})
        connection.close.assert_called_once()

    def test_connection_failure_is_structured_and_socket_is_closed(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        connection = self._http_connection()
        connection.connect.side_effect = ConnectionRefusedError('private test detail')
        with patch('local_agent.http.client.HTTPConnection', return_value=connection):
            with self.assertRaises(ProviderFailure) as caught:
                provider.request('/api/version')
        self.assertEqual(caught.exception.code, 'unavailable')
        self.assertNotIn('private test detail', str(caught.exception))
        connection.close.assert_called_once()

    def test_cancellation_during_response_stops_request(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        cancel = threading.Event()
        provider.cancel_event = cancel
        response = self.FakeResponse(body=b'{"version":"0.5.0"}')
        def read(size):
            cancel.set()
            return b'{"version":"0.5.0"}'
        response.read = read
        connection = self._http_connection()
        connection.getresponse.return_value = response
        with patch('local_agent.http.client.HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(ProviderFailure, 'cancelled'):
                provider.request('/api/version')
        connection.close.assert_called_once()

    def test_health_retries_only_one_transient_idempotent_probe(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        transient = self._http_connection(503, b'')
        healthy = self._http_connection()
        with patch('local_agent.http.client.HTTPConnection', side_effect=[transient, healthy]) as factory, \
             patch('local_agent.time.sleep'):
            self.assertEqual(provider.health_check(), {'healthy': True, 'version': '0.5.0'})
        self.assertEqual(factory.call_count, 2)

    def test_authentication_and_redirect_errors_are_not_retried(self):
        for status, code in ((401, 'authentication'), (302, 'redirect_rejected')):
            provider = Ollama(DEFAULT_ENDPOINT)
            connection = self._http_connection(status, b'')
            with patch('local_agent.http.client.HTTPConnection', return_value=connection) as factory:
                with self.assertRaises(ProviderFailure) as caught:
                    provider.health_check()
            self.assertEqual(caught.exception.code, code)
            factory.assert_called_once()

    def test_malformed_and_oversized_responses_fail_closed(self):
        provider = Ollama(DEFAULT_ENDPOINT)
        with patch('local_agent.http.client.HTTPConnection',
                   return_value=self._http_connection(body=b'not json')):
            with self.assertRaisesRegex(ProviderFailure, 'malformed JSON'):
                provider.request('/api/version')
        provider = Ollama(DEFAULT_ENDPOINT)
        with patch('local_agent.MAX_INFERENCE_RESPONSE_BYTES', 4), patch(
                'local_agent.http.client.HTTPConnection',
                return_value=self._http_connection(body=b'{"version":"long"}')):
            with self.assertRaisesRegex(ProviderFailure, 'response exceeds'):
                provider.request('/api/version')

    def test_fallback_modes_require_explicit_exact_substitution(self):
        row = {'from_provider': 'ssh_tunnel_ollama', 'from_model': 'mac-model',
               'to_provider': 'ollama', 'to_model': 'windows-model',
               'max_context': 32768, 'operator_approved': False,
               'data_sharing_authorized': False}
        policy = {'mode': 'ask', 'substitutions': [row]}
        candidate = _fallback_candidate(policy, 'ssh_tunnel_ollama', 'mac-model')
        self.assertIs(candidate, row)
        approval = Mock(return_value=True)
        self.assertTrue(_fallback_permitted(policy, candidate, approval))
        approval.assert_called_once()
        self.assertFalse(_fallback_permitted({'mode': 'disabled', 'substitutions': [row]}, row, approval))

    def test_fallback_approval_discloses_remote_data_sharing_scope(self):
        row = {'from_provider': 'ollama', 'from_model': 'windows-model',
               'to_provider': 'ssh_tunnel_ollama', 'to_model': 'mac-model',
               'max_context': 32768, 'operator_approved': False,
               'data_sharing_authorized': False}
        approval = Mock(return_value=False)
        self.assertFalse(_fallback_permitted({'mode': 'ask', 'substitutions': [row]}, row, approval))
        self.assertTrue(approval.call_args.args[0]['data_sharing'])

    def test_local_only_fallback_never_selects_remote_provider(self):
        local = {'from_provider': 'ssh_tunnel_ollama', 'from_model': 'mac-model',
                 'to_provider': 'ollama', 'to_model': 'windows-model',
                 'max_context': 32768, 'operator_approved': False,
                 'data_sharing_authorized': False}
        remote = {**local, 'to_provider': 'ssh_tunnel_ollama', 'to_model': 'other-mac-model'}
        self.assertTrue(_fallback_permitted({'mode': 'local_only', 'substitutions': [local]}, local, None))
        self.assertFalse(_fallback_permitted({'mode': 'local_only', 'substitutions': [remote]}, remote, None))

    def test_ask_fallback_rejection_stops_before_project_or_broker_setup(self):
        failed = Mock()
        failed.health_check.side_effect = ProviderFailure('unavailable', 'Provider unavailable.', retryable=True)
        policy = {'mode': 'ask', 'substitutions': [{
            'from_provider': 'ollama', 'from_model': 'windows-model',
            'to_provider': 'ssh_tunnel_ollama', 'to_model': 'mac-model',
            'max_context': 16384, 'operator_approved': False,
            'data_sharing_authorized': False,
        }]}
        with tempfile.TemporaryDirectory() as directory, patch(
                'local_agent.create_inference_provider', return_value=failed):
            with self.assertRaisesRegex(ProviderFailure, 'declined'):
                run_agent(Mock(), 'windows-model', 'task',
                          Path(directory) / 'execution', fallback_policy=policy,
                          fallback_approval=lambda proposal: False)
        failed.close.assert_called_once()

    def test_identity_failure_does_not_prompt_or_fallback(self):
        failed = Mock()
        failed.health_check.side_effect = ProviderFailure('identity_mismatch', 'Host identity rejected.', security=True)
        policy = {'mode': 'ask', 'substitutions': [{
            'from_provider': 'ollama', 'from_model': 'windows-model',
            'to_provider': 'ssh_tunnel_ollama', 'to_model': 'mac-model',
            'max_context': 16384, 'operator_approved': False,
            'data_sharing_authorized': False,
        }]}
        approval = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as directory, patch(
                'local_agent.create_inference_provider', return_value=failed):
            with self.assertRaises(ProviderFailure) as caught:
                run_agent(Mock(), 'windows-model', 'task', Path(directory) / 'execution',
                          fallback_policy=policy, fallback_approval=approval)
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        approval.assert_not_called()

    def test_redirects_are_rejected(self):
        with self.assertRaisesRegex(AgentError, 'redirects are forbidden'):
            NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.com')

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
