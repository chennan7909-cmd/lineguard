"""End-to-end lifecycle: replay through OPEN->PROPOSED->fills->RECONCILED,
multi-fixture isolation, partial-fill risk recalculation, restart recovery."""
from lineguard.execution import ExecConfig
from lineguard.txline.normalize import parse_score
from tests._helpers import decisions, mk_desk, mk_odds


def test_multi_fixture_isolation(tmp_path):
    desk = mk_desk(tmp_path)
    desk.on_odds(mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "a1"))
    desk.on_odds(mk_odds(2, 1_000_100, (0.50, 0.26, 0.24), "b1"))
    # fixture 1 spikes to take; fixture 2 stays flat
    desk.on_odds(mk_odds(1, 1_060_000, (0.58, 0.22, 0.20), "a2"))
    desk.on_odds(mk_odds(1, 1_061_000, (0.58, 0.22, 0.20), "a3"))  # fill after latency
    desk.on_odds(mk_odds(2, 1_062_000, (0.50, 0.26, 0.24), "b2"))
    ds = decisions(tmp_path)
    assert {d["fixture"] for d in ds if d["action"] == "PROPOSED"} == {1}
    assert 2 in desk.positions and 1 not in desk.positions


def test_partial_fill_recalculates_remaining_risk(tmp_path):
    desk = mk_desk(tmp_path, exec_cfg=ExecConfig(seed=1, fill_probability=1.0,
                                                 quote_halt_prob=0.0, max_slippage_bps=0,
                                                 latency_ms=800, price_protection_bps=10_000,
                                                 max_leg_liquidity=5.0))
    desk.on_odds(mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "m1"))
    desk.on_odds(mk_odds(1, 1_060_000, (0.58, 0.22, 0.20), "m2"))
    desk.on_odds(mk_odds(1, 1_061_000, (0.58, 0.22, 0.20), "m3"))
    desk.on_odds(mk_odds(1, 1_062_500, (0.58, 0.22, 0.20), "m4"))
    rec = [d for d in decisions(tmp_path) if d["action"] == "RECONCILED"]
    assert rec, "expected reconciliation"
    r = rec[0]
    assert any(l["state"] == "PARTIALLY_FILLED" for l in r["legs"])
    assert r["realized_floor"] < r["proposed_floor"]        # residual risk measured


def test_agent_restart_recovers_open_positions(tmp_path):
    desk = mk_desk(tmp_path)
    desk.on_odds(mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "m1"))
    desk.fh.close()
    desk2 = mk_desk(tmp_path)                                # fresh process, same log
    assert 1 in desk2.positions
    assert abs(desk2.positions[1]["pos"].entry_odds - round(1 / 0.44, 3)) < 0.01


def test_full_agent_lifecycle(tmp_path):
    """fixture starts -> OPEN -> sharp move (no goal) -> hedge PROPOSED ->
    partial fill -> residual risk recalculated -> RECONCILED -> hashes present."""
    desk = mk_desk(tmp_path, exec_cfg=ExecConfig(seed=2, fill_probability=1.0,
                                                 quote_halt_prob=0.0, max_slippage_bps=25,
                                                 latency_ms=800, price_protection_bps=10_000,
                                                 max_leg_liquidity=8.0))
    t = 1_000_000
    desk.on_score(parse_score({"FixtureId": 9, "Ts": t - 600_000, "Seq": 1,
                               "Action": "connected", "GameState": "running"}))
    desk.on_odds(mk_odds(9, t, (0.44, 0.28, 0.28), "s0"))            # position opens
    desk.on_odds(mk_odds(9, t + 60_000, (0.58, 0.22, 0.20), "s1"))    # sharp move, no goal
    desk.on_odds(mk_odds(9, t + 61_000, (0.58, 0.22, 0.20), "s2"))    # fills after latency
    desk.on_odds(mk_odds(9, t + 62_500, (0.58, 0.22, 0.20), "s3"))    # rework pass completes
    ds = decisions(tmp_path)
    acts = [d["action"] for d in ds]
    for expected in ("OPEN", "PROPOSED", "SUBMITTED", "PARTIALLY_FILLED", "REWORK", "RECONCILED"):
        assert expected in acts, f"{expected} missing from {acts}"
    rec = next(d for d in ds if d["action"] == "RECONCILED")
    assert rec["realized_floor"] < rec["proposed_floor"]
    assert all(d["anchor"]["hash"] for d in ds)


def test_rework_closes_residual_leg(tmp_path):
    """One leg fills, the other is price-protected away -> REWORK resubmits the
    remainder at current prices; reconciliation ends sane, not naked."""
    desk = mk_desk(tmp_path, exec_cfg=ExecConfig(seed=4, fill_probability=1.0,
                                                 quote_halt_prob=0.0, max_slippage_bps=0,
                                                 latency_ms=800, price_protection_bps=150))
    t = 1_000_000
    desk.on_odds(mk_odds(3, t, (0.44, 0.28, 0.28), "w1"))
    desk.on_odds(mk_odds(3, t + 60_000, (0.58, 0.22, 0.20), "w2"))      # trigger, propose @ these odds
    # only outcome2 crashes beyond protection; outcome1 (draw) stays in band
    desk.on_odds(mk_odds(3, t + 61_000, (0.5405, 0.2195, 0.24), "w3"))
    acts = [d["action"] for d in decisions(tmp_path)]
    assert "REWORK" in acts, acts
    desk.on_odds(mk_odds(3, t + 62_500, (0.5405, 0.2195, 0.24), "w4"))  # rework fills after latency
    ds = decisions(tmp_path)
    rec = [d for d in ds if d["action"] == "RECONCILED"]
    assert rec, [d["action"] for d in ds]
    assert all(l["filled"] >= l["requested"] - 0.01 for l in rec[0]["legs"])
    assert rec[0]["realized_floor"] > -100                               # exposure closed, not naked
