"""Backtest the detector + hedge engine over backfilled history.

    python -m lineguard.backtest --data data/               # all fixtures
    python -m lineguard.backtest --data data/ --fixture 18222446

For every signal: open a paper position (stake 100) on the shortening
outcome at the demargined odds, then mark it with the hedge engine at
+10 minutes and at the terminal price. Winner is inferred from the terminal
1X2 probabilities (the resolving market converges; > 0.90 counts as decided).
Outputs a per-signal log (JSONL) and summary hit-rates split by attribution.
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path

from .risk.hedge import Position, locked_pnl
from .signal.detector import DetectorConfig, MovementDetector
from .txline.normalize import MARKET_1X2, parse_odds, parse_score

STAKE = 100.0


def load_fixture(data_dir: Path, fid: str):
    odds, scores, seen = [], [], set()
    for f in sorted(glob.glob(str(data_dir / f"hist_odds_{fid}_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            u = parse_odds(json.loads(line)["payload"])
            if u and u.market == MARKET_1X2 and u.message_id not in seen:
                seen.add(u.message_id)
                odds.append(u)
    for f in sorted(glob.glob(str(data_dir / f"hist_scores_{fid}_*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            s = parse_score(json.loads(line)["payload"])
            if s:
                scores.append(s)
    odds.sort(key=lambda u: u.ts_ms)
    scores.sort(key=lambda s: s.ts_ms)
    return odds, scores


def terminal_winner(odds):
    """Index of the outcome the resolving market converged to, else None."""
    if not odds:
        return None
    last = odds[-1]
    best = max(range(3), key=lambda i: last.probs[i])
    return best if last.probs[best] >= 0.90 else None


def prob_at(odds, ts_ms, outcome):
    """Latest prob for `outcome` at or before ts_ms (linear scan ok offline)."""
    p = None
    for u in odds:
        if u.ts_ms > ts_ms:
            break
        p = u
    return p


def run_fixture(fid: str, data_dir: Path, cfg: DetectorConfig, log_fh):
    odds, scores = load_fixture(data_dir, fid)
    if not odds:
        return []
    det = MovementDetector(cfg)
    events = [(s.ts_ms, s.action) for s in scores]
    ei = 0
    results = []
    for u in odds:
        while ei < len(events) and events[ei][0] <= u.ts_ms:
            det.note_score_event(int(fid), events[ei][0], events[ei][1])
            ei += 1
        for sig in det.on_odds(u):
            pos = Position(sig.outcome, STAKE, sig.odds_at_signal[sig.outcome])
            u10 = prob_at(odds, sig.ts_ms + 10 * 60_000, sig.outcome)
            mtm10 = locked_pnl(pos, u10.decimal_odds) if u10 else None
            winner = terminal_winner(odds)
            hit = (winner == sig.outcome) if winner is not None else None
            row = {"fixture": int(fid), "ts": sig.ts_ms, "outcome": sig.outcome_name,
                   "in_running": sig.in_running, "event_driven": sig.event_driven,
                   "z": round(sig.z, 2), "prob": [round(sig.prob_before, 4), round(sig.prob_after, 4)],
                   "entry_odds": round(pos.entry_odds, 3),
                   "lockable_pnl_10m": None if mtm10 is None else round(mtm10, 2),
                   "winner": winner, "hit": hit}
            log_fh.write(json.dumps(row) + "\n")
            results.append(row)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--fixture", action="append")
    ap.add_argument("--z", type=float, default=2.5)
    ap.add_argument("--delta", type=float, default=0.04)
    args = ap.parse_args()
    data_dir = Path(args.data)
    cfg = DetectorConfig(z_min=args.z, delta_min=args.delta)

    if args.fixture:
        fids = args.fixture
    else:
        fids = sorted({p.split("hist_odds_")[1].split("_")[0]
                       for p in glob.glob(str(data_dir / "hist_odds_*.jsonl"))})
    out_path = data_dir / "backtest_signals.jsonl"
    all_rows = []
    with open(out_path, "w", encoding="utf-8") as fh:
        for fid in fids:
            rows = run_fixture(fid, data_dir, cfg, fh)
            print(f"[backtest] fixture {fid}: {len(rows)} signal(s)")
            all_rows += rows

    def summarize(rows, label):
        graded = [r for r in rows if r["hit"] is not None]
        hits = sum(1 for r in graded if r["hit"])
        mtms = [r["lockable_pnl_10m"] for r in graded if r["lockable_pnl_10m"] is not None]
        avg = sum(mtms) / len(mtms) if mtms else 0.0
        print(f"  {label}: {len(rows)} signals | graded {len(graded)} | "
              f"hit-rate {hits}/{len(graded)} ({hits/len(graded)*100:.0f}%)" if graded else
              f"  {label}: {len(rows)} signals | none graded", end="")
        if mtms:
            print(f" | avg 10-min lockable P/L per 100 stake: {avg:+.2f}")
        else:
            print()

    print(f"\n[backtest] TOTAL {len(all_rows)} signal(s) -> {out_path}")
    summarize([r for r in all_rows if not r["event_driven"]], "SHARP (no score event)")
    summarize([r for r in all_rows if r["event_driven"]], "event-driven")


if __name__ == "__main__":
    main()
