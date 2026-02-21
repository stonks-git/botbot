"""LLM client wrapper for consolidation prompts.

Uses Google Gemini 2.0 Flash for all background brain tasks:
contradiction detection, insight generation, narrative synthesis, etc.
Shares the same GOOGLE_API_KEY used for embeddings.
"""

import asyncio
import logging
import os

from google import genai

from .config import RetryConfig

logger = logging.getLogger("brain.llm")

DEFAULT_MODEL = "gemini-3-flash-preview"

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazy singleton for the Google GenAI client."""
    global _client
    if _client is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("LLM unavailable: no GOOGLE_API_KEY.")
        _client = genai.Client(api_key=api_key)
        logger.info("Gemini LLM client initialized (model: %s).", DEFAULT_MODEL)
    return _client


async def llm_call(
    prompt: str,
    max_tokens: int = 200,
    temperature: float = 0.2,
    model: str = DEFAULT_MODEL,
) -> str:
    """Single LLM call. Returns the text response."""
    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    return response.text.strip()


async def retry_llm_call(
    prompt: str,
    max_tokens: int = 200,
    temperature: float = 0.2,
    model: str = DEFAULT_MODEL,
    retry_config: RetryConfig | None = None,
) -> str:
    """LLM call with exponential backoff retry."""
    cfg = retry_config or RetryConfig()
    last_err = None
    for attempt in range(cfg.max_retries):
        try:
            return await llm_call(prompt, max_tokens, temperature, model)
        except Exception as e:
            last_err = e
            delay = min(cfg.base_delay * (2**attempt), cfg.max_delay)
            logger.warning(
                "LLM attempt %d failed: %s (retry in %.1fs)",
                attempt + 1,
                e,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"LLM call failed after {cfg.max_retries} attempts: {last_err}")
