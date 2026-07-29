"""
ollama_client.py — Legacy compatibility module forwarding to llm_client.py
"""

from controller.llm_client import (
    call_ollama,
    call_gemini,
    call_llm,
    build_prompt,
    parse_llm_response,
    SYSTEM_PROMPT,
    RESPONSE_FORMAT,
)

__all__ = [
    "call_ollama",
    "call_gemini",
    "call_llm",
    "build_prompt",
    "parse_llm_response",
    "SYSTEM_PROMPT",
    "RESPONSE_FORMAT",
]
