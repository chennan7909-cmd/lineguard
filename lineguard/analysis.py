"""Evidence: spikes are ephemeral.

For every backtest signal, mark-to-market the lockable P/L of a stake-100
position opened at the spike, at +1..+15 minutes. Averaged over hundreds of
signals this is the decay curve — the quantitative case for instant locking.

    python -m lineguard.analysis --data data/
Writes data/decay_curve.json and prints the table.
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

from .backtest import load_fixture, prob_at
from .risk.hedge import Position, locked_pnl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    args = ap.parse_args()
    data = Path(args.data)
    sig_path = data / "backtest_signals.jsonl"
    if not sig_path.exists():
        raise SystemExit("run `python -m lineguard.backtest` first")
    signals = [json.loads(l) for l in open(sig_path, encoding="utf-8")]
    cache = {}
    minutes = list(range(0, 16))
    sums = {m: [0.0, 0] for m in minutes}
    sums_sharp = {m: [0.0, 0] for m in minutes}
    for s in signals:
        fid = str(s["fixture"])
        if fid not in cache:
            cache[fid] = load_fixture(data, fid)[0]
        odds = cache[fid]
        pos = Position(0, 100.0, s["entry_odds"])  # outcome idx irrelevant: we re-find below
        # locate outcome index by matching entry odds at signal time
        u0 = prob_at(odds, s["ts"], 0)
        if not u0:
            continue
        try:
            i = min(range(3), key=lambda k: abs(u0.decimal_odds[k] - s["entry_odds"]))
        except ValueError:
            continue
        pos = Position(i, 100.0, s["entry_odds"])
        for m in minutes:
            u = prob_at(odds, s["ts"] + m * 60_000, i)
            if u:
                v = locked_pnl(pos, u.decimal_odds)
                sums[m][0] += v; sums[m][1] += 1
                if not s["event_driven"]:
                    sums_sharp[m][0] += v; sums_sharp[m][1] += 1
    curve = {"minutes": minutes,
             "all": [round(sums[m][0] / max(sums[m][1], 1), 2) for m in minutes],
             "sharp_only": [round(sums_sharp[m][0] / max(sums_sharp[m][1], 1), 2) for m in minutes],
             "n_signals": len(signals)}
    (data / "decay_curve.json").write_text(json.dumps(curve, indent=1))
    print(f"{'min':>4} {'all':>8} {'sharp':>8}   (avg lockable P/L per 100 stake, n={curve['n_signals']})")
    for k, m in enumerate(minutes):
        print(f"{m:>4} {curve['all'][k]:>8.2f} {curve['sharp_only'][k]:>8.2f}")


if __name__ == "__main__":
    main()
