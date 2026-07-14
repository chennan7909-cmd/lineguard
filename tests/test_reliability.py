"""End-to-end reliability suite — the tests judges actually worry about."""
import json
import threading
import time
from pathlib import Path

import pytest

from lineguard.agent import Desk
from lineguard.chain.anchor import canonical_hash
from lineguard.execution import ExecConfig
from lineguard.txline.auth import Credentials
from lineguard.txline.normalize import parse_odds, parse_score


class NullAnchor:
    enabled = False
    def anchor(self, decision):
        return {"hash": canonical_hash(decision), "sig": None, "mode": "disabled"}


class ExplodingAnchor:
    enabled = True
    def anchor(self, decision):
        raise RuntimeError("solana rpc down")


def mk_odds(fid, ts, probs, mid, in_running=True):
    return parse_odds({"FixtureId": fid, "MessageId": mid, "Ts": ts,
                       "SuperOddsType": "1X2_PARTICIPANT_RESULT",
                       "PriceNames": ["part1", "draw", "part2"],
                       "Prices": [round(1000 / p) for p in probs],
                       "Pct": [f"{p*100:.3f}" for p in probs],
                       "InRunning": in_running})


def mk_desk(tmp_path, anchor=None, **kw):
    args = dict(take=8.0, stop=-15.0, stake=100.0, stream_clock=True,
                exec_cfg=ExecConfig(seed=1, fill_probability=1.0, quote_halt_prob=0.0,
                                    max_slippage_bps=0, latency_ms=800,
                                    price_protection_bps=10_000))
    args.update(kw)
    return Desk(tmp_path, args.pop("take"), args.pop("stop"), args.pop("stake"),
                anchor or NullAnchor(), args.pop("stream_clock"), **args)


def decisions(tmp_path):
    p = tmp_path / "decisions.jsonl"
    return [json.loads(l) for l in open(p)] if p.exists() else []


def test_duplicate_messages_do_not_duplicate_positions(tmp_path):
    desk = mk_desk(tmp_path)
    u = mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "m1")
    desk.on_odds(u); desk.on_odds(u); desk.on_odds(u)
    assert sum(1 for d in decisions(tmp_path) if d["action"] == "OPEN") == 1


def test_out_of_order_data_is_rejected_not_traded(tmp_path):
    desk = mk_desk(tmp_path)
    desk.on_odds(mk_odds(1, 2_000_000, (0.44, 0.28, 0.28), "m1"))
    desk.on_odds(mk_odds(1, 1_500_000, (0.60, 0.20, 0.20), "m2"))  # stale/backwards
    acts = [d["action"] for d in decisions(tmp_path)]
    assert any(a.startswith("REJECT") for a in acts)
    assert acts.count("OPEN") == 1


def test_score_odds_timestamp_attribution(tmp_path):
    from lineguard.signal.detector import DetectorConfig, MovementDetector
    det = MovementDetector(DetectorConfig(min_window=3))
    t0 = 1_000_000
    base = [(0.440, 0.28, 0.28), (0.442, 0.279, 0.279), (0.438, 0.281, 0.281),
            (0.441, 0.2795, 0.2795), (0.439, 0.2805, 0.2805), (0.443, 0.2785, 0.2785)]
    for k in range(6):
        det.on_odds(mk_odds(1, t0 + k * 60_000, base[k], f"w{k}"))
    det.note_score_event(1, t0 + 6 * 60_000 - 30_000, "goal")      # 30s before spike
    sigs = det.on_odds(mk_odds(1, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike"))
    assert sigs and sigs[0].event_driven is True
    det2 = MovementDetector(DetectorConfig(min_window=3))
    for k in range(6):
        det2.on_odds(mk_odds(2, t0 + k * 60_000, base[k], f"x{k}"))
    det2.note_score_event(2, t0, "goal")                            # 6 min before -> outside window
    sigs2 = det2.on_odds(mk_odds(2, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike2"))
    assert sigs2 and sigs2[0].event_driven is False


def test_anchor_failure_never_loses_local_decision(tmp_path):
    desk = mk_desk(tmp_path, anchor=ExplodingAnchor())
    desk.on_odds(mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "m1"))
    ds = decisions(tmp_path)
    assert len(ds) == 1 and ds[0]["action"] == "OPEN"
    assert ds[0]["anchor"]["mode"].startswith("anchor_exception")


def test_canonical_hash_deterministic_no_duplicate_records():
    d = {"action": "LOCK", "fixture": 1, "ts": 5, "x": [1, 2]}
    assert canonical_hash(d) == canonical_hash(json.loads(json.dumps(d)))
    assert canonical_hash(d) != canonical_hash({**d, "ts": 6})


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


def test_jwt_renewed_transparently():
    class FakeClient:
        def __init__(self): self.calls = 0
        def post(self, url, **kw):
            self.calls += 1
            class R:
                def raise_for_status(self): pass
                def json(self): return {"token": "fresh_jwt"}
            return R()
    c = Credentials("api_tok")
    c._client = FakeClient()
    h = c.headers()
    assert h["Authorization"] == "Bearer fresh_jwt" and h["X-Api-Token"] == "api_tok"
    c.renew_jwt()
    assert c.jwt == "fresh_jwt" and c._client.calls == 2


def test_sse_reconnect_writes_marker_and_recovers(tmp_path, monkeypatch):
    from lineguard.txline import recorder as rec
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def iter_lines(self):
            yield "event: odds"
            yield 'data: {"FixtureId":1,"Ts":1}'
            yield ""

    class FakeStreamCtx:
        def __enter__(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("network blip")
            return FakeResp()
        def __exit__(self, *a): return False

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, *a, **k): return FakeStreamCtx()

    monkeypatch.setattr(rec.httpx, "Client", FakeClient)
    sink = rec.JsonlSink(tmp_path, "t")
    creds = Credentials("tok"); creds.jwt = "j"
    stop = threading.Event()
    th = threading.Thread(target=rec.stream_channel, args=("odds", sink, creds, stop), daemon=True)
    th.start(); time.sleep(2.5); stop.set(); th.join(timeout=3)
    lines = [json.loads(l) for f in tmp_path.glob("t_*.jsonl") for l in open(f)]
    events = [l["event"] for l in lines]
    assert "_reconnect" in events, f"no reconnect marker in {events}"
    assert "odds" in events, "stream did not recover after reconnect"


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
    ds = decisions(tmp_path)
    acts = [d["action"] for d in ds]
    for expected in ("OPEN", "PROPOSED", "SUBMITTED", "PARTIALLY_FILLED", "RECONCILED"):
        assert expected in acts, f"{expected} missing from {acts}"
    rec = next(d for d in ds if d["action"] == "RECONCILED")
    assert rec["realized_floor"] < rec["proposed_floor"]
    assert all(d["anchor"]["hash"] for d in ds)
