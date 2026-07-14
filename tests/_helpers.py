"""Shared fixtures for lifecycle tests."""
import json

from lineguard.agent import Desk
from lineguard.chain.anchor import canonical_hash
from lineguard.execution import ExecConfig
from lineguard.txline.normalize import parse_odds


class NullAnchor:
    enabled = False
    def anchor(self, decision):
        return {"hash": canonical_hash(decision), "sig": None, "mode": "disabled"}


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
