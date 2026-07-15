"""Execution data models: config, legs, orders."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExecConfig:
    latency_ms: int = 800
    max_slippage_bps: int = 75
    fill_probability: float = 0.92
    max_leg_liquidity: float = 5000.0
    quote_halt_prob: float = 0.03
    max_retries: int = 1
    price_protection_bps: int = 150   # cancel a leg if its odds worsen beyond this vs proposal
    seed: int = 7

    def as_dict(self):
        return self.__dict__.copy()


@dataclass
class Leg:
    outcome: int
    requested: float
    proposal_odds: float = 0.0
    filled: float = 0.0
    fill_odds: float = 0.0
    state: str = "SUBMITTED"
    attempts: int = 0


@dataclass
class Order:
    fixture: int
    pos: object                 # risk.hedge.Position
    proposed_floor: float
    intent: str
    submit_ts: int
    legs: list = field(default_factory=list)
    state: str = "SUBMITTED"
    rework_count: int = 0


