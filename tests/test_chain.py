"""Chain layer: canonical hashing, retry idempotency, dry-run isolation."""
import json
import os
import tempfile

from lineguard.chain.anchor import Anchor, canonical_hash


def test_canonical_hash_deterministic_no_duplicate_records():
    d = {"action": "LOCK", "fixture": 1, "ts": 5, "x": [1, 2]}
    assert canonical_hash(d) == canonical_hash(json.loads(json.dumps(d)))
    assert canonical_hash(d) != canonical_hash({**d, "ts": 6})


def test_retry_idempotent_same_decision_same_hash():
    d = {"action": "LOCK", "fixture": 7, "ts": 99}
    assert len({canonical_hash(d) for _ in range(5)}) == 1   # retries never fork the record


def test_dry_run_signs_but_never_sends():
    from solders.keypair import Keypair
    kp = Keypair()
    p = tempfile.mktemp()
    open(p, "w").write(json.dumps(list(bytes(kp))))
    os.environ["SOLANA_WALLET"] = p
    os.environ["ANCHOR_MODE"] = "dry"
    import importlib
    import lineguard.chain.anchor as m
    importlib.reload(m)
    res = m.Anchor().anchor({"action": "OPEN", "fixture": 1, "ts": 2})
    assert res["sig"] == "DRY_RUN" and res["hash"]
    os.environ.pop("ANCHOR_MODE"); os.environ.pop("SOLANA_WALLET")
    importlib.reload(m)


def test_borsh_encoder_matches_idl_layout():
    from lineguard.chain.borsh_min import encode
    assert encode("u16", 20624) == (20624).to_bytes(2, "little")
    assert encode("string", "ab") == b"\x02\x00\x00\x00ab"
    assert encode({"option": "string"}, None) == b"\x00"
    assert encode({"vec": "i32"}, [1862]) == b"\x01\x00\x00\x00" + (1862).to_bytes(4, "little", signed=True)


def test_g6_failure_quarantines_fixture(tmp_path):
    from tests._helpers import decisions, mk_desk, mk_odds
    desk = mk_desk(tmp_path, verifier=lambda fid, ts, mid: {"ok": False, "err": "root mismatch"})
    desk.on_odds(mk_odds(5, 1_000_000, (0.44, 0.28, 0.28), "q1"))
    desk.on_odds(mk_odds(5, 1_060_000, (0.58, 0.22, 0.20), "q2"))
    acts = [d["action"] for d in decisions(tmp_path)]
    assert "REJECT_G6" in acts and "OPEN" not in acts


def test_g6_pass_verifies_once_then_opens(tmp_path):
    from tests._helpers import decisions, mk_desk, mk_odds
    calls = []
    desk = mk_desk(tmp_path, verifier=lambda fid, ts, mid: (calls.append(fid) or {"ok": True, "units": 1_390_000, "pda": "F2k9"}))
    desk.on_odds(mk_odds(6, 1_000_000, (0.44, 0.28, 0.28), "v1"))
    desk.on_odds(mk_odds(6, 1_060_000, (0.45, 0.275, 0.275), "v2"))
    acts = [d["action"] for d in decisions(tmp_path)]
    assert acts.count("G6_VERIFIED") == 1 and "OPEN" in acts and calls == [6]
