"""Reconciliation: recompute the TRUE floor from actual fills.

Partial fills and single-leg risk are measured here, not assumed away.
"""
from __future__ import annotations

from .models import Order


def reconcile(order: Order) -> dict:
    """Recompute the realized floor/win-net from what actually filled."""
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
