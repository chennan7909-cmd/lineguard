"""Sharp-movement detector with score-event attribution.

Per (fixture, outcome) we keep a rolling window of 1X2 implied probabilities.
A signal fires only when BOTH hold (double gate — large relative to the
market's own recent noise AND large absolutely):

    |z| = |latest - baseline_mean| / baseline_std  >= z_min      (default 2.5)
    |latest - baseline_mean|                       >= delta_min  (default 0.04)

plus: shortening side only (prob rising), and a per-key cooldown so one
sustained move emits one signal.

Attribution: a signal within `event_window_s` after a score event (goal,
card, etc.) is tagged event_driven=True. Un-attributed ("sharp") signals are
the interesting ones: the market moved without a visible on-pitch cause.
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Signal:
    fixture_id: int
    ts_ms: int
    outcome: int
    outcome_name: str
    prob_before: float
    prob_after: float
    z: float
    in_running: bool
    event_driven: bool
    odds_at_signal: tuple


@dataclass
class DetectorConfig:
    lookback_ms: int = 15 * 60_000
    z_min: float = 2.5
    delta_min: float = 0.04
    cooldown_ms: int = 10 * 60_000
    min_window: int = 6
    event_window_ms: int = 120_000


class MovementDetector:
    def __init__(self, cfg: DetectorConfig | None = None):
        self.cfg = cfg or DetectorConfig()
        self._win: dict = defaultdict(deque)          # (fid, outcome) -> deque[(ts, prob)]
        self._cooldown_until: dict = defaultdict(int)  # (fid, outcome) -> ts
        self._last_event_ts: dict = defaultdict(int)   # fid -> ts of last score event

    def note_score_event(self, fixture_id: int, ts_ms: int, action: str) -> None:
        if action not in ("connected", "disconnected", "heartbeat", ""):
            self._last_event_ts[fixture_id] = max(self._last_event_ts[fixture_id], ts_ms)

    def on_odds(self, u) -> list:
        """Feed one normalized 1X2 OddsUpdate; returns 0..n Signals."""
        out = []
        for i, prob in enumerate(u.probs):
            key = (u.fixture_id, i)
            win = self._win[key]
            cutoff = u.ts_ms - self.cfg.lookback_ms
            while win and win[0][0] < cutoff:
                win.popleft()
            baseline = [p for (_, p) in win]
            win.append((u.ts_ms, prob))
            if len(baseline) < self.cfg.min_window:
                continue
            mean = statistics.fmean(baseline)
            std = statistics.pstdev(baseline)
            delta = prob - mean
            if delta < self.cfg.delta_min:      # shortening side only
                continue
            if std < 1e-9 or abs(delta) / std < self.cfg.z_min:
                continue
            if u.ts_ms < self._cooldown_until[key]:
                continue
            self._cooldown_until[key] = u.ts_ms + self.cfg.cooldown_ms
            ev = (u.ts_ms - self._last_event_ts[u.fixture_id]) <= self.cfg.event_window_ms
            name = u.outcome_names[i] if i < len(u.outcome_names) else str(i)
            out.append(Signal(u.fixture_id, u.ts_ms, i, name, mean, prob,
                              abs(delta) / std, u.in_running, ev, u.decimal_odds))
        return out
