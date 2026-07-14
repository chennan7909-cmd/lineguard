"""Infrastructure reliability: SSE reconnect, JWT renewal, dedup,
out-of-order rejection, anchor-failure isolation."""
import json
import threading
import time
from pathlib import Path

import pytest

from lineguard.chain.anchor import canonical_hash
from lineguard.txline.auth import Credentials
from tests._helpers import NullAnchor, decisions, mk_desk, mk_odds


class ExplodingAnchor:
    enabled = True
    def anchor(self, decision):
        raise RuntimeError("solana rpc down")


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


def test_anchor_failure_never_loses_local_decision(tmp_path):
    desk = mk_desk(tmp_path, anchor=ExplodingAnchor())
    desk.on_odds(mk_odds(1, 1_000_000, (0.44, 0.28, 0.28), "m1"))
    ds = decisions(tmp_path)
    assert len(ds) == 1 and ds[0]["action"] == "OPEN"
    assert ds[0]["anchor"]["mode"].startswith("anchor_exception")


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
