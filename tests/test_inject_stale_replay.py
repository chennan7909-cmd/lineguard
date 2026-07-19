import json
import re

from lineguard import agent
from lineguard.txline.normalize import parse_odds
from tests._helpers import decisions, mk_desk


def raw_odds(ts, mid, probs=(0.50, 0.25, 0.25), in_running=True):
    return {
        "FixtureId": 7001,
        "MessageId": mid,
        "Ts": ts,
        "SuperOddsType": "1X2_PARTICIPANT_RESULT",
        "PriceNames": ["part1", "draw", "part2"],
        "Prices": [round(1000 / p) for p in probs],
        "Pct": [f"{p * 100:.3f}" for p in probs],
        "InRunning": in_running,
    }


def write_replay(tmp_path, payloads):
    path = tmp_path / "hist_odds_7001_test.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for payload in payloads:
            fh.write(json.dumps({"payload": payload}) + "\n")
    return path


def test_inject_stale_inserts_one_real_fixture_odds_packet(tmp_path):
    source = [
        raw_odds(1_000_000, "normal-1"),
        raw_odds(1_060_000, "normal-2"),
    ]
    path = write_replay(tmp_path, source)

    rows = list(agent.replay_rows([str(path)], speed=0, inject_stale=True))

    assert len(rows) == 3
    assert rows[0] == ("odds", source[0])
    assert rows[2] == ("odds", source[1])
    stale = rows[1][1]
    assert stale["FixtureId"] == source[0]["FixtureId"]
    assert stale["Prices"] == source[0]["Prices"]
    assert stale["Pct"] == source[0]["Pct"]
    assert stale["MessageId"].startswith("INJECTED_STALE_DEMO:")
    assert source[0]["Ts"] - stale["Ts"] > 90_000


def test_replay_without_inject_stale_is_unchanged(tmp_path):
    source = [
        raw_odds(1_000_000, "normal-1"),
        raw_odds(1_060_000, "normal-2"),
    ]
    path = write_replay(tmp_path, source)

    rows = list(agent.replay_rows([str(path)], speed=0, inject_stale=False))

    assert rows == [("odds", source[0]), ("odds", source[1])]


def test_injected_stale_reaches_real_guard_and_does_not_trade(tmp_path, capsys):
    source = [
        raw_odds(1_000_000, "normal-1"),
        raw_odds(1_060_000, "normal-2"),
    ]
    path = write_replay(tmp_path, source)
    desk = mk_desk(tmp_path, display="compact")
    original_check = desk.guard.check
    checked_message_ids = []
    stale_guard_result = {}

    def check_spy(update):
        checked_message_ids.append(update.message_id)
        verdict = original_check(update)
        if update.message_id.startswith("INJECTED_STALE_DEMO:"):
            stale_guard_result["verdict"] = verdict
            stale_guard_result["diagnostics"] = desk.guard.last_diagnostics
        return verdict

    desk.guard.check = check_spy
    position_after_first = None
    for idx, (chan, payload) in enumerate(agent.replay_rows([str(path)], speed=0, inject_stale=True)):
        assert chan == "odds"
        desk.on_odds(parse_odds(payload))
        if idx == 0:
            position_after_first = desk._position_state_signature()

    out = capsys.readouterr().out
    ds = decisions(tmp_path)
    stale_decision = next(d for d in ds if d["action"] == "REJECT_G1")

    assert any(mid.startswith("INJECTED_STALE_DEMO:") for mid in checked_message_ids)
    assert stale_guard_result["verdict"].code == "G1"
    assert stale_guard_result["diagnostics"].freshness is False
    assert "stale vs stream clock" in stale_decision["detail"]
    assert "STALE INPUT REFUSED" in out
    assert re.search(r"Guard check\s+Freshness", out)
    assert re.search(r"Decision\s+REFUSED", out)
    assert re.search(r"Position state\s+UNCHANGED", out)
    assert re.search(r"Orders submitted\s+0", out)
    assert desk._position_state_signature() == position_after_first
    assert "SUBMITTED" not in [d["action"] for d in ds]


def test_injected_stale_refusal_does_not_leak_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TXLINE_API_TOKEN", "api_token_secret")
    path = write_replay(tmp_path, [raw_odds(1_000_000, "normal-1")])
    desk = mk_desk(tmp_path, display="compact")

    for chan, payload in agent.replay_rows([str(path)], speed=0, inject_stale=True):
        assert chan == "odds"
        desk.on_odds(parse_odds(payload))

    out = capsys.readouterr().out
    assert "api_token_secret" not in out
