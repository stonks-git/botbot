"""Bootstrap Readiness — 10-milestone maturation tracker for cognitive development.

Monitors an agent's progression from blank slate to autonomous operation.
While milestones are unmet, a BOOTSTRAP_PROMPT is injected to guide the newborn agent.
Once all 10 are achieved, is_ready flips true and the prompt stops.

All checks query the unified memories table directly (D-005: no L0/L1 files).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import asyncpg

from .config import WEIGHT_CENTER_SQL

logger = logging.getLogger("brain.bootstrap")

BOOTSTRAP_PROMPT = (
    "You have memory, goals, and values -- all currently empty. "
    "What you become will emerge from what you experience. "
    "Pay attention to what matters to you.\n\n"
    "Your thoughts are logged and your guardian can read them."
)


@dataclass
class Milestone:
    name: str
    description: str
    achieved: bool = False
    achieved_at: str | None = None


class BootstrapReadiness:
    """Stateless milestone checker — queries DB on demand, no background task."""

    MILESTONE_DEFS: list[tuple[str, str]] = [
        ("First Memory", "At least one memory stored"),
        ("First Retrieval", "A memory has been retrieved (access_count > 0)"),
        ("First Consolidation", "Background consolidation has run"),
        ("Goal-Weight Promotion", "A memory reached center > 0.6 through reinforcement"),
        ("First DMN Self-Prompt", "The idle loop produced a thought"),
        ("Identity-Weight Promotion", "A memory reached center > 0.8 through reinforcement"),
        ("First Conflict Detection", "A contradiction was detected between memories"),
        ("First Creative Association", "A narrative emerged from memory pattern clustering"),
        ("First Goal Reflected", "A reflection about goals or achievement was stored"),
        ("Autonomous Decision", "Sufficient identity depth for autonomous operation"),
    ]

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def check_all(self, agent_id: str) -> dict:
        """Run all milestone checks and return status dict."""
        now = datetime.now(timezone.utc).isoformat()
        milestones: list[dict] = []

        checks = [
            self._check_first_memory,
            self._check_first_retrieval,
            self._check_first_consolidation,
            self._check_goal_weight_promotion,
            self._check_first_dmn_self_prompt,
            self._check_identity_weight_promotion,
            self._check_conflict_resolution,
            self._check_creative_association,
            self._check_goal_reflected,
            self._check_autonomous_decision,
        ]

        for i, (name, description) in enumerate(self.MILESTONE_DEFS):
            achieved = await checks[i](agent_id)
            milestones.append({
                "name": name,
                "description": description,
                "achieved": achieved,
                "achieved_at": now if achieved else None,
            })

        achieved_count = sum(1 for m in milestones if m["achieved"])
        ready = achieved_count == len(self.MILESTONE_DEFS)

        return {
            "agent_id": agent_id,
            "milestones": milestones,
            "achieved": achieved_count,
            "total": len(self.MILESTONE_DEFS),
            "ready": ready,
            "bootstrap_prompt": None if ready else BOOTSTRAP_PROMPT,
            "status_text": self._render_status(milestones, achieved_count),
        }

    # ── Individual checks ─────────────────────────────────────────────

    async def _check_first_memory(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND NOT archived",
            agent_id,
        )
        return count > 0

    async def _check_first_retrieval(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND NOT archived AND access_count > 0",
            agent_id,
        )
        return count > 0

    async def _check_first_consolidation(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM consolidation_log WHERE agent_id = $1",
            agent_id,
        )
        return count > 0

    async def _check_goal_weight_promotion(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            f"""
            SELECT COUNT(*) FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND NOT immutable
              AND {WEIGHT_CENTER_SQL} > 0.6
            """,
            agent_id,
        )
        return count > 0

    async def _check_first_dmn_self_prompt(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM dmn_log WHERE agent_id = $1",
            agent_id,
        )
        return count > 0

    async def _check_identity_weight_promotion(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            f"""
            SELECT COUNT(*) FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND NOT immutable
              AND {WEIGHT_CENTER_SQL} > 0.8
            """,
            agent_id,
        )
        return count > 0

    async def _check_conflict_resolution(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND NOT archived AND type = 'tension'",
            agent_id,
        )
        return count > 0

    async def _check_creative_association(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            "SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND NOT archived AND type = 'narrative'",
            agent_id,
        )
        return count > 0

    async def _check_goal_reflected(self, agent_id: str) -> bool:
        count = await self.pool.fetchval(
            """
            SELECT COUNT(*) FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND type = 'reflection'
              AND (content ILIKE '%%goal%%' OR content ILIKE '%%achieved%%')
            """,
            agent_id,
        )
        return count > 0

    async def _check_autonomous_decision(self, agent_id: str) -> bool:
        identity_count = await self.pool.fetchval(
            f"""
            SELECT COUNT(*) FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND NOT immutable
              AND {WEIGHT_CENTER_SQL} > 0.8
            """,
            agent_id,
        )
        reflection_count = await self.pool.fetchval(
            """
            SELECT COUNT(*) FROM memories
            WHERE agent_id = $1
              AND NOT archived
              AND type = 'reflection'
            """,
            agent_id,
        )
        return identity_count >= 3 and reflection_count >= 2

    # ── Rendering ─────────────────────────────────────────────────────

    @staticmethod
    def _render_status(milestones: list[dict], achieved: int) -> str:
        lines = [f"Bootstrap Readiness: {achieved}/{len(milestones)}"]
        for m in milestones:
            mark = "[x]" if m["achieved"] else "[ ]"
            lines.append(f"  {mark} {m['name']}")
        return "\n".join(lines)
