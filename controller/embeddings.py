"""
embeddings.py — Local embedding model for RAG semantic similarity search.

Uses fastembed (ONNX runtime, CPU-only) so the controller can turn crash
logs into vectors without any external API call or GPU — the model is
baked into the Docker image at build time (see Dockerfile), so no
network access is needed at runtime either.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Optional

from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_model: Optional[TextEmbedding] = None


def init_embedding_model() -> None:
    """Load the ONNX model into memory. Call once at controller startup."""
    global _model
    if _model is not None:
        return
    _model = TextEmbedding(model_name=MODEL_NAME)
    logger.info("✅ Embedding model loaded (%s, dim=%d)", MODEL_NAME, EMBEDDING_DIM)


async def embed_text(text: str) -> list[float]:
    """
    Turn text into a 384-dim vector.

    Runs the CPU-bound ONNX inference in a worker thread so it doesn't
    block the asyncio event loop that kopf and the k8s API calls share.
    """
    if _model is None:
        raise RuntimeError("Embedding model not initialised — call init_embedding_model() first")

    def _run() -> list[float]:
        return next(iter(_model.embed([text]))).tolist()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)
