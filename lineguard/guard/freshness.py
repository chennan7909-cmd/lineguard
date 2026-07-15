"""The Guard — data-integrity gate in front of every decision.

TxLINE anchors every packet on Solana; LineGuard completes the loop on the
consumer side: no decision executes against data that fails these checks.
This mirrors HedgeGuard's Chainlink gate (PolyHedge, Base Sepolia) — the
validation is a hard gate, not a log line.

Checks on every odds update:
  G1 freshness      now - Ts <= max_age (default 90s; replay mode uses the
                    stream's own clock so recorded data validates identically)
  G2 demargin       |sum(Pct) - 1| <= tol (feed is demargined; a broken sum
                    means corrupted or foreign data)
  G3 consistency    Pct[i] ~ 1/odds[i] for every outcome (the two encodings
                    must agree — catches partial/garbled packets)
  G4 sanity         all probs in (0,1), odds > 1, 3 outcomes for 1X2
  G5 monotone time  Ts must not run backwards per fixture/market
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    code: str          # "PASS" or the failed check id
    detail: str = ""


class FreshnessGuard:
    def __init__(self, max_age_ms: int = 90_000, demargin_tol: float = 0.01,
                 consistency_tol: float = 0.02, stream_clock: bool = False,
                 out_of_order_tol_ms: int = 2_000):
        self.max_age_ms = max_age_ms
        self.demargin_tol = demargin_tol
        self.consistency_tol = consistency_tol
        self.stream_clock = stream_clock   # replay mode: compare against max Ts seen
        self.out_of_order_tol_ms = out_of_order_tol_ms  # live feeds interleave with ms-level jitter
        self._max_ts: dict = {}
        self._last_ts: dict = {}

    def check(self, u) -> GuardVerdict:
        key = (u.fixture_id, u.market)
        now_ms = self._max_ts.get("__global__", 0) if self.stream_clock else int(time.time() * 1000)
        self._max_ts["__global__"] = max(self._max_ts.get("__global__", 0), u.ts_ms)

        if len(u.probs) != 3 or len(u.decimal_odds) != 3:
            return GuardVerdict(False, "G4", f"expected 3 outcomes, got {len(u.probs)}")
        if any(not (0.0 < p < 1.0) for p in u.probs) or any(o <= 1.0 for o in u.decimal_odds):
            return GuardVerdict(False, "G4", "prob/odds out of range")
        if not self.stream_clock and now_ms - u.ts_ms > self.max_age_ms:
            return GuardVerdict(False, "G1", f"stale by {(now_ms - u.ts_ms)/1000:.1f}s")
        if self.stream_clock and now_ms - u.ts_ms > self.max_age_ms:
            return GuardVerdict(False, "G1", f"stale vs stream clock by {(now_ms - u.ts_ms)/1000:.1f}s")
        s = sum(u.probs)
        if abs(s - 1.0) > self.demargin_tol:
            return GuardVerdict(False, "G2", f"prob sum {s:.4f} outside demargined band")
        for p, o in zip(u.probs, u.decimal_odds):
            if abs(p - 1.0 / o) > self.consistency_tol:
                return GuardVerdict(False, "G3", f"Pct {p:.4f} vs 1/odds {1/o:.4f} disagree")
        last = self._last_ts.get(key)
        if last is not None and last - u.ts_ms > self.out_of_order_tol_ms:
            return GuardVerdict(False, "G5", f"time ran backwards {last} -> {u.ts_ms} "
                                             f"(> {self.out_of_order_tol_ms}ms tolerance)")
        self._last_ts[key] = max(u.ts_ms, last or 0)
        return GuardVerdict(True, "PASS")
