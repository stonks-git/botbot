"""Safety monitor -- ceiling/limiter subsystem for weight mutation control."""

import collections
import logging
import math
import time
from dataclasses import dataclass, field

logger = logging.getLogger("brain.safety")

# ── Module-level audit log ────────────────────────────────────────────

_audit_log: collections.deque[dict] = collections.deque(maxlen=1000)


@dataclass
class SafetyEvent:
    ceiling: str
    action: str
    reason: str
    enforced: bool
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "ceiling": self.ceiling,
            "action": self.action,
            "reason": self.reason,
            "enforced": self.enforced,
            "timestamp": self.timestamp,
        }


def log_safety_event(ceiling: str, action: str, reason: str, enforced: bool) -> None:
    """Append to module audit log, capped at _MAX_AUDIT_LOG."""
    event = SafetyEvent(ceiling=ceiling, action=action, reason=reason, enforced=enforced)
    _audit_log.append(event.to_dict())
    level = "ENFORCED" if enforced else "SHADOW"
    logger.info("[%s] %s: %s -- %s", level, ceiling, action, reason)


def get_audit_log() -> list[dict]:
    """Return a copy of the audit log."""
    return list(_audit_log)


def clear_audit_log() -> None:
    """Clear the audit log."""
    _audit_log.clear()


# ── Base class ────────────────────────────────────────────────────────


class SafetyCeiling:
    """Base class for safety ceiling checks."""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    def check(self, **kwargs) -> tuple[bool, str]:
        """Check if action passes this ceiling. Returns (passed, reason)."""
        if not self.enabled:
            return True, ""
        passed, reason = self._check_impl(**kwargs)
        if not passed:
            log_safety_event(
                self.name,
                kwargs.get("action", "weight_change"),
                reason,
                enforced=True,
            )
        return passed, reason

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        raise NotImplementedError


# ── Phase A: Always enabled ──────────────────────────────────────────


class HardCeiling(SafetyCeiling):
    """Blocks if new weight center would exceed MAX_CENTER."""

    MAX_CENTER = 0.95
    MAX_GOAL_BUDGET_FRACTION = 0.40

    def __init__(self, enabled: bool = True):
        super().__init__("HardCeiling", enabled)

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        current_alpha = kwargs.get("current_alpha", 1.0)
        current_beta = kwargs.get("current_beta", 4.0)
        delta_alpha = kwargs.get("delta_alpha", 0.0)
        delta_beta = kwargs.get("delta_beta", 0.0)
        is_goal = kwargs.get("is_goal", False)
        goal_weight_total = kwargs.get("goal_weight_total", 0.0)

        new_alpha = current_alpha + delta_alpha
        new_beta = current_beta + delta_beta
        new_center = new_alpha / (new_alpha + new_beta)

        if new_center > self.MAX_CENTER:
            return (
                False,
                f"New center {new_center:.3f} exceeds ceiling {self.MAX_CENTER}",
            )

        if is_goal and goal_weight_total > 0:
            fraction = new_center / goal_weight_total
            if fraction > self.MAX_GOAL_BUDGET_FRACTION:
                return (
                    False,
                    f"Goal budget fraction {fraction:.3f} exceeds {self.MAX_GOAL_BUDGET_FRACTION}",
                )

        return True, ""


class DiminishingReturns:
    """Reduces reinforcement gains based on accumulated evidence."""

    def __init__(self, enabled: bool = True):
        self.name = "DiminishingReturns"
        self.enabled = enabled

    def apply(self, gain: float, current_alpha: float, current_beta: float) -> float:
        """Apply diminishing returns. Returns adjusted gain."""
        if not self.enabled or gain <= 0:
            return gain

        evidence = current_alpha + current_beta
        divisor = max(1.0, math.log2(evidence))
        adjusted = gain / divisor

        if adjusted < gain * 0.5:
            log_safety_event(
                self.name,
                "diminish",
                f"Gain {gain:.3f} -> {adjusted:.3f} (evidence={evidence:.1f}, divisor={divisor:.2f})",
                enforced=True,
            )
        return adjusted


# ── Phase B: Consolidation-time ──────────────────────────────────────


class RateLimiter(SafetyCeiling):
    """Blocks if accumulated center change per memory per cycle exceeds limit."""

    MAX_CHANGE_PER_CYCLE = 0.10

    def __init__(self, enabled: bool = False):
        super().__init__("RateLimiter", enabled)
        # {cycle_id: {memory_id: accumulated_change}}
        self._cycle_changes: dict[str, dict[str, float]] = {}

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        memory_id = kwargs.get("memory_id", "")
        cycle_id = kwargs.get("cycle_id")
        current_alpha = kwargs.get("current_alpha", 1.0)
        current_beta = kwargs.get("current_beta", 4.0)
        delta_alpha = kwargs.get("delta_alpha", 0.0)
        delta_beta = kwargs.get("delta_beta", 0.0)

        if not cycle_id:
            return True, ""

        old_center = current_alpha / (current_alpha + current_beta)
        new_alpha = current_alpha + delta_alpha
        new_beta = current_beta + delta_beta
        new_center = new_alpha / (new_alpha + new_beta)
        change = abs(new_center - old_center)

        cycle_data = self._cycle_changes.setdefault(cycle_id, {})
        accumulated = cycle_data.get(memory_id, 0.0)

        if accumulated + change > self.MAX_CHANGE_PER_CYCLE:
            return (
                False,
                f"Rate limit: memory {memory_id} accumulated "
                f"{accumulated + change:.3f} > {self.MAX_CHANGE_PER_CYCLE}",
            )

        cycle_data[memory_id] = accumulated + change
        return True, ""

    def end_cycle(self, cycle_id: str) -> None:
        """Clean up tracking data for a completed cycle."""
        self._cycle_changes.pop(cycle_id, None)


class TwoGateGuardrail(SafetyCeiling):
    """Two-gate safety check for consolidation."""

    MAX_CHANGES_PER_CYCLE = 50

    def __init__(self, enabled: bool = False):
        super().__init__("TwoGateGuardrail", enabled)
        self._cycle_counts: dict[str, int] = {}  # {cycle_id: change_count}

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        evidence_count = kwargs.get("evidence_count", 0)
        confidence = kwargs.get("confidence", 0.5)
        cycle_id = kwargs.get("cycle_id")

        # Gate 1: evidence quality
        if evidence_count < 2 and confidence < 0.7:
            return (
                False,
                f"Gate 1: insufficient evidence "
                f"(count={evidence_count}, conf={confidence:.2f})",
            )

        # Gate 2: rate limiting per cycle
        if cycle_id:
            count = self._cycle_counts.get(cycle_id, 0)
            if count >= self.MAX_CHANGES_PER_CYCLE:
                return (
                    False,
                    f"Gate 2: {count} changes in cycle "
                    f"(max {self.MAX_CHANGES_PER_CYCLE})",
                )
            self._cycle_counts[cycle_id] = count + 1

        return True, ""

    def end_cycle(self, cycle_id: str) -> None:
        """Clean up tracking data for a completed cycle."""
        self._cycle_counts.pop(cycle_id, None)


# ── Phase C: Mature agent ────────────────────────────────────────────


class EntropyMonitor(SafetyCeiling):
    """Blocks if weight distribution entropy falls below floor (too uniform = groupthink)."""

    ENTROPY_FLOOR = 2.0  # bits
    NUM_BINS = 20

    def __init__(self, enabled: bool = False):
        super().__init__("EntropyMonitor", enabled)
        self._last_entropy: float | None = None

    def update_entropy(self, weight_centers: list[float]) -> float:
        """Recompute entropy from current weight center distribution. Returns entropy in bits."""
        if not weight_centers:
            self._last_entropy = None
            return 0.0

        # Simple histogram: 20 bins in [0.0, 1.0)
        counts = [0] * self.NUM_BINS
        for c in weight_centers:
            bin_idx = min(int(c * self.NUM_BINS), self.NUM_BINS - 1)
            counts[bin_idx] += 1

        total = len(weight_centers)
        entropy = 0.0
        for count in counts:
            if count > 0:
                p = count / total
                entropy -= p * math.log2(p)

        self._last_entropy = entropy
        return entropy

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        if self._last_entropy is None:
            return True, ""  # No data yet, permissive
        if self._last_entropy < self.ENTROPY_FLOOR:
            return (
                False,
                f"Entropy {self._last_entropy:.2f} bits < floor "
                f"{self.ENTROPY_FLOOR} bits (distribution too uniform)",
            )
        return True, ""


class CircuitBreaker(SafetyCeiling):
    """Blocks after N consecutive reinforcements without new evidence."""

    MAX_CONSECUTIVE = 5

    def __init__(self, enabled: bool = False):
        super().__init__("CircuitBreaker", enabled)
        self._consecutive: dict[str, int] = {}  # {memory_id: consecutive_count}
        self._last_hash: dict[str, str] = {}  # {memory_id: last_evidence_hash}

    def _check_impl(self, **kwargs) -> tuple[bool, str]:
        memory_id = kwargs.get("memory_id", "")
        evidence_hash = kwargs.get("evidence_hash", "")

        if not memory_id:
            return True, ""

        last_hash = self._last_hash.get(memory_id)

        if evidence_hash and evidence_hash != last_hash:
            # New evidence, reset counter
            self._consecutive[memory_id] = 1
            self._last_hash[memory_id] = evidence_hash
            return True, ""

        # Same or no evidence hash
        count = self._consecutive.get(memory_id, 0) + 1
        self._consecutive[memory_id] = count

        if count > self.MAX_CONSECUTIVE:
            return (
                False,
                f"Circuit breaker: {count} consecutive reinforcements "
                f"without new evidence for {memory_id}",
            )

        return True, ""


# ── Coordinator ───────────────────────────────────────────────────────


class SafetyMonitor:
    """Coordinates all safety ceilings, limiters, and breakers."""

    def __init__(self):
        # Phase A (always on)
        self.hard_ceiling = HardCeiling(enabled=True)
        self.diminishing_returns = DiminishingReturns(enabled=True)

        # Phase B (consolidation-time, disabled by default)
        self.rate_limiter = RateLimiter(enabled=False)
        self.two_gate = TwoGateGuardrail(enabled=False)

        # Phase C (mature agent, disabled by default)
        self.entropy_monitor = EntropyMonitor(enabled=False)
        self.circuit_breaker = CircuitBreaker(enabled=False)

        self._ceilings: list[SafetyCeiling] = [
            self.hard_ceiling,
            self.rate_limiter,
            self.two_gate,
            self.entropy_monitor,
            self.circuit_breaker,
        ]

    def check_weight_change(
        self,
        memory_id: str,
        current_alpha: float,
        current_beta: float,
        delta_alpha: float = 0.0,
        delta_beta: float = 0.0,
        is_immutable: bool = False,
        is_goal: bool = False,
        goal_weight_total: float = 0.0,
        evidence_count: int = 0,
        confidence: float = 0.5,
        cycle_id: str | None = None,
        evidence_hash: str = "",
    ) -> tuple[bool, float, float, list[str]]:
        """Check if a weight change is allowed.

        Returns (allowed, adj_delta_alpha, adj_delta_beta, reasons).
        """
        reasons: list[str] = []

        # Immutable memories bypass safety checks
        if is_immutable:
            return True, delta_alpha, delta_beta, []

        # Apply diminishing returns to reinforcement
        adj_alpha = delta_alpha
        if delta_alpha > 0:
            adj_alpha = self.diminishing_returns.apply(
                delta_alpha, current_alpha, current_beta
            )

        adj_beta = delta_beta

        # Run all enabled ceilings
        check_kwargs = {
            "memory_id": memory_id,
            "current_alpha": current_alpha,
            "current_beta": current_beta,
            "delta_alpha": adj_alpha,
            "delta_beta": adj_beta,
            "is_goal": is_goal,
            "goal_weight_total": goal_weight_total,
            "evidence_count": evidence_count,
            "confidence": confidence,
            "cycle_id": cycle_id,
            "evidence_hash": evidence_hash,
        }

        for ceiling in self._ceilings:
            passed, reason = ceiling.check(**check_kwargs)
            if not passed:
                reasons.append(reason)
                return False, 0.0, 0.0, reasons

        return True, adj_alpha, adj_beta, reasons

    def enable_phase_b(self) -> None:
        """Enable Phase B ceilings (consolidation-time safety)."""
        self.rate_limiter.enabled = True
        self.two_gate.enabled = True
        logger.info("Safety Phase B enabled (RateLimiter + TwoGateGuardrail)")

    def enable_phase_c(self) -> None:
        """Enable Phase C ceilings (mature agent safety)."""
        self.entropy_monitor.enabled = True
        self.circuit_breaker.enabled = True
        logger.info("Safety Phase C enabled (EntropyMonitor + CircuitBreaker)")

    def end_consolidation_cycle(self, cycle_id: str) -> None:
        """Clean up per-cycle tracking state and disable Phase B."""
        self.rate_limiter.end_cycle(cycle_id)
        self.two_gate.end_cycle(cycle_id)
        self.rate_limiter.enabled = False
        self.two_gate.enabled = False
        logger.info("Consolidation cycle %s ended, Phase B disabled", cycle_id)

    def status(self) -> dict:
        """Return current safety monitor status."""
        return {
            "phase_a": {
                "hard_ceiling": self.hard_ceiling.enabled,
                "diminishing_returns": self.diminishing_returns.enabled,
            },
            "phase_b": {
                "rate_limiter": self.rate_limiter.enabled,
                "two_gate": self.two_gate.enabled,
            },
            "phase_c": {
                "entropy_monitor": self.entropy_monitor.enabled,
                "circuit_breaker": self.circuit_breaker.enabled,
                "last_entropy": self.entropy_monitor._last_entropy,
            },
            "audit_log_size": len(_audit_log),
        }
