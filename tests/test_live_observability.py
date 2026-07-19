import pytest
import re

from lineguard import agent
from lineguard.guard.freshness import GuardDiagnostics
from lineguard.signal.detector import SignalCandidate
from lineguard.txline.normalize import parse_score
from tests.test_signal import BASE
from tests._helpers import mk_desk, mk_odds


class FakeCreds:
    jwt = "jwt_secret_value"

    def __init__(self):
        self.renewed = 0

    def headers(self):
        return {
            "Authorization": "Bearer jwt_secret_value",
            "X-Api-Token": "api_token_secret",
        }

    def renew_jwt(self):
        self.renewed += 1
        self.jwt = "jwt_secret_value_renewed"
        return self.jwt


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else []
        self.headers = headers or {"content-type": "text/event-stream"}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_successful_connection_state():
    obs = agent.LiveObserver(display="compact")
    resp = FakeResponse(200)
    agent._validate_sse_response(resp)
    obs.set_state("odds_sse", "CONNECTED")
    assert obs.snapshot()["odds_sse"] == "CONNECTED"


def test_empty_fixture_snapshot(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return FakeResponse(200, [])

    monkeypatch.setattr(agent.httpx, "Client", FakeClient)
    obs = agent.LiveObserver(display="compact")
    rows = agent._fetch_fixture_snapshot(FakeCreds(), obs)
    snap = obs.snapshot()
    assert rows == []
    assert snap["fixture_snapshot"] == "EMPTY"
    assert snap["active_fixtures"] == 0
    assert obs.agent_status() == "WAITING FOR ACTIVE FIXTURES"


def test_authentication_failure(monkeypatch, capsys):
    def fail():
        raise SystemExit("No API token: api_token_secret")

    monkeypatch.setattr(agent, "get_credentials", fail)
    obs = agent.LiveObserver(display="debug")
    with pytest.raises(SystemExit):
        next(agent.live_rows(obs))
    out = capsys.readouterr().out
    assert "txline_auth=FAILED" in out
    assert "api_token_secret" not in out


def test_sse_connection_failure():
    obs = agent.LiveObserver(display="compact")
    with pytest.raises(RuntimeError):
        agent._validate_sse_response(FakeResponse(500, headers={"content-type": "text/event-stream"}))
    obs.set_state("odds_sse", "FAILED")
    assert obs.snapshot()["odds_sse"] == "FAILED"


def test_heartbeat_formatting():
    obs = agent.LiveObserver(display="compact")
    obs.set_state("odds_sse", "CONNECTED")
    obs.set_state("scores_sse", "RETRYING")
    obs.active_fixtures.add(18237038)
    obs.last_odds_at = 100.0
    line = obs.heartbeat_line(now=115.0)
    assert line == (
        "[heartbeat] odds=CONNECTED scores=RETRYING fixtures=1 "
        "last_odds=15s last_score=never"
    )


def test_secrets_not_appearing_in_logs(monkeypatch, capsys):
    monkeypatch.setenv("TXLINE_API_TOKEN", "api_token_secret")
    monkeypatch.setenv("SOME_JWT", "jwt_secret_value")
    obs = agent.LiveObserver(display="debug")
    obs.set_state(
        "odds_sse",
        "FAILED",
        "Authorization: Bearer jwt_secret_value X-Api-Token=api_token_secret",
    )
    out = capsys.readouterr().out
    assert "jwt_secret_value" not in out
    assert "api_token_secret" not in out
    assert "Bearer [redacted]" in out


def test_compact_observability_formats_guard_and_signal_fields():
    guard = GuardDiagnostics(
        freshness=True,
        demargin_consistency=False,
        price_consistency=True,
        range_sanity=False,
        timestamp_monotonic=True,
    )
    candidate = SignalCandidate(
        fixture_id=1,
        ts_ms=1_000,
        outcome=0,
        outcome_name="part1",
        prob_before=0.4400,
        prob_after=0.48444,
        probability_move=0.04444,
        z=2.567,
        recent_score_event=True,
        fired=True,
    )
    out = agent.format_compact_observability(guard, candidate)
    assert "Freshness             PASS" in out
    assert re.search(r"Demargin consistency\s+FAIL", out)
    assert re.search(r"Price consistency\s+PASS", out)
    assert re.search(r"Range sanity\s+FAIL", out)
    assert re.search(r"Timestamp monotonic\s+PASS", out)
    assert re.search(r"Probability move\s+\+0.0444", out)
    assert re.search(r"Rolling z-score\s+2.57", out)
    assert re.search(r"Recent score event\s+YES", out)
    assert re.search(r"Classification\s+EVENT-DRIVEN", out)


def test_compact_observability_classifies_non_score_sharp():
    candidate = SignalCandidate(1, 1_000, 0, "part1", 0.44, 0.53, 0.09, 44.0, False, True)
    out = agent.format_compact_observability(
        GuardDiagnostics(True, True, True, True, True),
        candidate,
    )
    assert re.search(r"Recent score event\s+NO", out)
    assert re.search(r"Classification\s+NON-SCORE SHARP", out)


def test_compact_observability_classifies_no_signal():
    candidate = SignalCandidate(1, 1_000, 0, "part1", 0.44, 0.45, 0.01, 1.2, False, False)
    out = agent.format_compact_observability(
        GuardDiagnostics(True, True, True, True, True),
        candidate,
    )
    assert re.search(r"Probability move\s+\+0.0100", out)
    assert re.search(r"Rolling z-score\s+1.20", out)
    assert re.search(r"Classification\s+NO SIGNAL", out)


def _warm_compact_desk(desk, fid=77, t0=1_000_000):
    for k, probs in enumerate(BASE):
        desk.on_odds(mk_odds(fid, t0 + k * 60_000, probs, f"w{k}"))


def test_compact_candidate_without_desk_decision_stays_quiet(tmp_path, capsys):
    desk = mk_desk(tmp_path, display="compact")
    t0 = 1_000_000
    for k, probs in enumerate(BASE):
        desk.on_odds(mk_odds(70, t0 + k * 60_000, probs, f"cold{k}", in_running=False))
    desk.on_odds(mk_odds(70, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "cold_spike", in_running=False))
    out = capsys.readouterr().out
    assert "DATA GUARD" not in out
    assert "SIGNAL" not in out
    assert "[desk]" not in out


def test_compact_replay_prints_non_score_sharp_from_actual_detector(tmp_path, capsys):
    desk = mk_desk(tmp_path, display="compact")
    t0 = 1_000_000
    _warm_compact_desk(desk, fid=77, t0=t0)
    desk.on_odds(mk_odds(77, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike"))
    out = capsys.readouterr().out
    assert out.count("DATA GUARD") == 1
    assert out.index("Classification") < out.index("[desk] PROPOSED")
    assert "DATA GUARD" in out
    assert "SIGNAL" in out
    assert re.search(r"Probability move\s+\+0.0895", out)
    assert "Rolling z-score" in out
    assert re.search(r"Recent score event\s+NO", out)
    assert re.search(r"Classification\s+NON-SCORE SHARP", out)


def test_compact_replay_prints_event_driven_from_actual_detector(tmp_path, capsys):
    desk = mk_desk(tmp_path, display="compact")
    t0 = 1_000_000
    _warm_compact_desk(desk, fid=88, t0=t0)
    desk.on_score(parse_score({"FixtureId": 88, "Ts": t0 + 330_000, "Seq": 1,
                               "Action": "goal", "GameState": "running"}))
    desk.on_odds(mk_odds(88, t0 + 6 * 60_000, (0.53, 0.24, 0.23), "spike"))
    out = capsys.readouterr().out
    assert out.count("DATA GUARD") == 1
    assert out.index("Classification") < out.index("[desk] PROPOSED")
    assert re.search(r"Recent score event\s+YES", out)
    assert re.search(r"Classification\s+EVENT-DRIVEN", out)


def test_compact_observability_does_not_leak_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TXLINE_API_TOKEN", "api_token_secret")
    desk = mk_desk(tmp_path, display="compact")
    _warm_compact_desk(desk, fid=99)
    desk.on_odds(mk_odds(99, 1_360_000, (0.53, 0.24, 0.23), "spike"))
    out = capsys.readouterr().out
    assert "api_token_secret" not in out
