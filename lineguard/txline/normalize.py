"""Normalize raw TxLINE payloads into typed records.

Verified against real devnet payloads (2026-07-13):
  odds:   Prices = decimal odds x1000; Pct = implied prob strings summing ~100
          (feed is DEMARGINED — bookmaker 'TXLineStablePriceDemargined');
          SuperOddsType '1X2_PARTICIPANT_RESULT' is the 3-outcome match market;
          MessageId unique -> dedup key; Ts in ms; InRunning bool.
  scores: Action ('connected', goal events, 'game_finalised'), Seq, GameState.
"""
from __future__ import annotations

from dataclasses import dataclass

MARKET_1X2 = "1X2_PARTICIPANT_RESULT"


@dataclass(frozen=True)
class OddsUpdate:
    fixture_id: int
    ts_ms: int
    message_id: str
    market: str
    outcome_names: tuple      # e.g. ("part1", "draw", "part2")
    decimal_odds: tuple       # e.g. (1.862, 3.793, 5.016)
    probs: tuple              # e.g. (0.53706, 0.26364, 0.19936)
    in_running: bool
    market_period: object = None
    market_parameters: object = None

    def prob_sum(self) -> float:
        return sum(self.probs)


@dataclass(frozen=True)
class ScoreEvent:
    fixture_id: int
    ts_ms: int
    seq: int | None
    action: str
    game_state: str | None
    raw: dict


def parse_odds(payload: dict) -> OddsUpdate | None:
    """Return a typed OddsUpdate, or None if the row is malformed."""
    try:
        prices = payload.get("Prices") or []
        pct = payload.get("Pct") or []
        if not prices or len(prices) != len(pct):
            return None
        return OddsUpdate(
            fixture_id=int(payload["FixtureId"]),
            ts_ms=int(payload["Ts"]),
            message_id=str(payload.get("MessageId", "")),
            market=str(payload.get("SuperOddsType", "")),
            outcome_names=tuple(payload.get("PriceNames") or ()),
            decimal_odds=tuple(p / 1000.0 for p in prices),
            probs=tuple(float(x) / 100.0 for x in pct),
            in_running=bool(payload.get("InRunning")),
            market_period=payload.get("MarketPeriod"),
            market_parameters=payload.get("MarketParameters"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_score(payload: dict) -> ScoreEvent | None:
    try:
        return ScoreEvent(
            fixture_id=int(payload["FixtureId"]),
            ts_ms=int(payload["Ts"]),
            seq=payload.get("Seq"),
            action=str(payload.get("Action", "")),
            game_state=payload.get("GameState"),
            raw=payload,
        )
    except (KeyError, TypeError, ValueError):
        return None
