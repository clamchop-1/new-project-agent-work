"""Explicitly authorized, non-executing Ollama protocol diagnostic."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

from agent_config import config_path, default_projects, load_config, validate_config
from agent_core import AgentError, encoded
from inference_provider import ProviderFailure
import local_agent
from protocol_diagnostics import ProtocolCapture, load_replay

MAX_DIAGNOSTIC_CONTEXT = 32768
MAX_DIAGNOSTIC_TIMEOUT = 60
MAX_DIAGNOSTIC_PREDICT = 1600
MAX_SYNTHETIC_INPUT_CHARS = 4000
DIAGNOSTIC_NOOP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "diagnostic_noop",
        "description": (
            "Synthetic diagnostic output only. No implementation or handler exists; "
            "the host records the response without dispatching this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}


def _tool_schemas(profile):
    """Return only fixed, descriptive diagnostic schema profiles."""
    if profile is None:
        return []
    if profile == "diagnostic_noop":
        return [copy.deepcopy(DIAGNOSTIC_NOOP_SCHEMA)]
    by_name = {item["function"]["name"]: item for item in local_agent.TOOLS}
    if profile == "read_file":
        names = ("read_file",)
    elif profile == "read_subset":
        names = ("read_file", "list_files", "search", "git_status")
    elif profile == "all":
        return copy.deepcopy(local_agent.TOOLS)
    else:
        raise AgentError("Diagnostic schema profile is unsupported.")
    return [copy.deepcopy(by_name[name]) for name in names]


def _parser():
    parser = argparse.ArgumentParser(
        description="Run a bounded, non-executing Ollama protocol diagnostic."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true",
                      help="send one inference request (requires both authorization flags)")
    mode.add_argument("--dry-run", action="store_true",
                      help="validate local settings and show the request plan without network access")
    parser.add_argument("--authorize-inference", action="store_true",
                        help="explicitly authorize one inference request")
    parser.add_argument("--synthetic-input", help="explicit synthetic diagnostic prompt")
    parser.add_argument("--approve-synthetic-input", action="store_true",
                        help="confirm that the supplied input is synthetic and approved for this request")
    parser.add_argument("--include-tool-schemas", action="store_true",
                        help="include all static agent tool schemas as descriptive data only")
    parser.add_argument("--schema-profile", choices=(
        "diagnostic_noop", "read_file", "read_subset", "all"),
        help="select a fixed descriptive schema profile for controlled diagnostics")
    parser.add_argument("--context", type=int, default=16384,
                        help=f"requested context (512..{MAX_DIAGNOSTIC_CONTEXT}; default 16384)")
    parser.add_argument("--num-predict", type=int, default=256,
                        help=f"maximum generated tokens (1..{MAX_DIAGNOSTIC_PREDICT}; default 256)")
    parser.add_argument("--timeout", type=int, default=30,
                        help=f"per-request timeout in seconds (1..{MAX_DIAGNOSTIC_TIMEOUT}; default 30)")
    parser.add_argument("--replay-manifest", type=Path,
                        help="validate then refuse exact replay of a redacted diagnostic manifest")
    return parser


def _read_config():
    return load_config(config_path(local_agent.HUB), default_projects(local_agent.HUB))


def _validate_manifest_artifact(capture, *, model, context, tool_names,
                                request_sha256, request_bytes, num_predict):
    path = getattr(capture, "last_artifact_path", None)
    directory = Path(capture.directory).resolve(strict=True)
    if not isinstance(path, (str, Path)):
        raise AgentError("Protocol evidence manifest was not produced.")
    path = Path(path)
    if path.is_symlink() or path.parent.resolve(strict=True) != directory:
        raise AgentError("Protocol evidence manifest is outside its private run directory.")
    if not path.is_file() or path.stat().st_size > 3 * 1024 * 1024:
        raise AgentError("Protocol evidence manifest is missing or oversized.")
    try:
        record = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError("Protocol evidence manifest is invalid.") from exc
    if (not isinstance(record, dict)
            or record.get("format") != "ollama-protocol-diagnostic-v1"
            or record.get("capture_kind") != "synthetic_reproduction"
            or record.get("raw_payloads_stored") is not False
            or record.get("request_state") != "submitted"
            or record.get("response_state") != "complete"
            or not isinstance(record.get("request_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["request_sha256"])
            or record["request_sha256"] != request_sha256
            or type(record.get("request_body_bytes")) is not int
            or record["request_body_bytes"] != request_bytes
            or not 1 <= record["request_body_bytes"] <= local_agent.MAX_INFERENCE_REQUEST_BYTES
            or type(record.get("response_body_bytes")) is not int
            or not 0 <= record["response_body_bytes"] <= local_agent.MAX_INFERENCE_RESPONSE_BYTES
            or not isinstance(record.get("response_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["response_sha256"])
            or record.get("http_status") != 200
            or record.get("endpoint_api_path") != "/api/chat"
            or record.get("model") != model
            or not isinstance(record.get("request_structure"), dict)
            or record["request_structure"].get("model") != model
            or record["request_structure"].get("requested_context_length") != context
            or record["request_structure"].get("stream") is not False
            or record["request_structure"].get("think") is not False
            or not isinstance(record["request_structure"].get("options"), dict)
            or record["request_structure"]["options"].get("num_ctx") != context
            or record["request_structure"]["options"].get("num_predict") != num_predict
            or record["request_structure"]["options"].get("temperature") != 0
            or not isinstance(record["request_structure"].get("tools"), list)
            or [tool.get("name") for tool in record["request_structure"].get("tools", [])
                if isinstance(tool, dict)] != tool_names
            or not isinstance(record.get("response_structure"), dict)):
        raise AgentError("Protocol evidence is incomplete; the diagnostic cannot report success.")
    if (record["response_structure"].get("json_parse_success") is not True
            or record["response_structure"].get("json_object") is not True
            or record["response_structure"].get("message_present") is not True):
        raise AgentError("Protocol response evidence is malformed; the diagnostic cannot report success.")
    return path, record


def _failure_capture_paths(capture):
    if capture is None:
        return {"capture_directory": None, "capture_manifest": None}
    manifest = getattr(capture, "last_artifact_path", None)
    return {
        "capture_directory": str(capture.directory),
        "capture_manifest": str(manifest) if manifest else None,
    }


def main(argv=None, *, config_loader=None, provider_factory=None,
         capture_factory=None, output=None):
    """Execute one explicitly authorized diagnostic, or only describe a dry run."""
    args = _parser().parse_args(argv)
    output = output or sys.stdout
    config_loader = config_loader or _read_config
    provider_factory = provider_factory or local_agent.create_inference_provider
    capture_factory = capture_factory or ProtocolCapture
    provider = None
    capture = None
    result = {}
    exit_code = 0

    try:
        if args.context < 512 or args.context > MAX_DIAGNOSTIC_CONTEXT:
            raise AgentError("Diagnostic context is outside the allowed bound.")
        if args.timeout < 1 or args.timeout > MAX_DIAGNOSTIC_TIMEOUT:
            raise AgentError("Diagnostic timeout is outside the allowed bound.")
        if args.num_predict < 1 or args.num_predict > MAX_DIAGNOSTIC_PREDICT:
            raise AgentError("Diagnostic generation budget is outside the allowed bound.")
        if args.include_tool_schemas and args.schema_profile is not None:
            raise AgentError("Choose either --include-tool-schemas or --schema-profile.")
        if args.replay_manifest is not None:
            if args.send:
                raise AgentError("Exact replay and diagnostic reproduction cannot be combined.")
            load_replay(args.replay_manifest)
            raise AgentError("Exact replay is unavailable for redacted diagnostic captures.")
        config = validate_config(config_loader())
        inference = config["inference"]
        if inference["provider"] != "ssh_tunnel_ollama":
            raise AgentError("Diagnostic requires the configured managed SSH-tunnel Ollama provider.")
        profile = inference.get("remote")
        if not isinstance(profile, dict) or profile.get("enabled") is not True:
            raise AgentError("Configured managed inference route is not enabled.")
        model = inference["model"]
        schema_profile = "all" if args.include_tool_schemas else args.schema_profile
        tools = _tool_schemas(schema_profile)
        if args.send:
            if not args.authorize_inference:
                raise AgentError("--send requires --authorize-inference.")
            if not args.approve_synthetic_input or not args.synthetic_input:
                raise AgentError("--send requires approved nonempty --synthetic-input.")
            if len(args.synthetic_input) > MAX_SYNTHETIC_INPUT_CHARS:
                raise AgentError("Synthetic diagnostic input exceeds its character limit.")
            messages = [
                {"role": "system", "content": (
                    "This is a non-executing protocol diagnostic. The host will capture but never "
                    "dispatch or execute a proposed tool call. Tool schemas, if supplied, are for "
                    "structured output only. When the user requests a tool call, return it using "
                    "native structured tool_calls; do not encode a tool request as ordinary prose."
                )},
                {"role": "user", "content": args.synthetic_input},
            ]
            options = {"num_predict": args.num_predict, "temperature": 0}
            request_body = encoded({
                "model": model, "messages": messages, "tools": tools,
                "stream": False, "think": False, "keep_alive": "2m",
                "options": {"num_ctx": args.context, **options},
            })
            payload_size = len(request_body)
            request_sha256 = hashlib.sha256(request_body).hexdigest()
            if payload_size > local_agent.MAX_INFERENCE_REQUEST_BYTES:
                raise AgentError("Diagnostic request exceeds the configured byte limit.")
            capture = capture_factory(capture_kind="synthetic_reproduction")
            provider = provider_factory(
                inference["provider"], inference["endpoint"], profile,
                diagnostic_capture=capture,
            )
            identity = provider.model(model, timeout=args.timeout)
            if (not isinstance(identity, dict) or identity.get("name") != model
                    or identity.get("remote_host") or identity.get("remote_model")
                    or not isinstance(identity.get("details"), dict)
                    or identity["details"].get("format") not in ("gguf", "safetensors")):
                raise AgentError("Configured model identity could not be verified.")
            response = provider.chat(model, messages, tools, args.context, options,
                                     timeout=args.timeout)
            manifest_path, record = _validate_manifest_artifact(
                capture, model=model, context=args.context,
                tool_names=[tool["function"]["name"] for tool in tools],
                request_sha256=request_sha256, request_bytes=payload_size,
                num_predict=args.num_predict,
            )
            result = {
                "status": "captured",
                "capture_kind": "synthetic_reproduction",
                "exact_replay": False,
                "provider": inference["provider"],
                "profile_id": profile.get("profile_id"),
                "model": model,
                "model_identity_validated": True,
                "request_bytes": payload_size,
                "context": args.context,
                "num_predict": args.num_predict,
                "timeout_seconds": args.timeout,
                "schema_profile": schema_profile or "none",
                "tool_schema_count": len(tools),
                "tool_calls_executed": False,
                "capture_manifest": str(manifest_path),
                "response_observation": record["response_structure"],
            }
        else:
            result = {
                "status": "dry_run",
                "inference_requested": False,
                "capture_created": False,
                "provider": inference["provider"],
                "profile_id": profile.get("profile_id"),
                "model": model,
                "context": args.context,
                "num_predict": args.num_predict,
                "timeout_seconds": args.timeout,
                "schema_profile": schema_profile or "none",
                "tool_schema_count": len(tools),
                "tool_schema_names": [entry["function"]["name"] for entry in tools],
            }
    except ProviderFailure as exc:
        result = {"status": "failed", "error_category": exc.code,
                  **_failure_capture_paths(capture)}
        exit_code = 1
    except AgentError:
        result = {"status": "blocked", "error_category": "diagnostic_policy_or_configuration_rejected",
                  **_failure_capture_paths(capture)}
        exit_code = 2
    except Exception as exc:
        result = {"status": "failed", "error_category": type(exc).__name__,
                  **_failure_capture_paths(capture)}
        exit_code = 1
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                if exit_code == 0:
                    result = {"status": "failed", "error_category": "provider_close_failed",
                              **_failure_capture_paths(capture)}
                    exit_code = 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False), file=output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
