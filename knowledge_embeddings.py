"""Authorized local embedding adapters; neither adapter can access cloud APIs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import ctypes
from typing import Sequence

from agent_core import AgentError, no_secrets
from local_agent import Ollama

MAX_BATCH = 8
MAX_TEXT_BYTES = 3500


class OllamaEmbeddingProvider:
    """No redirects, cloud fallback, tunnel, model pull, or tool access."""
    def __init__(self, model_id: str):
        if (not isinstance(model_id, str) or not model_id or len(model_id) > 200
                or 'cloud' in model_id.casefold()):
            raise AgentError('Embedding model must name an installed local Ollama model.')
        self.model_id = model_id
        self.client = Ollama()
        identity = self.client.model(model_id)
        self._descriptor = {'provider': 'ollama_loopback', 'model_id': model_id,
            'digest': identity.get('digest', ''), 'modified_at': identity.get('modified_at', '')}
        if not self._descriptor['digest']:
            raise AgentError('Installed embedding model has no verifiable Ollama digest.')

    def descriptor(self) -> dict:
        current = self.client.model(self.model_id)
        observed = {'provider': 'ollama_loopback', 'model_id': self.model_id,
            'digest': current.get('digest', ''), 'modified_at': current.get('modified_at', '')}
        if observed != self._descriptor:
            raise AgentError('Installed embedding model identity changed; rebuild the knowledge index.')
        return dict(self._descriptor)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= MAX_BATCH:
            raise AgentError('Embedding batch must contain between one and eight inputs.')
        total = 0
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise AgentError('Embedding input must be nonempty text.')
            size = len(text.encode('utf-8'))
            total += size
            if size > MAX_TEXT_BYTES:
                raise AgentError('Embedding input exceeds its per-text byte limit.')
            no_secrets(text)
        if total > MAX_BATCH * MAX_TEXT_BYTES:
            raise AgentError('Embedding batch exceeds its aggregate byte limit.')
        self.descriptor()
        result = self.client.request('/api/embed', {'model': self.model_id,
            'input': list(texts), 'truncate': False}, timeout=60)
        vectors = result.get('embeddings')
        if (not isinstance(vectors, list) or len(vectors) != len(texts)
                or any(not isinstance(vector, list) or not vector for vector in vectors)):
            raise AgentError('Ollama embedding response is malformed or incomplete.')
        return vectors

    def close(self) -> None:
        self.client.close()


class LocalSentenceTransformerProvider:
    """Run the cached MiniLM model in the existing isolated OpenWebUI runtime.

    The application process does not import model code. A small worker is
    launched with an offline-only environment, a fixed local snapshot, CPU
    inference, and a bounded JSON protocol.
    """
    MODEL_ID = 'sentence-transformers/all-MiniLM-L6-v2'
    SNAPSHOT = '1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
    DIMENSIONS = 384
    MAX_BATCH = 256
    MAX_INPUT_BYTES = 3500
    TIMEOUT_SECONDS = 90

    def __init__(self, python_executable: Path | None = None,
                 snapshot_path: Path | None = None):
        home = Path.home()
        self.python_executable = (python_executable or
            home / 'OpenWebUI' / '.venv' / 'Scripts' / 'python.exe').resolve(strict=True)
        self.snapshot_path = (snapshot_path or home / '.cache' / 'huggingface' / 'hub' /
            ('models--' + self.MODEL_ID.replace('/', '--')) / 'snapshots' / self.SNAPSHOT).resolve(strict=True)
        self.worker_path = Path(__file__).with_name('knowledge_embedding_worker.py').resolve(strict=True)
        hub_root = (home / '.cache' / 'huggingface' / 'hub').resolve(strict=True)
        model_cache = (hub_root / ('models--' + self.MODEL_ID.replace('/', '--'))).resolve(strict=True)
        if (not self.python_executable.is_file() or self.python_executable.is_symlink()
                or not self.snapshot_path.is_dir() or not self.snapshot_path.is_relative_to(model_cache)
                or not self.worker_path.is_file() or self.worker_path.is_symlink()):
            raise AgentError('Cached local embedding runtime paths failed their allowlist checks.')
        config = json.loads((self.snapshot_path / 'config.json').read_text(encoding='utf-8'))
        modules = json.loads((self.snapshot_path / 'modules.json').read_text(encoding='utf-8'))
        if (config.get('model_type') != 'bert' or config.get('hidden_size') != self.DIMENSIONS
                or not any(row.get('type', '').endswith('.Normalize') for row in modules)):
            raise AgentError('Cached MiniLM model metadata does not match the expected sentence-embedding architecture.')
        self._model_file = (self.snapshot_path / 'model.safetensors').resolve(strict=True)
        if (not self._model_file.is_relative_to(hub_root)
                or self._model_file.parent.parent != hub_root / 'blobs'
                or not self._model_file.is_file()):
            raise AgentError('Cached embedding weights are outside the approved Hugging Face model cache.')
        self._digest = self._sha256(self._model_file)
        self._lock = threading.Lock()
        self._authorized = True
        self._process = None
        self._runtime_versions = {}
        self._tested = False
        self._peak_working_set_bytes = 0

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()

    def descriptor(self) -> dict:
        return {'provider_id': 'sentence_transformers_local_cpu',
            'runtime': 'sentence-transformers', 'model_id': self.MODEL_ID,
            'model_version': self.SNAPSHOT, 'model_digest': self._digest,
            'dimensions': self.DIMENSIONS, 'normalized': True,
            'host_id': 'windows', 'available': self.python_executable.is_file()
                and self.snapshot_path.is_dir(), 'authorized': self._authorized,
            'compatible': bool(self._runtime_versions), 'tested': self._tested,
            'loaded': self._process is not None and self._process.poll() is None,
            'runtime_versions': dict(self._runtime_versions),
            'peak_working_set_bytes': self._peak_working_set_bytes}

    def embed(self, texts: Sequence[str], *, cancel_event=None) -> list[list[float]]:
        if not self._authorized:
            raise AgentError('Local embedding model is not authorized.')
        if (not isinstance(texts, (list, tuple)) or not texts
                or len(texts) > self.MAX_BATCH):
            raise AgentError(f'Embedding batches must contain between one and {self.MAX_BATCH} texts.')
        if any(not isinstance(text, str) or not text.strip()
               or len(text.encode('utf-8')) > self.MAX_INPUT_BYTES for text in texts):
            raise AgentError('Embedding input must be nonempty and within the per-text byte limit.')
        no_secrets('\n'.join(texts))
        request = json.dumps({'snapshot': str(self.snapshot_path),
            'expected_model': self.MODEL_ID, 'expected_dimensions': self.DIMENSIONS,
            'texts': list(texts)}, ensure_ascii=False, separators=(',', ':'))
        runtime_root = self.python_executable.parent.parent
        system_root = os.environ.get('SYSTEMROOT', r'C:\Windows')
        path_entries = [str(self.python_executable.parent), str(Path(system_root) / 'System32')]
        torch_dlls = runtime_root / 'Lib' / 'site-packages' / 'torch' / 'lib'
        if torch_dlls.is_dir() and torch_dlls.resolve().is_relative_to(runtime_root):
            path_entries.insert(1, str(torch_dlls.resolve()))
        environment = {'PATH': os.pathsep.join(path_entries), 'SYSTEMROOT': system_root,
                       'WINDIR': system_root}
        for key in ('TEMP', 'TMP'):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        environment.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
            'HF_HUB_DISABLE_TELEMETRY': '1', 'TOKENIZERS_PARALLELISM': 'false',
            'PYTHONNOUSERSITE': '1'})
        with self._lock:
            process = self._ensure_worker_locked(environment, cancel_event)
            result = self._exchange_locked(process, request, cancel_event)
        try:
            vectors = result['vectors']
        except (KeyError, TypeError) as exc:
            raise AgentError('Local embedding worker response was malformed.') from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise AgentError('Local embedding worker returned the wrong number of vectors.')
        return vectors

    def _ensure_worker_locked(self, environment, cancel_event):
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._process = None
        process = subprocess.Popen([str(self.python_executable), '-I', '-B',
            str(self.worker_path), str(self.snapshot_path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding='utf-8', cwd=str(self.snapshot_path), env=environment,
            bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            ready = self._readline(process, self.TIMEOUT_SECONDS, cancel_event)
            response = json.loads(ready)
            if (not isinstance(response, dict) or response.get('ready') is not True
                    or response.get('dimensions') != self.DIMENSIONS
                    or not isinstance(response.get('runtime_versions'), dict)):
                raise AgentError('Local embedding runtime did not pass its model readiness check.')
            self._runtime_versions = response['runtime_versions']
            self._process = process
            return process
        except BaseException:
            process.kill()
            process.wait(timeout=3)
            raise

    def _readline(self, process, timeout, cancel_event):
        completed = threading.Event()
        output = {}
        failure = []

        def read_line():
            try:
                output['line'] = process.stdout.readline(4_000_001)
            except BaseException as exc:
                failure.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=read_line, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while not completed.wait(0.05):
            self._sample_peak_working_set(process.pid)
            if cancel_event is not None and cancel_event.is_set():
                raise AgentError('Embedding request was cancelled.')
            if process.poll() is not None:
                raise AgentError('Local embedding worker exited unexpectedly.')
            if time.monotonic() >= deadline:
                raise AgentError('Local embedding inference exceeded its 90-second limit.')
        if failure:
            raise AgentError('Local embedding worker failed safely.') from failure[0]
        line = output.get('line', '')
        if not line or len(line) > 4_000_000:
            raise AgentError('Local embedding worker returned an incomplete response.')
        return line

    def _sample_peak_working_set(self, pid: int) -> None:
        if os.name != 'nt':
            return

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                ('PrivateUsage', ctypes.c_size_t)]

        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel.OpenProcess(0x0410, False, pid)
            if not handle:
                return
            try:
                counters = PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(counters)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    self._peak_working_set_bytes = max(self._peak_working_set_bytes,
                        int(counters.PeakWorkingSetSize))
            finally:
                kernel.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return

    def _exchange_locked(self, process, request, cancel_event):
        try:
            process.stdin.write(request + '\n')
            process.stdin.flush()
            response = json.loads(self._readline(process, self.TIMEOUT_SECONDS, cancel_event))
        except BaseException:
            process.kill()
            process.wait(timeout=3)
            self._process = None
            raise
        if not isinstance(response, dict) or response.get('ok') is not True:
            process.kill()
            process.wait(timeout=3)
            self._process = None
            raise AgentError('Local embedding worker rejected the request or failed inference.')
        self._tested = True
        return response

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=3)
