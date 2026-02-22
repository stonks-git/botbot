"""LLM client wrapper for consolidation prompts.

Uses Google Gemini 3 Flash Preview for all background brain tasks:
contradiction detection, insight generation, narrative synthesis, etc.
Shares the same GOOGLE_API_KEY used for embeddings.

NOTE: gemini-3-flash-preview is a thinking model. Its internal chain-of-thought
consumes from max_output_tokens budget. We do NOT set max_output_tokens so the
model can think freely and produce complete output. (BUG-001: setting
max_tokens=200 caused all consolidation output to be sentence fragments.)
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
    temperature: float = 0.2,
    model: str = DEFAULT_MODEL,
) -> str:
    """Single LLM call. Returns the text response.

    No max_output_tokens set — the thinking model needs unrestricted budget
    to produce complete output after its internal chain-of-thought.
    """
    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            temperature=temperature,
        ),
    )
    return response.text.strip()


async def retry_llm_call(
    prompt: str,
    temperature: float = 0.2,
    model: str = DEFAULT_MODEL,
    retry_config: RetryConfig | None = None,
    # Legacy parameter — ignored. Kept for call-site compatibility.
    max_tokens: int | None = None,
) -> str:
    """LLM call with exponential backoff retry."""
    cfg = retry_config or RetryConfig()
    last_err = None
    for attempt in range(cfg.max_retries):
        try:
            return await llm_call(prompt, temperature, model)
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


async def llm_call_with_search(
    prompt: str,
    temperature: float = 1.0,
    model: str = DEFAULT_MODEL,
) -> tuple[str, list[dict], int]:
    """LLM call with Google Search grounding (D-016).

    Returns (response_text, grounding_sources, grounding_chunk_count).
    Temperature 1.0 recommended for Gemini 3 with tools.
    """
    client = _get_client()
    search_tool = genai.types.Tool(google_search=genai.types.GoogleSearch())
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            temperature=temperature,
            tools=[search_tool],
        ),
    )
    sources: list[dict] = []
    grounding_chunk_count = 0
    for candidate in (response.candidates or []):
        gm = getattr(candidate, "grounding_metadata", None)
        if gm:
            chunks = getattr(gm, "grounding_chunks", None) or []
            grounding_chunk_count = len(chunks)
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web:
                    sources.append({
                        "uri": getattr(web, "uri", ""),
                        "title": getattr(web, "title", ""),
                    })
    return response.text.strip(), sources, grounding_chunk_count


async def retry_llm_call_with_search(
    prompt: str,
    temperature: float = 1.0,
    model: str = DEFAULT_MODEL,
    retry_config: RetryConfig | None = None,
) -> tuple[str, list[dict], int]:
    """LLM call with Google Search grounding + retry."""
    cfg = retry_config or RetryConfig()
    last_err = None
    for attempt in range(cfg.max_retries):
        try:
            return await llm_call_with_search(prompt, temperature, model)
        except Exception as e:
            last_err = e
            delay = min(cfg.base_delay * (2**attempt), cfg.max_delay)
            logger.warning(
                "Search LLM attempt %d failed: %s (retry in %.1fs)",
                attempt + 1, e, delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Search LLM call failed after {cfg.max_retries} attempts: {last_err}")
