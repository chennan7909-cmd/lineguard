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
