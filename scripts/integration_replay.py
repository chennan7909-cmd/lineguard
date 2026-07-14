"""CI integration test: synthesize a match, run the full agent in replay
mode (anchor dry), and assert the complete lifecycle appears in decisions."""
import json, os, random, subprocess, sys, tempfile
from pathlib import Path

random.seed(3)
d = Path(tempfile.mkdtemp())
rows, p, t = [], [0.44, 0.28, 0.28], 1_783_000_000_000
for step in range(200):
    t += 60_000
    if step == 100:
        p = [0.56, 0.22, 0.22]
    q = [max(.005, x + random.gauss(0, .002)) for x in p]
    s = sum(q); q = [x / s for x in q]
    rows.append({"recv_ts": 0, "payload": {
        "FixtureId": 777, "MessageId": f"m{step}", "Ts": t,
        "SuperOddsType": "1X2_PARTICIPANT_RESULT",
        "PriceNames": ["part1", "draw", "part2"],
        "Prices": [round(1000 / x) for x in q], "Pct": [f"{x*100:.3f}" for x in q],
        "InRunning": step > 20}})
(d / "hist_odds_777_t.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

env = {**os.environ, "ANCHOR_MODE": "dry", "SOLANA_WALLET": "/nonexistent"}
subprocess.run([sys.executable, "-m", "lineguard.agent",
                "--replay", str(d / "hist_odds_777_t.jsonl"),
                "--speed", "0", "--out", str(d), "--inject-stale"],
               check=True, env=env, timeout=120)
acts = [json.loads(l)["action"] for l in open(d / "decisions.jsonl")]
for expected in ("OPEN", "REJECT_G1", "PROPOSED", "SUBMITTED", "RECONCILED"):
    assert expected in acts, f"{expected} missing: {acts}"
print("integration replay OK:", acts)
