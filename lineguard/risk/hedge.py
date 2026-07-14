"""Three-outcome hedge engine — the mathematical core of LineGuard.

Setting: a paper position of stake S on outcome i of a 3-outcome market
(1X2), entered at decimal odds a. The market has since moved to demargined
odds (o1,o2,o3) with implied probs p_j = 1/o_j, sum(p) = 1.

All intents reduce to ONE closed form. Choose a floor F = the guaranteed
net P/L in every losing state, then dutch the other outcomes so each pays
the same gross T if it wins:

    stake on outcome j (j != i):   h_j = T * p_j        (p_j = 1/o_j)
    equal gross                    T   = (S + F) / q_i
    where                          q_i = 1 - sum_{j != i} p_j

Proof: if j wins, net = h_j*o_j - S - sum(h) = T - S - T*(1-q_i) = T*q_i - S = F.
q_i is EXACT for any book: it does not assume the market is demargined.
On a perfectly demargined feed q_i = p_i; with overround q_i < p_i, so the
same formula automatically prices the house edge into the hedge.

Intents:
  break_even_on_loss:  F = 0
  custom_floor:        F given (must satisfy -S <= F <= F_lock)
  lock_profit:         F such that win-state net also = F -> F_lock = S*(a*q_i - 1)

The identity F_lock = S*(a*q_i - 1) is the whole risk story in one line:
locked P/L is positive iff a*q_i > 1. Position management becomes "watch
a*q_i(t)": a pure, monotone function of one observable.

Feed note: TxLINE StablePrice free tier is demargined (sum p = 1), so these
formulas apply directly. Execution against a real book pays a margin; the
engine accepts an optional `margin` that shrinks effective hedge odds for
conservative paper fills.
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


@dataclass(frozen=True)
class Position:
    outcome: int          # index of backed outcome (0,1,2)
    stake: float          # S > 0
    entry_odds: float     # a > 1


@dataclass(frozen=True)
class HedgePlan:
    intent: str
    floor: float                  # guaranteed net P/L in losing states
    win_net: float                # net P/L if backed outcome wins
    hedge_stakes: tuple           # stakes aligned to odds order, 0 at own index
    total_hedge_cost: float
    feasible: bool
    reason: str = ""


def _validate(pos: Position, odds) -> tuple:
    if pos.stake <= 0 or pos.entry_odds <= 1:
        raise ValueError("stake must be > 0 and entry odds > 1")
    if len(odds) != 3 or any(o <= 1 for o in odds):
        raise ValueError("need three decimal odds all > 1")
    return tuple(1.0 / o for o in odds)


def effective_odds(odds, margin: float = 0.0):
    """Conservative fill model: hedge legs execute at 1+(o-1)*(1-margin)."""
    if not 0 <= margin < 0.5:
        raise ValueError("margin in [0, 0.5)")
    return tuple(1 + (o - 1) * (1 - margin) for o in odds)


def hedge_to_floor(pos: Position, odds, floor: float, margin: float = 0.0,
                   intent: str = "custom_floor") -> HedgePlan:
    """Dutch the other two outcomes so every losing state nets exactly `floor`."""
    odds_eff = effective_odds(odds, margin)
    probs = _validate(pos, odds_eff)
    p_i = probs[pos.outcome]
    if floor < -pos.stake - EPS:
        return HedgePlan(intent, floor, 0.0, (0.0,) * 3, 0.0, False,
                         "floor below -stake: cannot lose more than the stake")
    q_i = 1.0 - sum(p for j, p in enumerate(probs) if j != pos.outcome)
    if q_i <= EPS:
        return HedgePlan(intent, floor, 0.0, (0.0,) * 3, 0.0, False,
                         "overround too large: losing outcomes already imply >= 100%")
    T = (pos.stake + floor) / q_i
    stakes = [0.0, 0.0, 0.0]
    for j in range(3):
        if j != pos.outcome:
            stakes[j] = T * probs[j]
    cost = sum(stakes)
    win_net = pos.stake * pos.entry_odds - pos.stake - cost
    if win_net < floor - EPS:
        return HedgePlan(intent, floor, win_net, tuple(stakes), cost, False,
                         "floor exceeds lockable maximum")
    return HedgePlan(intent, floor, win_net, tuple(stakes), cost, True)


def break_even_on_loss(pos: Position, odds, margin: float = 0.0) -> HedgePlan:
    return hedge_to_floor(pos, odds, 0.0, margin, intent="break_even_on_loss")


def lock_profit(pos: Position, odds, margin: float = 0.0) -> HedgePlan:
    """Equalize net P/L across all three outcomes: F = S*(a*p_i - 1)."""
    odds_eff = effective_odds(odds, margin)
    probs = _validate(pos, odds_eff)
    q_i = 1.0 - sum(p for j, p in enumerate(probs) if j != pos.outcome)
    F = pos.stake * (pos.entry_odds * q_i - 1.0)
    return hedge_to_floor(pos, odds, F, margin, intent="lock_profit")


def locked_pnl(pos: Position, odds) -> float:
    """The one-line identity F_lock = S*(a*q_i - 1), exact for any book."""
    probs = tuple(1.0 / o for o in odds)
    q_i = 1.0 - sum(p for j, p in enumerate(probs) if j != pos.outcome)
    return pos.stake * (pos.entry_odds * q_i - 1.0)
