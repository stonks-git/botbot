"""Brain service configuration constants."""

from dataclasses import dataclass


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 3072

MEMORY_TYPE_PREFIXES = {
    "episodic": "Personal experience memory: ",
    "semantic": "Factual knowledge: ",
    "procedural": "How-to instruction: ",
    "preference": "User preference: ",
    "reflection": "Self-reflection insight: ",
    "correction": "Past error correction: ",
    "narrative": "Identity narrative: ",
    "tension": "Internal contradiction: ",
}
