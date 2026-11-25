"""Offline, CPU-only worker for the fixed local MiniLM snapshot."""
from __future__ import annotations

import json
import os
import sys

MAX_INPUTS = 256
MAX_TEXT_BYTES = 3500
MAX_REQUEST_BYTES = 2_000_000


def main() -> int:
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    try:
        snapshot = sys.argv[1]
        if len(sys.argv) != 2 or not os.path.isdir(snapshot):
            return 2
        from sentence_transformers import SentenceTransformer
        import numpy as np
        model = SentenceTransformer(snapshot, device='cpu', local_files_only=True,
                                    trust_remote_code=False)
        if model.get_embedding_dimension() != 384:
            return 3
        from importlib.metadata import version
        runtime_versions = {name: version(name) for name in
            ('sentence-transformers','torch','transformers','numpy','safetensors')}
        sys.stdout.write(json.dumps({'ready': True, 'dimensions': 384,
            'runtime_versions': runtime_versions}) + '\n')
        sys.stdout.flush()
        for raw in sys.stdin.buffer:
            if len(raw) > MAX_REQUEST_BYTES:
                sys.stdout.write(json.dumps({'ok': False}) + '\n'); sys.stdout.flush(); continue
            try:
                request = json.loads(raw)
                texts = request.get('texts')
                if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_INPUTS
                        or any(not isinstance(text, str) or not text.strip()
                               or len(text.encode('utf-8')) > MAX_TEXT_BYTES for text in texts)):
                    raise ValueError('invalid input')
                vectors = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                    show_progress_bar=False, batch_size=16)
                if (vectors.shape != (len(texts), 384) or not np.isfinite(vectors).all()
                        or not np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)):
                    raise ValueError('invalid output')
                result = {'ok': True, 'vectors': vectors.tolist()}
            except Exception:
                result = {'ok': False}
            sys.stdout.write(json.dumps(result, separators=(',', ':')) + '\n')
            sys.stdout.flush()
        return 0
    except Exception:
        # Avoid leaking imported text, paths, or internal environment details.
        return 4


if __name__ == '__main__':
    raise SystemExit(main())
