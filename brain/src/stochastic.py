"""Beta-distributed stochastic weight for memories."""

import random


class StochasticWeight:
    __slots__ = ("alpha", "beta")

    def __init__(self, alpha: float = 1.0, beta: float = 4.0):
        self.alpha = alpha
        self.beta = beta

    def observe(self) -> float:
        """Stochastic sample from Beta(alpha, beta)."""
        return random.betavariate(self.alpha, self.beta)

    @property
    def center(self) -> float:
        """Deterministic expected value: alpha / (alpha + beta)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def depth_weight(self) -> float:
        """Alias for center."""
        return self.center

    @property
    def variance(self) -> float:
        ab = self.alpha + self.beta
        return (self.alpha * self.beta) / (ab * ab * (ab + 1))

    @property
    def total_evidence(self) -> float:
        return self.alpha + self.beta

    @property
    def is_contested(self) -> bool:
        return self.alpha > 5 and self.beta > 5

    @property
    def is_uninformed(self) -> bool:
        return self.alpha < 2 and self.beta < 2

    def reinforce(self, amount: float = 1.0) -> None:
        self.alpha += amount

    def contradict(self, amount: float = 0.5) -> None:
        self.beta += amount

    @classmethod
    def from_db(cls, alpha: float, beta: float) -> "StochasticWeight":
        w = cls.__new__(cls)
        w.alpha = alpha
        w.beta = beta
        return w

    def __repr__(self) -> str:
        return f"StochasticWeight(alpha={self.alpha:.2f}, beta={self.beta:.2f}, center={self.center:.3f})"
