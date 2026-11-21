"""Opt-in redacted Ollama protocol evidence and non-dispatching replay."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid

from agent_core import AgentError, encoded

MAX_CAPTURE_BYTES = 3 * 1024 * 1024
APPROVED_ROOT_NAME = "LocalCodingAgent\\ProtocolDiagnostics"
POWERSHELL_ACL_PATH_ENV = "LOCAL_CODING_AGENT_DIAGNOSTIC_ACL_PATH"
IS_WINDOWS = os.name == "nt"
SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|password|access[_-]?token|authorization|secret)"
    r"(\s*[:=]\s*)([^\s,;\"']+)"
)
BEARER = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b")


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = SECRET_VALUE.sub(r"\1\2[REDACTED]", value)
        value = BEARER.sub("Bearer [REDACTED]", value)
        return TOKEN.sub("[REDACTED]", value)
    return value


class ProtocolCapture:
    """Capture redacted protocol evidence under the fixed per-user diagnostic root."""

    def __init__(self, directory=None, *, capture_kind="transport_capture"):
        # A caller may not select a storage location. Keep an optional argument only
        # to produce a clear rejection for older callers.
        if directory is not None:
            raise AgentError("Diagnostic paths are fixed; caller-selected paths are forbidden.")
        if capture_kind not in {"transport_capture", "synthetic_reproduction"}:
            raise AgentError("Diagnostic capture kind is unsupported.")
        base = os.environ.get("LOCALAPPDATA") if IS_WINDOWS else os.environ.get("XDG_STATE_HOME")
        if not base:
            if IS_WINDOWS:
                raise AgentError("LOCALAPPDATA is required for diagnostic storage.")
            base = str(Path.home() / ".local" / "state")
        approved_root = Path(base) / APPROVED_ROOT_NAME
        _reject_reparse_chain(approved_root)
        try:
            approved_root.mkdir(parents=True, exist_ok=True)
            _ensure_private_permissions(approved_root)
            run_dir = approved_root / ("run-" + uuid.uuid4().hex)
            run_dir.mkdir(exist_ok=False)
            _ensure_private_permissions(run_dir)
            _validate_private_directory(run_dir)
        except AgentError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"Cannot establish private diagnostic storage: {type(exc).__name__}.") from exc
        self.directory = run_dir
        self.capture_kind = capture_kind
        self.last_artifact_path = None

    def verify_ready(self):
        _validate_private_directory(self.directory)
        if not self.directory.is_dir():
            raise AgentError("Diagnostic run directory is unavailable.")

    def save(self, request_bytes, response_bytes, *, endpoint, model, status,
             elapsed_seconds, response_state=None, request_state="submitted", error=None):
        request_bytes = bytes(request_bytes or b"")
        if response_bytes is not None:
            response_bytes = bytes(response_bytes)
        if len(request_bytes) > MAX_CAPTURE_BYTES or (
                response_bytes is not None and len(response_bytes) > MAX_CAPTURE_BYTES):
            raise AgentError("Protocol diagnostic capture exceeds its size limit.")
        try:
            request = json.loads(request_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError("Diagnostic request is not valid JSON; no evidence was written.") from exc
        response = None
        response_parse_error = False
        if response_bytes is not None:
            try:
                response = json.loads(response_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_parse_error = True
        if response_state is None:
            response_state = "complete" if response_bytes is not None else "unavailable"
        if response_state not in {"complete", "partial", "unavailable", "oversized"}:
            raise AgentError("Diagnostic response completeness state is invalid.")
        if request_state not in {"not_sent", "submitted", "uncertain"}:
            raise AgentError("Diagnostic request transmission state is invalid.")
        if response_state == "unavailable" and response_bytes is not None:
            raise AgentError("Unavailable response state cannot contain response bytes.")
        if response_state in {"complete", "oversized"} and response_bytes is None:
            raise AgentError("Complete or oversized response state requires received bytes.")
        _validate_private_directory(self.directory)
        record = {
            "format": "ollama-protocol-diagnostic-v1",
            "capture_kind": self.capture_kind,
            "created_at_epoch": time.time(), "endpoint_api_path": redact(str(endpoint)),
            "model": redact(str(model)), "http_status": status,
            "elapsed_seconds": elapsed_seconds,
            "request_state": request_state,
            "response_state": response_state,
            "error_category": _safe_error_category(error),
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "response_sha256": (hashlib.sha256(response_bytes).hexdigest()
                                if response_bytes is not None else None),
            "request_body_bytes": len(request_bytes),
            "response_body_bytes": (len(response_bytes) if response_bytes is not None else None),
            "request_structure": _request_summary(request),
            "response_structure": _response_summary(response, response_parse_error,
                                                     response_bytes, response_state),
            "raw_payloads_stored": False,
        }
        name = uuid.uuid4().hex + ".json"
        self.last_artifact_path = _atomic_create(self.directory, name, encoded(record))
        return self.last_artifact_path


def _safe_error_category(error):
    allowed = {"timeout", "cancelled", "unavailable", "connection_lost", "partial_response",
               "response_too_large", "malformed_response", "redirect_rejected", "authentication",
               "model_missing", "service_unavailable", "request_rejected", "diagnostic_capture_failed"}
    if isinstance(error, str) and error in allowed:
        return error
    return "other" if error else None


def _value_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _shape(value, depth=0):
    kind = _value_type(value)
    if depth >= 8:
        return {"type": kind, "truncated": True}
    if isinstance(value, dict):
        return {"type": "object", "properties": {
            str(key): _shape(item, depth + 1) for key, item in sorted(value.items())
            if isinstance(key, str) and len(key) <= 80
        }}
    if isinstance(value, list):
        return {"type": "array", "items": _shape(value[0], depth + 1) if value else None,
                "length": len(value)}
    return {"type": kind}


def _schema_shape(schema, depth=0):
    if not isinstance(schema, dict):
        return {"type": _value_type(schema)}
    if depth >= 8:
        return {"truncated": True}
    result = {}
    type_name = schema.get("type")
    if isinstance(type_name, str) and type_name in {
            "object", "array", "string", "integer", "number", "boolean", "null"}:
        result["type"] = type_name
    properties = schema.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            key: _schema_shape(value, depth + 1) for key, value in sorted(properties.items())
            if isinstance(key, str) and len(key) <= 80
        }
    required = schema.get("required")
    if isinstance(required, list):
        result["required"] = [name for name in required[:128]
                              if isinstance(name, str) and len(name) <= 80]
    if isinstance(schema.get("additionalProperties"), bool):
        result["additional_properties"] = schema["additionalProperties"]
    if isinstance(schema.get("items"), dict):
        result["items"] = _schema_shape(schema["items"], depth + 1)
    for key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            result[key] = [_schema_shape(item, depth + 1) for item in branches[:32]]
    if isinstance(schema.get("enum"), list):
        result["enum"] = {"length": len(schema["enum"]),
                           "item_types": sorted({_value_type(item) for item in schema["enum"]})}
    if isinstance(schema.get("description"), str):
        result["description_characters"] = len(schema["description"])
    return result


def _text_metadata(value):
    if isinstance(value, str):
        return {"present": bool(value), "characters": len(value)}
    if value is None:
        return {"present": False, "type": "null"}
    return {"present": True, "type": _value_type(value)}


def _tool_calls_metadata(value):
    calls = value if isinstance(value, list) else []
    result = []
    for call in calls[:64]:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            result.append({"call_type": _value_type(call), "structure_valid": False})
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        result.append({
            "name": name if isinstance(name, str) and len(name) <= 100 else None,
            "arguments": _shape(arguments),
            "structure_valid": (isinstance(name, str) and bool(name)
                                and isinstance(arguments, dict)),
        })
    return result


def _request_summary(request):
    if not isinstance(request, dict):
        return {"json_object": False}
    messages = request.get("messages")
    message_metadata = []
    if isinstance(messages, list):
        for message in messages[:512]:
            if not isinstance(message, dict):
                message_metadata.append({"message_type": _value_type(message)})
                continue
            item = {"role": message.get("role") if isinstance(message.get("role"), str) else None,
                    "content": _text_metadata(message.get("content"))}
            if "tool_calls" in message:
                item["structured_tool_calls_present"] = isinstance(message["tool_calls"], list)
                item["tool_calls"] = _tool_calls_metadata(message["tool_calls"])
            item["reasoning_field_present"] = any(
                key in message for key in ("thinking", "reasoning", "thought")
            )
            message_metadata.append(item)
    tools = request.get("tools")
    tool_metadata = []
    if isinstance(tools, list):
        for tool in tools[:256]:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict):
                tool_metadata.append({
                    "name": fn.get("name") if isinstance(fn.get("name"), str) else None,
                    "description": _text_metadata(fn.get("description")),
                    "parameters": _schema_shape(fn.get("parameters")),
                })
            else:
                tool_metadata.append({"tool_type": _value_type(tool)})
    options = request.get("options")
    safe_options = {}
    if isinstance(options, dict):
        for key, value in options.items():
            if not isinstance(key, str) or len(key) > 80:
                continue
            safe_options[key] = (value if isinstance(value, (int, float, bool)) or value is None
                                 else {"type": _value_type(value),
                                       "characters": len(value) if isinstance(value, str) else None})
    return {
        "model": request.get("model") if isinstance(request.get("model"), str) else None,
        "stream": request.get("stream") if isinstance(request.get("stream"), bool) else None,
        "think": request.get("think") if isinstance(request.get("think"), bool) else None,
        "options": safe_options,
        "keep_alive": (request.get("keep_alive") if isinstance(request.get("keep_alive"), (bool, int, float))
                       else {"type": _value_type(request.get("keep_alive")),
                             "characters": len(request["keep_alive"])
                             if isinstance(request.get("keep_alive"), str) else None}),
        "requested_context_length": options.get("num_ctx") if isinstance(options, dict) else None,
        "messages_ordered": message_metadata,
        "tools": tool_metadata,
    }


def _response_summary(response, parse_error, response_bytes, response_state):
    summary = {"json_parse_success": response is not None and not parse_error,
               "response_state": response_state,
               "byte_length": len(response_bytes) if response_bytes is not None else None}
    if not isinstance(response, dict):
        summary.update({"json_object": False, "message_present": False,
                        "tool_calls_field_present": False, "tool_calls_present": False,
                        "tool_calls_field_valid": False, "invalid_tool_call_count": 0,
                        "assistant_text_present": False, "recognized_tool_markup": False})
        return summary
    summary["json_object"] = True
    allowed_envelope_fields = ("model", "created_at", "done", "done_reason", "total_duration",
                               "load_duration", "prompt_eval_count", "prompt_eval_duration",
                               "eval_count", "eval_duration")
    summary["envelope_fields"] = sorted(key for key in allowed_envelope_fields if key in response)
    message = response.get("message")
    if not isinstance(message, dict):
        summary.update({"message_present": False, "tool_calls_field_present": False,
                        "tool_calls_present": False, "tool_calls_field_valid": False,
                        "invalid_tool_call_count": 0, "assistant_text_present": False,
                        "recognized_tool_markup": False})
        return summary
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls = message.get("tool_calls")
    call_metadata = _tool_calls_metadata(calls)
    summary.update({
        "message_present": True,
        "message_role": message.get("role") if isinstance(message.get("role"), str) else None,
        "tool_calls_present": isinstance(calls, list) and bool(calls),
        "tool_calls_field_present": "tool_calls" in message,
        "tool_calls_field_type": _value_type(calls) if "tool_calls" in message else None,
        "tool_calls_field_valid": "tool_calls" not in message or isinstance(calls, list),
        "tool_calls": call_metadata,
        "invalid_tool_call_count": (sum(not call.get("structure_valid", False)
                                         for call in call_metadata)
                                    + int("tool_calls" in message and not isinstance(calls, list))),
        "assistant_text_present": bool(text),
        "assistant_text_characters": len(text),
        "recognized_tool_markup": bool(re.search(r"<tool_call>|\[TOOL_CALL\]|```(?:json)?\s*\{", text, re.I)),
        "reasoning_field_present": any(key in message for key in ("thinking", "reasoning", "thought")),
    })
    return summary


def _reject_reparse_chain(path):
    """Reject any existing symlink/reparse component without resolving it away."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AgentError("Diagnostic storage path contains a symlink or reparse point.")


def _ensure_private_permissions(path):
    if IS_WINDOWS:
        _set_and_verify_windows_acl(path)
        return
    os.chmod(path, 0o700)
    _validate_private_directory(path)


def _set_and_verify_windows_acl(path):
    # Use SIDs (not localized account names), protect this directory only, and
    # verify the resulting DACL through .NET rather than trusting icacls exit code.
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:LOCAL_CODING_AGENT_DIAGNOSTIC_ACL_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$acl = Get-Acl -LiteralPath $p
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { [void]$acl.RemoveAccessRuleSpecific($_) }
$rights = [System.Security.AccessControl.FileSystemRights]::FullControl
$inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$prop = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
foreach ($who in @($sid, $system)) {
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($who, $rights, $inherit, $prop, $allow)
  $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $p -AclObject $acl
$check = Get-Acl -LiteralPath $p
if (-not $check.AreAccessRulesProtected) { throw 'ACL inheritance remains enabled' }
$rules = $check.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
$allowed = @($sid.Value, 'S-1-5-18')
if ($rules.Count -ne 2) { throw 'Unexpected ACL rule count' }
foreach ($r in $rules) {
  if ($r.IsInherited -or $r.AccessControlType -ne $allow -or
      $r.FileSystemRights -ne $rights -or $r.InheritanceFlags -ne $inherit -or
      $r.PropagationFlags -ne $prop -or $r.IdentityReference.Value -notin $allowed) {
    throw 'ACL verification failed'
  }
}
if (($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique).Count -ne 2) {
  throw 'Required ACL principals are missing'
}
'''
    acl_env = os.environ.copy()
    acl_env[POWERSHELL_ACL_PATH_ENV] = str(path)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15, check=False, env=acl_env,
    )
    if result.returncode != 0:
        raise AgentError("Windows diagnostic ACL could not be established and verified.")


def _validate_private_directory(path):
    path = Path(path)
    _reject_reparse_chain(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise AgentError("Diagnostic run path is not a directory.")
    if not IS_WINDOWS and stat.S_IMODE(info.st_mode) & 0o077:
        raise AgentError("Diagnostic directory permissions are broader than owner-only.")
    if IS_WINDOWS:
        _verify_windows_acl(path)


def _verify_windows_acl(path):
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:LOCAL_CODING_AGENT_DIAGNOSTIC_ACL_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -LiteralPath $p
if (-not $acl.AreAccessRulesProtected) { throw 'ACL inheritance remains enabled' }
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
$allowed = @($sid, 'S-1-5-18')
$rights = [System.Security.AccessControl.FileSystemRights]::FullControl
$inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$prop = [System.Security.AccessControl.PropagationFlags]::None
 $allow = [System.Security.AccessControl.AccessControlType]::Allow
if ($rules.Count -ne 2) { throw 'Unexpected ACL rule count' }
foreach ($r in $rules) {
  if ($r.IsInherited -or $r.AccessControlType -ne $allow -or
      $r.FileSystemRights -ne $rights -or $r.InheritanceFlags -ne $inherit -or
      $r.PropagationFlags -ne $prop -or $r.IdentityReference.Value -notin $allowed) {
    throw 'ACL verification failed'
  }
}
if (($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique).Count -ne 2) {
  throw 'Required ACL principals are missing'
}
'''
    acl_env = os.environ.copy()
    acl_env[POWERSHELL_ACL_PATH_ENV] = str(path)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15, check=False, env=acl_env,
    )
    if result.returncode != 0:
        raise AgentError("Windows diagnostic ACL verification failed.")


def _atomic_create(directory, name, data):
    directory = Path(directory)
    _validate_private_directory(directory)
    target = directory / name
    if target.parent != directory or target.exists() or target.is_symlink():
        raise AgentError("Diagnostic artifact destination is unsafe or already exists.")
    fd, temp_name = tempfile.mkstemp(prefix=".capture-", dir=directory)
    temp = Path(temp_name)
    descriptor_open = True
    try:
        _validate_private_directory(directory)
        with os.fdopen(fd, "wb") as stream:
            descriptor_open = False
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_reparse_chain(temp)
        # Hard-link creation is atomic and fails if target already exists.
        os.link(temp, target)
        temp.unlink()
        return target
    except FileExistsError as exc:
        raise AgentError("Diagnostic artifact overwrite was prevented.") from exc
    except OSError as exc:
        raise AgentError("Atomic diagnostic artifact write failed closed.") from exc
    finally:
        if descriptor_open:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if temp.exists() and not temp.is_symlink():
                temp.unlink()
        except OSError:
            pass


def load_replay(path):
    path = Path(path)
    _reject_reparse_chain(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE_BYTES:
            raise AgentError("Diagnostic manifest is not a bounded regular file.")
        record = json.loads(path.read_bytes())
    except AgentError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentError("Diagnostic manifest is unavailable or invalid.") from exc
    required = {
        "format", "capture_kind", "request_state", "response_state", "request_sha256",
        "response_sha256", "request_body_bytes", "response_body_bytes", "request_structure",
        "response_structure", "raw_payloads_stored",
    }
    if (not isinstance(record, dict) or not required.issubset(record)
            or record.get("format") != "ollama-protocol-diagnostic-v1"
            or record.get("raw_payloads_stored") is not False
            or record.get("capture_kind") not in {"transport_capture", "synthetic_reproduction"}
            or record.get("request_state") not in {"submitted", "uncertain"}
            or record.get("response_state") not in {"complete", "partial", "oversized", "unavailable"}
            or not isinstance(record.get("request_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["request_sha256"])
            or type(record.get("request_body_bytes")) is not int
            or not 1 <= record["request_body_bytes"] <= 1024 * 1024
            or not isinstance(record.get("request_structure"), dict)
            or not isinstance(record.get("response_structure"), dict)):
        raise AgentError("Diagnostic manifest is incomplete or invalid for replay.")
    response_state = record["response_state"]
    response_hash = record.get("response_sha256")
    response_length = record.get("response_body_bytes")
    if response_state == "unavailable":
        valid_response_metadata = response_hash is None and response_length is None
    elif response_length is None:
        valid_response_metadata = response_state == "partial" and response_hash is None
    else:
        valid_response_metadata = (
            type(response_length) is int and 0 <= response_length <= MAX_CAPTURE_BYTES
            and isinstance(response_hash, str) and re.fullmatch(r"[0-9a-f]{64}", response_hash)
        )
    if not valid_response_metadata:
        raise AgentError("Diagnostic manifest response integrity metadata is invalid.")
    # A hash is not sufficient to reconstruct bytes. The stored inspection
    # metadata deliberately omits request content, so exact replay is impossible.
    raise AgentError("Exact replay is refused: original request bytes were omitted and cannot be hash-verified.")
