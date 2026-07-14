"""Canonical, reproducible backtest results — the single source of truth.

Every number quoted in README / docs / video comes from THIS module and
nowhere else. One command regenerates everything:

    python -m lineguard.results --data data/ --output results/

Outputs:
    results/backtest_summary.json    full metadata + statistics
    results/event_level_results.csv  one row per signal (audit-ready)
    results/decay_curve.png          avg lockable P/L vs minutes-after-signal
    results/equity_curve.png         cumulative P/L: 3 policies compared
    results/RESULTS.md               ready-to-commit markdown tables
    data/decay_curve.json            (kept in sync for the dashboard)

Methodology (pinned, echoed into the JSON):
  * Odds: decimal (TxLINE Prices/1000); probabilities = Pct/100.
  * Feed is TXLineStablePriceDemargined -> bookmaker margin ALREADY removed
    (sum p ~ 1.000). A conservative-fill variant applies `--margin` (default
    2%) to hedge legs: o_eff = 1 + (o-1)*(1-margin).
  * Signal: dual gate on 1X2 implied prob, |z|>=2.5 AND delta>=+0.04 vs a
    15-min rolling baseline, min 6 obs, 10-min cooldown, shortening side
    only. Attribution: event_driven iff a score event occurred <=120s before.
  * Position: paper stake 100 on the spiking outcome at signal-time odds.
  * P/L metric: lockable P/L F_lock = S*(a*q_i - 1), q_i = 1 - sum_{j!=i} 1/o_j
    -- the net P/L a dutching lock would guarantee at that instant.
  * Horizons: lock at +60s / +2m / +5m / +10m / +15m (nearest update at or
    before the horizon; signals without coverage at a horizon are excluded
    from that horizon's stats and counted in `coverage`).
  * Terminal (unhedged) P/L: +100*(a-1) if the backed outcome wins else -100,
    winner = outcome the resolving market converged to (terminal prob>=0.90).
  * CI: normal-approx 95% (mean +/- 1.96*sd/sqrt(n)).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics as st
from pathlib import Path

from .backtest import load_fixture, prob_at, terminal_winner
from .risk.hedge import Position, effective_odds, locked_pnl
from .signal.detector import DetectorConfig, MovementDetector
from .txline.normalize import parse_odds

STAKE = 100.0
HORIZONS_S = [60, 120, 300, 600, 900]


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    m = st.fmean(xs)
    sd = st.pstdev(xs) if len(xs) > 1 else 0.0
    half = 1.96 * sd / (len(xs) ** 0.5) if len(xs) > 1 else 0.0
    return {"n": len(xs), "mean": round(m, 2), "median": round(st.median(xs), 2),
            "std": round(sd, 2), "ci95": [round(m - half, 2), round(m + half, 2)]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--output", default="results")
    ap.add_argument("--margin", type=float, default=0.02,
                    help="conservative fill margin on hedge legs (0 = frictionless)")
    args = ap.parse_args()
    data, out = Path(args.data), Path(args.output)
    out.mkdir(exist_ok=True)
    cfg = DetectorConfig()

    fids = sorted({p.split("hist_odds_")[1].split("_")[0]
                   for p in glob.glob(str(data / "hist_odds_*.jsonl"))})
    total_lines = sum(1 for f in glob.glob(str(data / "hist_odds_*.jsonl"))
                      for _ in open(f, encoding="utf-8"))

    rows, n_1x2, matches_used = [], 0, 0
    for fid in fids:
        odds, scores = load_fixture(data, fid)
        if not odds:
            continue
        matches_used += 1
        n_1x2 += len(odds)
        det = MovementDetector(cfg)
        events = [(s.ts_ms, s.action) for s in scores]
        ei = 0
        winner = terminal_winner(odds)
        for u in odds:
            while ei < len(events) and events[ei][0] <= u.ts_ms:
                det.note_score_event(int(fid), events[ei][0], events[ei][1])
                ei += 1
            for sig in det.on_odds(u):
                pos = Position(sig.outcome, STAKE, sig.odds_at_signal[sig.outcome])
                r = {"fixture": int(fid), "ts_ms": sig.ts_ms, "outcome_idx": sig.outcome,
                     "outcome": sig.outcome_name, "in_running": sig.in_running,
                     "event_driven": sig.event_driven, "z": round(sig.z, 2),
                     "prob_before": round(sig.prob_before, 4), "prob_after": round(sig.prob_after, 4),
                     "entry_odds": round(pos.entry_odds, 3)}
                o0 = effective_odds(sig.odds_at_signal, args.margin)
                r["lock_immediate"] = round(locked_pnl(pos, o0), 2)
                for h in HORIZONS_S:
                    uh = prob_at(odds, sig.ts_ms + h * 1000, sig.outcome)
                    r[f"lock_{h}s"] = (round(locked_pnl(pos, effective_odds(uh.decimal_odds, args.margin)), 2)
                                       if uh else None)
                if winner is not None:
                    r["unhedged_terminal"] = round(STAKE * (pos.entry_odds - 1), 2) if winner == sig.outcome else -STAKE
                    r["hit"] = winner == sig.outcome
                else:
                    r["unhedged_terminal"], r["hit"] = None, None
                rows.append(r)

    # ---- summary ---------------------------------------------------------
    def block(sel):
        b = {"lock_immediate": _stats([r["lock_immediate"] for r in sel]),
             "unhedged_terminal": _stats([r["unhedged_terminal"] for r in sel]),
             "hit_rate": None}
        graded = [r for r in sel if r["hit"] is not None]
        if graded:
            b["hit_rate"] = {"hits": sum(r["hit"] for r in graded), "graded": len(graded),
                             "rate": round(sum(r["hit"] for r in graded) / len(graded), 3)}
        for h in HORIZONS_S:
            b[f"lock_{h}s"] = _stats([r[f"lock_{h}s"] for r in sel])
        return b

    sharp = [r for r in rows if not r["event_driven"]]
    summary = {
        "generated_by": "python -m lineguard.results --data {} --output {} --margin {}".format(
            args.data, args.output, args.margin),
        "methodology": {
            "odds_convention": "decimal (TxLINE Prices/1000); probs = Pct/100",
            "feed": "TXLineStablePriceDemargined (bookmaker margin already removed; sum p ~ 1.000)",
            "conservative_fill_margin_on_hedge_legs": args.margin,
            "signal_definition": {"z_min": cfg.z_min, "delta_min": cfg.delta_min,
                                  "lookback_min": cfg.lookback_ms // 60000,
                                  "min_window_obs": cfg.min_window,
                                  "cooldown_min": cfg.cooldown_ms // 60000,
                                  "direction": "shortening side only (prob rising)",
                                  "attribution_window_s": cfg.event_window_ms // 1000},
            "pnl_metric": "lockable P/L F_lock = S*(a*q_i - 1), q_i = 1 - sum_{j!=i} 1/o_j; stake 100",
            "terminal_grading": "winner = outcome with terminal market prob >= 0.90",
            "ci": "normal-approx 95%",
        },
        "sample": {"matches": matches_used, "odds_updates_total_rows": total_lines,
                   "odds_updates_1x2_deduped": n_1x2, "signals": len(rows),
                   "signals_sharp": len(sharp), "signals_event_driven": len(rows) - len(sharp)},
        "results_all_signals": block(rows),
        "results_sharp_only": block(sharp),
        "results_event_driven": block([r for r in rows if r["event_driven"]]),
    }
    (out / "backtest_summary.json").write_text(json.dumps(summary, indent=1))

    # ---- CSV -------------------------------------------------------------
    if rows:
        with open(out / "event_level_results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    # ---- decay curve json (dashboard) + plots ----------------------------
    decay = {"minutes": [0, 1, 2, 5, 10, 15],
             "all": [], "sharp_only": [], "n_signals": len(rows)}
    for m in decay["minutes"]:
        key = "lock_immediate" if m == 0 else f"lock_{m*60}s"
        a = [r[key] for r in rows if r.get(key) is not None]
        s = [r[key] for r in sharp if r.get(key) is not None]
        decay["all"].append(round(st.fmean(a), 2) if a else None)
        decay["sharp_only"].append(round(st.fmean(s), 2) if s else None)
    (data / "decay_curve.json").write_text(json.dumps(decay, indent=1))
    (out / "decay_curve.json").write_text(json.dumps(decay, indent=1))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(decay["minutes"], decay["all"], marker="o", label="all signals")
        ax.plot(decay["minutes"], decay["sharp_only"], marker="s", label="sharp (no score event)")
        ax.axhline(0, lw=0.8, color="gray")
        ax.set_xlabel("minutes after signal"); ax.set_ylabel("avg lockable P/L per 100 stake")
        ax.set_title(f"Spikes are ephemeral (n={len(rows)} signals, {matches_used} matches, "
                     f"margin={args.margin:.0%})")
        ax.legend(); fig.tight_layout(); fig.savefig(out / "decay_curve.png", dpi=150)

        rows_t = sorted(rows, key=lambda r: r["ts_ms"])
        def cum(key):
            tot, ys = 0.0, []
            for r in rows_t:
                v = r.get(key)
                tot += v if v is not None else 0.0
                ys.append(tot)
            return ys
        fig2, ax2 = plt.subplots(figsize=(7, 4))
        ax2.plot(cum("lock_immediate"), label="lock immediately (t=0)")
        ax2.plot(cum("lock_600s"), label="wait 10 min, then lock")
        ax2.plot(cum("unhedged_terminal"), label="never hedge (hold to result)")
        ax2.set_xlabel("signal # (chronological)"); ax2.set_ylabel("cumulative P/L per 100 stake")
        ax2.set_title("Policy comparison across all signals")
        ax2.legend(); fig2.tight_layout(); fig2.savefig(out / "equity_curve.png", dpi=150)
    except ImportError:
        print("[results] matplotlib not installed -> skipping PNGs (pip install matplotlib)")

    # ---- RESULTS.md ------------------------------------------------------
    def md_block(name, b):
        lines = [f"### {name}", "", "| metric | n | mean | median | std | 95% CI |", "|---|---|---|---|---|---|"]
        for k in ["lock_immediate"] + [f"lock_{h}s" for h in HORIZONS_S] + ["unhedged_terminal"]:
            s = b[k]
            if s["n"]:
                lines.append(f"| {k} | {s['n']} | {s['mean']:+.2f} | {s['median']:+.2f} | {s['std']:.2f} | [{s['ci95'][0]:+.2f}, {s['ci95'][1]:+.2f}] |")
        if b["hit_rate"]:
            hr = b["hit_rate"]
            lines.append(f"| hit_rate | {hr['graded']} | {hr['rate']:.1%} ({hr['hits']}/{hr['graded']}) | | | |")
        return "\n".join(lines) + "\n"

    md = ["# Canonical Backtest Results", "",
          f"Reproduce: `{summary['generated_by']}`", "",
          f"**Sample:** {matches_used} matches · {total_lines:,} raw odds rows · "
          f"{n_1x2:,} deduped 1X2 updates · {len(rows)} signals "
          f"({len(sharp)} sharp / {len(rows)-len(sharp)} event-driven) · "
          f"conservative fill margin {args.margin:.0%} on hedge legs", "",
          "P/L metric: lockable P/L `F_lock = S(a·q_i − 1)` per 100 stake. "
          "Full methodology in `backtest_summary.json`.", "",
          md_block("All signals", summary["results_all_signals"]),
          md_block("Sharp only (no score event within 120s)", summary["results_sharp_only"]),
          md_block("Event-driven", summary["results_event_driven"]),
          "![decay](decay_curve.png)", "", "![equity](equity_curve.png)", ""]
    (out / "RESULTS.md").write_text("\n".join(md))
    print(f"[results] {matches_used} matches, {len(rows)} signals -> {out}/")
    print(f"[results] headline: all-signals lock@60s mean = "
          f"{summary['results_all_signals']['lock_60s'].get('mean')}, "
          f"@10m = {summary['results_all_signals']['lock_600s'].get('mean')}")


if __name__ == "__main__":
    main()
