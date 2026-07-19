"""LineGuard agent — the autonomous in-play risk desk.

Deterministic policy (no LLM in the decision path):

  1. OPEN    when a fixture goes in-running, back the market favorite with a
             fixed paper stake (the "portfolio" the desk protects).
  2. WATCH   every guarded odds update re-prices each open position via the
             one-line identity F_lock = S*(a*q_i - 1).
  3. LOCK    execute the dutch hedge (lock_profit) the moment F_lock crosses
             +take or -stop bands. Spikes are ephemeral (canonical backtest,
             see results/RESULTS.md: avg lockable P/L turns negative within 60s
             incl. 2% fill margin), so the lock is immediate and closed-form.
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
import datetime as dt
import glob
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .chain.anchor import Anchor
from .execution import ExecConfig, SimulatedExecutor
from .guard.freshness import FreshnessGuard, GuardDiagnostics
from .risk.hedge import Position, lock_profit, locked_pnl
from .signal.detector import DetectorConfig, MovementDetector, SignalCandidate
from .txline.auth import ORIGIN, get_credentials
from .txline.normalize import MARKET_1X2, parse_odds, parse_score


SECRET_KEYS = ("TOKEN", "JWT", "SECRET", "PRIVATE", "SEED", "AUTHORIZATION")
COMPACT_OBSERVABILITY_ACTIONS = {"HOLD", "PROPOSED", "REFUSED"}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fixture_id(payload: dict) -> int | None:
    for key in ("FixtureId", "fixtureId", "id", "Id"):
        try:
            if payload.get(key) is not None:
                return int(payload[key])
        except (TypeError, ValueError):
            return None
    return None


def _is_active_fixture(fx: dict) -> bool:
    if fx.get("InRunning") is True or fx.get("inRunning") is True:
        return True
    state = str(fx.get("GameState") or fx.get("gameState") or fx.get("Status") or fx.get("status") or "").lower()
    return state in {"live", "inrunning", "in_running", "running", "started", "1h", "2h", "ht", "et", "pen"}


def _fixture_rows(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("fixtures", "Fixtures", "data", "Data", "items", "Items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _secret_values() -> list[str]:
    vals = []
    for key, val in os.environ.items():
        if val and len(val) >= 6 and any(marker in key.upper() for marker in SECRET_KEYS):
            vals.append(val)
    return vals


def redact_secrets(text: object) -> str:
    out = str(text)
    for val in _secret_values():
        out = out.replace(val, "[redacted]")
    out = re.sub(r"(Authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", out, flags=re.I)
    out = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", out, flags=re.I)
    out = re.sub(r"((?:X-Api-Token|api[_ -]?token|jwt|private[_ -]?key|seed(?: phrase)?)['\"]?\s*[:=]\s*['\"]?)[^,'\"\s}]+", r"\1[redacted]", out, flags=re.I)
    return out


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _format_metric(value: float | None, places: int, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed else ""
    return f"{value:{prefix}.{places}f}"


def _signal_classification(candidate: SignalCandidate | None) -> str:
    if candidate is None or not candidate.fired:
        return "NO SIGNAL"
    return "EVENT-DRIVEN" if candidate.recent_score_event else "NON-SCORE SHARP"


def format_compact_observability(guard: GuardDiagnostics,
                                 candidate: SignalCandidate | None) -> str:
    return "\n".join([
        "DATA GUARD",
        f"{'Freshness':<22}{_pass_fail(guard.freshness)}",
        f"{'Demargin consistency':<22}{_pass_fail(guard.demargin_consistency)}",
        f"{'Price consistency':<22}{_pass_fail(guard.price_consistency)}",
        f"{'Range sanity':<22}{_pass_fail(guard.range_sanity)}",
        f"{'Timestamp monotonic':<22}{_pass_fail(guard.timestamp_monotonic)}",
        "",
        "SIGNAL",
        f"{'Probability move':<22}{_format_metric(candidate.probability_move if candidate else None, 4, True)}",
        f"{'Rolling z-score':<22}{_format_metric(candidate.z if candidate else None, 2)}",
        f"{'Recent score event':<22}{'YES' if candidate and candidate.recent_score_event else 'NO'}",
        f"{'Classification':<22}{_signal_classification(candidate)}",
    ])


def format_compact_stale_refusal(reason: str, position_unchanged: bool,
                                 orders_submitted: int) -> str:
    return "\n".join([
        "STALE INPUT REFUSED",
        f"{'Guard check':<22}Freshness",
        f"{'Reason':<22}{reason}",
        f"{'Decision':<22}REFUSED",
        f"{'Position state':<22}{'UNCHANGED' if position_unchanged else 'CHANGED'}",
        f"{'Orders submitted':<22}{orders_submitted}",
    ])


class LiveObserver:
    def __init__(self, display: str = "normal", heartbeat_s: float = 15.0):
        self.display = display
        self.heartbeat_s = heartbeat_s
        self.txline_auth = "CONNECTING"
        self.fixture_snapshot = "REQUESTING"
        self.odds_sse = "CONNECTING"
        self.scores_sse = "CONNECTING"
        self.anchor_wallet = "READY"
        self.active_fixtures: set[int] = set()
        self.open_positions = 0
        self.last_odds_at: float | None = None
        self.last_score_at: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _label(self, field: str) -> str:
        return {
            "txline_auth": "TxLINE auth",
            "fixture_snapshot": "Fixture snapshot",
            "odds_sse": "Odds SSE",
            "scores_sse": "Scores SSE",
            "anchor_wallet": "Anchor wallet",
        }.get(field, field)

    def set_anchor(self, anchor: Anchor) -> None:
        with self._lock:
            self.anchor_wallet = "READY" if anchor.enabled else "FAILED"

    def set_state(self, field: str, state: str, detail: str | None = None) -> None:
        with self._lock:
            setattr(self, field, state)
        if self.display == "debug" and detail:
            print(f"[live:debug] {field}={state} {redact_secrets(detail)}")
        if self.display == "normal":
            self.print_panel()
        elif self.display == "compact":
            print(f"{self._label(field):<21}{state}")

    def set_fixture_snapshot(self, state: str, rows=None) -> None:
        active = set()
        for fx in rows or []:
            if isinstance(fx, dict) and _is_active_fixture(fx):
                fid = _fixture_id(fx)
                if fid is not None:
                    active.add(fid)
        with self._lock:
            self.fixture_snapshot = state
            self.active_fixtures = active
        if self.display == "normal":
            self.print_panel()
        elif self.display == "compact":
            print(f"{self._label('fixture_snapshot'):<21}{state}")
            print(f"{'Active fixtures':<21}{len(active)}")
            print(f"{'Agent status':<21}{'MONITORING' if active else 'WAITING FOR ACTIVE FIXTURES'}")

    def note_event(self, channel: str, payload: dict, open_positions: int) -> None:
        fid = _fixture_id(payload)
        now = time.time()
        with self._lock:
            if channel == "odds":
                self.last_odds_at = now
                if payload.get("InRunning") is True and fid is not None:
                    self.active_fixtures.add(fid)
            else:
                self.last_score_at = now
                if fid is not None:
                    self.active_fixtures.add(fid)
            self.open_positions = open_positions
        if fid is None:
            fid = "?"
        print(f"[txline:{channel[:-1] if channel.endswith('s') else channel}] fixture={fid} received_at={_utc_now_iso()}")

    def note_open_positions(self, count: int) -> None:
        with self._lock:
            self.open_positions = count

    def agent_status(self) -> str:
        with self._lock:
            return "MONITORING" if self.active_fixtures else "WAITING FOR ACTIVE FIXTURES"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "txline_auth": self.txline_auth,
                "fixture_snapshot": self.fixture_snapshot,
                "odds_sse": self.odds_sse,
                "scores_sse": self.scores_sse,
                "anchor_wallet": self.anchor_wallet,
                "active_fixtures": len(self.active_fixtures),
                "open_positions": self.open_positions,
                "last_odds_at": self.last_odds_at,
                "last_score_at": self.last_score_at,
            }

    def print_header(self) -> None:
        print("LINEGUARD / LIVE")
        print("────────────────────────────────")
        self.print_panel()

    def print_panel(self) -> None:
        s = self.snapshot()
        rows = [
            ("TxLINE auth", s["txline_auth"]),
            ("Fixture snapshot", s["fixture_snapshot"]),
            ("Odds SSE", s["odds_sse"]),
            ("Scores SSE", s["scores_sse"]),
            ("Anchor wallet", s["anchor_wallet"]),
            ("Active fixtures", str(s["active_fixtures"])),
            ("Open positions", str(s["open_positions"])),
            ("Agent status", "MONITORING" if s["active_fixtures"] else "WAITING FOR ACTIVE FIXTURES"),
        ]
        for label, value in rows:
            print(f"{label:<21}{value}")

    def heartbeat_line(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        s = self.snapshot()

        def age(ts):
            return "never" if ts is None else f"{max(0, int(now - ts))}s"

        return (
            f"[heartbeat] odds={s['odds_sse']} scores={s['scores_sse']} "
            f"fixtures={s['active_fixtures']} last_odds={age(s['last_odds_at'])} "
            f"last_score={age(s['last_score_at'])}"
        )

    def start_heartbeat(self) -> None:
        def run():
            while not self._stop.wait(self.heartbeat_s):
                print(self.heartbeat_line())
        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class Desk:
    def __init__(self, out: Path, take: float, stop: float, stake: float, anchor: Anchor,
                 stream_clock: bool, confirm_stop_ms: int = 240_000,
                 exec_cfg: ExecConfig | None = None, exec_mode: str = "simulated",
                 verifier=None, display: str = "normal"):
        self.guard = FreshnessGuard(stream_clock=stream_clock)
        self.detector = MovementDetector(DetectorConfig())
        self.anchor = anchor
        self.take, self.stop, self.stake = take, stop, stake
        self.confirm_stop_ms = confirm_stop_ms
        self._held_through: dict = {}
        self.exec_mode = exec_mode
        self.display = display
        self.executor = SimulatedExecutor(exec_cfg)
        self.pending: dict = {}       # fixture -> Order
        self._repropose_after: dict = {}
        self.verifier = verifier          # callable(fixture_id, ts, message_id) -> verdict dict
        self._g6: dict = {}               # fixture -> True/False (cached spot-check)
        self._reject_last: dict = {}      # (fixture, code) -> last anchored ts
        self._reject_suppressed: dict = {}
        self.positions: dict = {}      # fixture -> {pos, opened_ts, names}
        self.locked: dict = {}         # fixture -> plan summary
        self.out = out
        self.fh = open(out / "decisions.jsonl", "a", encoding="utf-8")
        self.seen: set = set()
        self._compact_observed: set = set()
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
        act = decision.get("action", "")
        if act.startswith("REJECT"):
            key = (decision.get("fixture"), act)
            now = decision.get("ts", 0)
            last = self._reject_last.get(key)
            if last is not None and now - last < 60_000:
                self._reject_suppressed[key] = self._reject_suppressed.get(key, 0) + 1
                decision["anchor"] = {"hash": None, "sig": None, "mode": "rate_limited",
                                      "suppressed_in_window": self._reject_suppressed[key]}
                self.fh.write(json.dumps(decision, ensure_ascii=False) + "\n")
                self.fh.flush()
                return
            self._reject_last[key] = now
            self._reject_suppressed[key] = 0
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

    def _candidate_for(self, outcome: int | None) -> SignalCandidate | None:
        candidates = self.detector.last_candidates
        if outcome is not None:
            for candidate in candidates:
                if candidate.outcome == outcome:
                    return candidate
        fired = [candidate for candidate in candidates if candidate.fired]
        if fired:
            return max(fired, key=lambda candidate: candidate.z)
        if candidates:
            return max(candidates, key=lambda candidate: candidate.z)
        return None

    def _print_compact_observability(self, candidate: SignalCandidate | None) -> None:
        if self.display != "compact":
            return
        key = None
        if candidate is not None:
            key = (candidate.fixture_id, candidate.ts_ms, candidate.outcome)
            if key in self._compact_observed:
                return
            self._compact_observed.add(key)
        print(format_compact_observability(self.guard.last_diagnostics, candidate))

    def _decide_with_compact_observability(self, decision: dict,
                                           candidate: SignalCandidate | None) -> None:
        if decision.get("action") in COMPACT_OBSERVABILITY_ACTIONS:
            self._print_compact_observability(candidate)
        self.decide(decision)

    def _position_state_signature(self) -> tuple:
        positions = tuple(sorted(
            (fid, st["pos"].outcome, st["pos"].stake, st["pos"].entry_odds)
            for fid, st in self.positions.items()
        ))
        pending = tuple(sorted(self.pending.keys()))
        locked = tuple(sorted(
            (fid, tuple(sorted(summary.items())))
            for fid, summary in self.locked.items()
        ))
        return positions, pending, locked

    def _print_compact_stale_refusal(self, reason: str, state_before: tuple,
                                     pending_before: int) -> None:
        if self.display != "compact":
            return
        print(format_compact_stale_refusal(
            reason,
            self._position_state_signature() == state_before,
            max(0, len(self.pending) - pending_before),
        ))

    def on_score(self, s):
        self.detector.note_score_event(s.fixture_id, s.ts_ms, s.action)

    def on_odds(self, u):
        if u.market != MARKET_1X2 or u.message_id in self.seen:
            return
        self.seen.add(u.message_id)
        state_before_guard = self._position_state_signature()
        pending_before_guard = len(self.pending)
        v = self.guard.check(u)
        if not v.ok:
            if v.code == "G1":
                self._print_compact_stale_refusal(v.detail, state_before_guard, pending_before_guard)
            self.decide({"action": f"REJECT_{v.code}", "fixture": u.fixture_id,
                         "ts": u.ts_ms, "detail": v.detail})
            return
        self.detector.on_odds(u)   # keeps attribution state warm

        fid = u.fixture_id
        if fid not in self.positions and fid not in self.locked and fid not in self.pending and u.in_running:
            if self.verifier is not None and fid not in self._g6:
                try:
                    v = self.verifier(fid, u.ts_ms, u.message_id)
                except Exception as e:
                    v = {"ok": False, "err": f"{type(e).__name__}: {e}"}
                self._g6[fid] = bool(v.get("ok"))
                if not self._g6[fid]:
                    self.decide({"action": "REJECT_G6", "fixture": fid, "ts": u.ts_ms,
                                 "verdict": v, "detail": "Merkle proof failed on-chain view — fixture quarantined"})
                else:
                    self.decide({"action": "G6_VERIFIED", "fixture": fid, "ts": u.ts_ms,
                                 "pda": v.get("pda"), "units": v.get("units"),
                                 "detail": f"packet cryptographically anchored (validate_odds view, {v.get('units')} CU)"})
            if self._g6.get(fid) is False:
                return
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
                unfilled = [l for l in order.legs if l.filled < l.requested - 1e-9]
                filled_any = any(l.filled > 1e-9 for l in order.legs)
                if unfilled and filled_any and order.rework_count < 1:
                    evs = self.executor.rework(order, u.decimal_odds, u.ts_ms)
                    for ev in evs:
                        self.decide({"action": "REWORK", "fixture": fid, "ts": u.ts_ms, **ev,
                                     "detail": f"re-working leg {ev['leg']}: {ev['remaining']} remaining "
                                               f"@ new baseline {ev['new_baseline_odds']}"})
                    return
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
                    self._decide_with_compact_observability(
                        {"action": "HOLD", "fixture": fid, "ts": u.ts_ms,
                         "detail": f"F_lock={f:+.2f} < stop but unattributed (no score event "
                                   f"within {self.confirm_stop_ms//1000}s) — spikes revert, holding"},
                        self._candidate_for(pos.outcome),
                    )
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
            self._decide_with_compact_observability(
                {"action": "PROPOSED", "fixture": fid, "ts": u.ts_ms,
                 "intent": plan.intent, "reason": reason,
                 "proposed_floor": round(plan.floor, 2),
                 "hedge_stakes": [round(h, 2) for h in plan.hedge_stakes],
                 "exec_config": self.executor.cfg.as_dict(),
                 "detail": f"F_lock={plan.floor:+.2f} ({reason}) -> hedge proposed"},
                self._candidate_for(pos.outcome),
            )
            self.decide({"action": "SUBMITTED", "fixture": fid, "ts": u.ts_ms,
                         "legs": [{"outcome": l.outcome, "stake": round(l.requested, 2)} for l in order.legs],
                         "detail": f"{len(order.legs)} leg(s) to venue (latency {self.executor.cfg.latency_ms}ms)"})


def _inject_stale_replay_packet(rows: list) -> None:
    max_age_ms = FreshnessGuard().max_age_ms
    for idx, (arrival_ts, chan, payload) in enumerate(rows):
        if chan != "odds":
            continue
        u = parse_odds(payload)
        if u is None or u.market != MARKET_1X2:
            continue
        stale = json.loads(json.dumps(payload))
        stale["Ts"] = u.ts_ms - max_age_ms - 1_000
        stale["MessageId"] = f"INJECTED_STALE_DEMO:{u.message_id}"
        rows.insert(idx + 1, (arrival_ts, chan, stale))
        return


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
        _inject_stale_replay_packet(rows)
    prev = None
    for ts, chan, payload in rows:
        real_ts = payload.get("Ts", ts)
        if speed > 0 and prev is not None and ts > prev:
            time.sleep(min((ts - prev) / 1000.0 / speed, 2.0))
        prev = ts
        yield chan, payload


def _fetch_fixture_snapshot(creds, observer: LiveObserver):
    observer.set_state("fixture_snapshot", "REQUESTING")
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{ORIGIN}/api/fixtures/snapshot", headers=creds.headers())
        if r.status_code in (401, 403):
            creds.renew_jwt()
            r = client.get(f"{ORIGIN}/api/fixtures/snapshot", headers=creds.headers())
        if r.status_code != 200:
            observer.set_state("fixture_snapshot", "FAILED", f"status={r.status_code}")
            return []
        data = r.json()
        rows = _fixture_rows(data)
        observer.set_fixture_snapshot("OK" if rows else "EMPTY", rows)
        return rows


def _validate_sse_response(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise PermissionError(str(resp.status_code))
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if ctype and "text/event-stream" not in ctype.lower():
        raise RuntimeError(f"unexpected content-type {ctype}")


def live_rows(observer: LiveObserver | None = None):
    from .txline.recorder import _iter_sse_lines
    import threading, queue
    observer = observer or LiveObserver()
    observer.set_state("txline_auth", "CONNECTING")
    try:
        creds = get_credentials()
    except BaseException as e:
        observer.set_state("txline_auth", "FAILED", f"{type(e).__name__}: {e}")
        raise
    try:
        creds.headers()
    except Exception as e:
        observer.set_state("txline_auth", "FAILED", f"{type(e).__name__}: {e}")
        raise
    observer.set_state("txline_auth", "OK")
    try:
        _fetch_fixture_snapshot(creds, observer)
    except Exception as e:
        observer.set_state("fixture_snapshot", "FAILED", f"{type(e).__name__}: {e}")
    q: "queue.Queue" = queue.Queue()

    def pump(channel):
        field = "odds_sse" if channel == "odds" else "scores_sse"
        while True:
            try:
                observer.set_state(field, "CONNECTING")
                with httpx.Client(timeout=httpx.Timeout(10, read=120)) as client:
                    with client.stream("GET", f"{ORIGIN}/api/{channel}/stream",
                                       headers={**creds.headers(), "Accept": "text/event-stream"}) as resp:
                        try:
                            _validate_sse_response(resp)
                        except PermissionError:
                            creds.renew_jwt()
                            observer.set_state(field, "RETRYING", f"status={resp.status_code} jwt_renewed=true")
                            continue
                        observer.set_state(field, "CONNECTED", f"status={resp.status_code}")
                        for msg in _iter_sse_lines(resp):
                            try:
                                payload = json.loads(msg["data"])
                                q.put((channel, payload))
                            except (json.JSONDecodeError, TypeError):
                                pass
            except Exception as e:
                observer.set_state(field, "FAILED", f"{type(e).__name__}: {e}")
                observer.set_state(field, "RETRYING")
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
    ap.add_argument("--display", choices=["normal", "compact", "debug"], default="normal")
    ap.add_argument("--verify-anchor", action="store_true",
                    help="G6: cryptographic Merkle spot-check per fixture before opening (validate_odds on-chain view)")
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(exist_ok=True)
    anchor = Anchor()
    observer = LiveObserver(args.display) if not args.replay else None
    if observer:
        observer.set_anchor(anchor)
        observer.print_header()
    else:
        print(f"[agent] anchor: {'ON pubkey=' + anchor.pubkey if anchor.enabled else getattr(anchor, 'reason', 'off')}")
    exec_cfg = ExecConfig(latency_ms=args.exec_latency_ms, max_slippage_bps=args.exec_slippage_bps,
                          fill_probability=args.exec_fill_prob, max_leg_liquidity=args.exec_liquidity,
                          seed=args.exec_seed)
    verifier = None
    if args.verify_anchor:
        from .chain.verify import verify_update
        creds_v = get_credentials()
        verifier = lambda fid, ts, mid: verify_update(creds_v, fixture_id=fid, ts=ts, message_id=mid)
    desk = Desk(out, args.take, args.stop, args.stake, anchor, stream_clock=bool(args.replay),
                confirm_stop_ms=args.confirm_stop_s * 1000 if args.confirm_stop_s > 0 else 0,
                exec_cfg=exec_cfg, exec_mode=args.exec_mode, verifier=verifier,
                display=args.display)
    if observer:
        observer.note_open_positions(len(desk.positions) + len(desk.pending))
        observer.start_heartbeat()
    src = replay_rows(args.replay, args.speed, args.inject_stale) if args.replay else live_rows(observer)
    print(f"[agent] running ({'replay' if args.replay else 'LIVE'}); take={args.take} stop={args.stop}")
    for chan, payload in src:
        if observer:
            observer.note_event(chan, payload, len(desk.positions) + len(desk.pending))
        if chan == "odds":
            u = parse_odds(payload)
            if u:
                desk.on_odds(u)
        else:
            s = parse_score(payload)
            if s:
                desk.on_score(s)
        if observer:
            observer.note_open_positions(len(desk.positions) + len(desk.pending))
    print("[agent] stream ended")
    for fid, st in desk.positions.items():
        print(f"[agent] still open: fx={fid} {st['names'][st['pos'].outcome]}")
    for fid in desk.pending:
        print(f"[agent] order pending at stream end: fx={fid}")


if __name__ == "__main__":
    main()
