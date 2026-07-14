"""Simulated execution adapter — computing a hedge is not executing one.

Lifecycle (every transition is a logged + anchored decision):

    Signal -> Hedge PROPOSED -> SUBMITTED -> (latency, market moves)
           -> PARTIALLY_FILLED | FILLED | REJECTED (per leg, one retry)
           -> CANCELLED (unfilled remainder)
           -> RECONCILED (realized floor recomputed from ACTUAL fills)

Causality: fills are priced at the odds prevailing AFTER submit_ts + latency,
taken from the live/replay stream itself — the simulator never looks ahead.

Frictions modelled (all seeded => deterministic, echoed into every decision):
    latency_ms          time between submit and earliest possible fill
    max_slippage_bps    per-leg fill odds worsened by U(0, max) bps
    fill_probability    per-leg chance of executing at all (else reject)
    max_leg_liquidity   stake cap per leg => partial fills above it
    quote_halt_prob     chance a leg's venue is halted this attempt (retry)

Single-leg risk emerges naturally: if one leg fills and the other rejects,
reconciliation reports the true (asymmetric, worse) floor — and the desk's
retry logic is what closes it, not an assumption.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class ExecConfig:
    latency_ms: int = 800
    max_slippage_bps: int = 75
    fill_probability: float = 0.92
    max_leg_liquidity: float = 5000.0
    quote_halt_prob: float = 0.03
    max_retries: int = 1
    seed: int = 7

    def as_dict(self):
        return self.__dict__.copy()


@dataclass
class Leg:
    outcome: int
    requested: float
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


class SimulatedExecutor:
    def __init__(self, cfg: ExecConfig | None = None):
        self.cfg = cfg or ExecConfig()
        self._rng = random.Random(self.cfg.seed)

    def submit(self, fixture: int, pos, plan, ts_ms: int) -> Order:
        legs = [Leg(j, stake) for j, stake in enumerate(plan.hedge_stakes) if stake > 1e-9]
        return Order(fixture, pos, plan.floor, plan.intent, ts_ms, legs)

    def poll(self, order: Order, odds_now, ts_ms: int) -> list[dict]:
        """Attempt fills once latency has elapsed. Returns event dicts."""
        if ts_ms < order.submit_ts + self.cfg.latency_ms:
            return []
        events = []
        for leg in order.legs:
            if leg.state not in ("SUBMITTED", "RETRY"):
                continue
            leg.attempts += 1
            if self._rng.random() < self.cfg.quote_halt_prob:
                leg.state = "RETRY" if leg.attempts <= self.cfg.max_retries else "CANCELLED"
                events.append({"leg": leg.outcome, "event": "QUOTE_HALT",
                               "state": leg.state, "attempt": leg.attempts})
                continue
            if self._rng.random() > self.cfg.fill_probability:
                leg.state = "RETRY" if leg.attempts <= self.cfg.max_retries else "REJECTED"
                events.append({"leg": leg.outcome, "event": "NO_FILL",
                               "state": leg.state, "attempt": leg.attempts})
                continue
            slip = self._rng.uniform(0, self.cfg.max_slippage_bps) / 10_000
            o = odds_now[leg.outcome]
            leg.fill_odds = 1 + (o - 1) * (1 - slip)
            fillable = min(leg.requested - leg.filled, self.cfg.max_leg_liquidity)
            leg.filled += fillable
            leg.state = "FILLED" if leg.filled >= leg.requested - 1e-9 else "PARTIALLY_FILLED"
            events.append({"leg": leg.outcome, "event": leg.state,
                           "filled": round(leg.filled, 2), "requested": round(leg.requested, 2),
                           "fill_odds": round(leg.fill_odds, 3), "slippage_bps": round(slip * 10_000, 1)})
        if all(l.state in ("FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED") for l in order.legs):
            order.state = "SETTLED"
        return events

    @staticmethod
    def reconcile(order: Order) -> dict:
        """Recompute the TRUE floor from actual fills (single-leg risk included)."""
        pos = order.pos
        total_staked = pos.stake + sum(l.filled for l in order.legs)
        nets = {}
        for w in range(3):
            if w == pos.outcome:
                gross = pos.stake * pos.entry_odds
            else:
                leg = next((l for l in order.legs if l.outcome == w), None)
                gross = leg.filled * leg.fill_odds if leg and leg.filled > 0 else 0.0
            nets[w] = gross - total_staked
        losing = [nets[w] for w in range(3) if w != pos.outcome]
        return {"realized_floor": round(min(losing), 2),
                "realized_win_net": round(nets[pos.outcome], 2),
                "proposed_floor": round(order.proposed_floor, 2),
                "friction_cost": round(order.proposed_floor - min(losing), 2),
                "legs": [{"outcome": l.outcome, "state": l.state,
                          "filled": round(l.filled, 2), "requested": round(l.requested, 2),
                          "fill_odds": round(l.fill_odds, 3)} for l in order.legs]}
