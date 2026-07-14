from lineguard.guard.freshness import FreshnessGuard
from lineguard.txline.normalize import parse_odds

BASE = {"FixtureId": 1, "MessageId": "m1", "Ts": 0, "SuperOddsType": "1X2_PARTICIPANT_RESULT",
        "PriceNames": ["part1", "draw", "part2"], "Prices": [1862, 3793, 5016],
        "Pct": ["53.706", "26.364", "19.936"], "InRunning": True}


def mk(**over):
    d = dict(BASE)
    d.update(over)
    return parse_odds(d)


def test_pass_and_stale():
    g = FreshnessGuard(stream_clock=True)
    assert g.check(mk(Ts=1_000_000)).ok
    v = g.check(mk(Ts=1_000_000 - 200_000, MessageId="m2"))   # 200s older than stream clock
    assert not v.ok and v.code == "G1"


def test_demargin_break():
    g = FreshnessGuard(stream_clock=True)
    v = g.check(mk(Pct=["60.0", "30.0", "20.0"]))   # sums to 110
    assert not v.ok and v.code == "G2"


def test_consistency_break():
    g = FreshnessGuard(stream_clock=True)
    v = g.check(mk(Prices=[2500, 3793, 5016]))       # odds changed, Pct not
    assert not v.ok and v.code == "G3"


def test_time_monotone():
    g = FreshnessGuard(stream_clock=True)
    assert g.check(mk(Ts=2_000_000)).ok
    v = g.check(mk(Ts=1_500_000, MessageId="m3"))
    assert not v.ok and v.code in ("G1", "G5")
