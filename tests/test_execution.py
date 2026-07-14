from lineguard.execution import ExecConfig, SimulatedExecutor
from lineguard.risk.hedge import Position, lock_profit

ODDS = (1.862, 3.793, 5.016)


def make_order(exe, seed_odds=ODDS):
    pos = Position(0, 100.0, 2.4)
    plan = lock_profit(pos, seed_odds)
    return pos, plan, exe.submit(1, pos, plan, ts_ms=1_000_000)


def test_latency_gates_fills():
    exe = SimulatedExecutor(ExecConfig(latency_ms=800, seed=1))
    _, _, order = make_order(exe)
    assert exe.poll(order, ODDS, 1_000_500) == []          # before latency
    assert exe.poll(order, ODDS, 1_000_900) != []          # after latency


def test_deterministic_given_seed():
    a = SimulatedExecutor(ExecConfig(seed=42)); b = SimulatedExecutor(ExecConfig(seed=42))
    _, _, oa = make_order(a); _, _, ob = make_order(b)
    assert a.poll(oa, ODDS, 2_000_000) == b.poll(ob, ODDS, 2_000_000)


def test_full_fill_reconciles_close_to_proposed():
    exe = SimulatedExecutor(ExecConfig(seed=3, fill_probability=1.0, quote_halt_prob=0.0,
                                       max_slippage_bps=0))
    _, plan, order = make_order(exe)
    exe.poll(order, ODDS, 2_000_000)
    rec = exe.reconcile(order)
    assert order.state == "SETTLED"
    assert abs(rec["realized_floor"] - rec["proposed_floor"]) < 0.01
    assert rec["friction_cost"] < 0.01


def test_slippage_makes_floor_worse():
    exe = SimulatedExecutor(ExecConfig(seed=5, fill_probability=1.0, quote_halt_prob=0.0,
                                       max_slippage_bps=200))
    _, _, order = make_order(exe)
    exe.poll(order, ODDS, 2_000_000)
    rec = exe.reconcile(order)
    assert rec["realized_floor"] < rec["proposed_floor"]
    assert rec["friction_cost"] > 0


def test_liquidity_cap_partial_fill():
    exe = SimulatedExecutor(ExecConfig(seed=7, fill_probability=1.0, quote_halt_prob=0.0,
                                       max_leg_liquidity=10.0))
    _, _, order = make_order(exe)
    exe.poll(order, ODDS, 2_000_000)
    assert any(l.state == "PARTIALLY_FILLED" for l in order.legs)


def test_rejection_leaves_single_leg_risk_visible():
    exe = SimulatedExecutor(ExecConfig(seed=9, fill_probability=0.0, quote_halt_prob=0.0,
                                       max_retries=0))
    _, _, order = make_order(exe)
    exe.poll(order, ODDS, 2_000_000)
    rec = exe.reconcile(order)
    assert all(l.state == "REJECTED" for l in order.legs)
    assert rec["realized_floor"] <= -100.0 + 0.01   # naked position: losing states lose the stake


def test_price_protection_cancels_on_adverse_move():
    exe = SimulatedExecutor(ExecConfig(seed=13, fill_probability=1.0, quote_halt_prob=0.0,
                                       price_protection_bps=150))
    pos = Position(0, 100.0, 2.4)
    plan = lock_profit(pos, ODDS)
    order = exe.submit(1, pos, plan, ts_ms=1_000_000, odds_at_proposal=ODDS)
    crashed = (1.4, 2.9, 3.8)   # both hedge legs' odds shortened >150bps
    exe.poll(order, crashed, 2_000_000)
    assert order.state == "SETTLED_UNFILLED"
    assert all(l.state == "CANCELLED" and l.filled == 0 for l in order.legs)


def test_within_band_still_fills():
    exe = SimulatedExecutor(ExecConfig(seed=17, fill_probability=1.0, quote_halt_prob=0.0,
                                       max_slippage_bps=0, price_protection_bps=150))
    pos = Position(0, 100.0, 2.4)
    plan = lock_profit(pos, ODDS)
    order = exe.submit(1, pos, plan, ts_ms=1_000_000, odds_at_proposal=ODDS)
    exe.poll(order, ODDS, 2_000_000)   # unchanged odds -> inside band
    assert order.state == "SETTLED"
    assert all(l.state == "FILLED" for l in order.legs)
