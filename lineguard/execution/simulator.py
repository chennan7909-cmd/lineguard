"""Venue simulator: latency, slippage, partial fills, rejects, halts,
price protection — all seeded and deterministic."""
from __future__ import annotations

import random

from .models import ExecConfig, Leg, Order
from .reconciliation import reconcile


class SimulatedExecutor:
    def __init__(self, cfg: ExecConfig | None = None):
        self.cfg = cfg or ExecConfig()
        self._rng = random.Random(self.cfg.seed)

    def submit(self, fixture: int, pos, plan, ts_ms: int, odds_at_proposal=None) -> Order:
        legs = [Leg(j, stake, odds_at_proposal[j] if odds_at_proposal else 0.0)
                for j, stake in enumerate(plan.hedge_stakes) if stake > 1e-9]
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
            if leg.proposal_odds > 1:
                protected_min = 1 + (leg.proposal_odds - 1) * (1 - self.cfg.price_protection_bps / 10_000)
                if odds_now[leg.outcome] < protected_min:
                    leg.state = "CANCELLED"
                    events.append({"leg": leg.outcome, "event": "PRICE_PROTECTION",
                                   "state": "CANCELLED",
                                   "proposal_odds": round(leg.proposal_odds, 3),
                                   "odds_now": round(odds_now[leg.outcome], 3),
                                   "limit_bps": self.cfg.price_protection_bps})
                    continue
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
            if all(l.filled <= 1e-9 for l in order.legs):
                order.state = "SETTLED_UNFILLED"
        return events

    @staticmethod
    def reconcile(order: Order) -> dict:   # kept for API compatibility
        return reconcile(order)
