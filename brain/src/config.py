"""Brain service configuration constants."""

from dataclasses import dataclass


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0


EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 3072

NOVELTY_THRESHOLD = 0.85
WEIGHT_CENTER_SQL = "depth_weight_alpha / (depth_weight_alpha + depth_weight_beta)"

MEMORY_TYPE_PREFIXES = {
    "episodic": "Personal experience memory: ",
    "semantic": "Factual knowledge: ",
    "procedural": "How-to instruction: ",
    "preference": "User preference: ",
    "reflection": "Self-reflection insight: ",
    "correction": "Past error correction: ",
    "narrative": "Identity narrative: ",
    "tension": "Internal contradiction: ",
    "research_finding": "Research finding: ",
}

# Research sessions (D-016/DJ-008)
RESEARCH_HOURLY_LIMIT = 1
RESEARCH_DAILY_LIMIT = 24
RESEARCH_MIN_WEIGHT = 0.3
RESEARCH_DISPLACE_BETA = 5.0
RESEARCH_CONFIRMATION_HOURS = 24

# Notification system (D-019)
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
NOTIFICATION_DELIVERY_INTERVAL = 30
NOTIFICATION_EXPIRY_HOURS = 24
NOTIFICATION_MAX_PASSIVE_PER_CONTEXT = 3
