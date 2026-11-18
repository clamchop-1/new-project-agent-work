"""Optional embeddings using only an already-installed Windows Ollama model."""
from __future__ import annotations

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
