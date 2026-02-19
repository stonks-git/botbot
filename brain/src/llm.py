"""LLM client wrapper for consolidation prompts.

Uses Anthropic Claude (Haiku 4.5) for all background consolidation tasks:
contradiction detection, insight generation, narrative synthesis, etc.
"""

import asyncio
import logging
import os

import anthropic

from .config import RetryConfig

logger = logging.getLogger("brain.llm")

DEFAULT_MODEL = "claude-haiku-4-5"

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Lazy singleton for the Anthropic async client."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("LLM unavailable: no ANTHROPIC_API_KEY.")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
        logger.info("Anthropic LLM client initialized.")
    return _client


async def llm_call(
    prompt: str,
    max_tokens: int = 200,
    temperature: float = 0.2,
    model: str = DEFAULT_MODEL,
) -> str:
    """Single LLM call. Returns the text response."""
    client = _get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


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
