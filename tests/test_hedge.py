import math
import pytest
from lineguard.risk.hedge import (Position, break_even_on_loss, hedge_to_floor,
                                  lock_profit, locked_pnl)

ODDS = (1.862, 3.793, 5.016)  # real demargined sample (ARG-SUI pre-match)


def net_by_state(pos, odds, stakes):
    """Brute-force P/L in each of the 3 terminal states."""
    total = pos.stake + sum(stakes)
    out = []
    for w in range(3):
        gross = pos.stake * pos.entry_odds if w == pos.outcome else stakes[w] * odds[w]
        out.append(gross - total)
    return out


@pytest.mark.parametrize("outcome", [0, 1, 2])
@pytest.mark.parametrize("entry", [1.5, 2.4, 6.0])
def test_lock_profit_equalizes_all_states(outcome, entry):
    pos = Position(outcome, 100.0, entry)
    plan = lock_profit(pos, ODDS)
    nets = net_by_state(pos, ODDS, plan.hedge_stakes)
    assert all(math.isclose(n, plan.floor, abs_tol=1e-6) for n in nets)
    q_i = 1 - sum(1 / o for j, o in enumerate(ODDS) if j != outcome)
    assert math.isclose(plan.floor, 100 * (entry * q_i - 1), abs_tol=1e-9)


def test_lock_profit_sign_follows_prob_move():
    pos = Position(0, 100.0, 2.4)          # entered at implied 41.7%
    assert lock_profit(pos, ODDS).floor > 0            # now 53.7% -> profit
    drifted = (1 / 0.35, 1 / 0.30, 1 / 0.35)   # demargined probs (35%, 30%, 35%)
    assert lock_profit(pos, drifted).floor < 0          # now 35% < entry 41.7% -> locked loss
    assert math.isclose(locked_pnl(pos, ODDS), lock_profit(pos, ODDS).floor, abs_tol=1e-9)


def test_break_even_floors_losses_at_zero():
    pos = Position(2, 50.0, 6.5)
    plan = break_even_on_loss(pos, ODDS)
    nets = net_by_state(pos, ODDS, plan.hedge_stakes)
    assert math.isclose(nets[0], 0, abs_tol=1e-6) and math.isclose(nets[1], 0, abs_tol=1e-6)
    assert nets[2] > 0 and math.isclose(nets[2], plan.win_net, abs_tol=1e-6)


def test_custom_floor_hits_target_exactly():
    pos = Position(1, 80.0, 4.0)
    plan = hedge_to_floor(pos, ODDS, floor=-20.0)
    nets = net_by_state(pos, ODDS, plan.hedge_stakes)
    assert math.isclose(nets[0], -20, abs_tol=1e-6) and math.isclose(nets[2], -20, abs_tol=1e-6)
    assert plan.feasible and plan.win_net > -20


def test_infeasible_floor_flagged():
    pos = Position(0, 100.0, 1.5)
    plan = hedge_to_floor(pos, ODDS, floor=1000.0)
    assert not plan.feasible


def test_margin_makes_floor_worse_never_better():
    pos = Position(0, 100.0, 2.4)
    assert lock_profit(pos, ODDS, margin=0.05).floor < lock_profit(pos, ODDS).floor
