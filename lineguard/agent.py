"""LineGuard agent — the autonomous in-play risk desk.

Deterministic policy (no LLM in the decision path):

  1. OPEN    when a fixture goes in-running, back the market favorite with a
             fixed paper stake (the "portfolio" the desk protects).
  2. WATCH   every guarded odds update re-prices each open position via the
             one-line identity F_lock = S*(a*q_i - 1).
  3. LOCK    execute the dutch hedge (lock_profit) the moment F_lock crosses
             +take or -stop bands. Spikes are ephemeral (27-match backtest:
             unhedged spike value decays -16/100 within 10 min), so the lock
             is immediate and closed-form.
  4. REJECT  any update failing the Guard (stale/corrupt) triggers no
             decision and is itself logged + anchored.

Every decision is appended to data/decisions.jsonl and anchored on Solana
devnet via SPL Memo (hash + compact fields), so the audit trail survives
independently of this process.

    python -m lineguard.agent --replay "data/hist_odds_18222446_*.jsonl" --speed 60
    python -m lineguard.agent --live
"""
from __future__ import annotations

import argparse
import glob
import json
import time
from pathlib import Path

import httpx

from .chain.anchor import Anchor
from .execution import ExecConfig, SimulatedExecutor
from .guard.freshness import FreshnessGuard
from .risk.hedge import Position, lock_profit, locked_pnl
from .signal.detector import DetectorConfig, MovementDetector
from .txline.auth import ORIGIN, get_credentials
from .txline.normalize import MARKET_1X2, parse_odds, parse_score


class Desk:
    def __init__(self, out: Path, take: float, stop: float, stake: float, anchor: Anchor,
                 stream_clock: bool, confirm_stop_ms: int = 240_000,
                 exec_cfg: ExecConfig | None = None, exec_mode: str = "simulated"):
        self.guard = FreshnessGuard(stream_clock=stream_clock)
        self.detector = MovementDetector(DetectorConfig())
        self.anchor = anchor
        self.take, self.stop, self.stake = take, stop, stake
        self.confirm_stop_ms = confirm_stop_ms
        self._held_through: dict = {}
        self.exec_mode = exec_mode
        self.executor = SimulatedExecutor(exec_cfg)
        self.pending: dict = {}       # fixture -> Order
        self._repropose_after: dict = {}
        self.positions: dict = {}      # fixture -> {pos, opened_ts, names}
        self.locked: dict = {}         # fixture -> plan summary
        self.out = out
        self.fh = open(out / "decisions.jsonl", "a", encoding="utf-8")
        self.seen: set = set()
        self._restore(out / "decisions.jsonl")

    def _restore(self, path: Path):
        """Rebuild open positions from the decision log (crash/restart recovery).
        Last state per fixture wins: OPEN/CANCELLED -> open; LOCK/RECONCILED -> locked.
        Orders pending at crash resolve to open (safe: the desk re-proposes)."""
        if not path.exists():
            return
        last: dict = {}
        for line in open(path, encoding="utf-8"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            fid, act = d.get("fixture"), d.get("action", "")
            if fid is None:
                continue
            if act == "OPEN":
                last[fid] = ("open", d)
            elif act in ("LOCK", "RECONCILED"):
                last[fid] = ("locked", d)
            elif act == "CANCELLED" and last.get(fid, ("", None))[0] != "locked":
                last[fid] = ("open", last.get(fid, (None, None))[1] or d)
        restored = 0
        for fid, (state, d) in last.items():
            if state == "locked":
                self.locked[fid] = {"floor": d.get("locked_pnl", d.get("realized_floor"))}
            elif state == "open" and d and "outcome_idx" in d:
                self.positions[fid] = {"pos": Position(d["outcome_idx"], d.get("stake", 100.0), d["odds"]),
                                       "names": ("part1", "draw", "part2"), "opened": d.get("ts", 0)}
                restored += 1
        if restored or self.locked:
            print(f"[desk] restored from log: {restored} open, {len(self.locked)} locked")

    def decide(self, decision: dict):
        try:
            res = self.anchor.anchor(decision)
        except Exception as e:   # anchoring must NEVER break the desk
            res = {"hash": None, "sig": None, "mode": f"anchor_exception:{type(e).__name__}"}
        decision["anchor"] = res
        self.fh.write(json.dumps(decision, ensure_ascii=False) + "\n")
        self.fh.flush()
        sig = (res.get("sig") or "-")[:16]
        print(f"[desk] {decision['action']:<12} fx={decision.get('fixture')} "
              f"{decision.get('detail','')}  ⚓{sig}")

    def on_score(self, s):
        self.detector.note_score_event(s.fixture_id, s.ts_ms, s.action)

    def on_odds(self, u):
        if u.market != MARKET_1X2 or u.message_id in self.seen:
            return
        self.seen.add(u.message_id)
        v = self.guard.check(u)
        if not v.ok:
            self.decide({"action": f"REJECT_{v.code}", "fixture": u.fixture_id,
                         "ts": u.ts_ms, "detail": v.detail})
            return
        self.detector.on_odds(u)   # keeps attribution state warm

        fid = u.fixture_id
        if fid not in self.positions and fid not in self.locked and fid not in self.pending and u.in_running:
            i = max(range(3), key=lambda k: u.probs[k])
            pos = Position(i, self.stake, u.decimal_odds[i])
            self.positions[fid] = {"pos": pos, "names": u.outcome_names, "opened": u.ts_ms}
            self.decide({"action": "OPEN", "fixture": fid, "ts": u.ts_ms,
                         "outcome": u.outcome_names[i], "outcome_idx": i,
                         "odds": round(pos.entry_odds, 3), "stake": self.stake,
                         "detail": f"back {u.outcome_names[i]} @ {pos.entry_odds:.3f}"})
            return

        pend = self.pending.get(fid)
        if pend:
            order, pst = pend
            for ev in self.executor.poll(order, u.decimal_odds, u.ts_ms):
                self.decide({"action": ev.get("event", ev.get("state", "FILL_EVENT")),
                             "fixture": fid, "ts": u.ts_ms, **ev,
                             "detail": f"leg->{ev['leg']} {ev.get('state', ev.get('event'))}"})
            if order.state == "SETTLED_UNFILLED":
                self.positions[fid] = pst
                del self.pending[fid]
                self._repropose_after[fid] = u.ts_ms + 120_000
                self.decide({"action": "CANCELLED", "fixture": fid, "ts": u.ts_ms,
                             "detail": "no leg filled (price protection / rejects) — "
                                       "position returned to book, re-propose after 120s"})
                return
            if order.state == "SETTLED":
                rec = self.executor.reconcile(order)
                self.locked[fid] = {"floor": rec["realized_floor"]}
                del self.pending[fid]
                self.decide({"action": "RECONCILED", "fixture": fid, "ts": u.ts_ms, **rec,
                             "detail": (f"realized_floor={rec['realized_floor']:+.2f} vs "
                                        f"proposed={rec['proposed_floor']:+.2f} "
                                        f"(friction {rec['friction_cost']:+.2f})")})
            return

        st = self.positions.get(fid)
        if not st:
            return
        pos = st["pos"]
        f = locked_pnl(pos, u.decimal_odds)
        if f <= self.stop:
            last_ev = self.detector._last_event_ts.get(fid, 0)
            if u.ts_ms - last_ev > self.confirm_stop_ms:
                # adverse spike with NO score event: evidence says it reverts -> HOLD
                if not self._held_through.get(fid):
                    self._held_through[fid] = True
                    self.decide({"action": "HOLD", "fixture": fid, "ts": u.ts_ms,
                                 "detail": f"F_lock={f:+.2f} < stop but unattributed (no score event "
                                           f"within {self.confirm_stop_ms//1000}s) — spikes revert, holding"})
                return
        if u.ts_ms < self._repropose_after.get(fid, 0):
            return
        if f >= self.take or f <= self.stop:
            plan = lock_profit(pos, u.decimal_odds)
            reason = 'take' if f >= self.take else 'stop'
            if self.exec_mode == "instant":
                self.locked[fid] = {"floor": plan.floor}
                del self.positions[fid]
                self.decide({"action": "LOCK", "fixture": fid, "ts": u.ts_ms,
                             "outcome": st["names"][pos.outcome],
                             "hedge_stakes": [round(h, 2) for h in plan.hedge_stakes],
                             "locked_pnl": round(plan.floor, 2),
                             "detail": f"F_lock={plan.floor:+.2f} ({reason})"})
                return
            order = self.executor.submit(fid, pos, plan, u.ts_ms, odds_at_proposal=u.decimal_odds)
            self.pending[fid] = (order, st)
            del self.positions[fid]
            self.decide({"action": "PROPOSED", "fixture": fid, "ts": u.ts_ms,
                         "intent": plan.intent, "reason": reason,
                         "proposed_floor": round(plan.floor, 2),
                         "hedge_stakes": [round(h, 2) for h in plan.hedge_stakes],
                         "exec_config": self.executor.cfg.as_dict(),
                         "detail": f"F_lock={plan.floor:+.2f} ({reason}) -> hedge proposed"})
            self.decide({"action": "SUBMITTED", "fixture": fid, "ts": u.ts_ms,
                         "legs": [{"outcome": l.outcome, "stake": round(l.requested, 2)} for l in order.legs],
                         "detail": f"{len(order.legs)} leg(s) to venue (latency {self.executor.cfg.latency_ms}ms)"})


def replay_rows(patterns, speed, inject_stale=False):
    files = sorted(sum((glob.glob(p) for p in patterns), []))
    rows = []
    for f in files:
        chan = "scores" if "scores" in Path(f).name else "odds"
        for line in open(f, encoding="utf-8"):
            rec = json.loads(line)
            rows.append((rec["payload"].get("Ts", 0), chan, rec["payload"]))
    rows.sort(key=lambda r: r[0])
    if inject_stale and rows:
        mid = len(rows) // 2
        ts_mid = rows[mid][0]
        bad = dict(rows[mid][2]); bad = json.loads(json.dumps(bad))
        bad["Ts"] = ts_mid - 9_000_000
        bad["MessageId"] = "INJECTED_STALE_DEMO"
        rows.insert(mid, (ts_mid, rows[mid][1], bad))   # arrives NOW, stamped 2.5h ago
    prev = None
    for ts, chan, payload in rows:
        real_ts = payload.get("Ts", ts)
        if speed > 0 and prev is not None and ts > prev:
            time.sleep(min((ts - prev) / 1000.0 / speed, 2.0))
        prev = ts
        yield chan, payload


def live_rows():
    from .txline.recorder import _iter_sse_lines
    creds = get_credentials()
    import threading, queue
    q: "queue.Queue" = queue.Queue()

    def pump(channel):
        while True:
            try:
                with httpx.Client(timeout=httpx.Timeout(10, read=120)) as client:
                    with client.stream("GET", f"{ORIGIN}/api/{channel}/stream",
                                       headers={**creds.headers(), "Accept": "text/event-stream"}) as resp:
                        if resp.status_code in (401, 403):
                            creds.renew_jwt(); continue
                        for msg in _iter_sse_lines(resp):
                            try:
                                q.put((channel, json.loads(msg["data"])))
                            except (json.JSONDecodeError, TypeError):
                                pass
            except Exception:
                time.sleep(2)

    for ch in ("odds", "scores"):
        threading.Thread(target=pump, args=(ch,), daemon=True).start()
    while True:
        yield q.get()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", nargs="*", help="glob(s) of hist/recorded jsonl; else --live")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--speed", type=float, default=60.0)
    ap.add_argument("--take", type=float, default=8.0)
    ap.add_argument("--stop", type=float, default=-15.0)
    ap.add_argument("--stake", type=float, default=100.0)
    ap.add_argument("--out", default="data")
    ap.add_argument("--inject-stale", action="store_true", help="demo: inject one stale packet mid-stream")
    ap.add_argument("--confirm-stop-s", type=int, default=240,
                    help="stop only if a score event occurred within this window (0 = always stop)")
    ap.add_argument("--exec-mode", choices=["simulated", "instant"], default="simulated")
    ap.add_argument("--exec-latency-ms", type=int, default=800)
    ap.add_argument("--exec-slippage-bps", type=int, default=75)
    ap.add_argument("--exec-fill-prob", type=float, default=0.92)
    ap.add_argument("--exec-liquidity", type=float, default=5000.0)
    ap.add_argument("--exec-seed", type=int, default=7)
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(exist_ok=True)
    anchor = Anchor()
    print(f"[agent] anchor: {'ON pubkey=' + anchor.pubkey if anchor.enabled else getattr(anchor, 'reason', 'off')}")
    exec_cfg = ExecConfig(latency_ms=args.exec_latency_ms, max_slippage_bps=args.exec_slippage_bps,
                          fill_probability=args.exec_fill_prob, max_leg_liquidity=args.exec_liquidity,
                          seed=args.exec_seed)
    desk = Desk(out, args.take, args.stop, args.stake, anchor, stream_clock=bool(args.replay),
                confirm_stop_ms=args.confirm_stop_s * 1000 if args.confirm_stop_s > 0 else 0,
                exec_cfg=exec_cfg, exec_mode=args.exec_mode)
    src = replay_rows(args.replay, args.speed, args.inject_stale) if args.replay else live_rows()
    print(f"[agent] running ({'replay' if args.replay else 'LIVE'}); take={args.take} stop={args.stop}")
    for chan, payload in src:
        if chan == "odds":
            u = parse_odds(payload)
            if u:
                desk.on_odds(u)
        else:
            s = parse_score(payload)
            if s:
                desk.on_score(s)
    print("[agent] stream ended")
    for fid, st in desk.positions.items():
        print(f"[agent] still open: fx={fid} {st['names'][st['pos'].outcome]}")
    for fid in desk.pending:
        print(f"[agent] order pending at stream end: fx={fid}")


if __name__ == "__main__":
    main()
