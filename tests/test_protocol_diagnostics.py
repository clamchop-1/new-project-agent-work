"""Focused tests for private protocol diagnostic storage."""
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest import mock

import protocol_diagnostics as diagnostics
from agent_core import AgentError
from agent_config import DEFAULT_ENDPOINT
from inference_provider import ProviderFailure
import local_agent
from local_agent import MAX_INFERENCE_RESPONSE_BYTES, Ollama, create_inference_provider
import protocol_diagnostic_cli as diagnostic_cli
from agent_core import encoded


class ProtocolDiagnosticStorageTests(unittest.TestCase):
    def setUp(self):
        self.real_set_windows_acl = diagnostics._set_and_verify_windows_acl
        self.real_verify_windows_acl = diagnostics._verify_windows_acl
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = "LOCALAPPDATA" if diagnostics.IS_WINDOWS else "XDG_STATE_HOME"
        self.env_patch = mock.patch.dict(os.environ, {self.env: str(self.root)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.windows_patch = mock.patch.object(diagnostics, "IS_WINDOWS", True)
        self.windows_patch.start()
        self.addCleanup(self.windows_patch.stop)
        self.acl_patch = mock.patch.object(diagnostics, "_set_and_verify_windows_acl")
        self.acl_patch.start()
        self.addCleanup(self.acl_patch.stop)
        self.verify_patch = mock.patch.object(diagnostics, "_verify_windows_acl")
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)

    def test_creates_unique_run_under_fixed_approved_root(self):
        first = diagnostics.ProtocolCapture()
        second = diagnostics.ProtocolCapture()
        approved = self.root / diagnostics.APPROVED_ROOT_NAME
        self.assertEqual(first.directory.parent, approved)
        self.assertNotEqual(first.directory, second.directory)
        self.assertTrue(first.directory.is_dir())

    def test_rejects_caller_selected_and_traversal_paths(self):
        with self.assertRaises(AgentError):
            diagnostics.ProtocolCapture(self.root / ".." / "outside")
        self.assertFalse((self.root.parent / "outside").exists())

    def test_rejects_existing_run_directory(self):
        run_id = mock.Mock(hex="collision")
        with mock.patch.object(diagnostics.uuid, "uuid4", return_value=run_id):
            first = diagnostics.ProtocolCapture()
            with self.assertRaises(AgentError):
                diagnostics.ProtocolCapture()
        self.assertTrue(first.directory.is_dir())

    def test_rejects_symlink_or_reparse_component(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(AgentError):
            diagnostics._reject_reparse_chain(link / "child")

    def test_acl_verification_failure_fails_closed(self):
        with mock.patch.object(diagnostics, "IS_WINDOWS", True), \
                mock.patch.object(diagnostics, "_set_and_verify_windows_acl",
                                  side_effect=AgentError("ACL verification failed")):
            with self.assertRaisesRegex(AgentError, "ACL verification failed"):
                diagnostics._ensure_private_permissions(self.root)

    def test_windows_acl_helpers_pass_path_through_child_environment(self):
        path = self.root / "acl-check"
        result = mock.Mock(returncode=0)
        with mock.patch.object(diagnostics, "_set_and_verify_windows_acl",
                               self.real_set_windows_acl), \
                mock.patch.object(diagnostics, "_verify_windows_acl",
                                  self.real_verify_windows_acl), \
                mock.patch.object(diagnostics.subprocess, "run", return_value=result) as run:
            diagnostics._set_and_verify_windows_acl(path)
            diagnostics._verify_windows_acl(path)

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
            self.assertEqual(len(command), 5)
            self.assertIn("$env:" + diagnostics.POWERSHELL_ACL_PATH_ENV, command[4])
            self.assertNotIn("$args[0]", command[4])
            self.assertEqual(call.kwargs["env"][diagnostics.POWERSHELL_ACL_PATH_ENV], str(path))
            self.assertEqual(call.kwargs["timeout"], 15)
        self.assertNotIn(diagnostics.POWERSHELL_ACL_PATH_ENV, os.environ)

    @unittest.skipUnless(os.name == "nt", "Windows ACL verification requires Windows")
    def test_windows_acl_is_established_and_verified_without_admin(self):
        path = self.root / "acl-check"
        path.mkdir()
        with mock.patch.object(diagnostics, "IS_WINDOWS", True), \
                mock.patch.object(diagnostics, "_set_and_verify_windows_acl",
                                  self.real_set_windows_acl), \
                mock.patch.object(diagnostics, "_verify_windows_acl",
                                  self.real_verify_windows_acl):
            diagnostics._set_and_verify_windows_acl(path)
            diagnostics._verify_windows_acl(path)

    def test_rejects_unsafe_inherited_permissions(self):
        path = self.root / "broad"
        path.mkdir()
        with mock.patch.object(diagnostics, "IS_WINDOWS", False), \
                mock.patch.object(diagnostics.stat, "S_IMODE", return_value=0o755):
            with self.assertRaisesRegex(AgentError, "broader than owner-only"):
                diagnostics._validate_private_directory(path)

    def test_atomic_write_creates_complete_artifact(self):
        run = self.root / "run"
        run.mkdir()
        payload = b'{"complete":true}'
        target = diagnostics._atomic_create(run, "evidence.json", payload)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(list(run.glob(".capture-*")))

    def test_atomic_write_prevents_overwrite(self):
        run = self.root / "run"
        run.mkdir()
        target = run / "evidence.json"
        target.write_bytes(b"original")
        with self.assertRaises(AgentError):
            diagnostics._atomic_create(run, target.name, b"replacement")
        self.assertEqual(target.read_bytes(), b"original")

    def test_atomic_write_fails_closed_if_link_creation_fails(self):
        run = self.root / "run"
        run.mkdir()
        with mock.patch.object(diagnostics.os, "link", side_effect=OSError("denied")):
            with self.assertRaisesRegex(AgentError, "failed closed"):
                diagnostics._atomic_create(run, "evidence.json", b"body")
        self.assertFalse((run / "evidence.json").exists())

    def test_capture_hashes_exact_bytes_and_omits_free_form_content(self):
        capture = diagnostics.ProtocolCapture()
        request = json.dumps({
            "messages": [{"role": "user", "content": "private free form secret-value"}],
            "tools": [{"function": {"name": "inspect", "arguments": {"token": "TOPSECRET"}}}],
        }, separators=(",", ":")).encode()
        response = b'{"message":{"content":"response private data"}}'
        path = capture.save(request, response, endpoint="http://localhost/api/chat", model="test",
                            status=200, elapsed_seconds=0.1, error="sensitive trace")
        record = json.loads(path.read_bytes())
        self.assertEqual(record["request_sha256"], hashlib.sha256(request).hexdigest())
        self.assertEqual(record["response_sha256"], hashlib.sha256(response).hexdigest())
        text = path.read_text(encoding="utf-8")
        for secret in ("private free form secret-value", "TOPSECRET", "response private data", "sensitive trace"):
            self.assertNotIn(secret, text)
        self.assertEqual(record["raw_payloads_stored"], False)
        self.assertEqual(record["response_state"], "complete")

    def test_recursive_known_secret_redaction(self):
        value = {"nested": [{"authorization": "Bearer abcdefghijklmnop"},
                            {"note": "api_key=secret123"}]}
        rendered = json.dumps(diagnostics.redact(value))
        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertNotIn("secret123", rendered)

    def test_invalid_capture_does_not_create_artifact(self):
        capture = diagnostics.ProtocolCapture()
        with self.assertRaises(AgentError):
            capture.save(b"not-json", b"{}", endpoint="x", model="m", status=200,
                         elapsed_seconds=0)
        self.assertEqual(list(capture.directory.iterdir()), [])

    def test_capture_size_limit_fails_closed(self):
        capture = diagnostics.ProtocolCapture()
        with self.assertRaisesRegex(AgentError, "size limit"):
            capture.save(b"x" * (diagnostics.MAX_CAPTURE_BYTES + 1), b"{}", endpoint="x",
                         model="m", status=200, elapsed_seconds=0)
        self.assertEqual(list(capture.directory.iterdir()), [])

    class FakeResponse:
        def __init__(self, status=200, body=b""):
            self.status = status
            self.body = body

        def read(self, size):
            result, self.body = self.body[:size], self.body[size:]
            return result

    class FakeConnection:
        def __init__(self, response):
            self.sock = None
            self.response = response
            self.request_args = None
            self.closed = False
            self.connect_error = None

        def connect(self):
            if self.connect_error is not None:
                raise self.connect_error

        def request(self, *args, **kwargs):
            self.request_args = (args, kwargs)

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    def _transport(self, response, capture=None):
        client = Ollama(DEFAULT_ENDPOINT, diagnostic_capture=capture)
        connection = self.FakeConnection(response)
        factory = mock.patch("local_agent.http.client.HTTPConnection", return_value=connection)
        factory.start()
        self.addCleanup(factory.stop)
        return client, connection

    def _record(self, capture):
        files = list(capture.directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text(encoding="utf-8"))

    def test_diagnostics_disabled_by_default_without_storage_creation(self):
        client = Ollama(DEFAULT_ENDPOINT)
        self.assertIsNone(client.diagnostic_capture)
        self.assertFalse((self.root / diagnostics.APPROVED_ROOT_NAME).exists())

    def test_explicit_capture_opt_in_flows_through_provider_factory(self):
        capture = diagnostics.ProtocolCapture()
        provider = create_inference_provider(
            "ollama", DEFAULT_ENDPOINT, diagnostic_capture=capture
        )
        self.assertIs(provider.diagnostic_capture, capture)

    def test_exact_serialized_request_and_received_response_hashes(self):
        capture = diagnostics.ProtocolCapture()
        response_body = b'{"message":{"role":"assistant","content":"ok"}}'
        client, connection = self._transport(self.FakeResponse(body=response_body), capture)
        payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "secret prompt"}],
                   "tools": [{"type": "function", "function": {"name": "inspect",
                       "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                      "required": ["path"], "additionalProperties": False}}}],
                   "stream": False, "think": False, "options": {"num_ctx": 16384, "temperature": 0}}
        client.request("/api/chat", payload)
        sent_body = connection.request_args[1]["body"]
        record = self._record(capture)
        self.assertEqual(record["request_sha256"], hashlib.sha256(sent_body).hexdigest())
        self.assertEqual(record["response_sha256"], hashlib.sha256(response_body).hexdigest())
        self.assertEqual(record["request_state"], "submitted")
        self.assertEqual(record["request_structure"]["messages_ordered"][0]["role"], "user")
        self.assertEqual(record["request_structure"]["requested_context_length"], 16384)
        schema = record["request_structure"]["tools"][0]["parameters"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["path"]["type"], "string")
        self.assertEqual(schema["required"], ["path"])
        self.assertNotIn("secret prompt", json.dumps(record))

    def test_structured_tool_call_response_is_observed_without_dispatch(self):
        capture = diagnostics.ProtocolCapture()
        body = (b'{"message":{"role":"assistant","content":"", "tool_calls":['
                b'{"function":{"name":"read_file","arguments":{"path":"private.py"}}}]}}')
        client, _ = self._transport(self.FakeResponse(body=body), capture)
        result = client.request("/api/chat", {"model": "fixture", "messages": [], "options": {"num_ctx": 64}})
        self.assertIn("tool_calls", result["message"])
        summary = self._record(capture)["response_structure"]
        self.assertTrue(summary["tool_calls_present"])
        self.assertEqual(summary["tool_calls"][0]["name"], "read_file")
        self.assertNotIn("private.py", json.dumps(summary))

    def test_text_only_response_is_classified_without_storing_text(self):
        capture = diagnostics.ProtocolCapture()
        body = b'{"message":{"role":"assistant","content":"<tool_call>secret</tool_call>"}}'
        client, _ = self._transport(self.FakeResponse(body=body), capture)
        client.request("/api/chat", {"model": "fixture", "messages": []})
        summary = self._record(capture)["response_structure"]
        self.assertFalse(summary["tool_calls_present"])
        self.assertTrue(summary["assistant_text_present"])
        self.assertTrue(summary["recognized_tool_markup"])
        self.assertNotIn("secret", json.dumps(summary))

    def test_malformed_json_response_is_captured_as_complete_bytes(self):
        capture = diagnostics.ProtocolCapture()
        body = b"not-json-private"
        client, _ = self._transport(self.FakeResponse(body=body), capture)
        with self.assertRaises(ProviderFailure) as caught:
            client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "malformed_response")
        record = self._record(capture)
        self.assertEqual(record["response_state"], "complete")
        self.assertEqual(record["response_sha256"], hashlib.sha256(body).hexdigest())
        self.assertFalse(record["response_structure"]["json_parse_success"])
        self.assertNotIn("not-json-private", json.dumps(record))

    def test_http_error_body_and_status_are_preserved(self):
        capture = diagnostics.ProtocolCapture()
        body = b'{"error":"private server response"}'
        client, _ = self._transport(self.FakeResponse(500, body), capture)
        with self.assertRaises(ProviderFailure) as caught:
            client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "service_unavailable")
        record = self._record(capture)
        self.assertEqual(record["http_status"], 500)
        self.assertEqual(record["response_state"], "complete")
        self.assertNotIn("private server response", json.dumps(record))

    def test_timeout_without_body_bytes_is_not_recorded_as_empty_complete_body(self):
        capture = diagnostics.ProtocolCapture()
        response = self.FakeResponse()
        response.read = mock.Mock(side_effect=socket.timeout())
        client, _ = self._transport(response, capture)
        with self.assertRaises(ProviderFailure) as caught:
            client.request("/api/chat", {"model": "fixture", "messages": []}, timeout=0.01)
        self.assertEqual(caught.exception.code, "timeout")
        record = self._record(capture)
        self.assertEqual(record["response_state"], "partial")
        self.assertIsNone(record["response_sha256"])
        self.assertIsNone(record["response_body_bytes"])

    def test_connection_failure_records_response_as_unavailable(self):
        capture = diagnostics.ProtocolCapture()
        client, connection = self._transport(self.FakeResponse(), capture)
        connection.connect_error = ConnectionRefusedError("private transport detail")
        with self.assertRaises(ProviderFailure) as caught:
            client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "unavailable")
        record = self._record(capture)
        self.assertEqual(record["response_state"], "unavailable")
        self.assertEqual(record["request_state"], "not_sent")
        self.assertIsNone(record["response_sha256"])
        self.assertNotIn("private transport detail", json.dumps(record))

    def test_partial_response_bytes_and_state_are_preserved(self):
        capture = diagnostics.ProtocolCapture()
        response = self.FakeResponse()
        response.read = mock.Mock(side_effect=[b"{", http.client.IncompleteRead(b"", 2)])
        client, _ = self._transport(response, capture)
        with self.assertRaises(ProviderFailure) as caught:
            client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "partial_response")
        record = self._record(capture)
        self.assertEqual(record["response_state"], "partial")
        self.assertEqual(record["response_sha256"], hashlib.sha256(b"{").hexdigest())

    def test_oversized_response_is_captured_as_bounded_partial(self):
        capture = diagnostics.ProtocolCapture()
        client, _ = self._transport(self.FakeResponse(body=b"123456"), capture)
        with mock.patch("local_agent.MAX_INFERENCE_RESPONSE_BYTES", 4):
            with self.assertRaises(ProviderFailure) as caught:
                client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "response_too_large")
        record = self._record(capture)
        self.assertEqual(record["response_state"], "oversized")
        self.assertEqual(record["response_body_bytes"], 5)

    def test_capture_failure_before_transmission_stops_request(self):
        capture = mock.Mock()
        capture.verify_ready.side_effect = OSError("private detail")
        client = Ollama(DEFAULT_ENDPOINT, diagnostic_capture=capture)
        with mock.patch("local_agent.http.client.HTTPConnection") as connection:
            with self.assertRaises(ProviderFailure) as caught:
                client.request("/api/chat", {"model": "fixture", "messages": []})
        self.assertEqual(caught.exception.code, "diagnostic_capture_failed")
        connection.assert_not_called()

    def test_capture_failure_after_tool_response_prevents_response_return(self):
        capture = mock.Mock()
        capture.verify_ready.return_value = None
        capture.save.side_effect = OSError("private detail")
        body = b'{"message":{"role":"assistant","tool_calls":[]}}'
        client, connection = self._transport(self.FakeResponse(body=body), capture)
        with self.assertRaises(ProviderFailure) as caught:
            client.chat("fixture", [], [], 32, {})
        self.assertEqual(caught.exception.code, "diagnostic_capture_failed")
        self.assertTrue(connection.closed)
        capture.save.assert_called_once()

    def test_capture_failure_prevents_tool_response_reaching_dispatch_caller(self):
        capture = mock.Mock()
        capture.verify_ready.return_value = None
        capture.save.side_effect = OSError("private detail")
        body = b'{"message":{"role":"assistant","tool_calls":[{"function":{"name":"read_file","arguments":{"path":"x"}}}]}}'
        client, _ = self._transport(self.FakeResponse(body=body), capture)
        downstream_dispatch = mock.Mock()
        with self.assertRaises(ProviderFailure):
            response = client.chat("fixture", [], [], 32, {})
            downstream_dispatch(response)
        downstream_dispatch.assert_not_called()

    def _cli_config(self):
        return {
            "version": 3,
            "projects": {"localcodingagent": {
                "name": "LocalCodingAgent", "root": "D:/Workspaces/LocalCodingAgent"}},
            "inference": {
                "provider": "ssh_tunnel_ollama", "model": "fixture-model:latest",
                "endpoint": "http://127.0.0.1:22001",
                "remote": {
                    "enabled": True, "profile_id": "mac_studio", "port": 22001,
                    "ssh_host_alias": "mac-studio-inference",
                    "ssh_host_key_sha256": "SHA256:" + "A" * 43,
                    "credential_ref": "ssh-agent",
                    "known_hosts_file": "C:/Users/test/.ssh/known_hosts",
                    "ssh_config_file": "C:/Users/test/.ssh/config",
                },
            },
        }

    class FakeDiagnosticProvider:
        def __init__(self, capture, response_body, identity=None):
            self.capture = capture
            self.response_body = response_body
            self.identity = identity or {"name": "fixture-model:latest",
                                         "details": {"format": "gguf"}}
            self.model_calls = []
            self.chat_calls = []
            self.closed = False

        def model(self, name, timeout=None):
            self.model_calls.append((name, timeout))
            return self.identity

        def chat(self, model, messages, tools, context, options, timeout=None):
            self.chat_calls.append((model, messages, tools, context, options, timeout))
            payload = {
                "model": model, "messages": messages, "tools": tools,
                "stream": False, "think": False, "keep_alive": "2m",
                "options": {"num_ctx": context, **options},
            }
            self.capture.save(
                encoded(payload), self.response_body, endpoint="/api/chat", model=model,
                status=200, elapsed_seconds=0.01, request_state="submitted",
                response_state="complete",
            )
            return json.loads(self.response_body)

        def close(self):
            self.closed = True

    def _run_cli(self, extra, response_body=None, *, identity=None,
                 capture_factory=None, provider_factory=None):
        response_body = response_body or b'{"message":{"role":"assistant","content":"ok"}}'
        captures = []
        providers = []

        def make_capture(**kwargs):
            value = (capture_factory(**kwargs) if capture_factory
                     else diagnostics.ProtocolCapture(**kwargs))
            captures.append(value)
            return value

        def make_provider(provider_name, endpoint, profile, *, diagnostic_capture):
            if provider_factory:
                return provider_factory(provider_name, endpoint, profile,
                                        diagnostic_capture=diagnostic_capture)
            value = self.FakeDiagnosticProvider(diagnostic_capture, response_body, identity)
            providers.append(value)
            return value

        output = io.StringIO()
        code = diagnostic_cli.main(
            extra, config_loader=self._cli_config, provider_factory=make_provider,
            capture_factory=make_capture, output=output,
        )
        rendered = json.loads(output.getvalue())
        return code, rendered, captures, providers

    def _authorized_cli_args(self, *extra):
        return ["--send", "--authorize-inference", "--approve-synthetic-input",
                "--synthetic-input", "Use the supplied diagnostic schema safely.", *extra]

    def test_cli_missing_authorization_flag_makes_no_provider_or_capture(self):
        code, result, captures, providers = self._run_cli([
            "--send", "--approve-synthetic-input", "--synthetic-input", "Synthetic only."])
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(captures, [])
        self.assertEqual(providers, [])

    def test_cli_dry_run_is_default_and_performs_no_network_or_storage(self):
        code, result, captures, providers = self._run_cli(["--include-tool-schemas"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["inference_requested"])
        self.assertFalse(result["capture_created"])
        self.assertGreater(result["tool_schema_count"], 0)
        self.assertEqual(captures, [])
        self.assertEqual(providers, [])
        self.assertFalse((self.root / diagnostics.APPROVED_ROOT_NAME).exists())

    def test_cli_authorized_request_uses_managed_route_and_static_descriptive_schemas(self):
        original_broker = local_agent.Broker
        with mock.patch.object(local_agent, "Broker") as broker, \
                mock.patch.object(local_agent, "run_agent") as run_agent:
            code, result, captures, providers = self._run_cli(
                self._authorized_cli_args("--include-tool-schemas"))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "captured")
        self.assertFalse(result["exact_replay"])
        self.assertFalse(result["tool_calls_executed"])
        self.assertEqual(result["provider"], "ssh_tunnel_ollama")
        self.assertEqual(result["profile_id"], "mac_studio")
        self.assertEqual(result["tool_schema_count"], len(local_agent.TOOLS))
        self.assertIs(local_agent.Broker, original_broker)
        broker.assert_not_called()
        run_agent.assert_not_called()
        self.assertEqual(providers[0].model_calls, [("fixture-model:latest", 30)])
        self.assertEqual(providers[0].chat_calls[0][2], local_agent.TOOLS)
        self.assertEqual(providers[0].chat_calls[0][-1], 30)
        self.assertTrue(providers[0].closed)
        record = json.loads(Path(result["capture_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(record["capture_kind"], "synthetic_reproduction")
        self.assertNotIn("Use the supplied diagnostic schema safely.", json.dumps(record))

    def test_cli_schema_profiles_and_generation_budget_are_bounded_and_non_dispatching(self):
        expected = {
            "diagnostic_noop": ["diagnostic_noop"],
            "read_file": ["read_file"],
            "read_subset": ["read_file", "list_files", "search", "git_status"],
            "all": [tool["function"]["name"] for tool in local_agent.TOOLS],
        }
        tool_schemas_before = copy.deepcopy(local_agent.TOOLS)
        shared_messages = []
        original_broker = local_agent.Broker
        with mock.patch.object(local_agent, "Broker") as broker, \
                mock.patch.object(local_agent, "run_agent") as run_agent:
            for profile, names in expected.items():
                code, result, captures, providers = self._run_cli(
                    self._authorized_cli_args(
                        "--schema-profile", profile, "--num-predict", "1600"))
                self.assertEqual(code, 0)
                self.assertEqual(result["schema_profile"], profile)
                self.assertEqual(result["num_predict"], 1600)
                self.assertEqual(result["tool_schema_count"], len(names))
                self.assertEqual(result["tool_calls_executed"], False)
                self.assertEqual(captures[0].last_artifact_path.is_file(), True)
                chat = providers[0].chat_calls[0]
                schemas = chat[2]
                self.assertEqual([schema["function"]["name"] for schema in schemas], names)
                self.assertEqual(chat[4], {"num_predict": 1600, "temperature": 0})
                self.assertEqual(chat[3], 16384)
                if profile == "diagnostic_noop":
                    parameters = schemas[0]["function"]["parameters"]
                    self.assertEqual(parameters["required"], ["status"])
                    self.assertEqual(parameters["properties"]["status"]["enum"], ["ok"])
                elif profile in {"read_file", "read_subset", "all"}:
                    shared_messages.append(chat[1])

        self.assertEqual(shared_messages[0], shared_messages[1])
        self.assertEqual(shared_messages[1], shared_messages[2])
        self.assertEqual(local_agent.TOOLS, tool_schemas_before)
        self.assertIs(local_agent.Broker, original_broker)
        broker.assert_not_called()
        run_agent.assert_not_called()

    def test_cli_rejects_conflicting_schema_selection_and_unbounded_generation(self):
        for extra in (
            ("--include-tool-schemas", "--schema-profile", "read_file"),
            ("--num-predict", "0"),
            ("--num-predict", "1601"),
        ):
            code, result, captures, providers = self._run_cli(
                self._authorized_cli_args(*extra))
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(captures, [])
            self.assertEqual(providers, [])

    def test_cli_endpoint_validation_rejects_unapproved_route(self):
        config = self._cli_config()
        config["inference"]["endpoint"] = "http://127.0.0.1:9999"
        output = io.StringIO()
        code = diagnostic_cli.main(["--dry-run"], config_loader=lambda: config,
                                   output=output)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_cli_rejects_local_provider_when_mac_managed_route_is_required(self):
        config = self._cli_config()
        config["inference"]["provider"] = "ollama"
        config["inference"]["endpoint"] = "http://127.0.0.1:11434"
        config["inference"]["remote"]["enabled"] = False
        output = io.StringIO()
        code = diagnostic_cli.main(["--dry-run"], config_loader=lambda: config, output=output)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_cli_model_identity_mismatch_stops_before_chat(self):
        wrong = {"name": "other-model", "details": {"format": "gguf"}}
        code, result, captures, providers = self._run_cli(
            self._authorized_cli_args(), identity=wrong)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(providers[0].chat_calls, [])
        self.assertTrue(providers[0].closed)

    def test_cli_requires_separate_synthetic_input_approval(self):
        code, result, captures, providers = self._run_cli([
            "--send", "--authorize-inference", "--synthetic-input", "Synthetic only."])
        self.assertEqual(code, 2)
        self.assertEqual(captures, [])
        self.assertEqual(providers, [])

    def test_cli_enforces_context_timeout_and_input_size_bounds(self):
        for args in (
            self._authorized_cli_args("--context", "32769"),
            self._authorized_cli_args("--timeout", "61"),
            ["--send", "--authorize-inference", "--approve-synthetic-input",
             "--synthetic-input", "x" * (diagnostic_cli.MAX_SYNTHETIC_INPUT_CHARS + 1)],
        ):
            code, result, captures, providers = self._run_cli(args)
            self.assertEqual(code, 2)
            self.assertEqual(captures, [])
            self.assertEqual(providers, [])

    def test_cli_enforces_serialized_request_byte_limit(self):
        with mock.patch.object(local_agent, "MAX_INFERENCE_REQUEST_BYTES", 128):
            code, result, captures, providers = self._run_cli(self._authorized_cli_args())
        self.assertEqual(code, 2)
        self.assertEqual(captures, [])
        self.assertEqual(providers, [])

    def test_cli_rejects_oversized_response_evidence(self):
        body = b'{"message":{"role":"assistant","content":"a lot"}}'
        with mock.patch.object(local_agent, "MAX_INFERENCE_RESPONSE_BYTES", 8):
            code, result, captures, providers = self._run_cli(
                self._authorized_cli_args(), response_body=body)
        self.assertEqual(code, 2)
        self.assertEqual(result["error_category"], "diagnostic_policy_or_configuration_rejected")
        self.assertTrue(providers[0].closed)

    def test_cli_capture_setup_failure_prevents_provider_creation(self):
        def fail_capture(**kwargs):
            raise AgentError("private ACL detail")
        factory = mock.Mock()
        code, result, captures, providers = self._run_cli(
            self._authorized_cli_args(), capture_factory=fail_capture,
            provider_factory=factory)
        self.assertEqual(code, 2)
        factory.assert_not_called()
        self.assertNotIn("private ACL detail", json.dumps(result))

    def test_structured_tool_response_is_classified_without_broker_or_dispatch(self):
        response = (b'{"message":{"role":"assistant","content":"", "tool_calls":['
                    b'{"function":{"name":"read_file","arguments":{"path":"x"}}}]}}')
        with mock.patch.object(local_agent, "Broker") as broker, \
                mock.patch.object(local_agent, "run_agent") as run_agent:
            code, result, _, _ = self._run_cli(
                self._authorized_cli_args("--include-tool-schemas"), response)
        self.assertEqual(code, 0)
        observation = result["response_observation"]
        self.assertTrue(observation["tool_calls_present"])
        self.assertEqual(observation["invalid_tool_call_count"], 0)
        self.assertFalse(result["tool_calls_executed"])
        broker.assert_not_called()
        run_agent.assert_not_called()

    def test_textual_tool_markup_is_classified_as_text_and_not_executed(self):
        response = b'{"message":{"role":"assistant","content":"<tool_call>private</tool_call>"}}'
        code, result, _, _ = self._run_cli(self._authorized_cli_args(), response)
        self.assertEqual(code, 0)
        observation = result["response_observation"]
        self.assertFalse(observation["tool_calls_present"])
        self.assertTrue(observation["recognized_tool_markup"])
        self.assertNotIn("private", json.dumps(result))

    def test_invalid_tool_argument_structure_is_observed(self):
        response = (b'{"message":{"role":"assistant","tool_calls":['
                    b'{"function":{"name":"read_file","arguments":"not-an-object"}}]}}')
        code, result, _, _ = self._run_cli(self._authorized_cli_args(), response)
        self.assertEqual(code, 0)
        self.assertEqual(result["response_observation"]["invalid_tool_call_count"], 1)

    def test_cli_rejects_redacted_exact_replay_input(self):
        capture = diagnostics.ProtocolCapture()
        manifest = capture.save(
            b'{"model":"fixture","messages":[{"role":"user","content":"secret"}]}',
            b'{"message":{"role":"assistant","content":"answer"}}',
            endpoint="/api/chat", model="fixture", status=200, elapsed_seconds=0.1,
        )
        output = io.StringIO()
        code = diagnostic_cli.main(["--replay-manifest", str(manifest)], output=output)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_cli_rejects_incomplete_and_invalid_replay_manifests(self):
        for contents in (b'{"format":"ollama-protocol-diagnostic-v1"}', b"not-json"):
            path = self.root / ("manifest-" + str(len(contents)) + ".json")
            path.write_bytes(contents)
            output = io.StringIO()
            code = diagnostic_cli.main(["--replay-manifest", str(path)], output=output)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
