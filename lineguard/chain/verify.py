"""G6 — cryptographic anchor verification (Merkle proof via on-chain view).

TxLINE anchors every odds packet under a per-day Merkle root stored on
Solana. This module completes LineGuard's slogan literally: fetch the
validation bundle for an update, rebuild the program's `validate_odds`
instruction (vendored IDL, minimal borsh), and `simulateTransaction`
against devnet. The Merkle logic runs INSIDE the Txoracle program — if the
simulation succeeds, the packet is cryptographically committed on-chain;
if anyone tampered with it, the view call fails.

    python -m lineguard.chain.verify --fixture 18237038 --ts 1784142000000
    python -m lineguard.chain.verify --message-id "1836…-stab" --ts 1784142000123

Design: heuristic checks (G1–G5) run on EVERY packet; G6 is a cryptographic
spot-check at decision boundaries (once per fixture before the desk opens a
position, via `agent --verify-anchor`), because a full proof verification
costs ~1.4M compute units — right tool, right frequency.
"""
from __future__ import annotations

import argparse
import base64
import json
import struct

import httpx

from ..txline.auth import NETWORK, NETWORKS, ORIGIN, get_credentials
from .borsh_min import encode_instruction

PROGRAM_ID = NETWORKS[NETWORK]["program_id"]
RPC = NETWORKS[NETWORK]["rpc"]

BUNDLE_ENDPOINTS = [
    "/api/odds/validation?messageId={mid}&ts={ts}",       # confirmed param names
    "/api/odds/validation?fixtureId={fid}&ts={ts}",
]
PDA_SEEDS = [b"daily_batch_roots"]                        # confirmed: docs/programs/addresses


def fetch_bundle(creds, fid=None, ts=None, message_id=None) -> dict:
    with httpx.Client(timeout=30) as client:
        last = None
        for tmpl in BUNDLE_ENDPOINTS:
            url = ORIGIN + tmpl.format(fid=fid, ts=ts, mid=message_id)
            if ("fixtureId" in tmpl and fid is None) or ("messageId" in tmpl and message_id is None):
                continue
            r = client.get(url, headers=creds.headers())
            if r.status_code == 401:
                creds.renew_jwt()
                r = client.get(url, headers=creds.headers())
            last = (url, r.status_code, r.text[:200])
            if r.status_code == 200 and r.content.strip():
                return r.json()
        raise RuntimeError(f"no validation bundle: last tried {last}")




def _payer_pubkey():
    """Fee payer for simulation must be a real funded account (sigVerify=false,
    so no signature needed — just existence). Use the agent wallet's pubkey."""
    import os
    from pathlib import Path
    from solders.keypair import Keypair
    wallet = os.environ.get("SOLANA_WALLET", str(Path.home() / "lineguard-keys" / "wallet.json"))
    raw = json.loads(Path(wallet).read_text())
    return Keypair.from_bytes(bytes(raw)).pubkey()


def _pda(seed: bytes, epoch_day: int) -> str:
    from solders.pubkey import Pubkey
    day = struct.pack("<H", epoch_day)
    addr, _ = Pubkey.find_program_address([seed, day], Pubkey.from_string(PROGRAM_ID))
    return str(addr)


def _bytes32(v) -> list:
    if isinstance(v, str):
        s = v[2:] if v.startswith("0x") else v
        try:
            return list(bytes.fromhex(s))
        except ValueError:
            import base64 as b64
            return list(b64.b64decode(s))
    return list(bytes(bytearray(v)))


def _proof(nodes) -> list:
    return [{"hash": _bytes32(n["hash"]),
             "is_right_sibling": bool(n.get("isRightSibling", n.get("is_right_sibling")))}
            for n in (nodes or [])]


def build_args(bundle: dict) -> dict:
    s = bundle.get("odds") or bundle["snapshot"]
    summ = bundle["summary"]
    root = summ.get("oddsSubTreeRoot") or summ.get("updateSubTreeRoot") or bundle.get("subTreeRoot")
    return {
        "ts": int(s["Ts"]),
        "odds_snapshot": {
            "fixture_id": int(s["FixtureId"]), "message_id": s["MessageId"],
            "ts": int(s["Ts"]), "bookmaker": s["Bookmaker"],
            "bookmaker_id": int(s["BookmakerId"]), "super_odds_type": s["SuperOddsType"],
            "game_state": s.get("GameState"), "in_running": bool(s.get("InRunning")),
            "market_parameters": s.get("MarketParameters"), "market_period": s.get("MarketPeriod"),
            "price_names": list(s.get("PriceNames") or []),
            "prices": [int(p) for p in (s.get("Prices") or [])],
        },
        "summary": {
            "fixture_id": int(summ["fixtureId"]),
            "update_stats": {
                "update_count": int(summ["updateStats"]["updateCount"]),
                "min_timestamp": int(summ["updateStats"]["minTimestamp"]),
                "max_timestamp": int(summ["updateStats"]["maxTimestamp"]),
            },
            "odds_sub_tree_root": _bytes32(root),
        },
        "sub_tree_proof": _proof(bundle["subTreeProof"]),
        "main_tree_proof": _proof(bundle["mainTreeProof"]),
    }


def simulate(args: dict, payer_pubkey: str, rpc_post=None) -> dict:
    """Build the unsigned view tx and simulate it. Returns verdict dict."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    from solders.keypair import Keypair

    data = encode_instruction("validate_odds", args)
    epoch_day = args["ts"] // 86_400_000
    last_err = None
    for seed in PDA_SEEDS:
        pda = _pda(seed, epoch_day)
        ix = Instruction(Pubkey.from_string(PROGRAM_ID), data,
                         [AccountMeta(Pubkey.from_string(pda), False, False)])
        # compute budget: 1.4M units
        cb = Instruction(Pubkey.from_string("ComputeBudget111111111111111111111111111111"),
                         bytes([2]) + struct.pack("<I", 1_400_000), [])
        bh = Hash.default()
        msg = Message.new_with_blockhash([cb, ix], _payer_pubkey(), bh)
        tx = Transaction.new_unsigned(msg)
        body = {"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
                "params": [base64.b64encode(bytes(tx)).decode(),
                           {"encoding": "base64", "sigVerify": False,
                            "replaceRecentBlockhash": True, "commitment": "confirmed"}]}
        post = rpc_post or (lambda b: httpx.post(RPC, json=b, timeout=30).json())
        res = post(body)
        val = (res.get("result") or {}).get("value") or {}
        if val.get("err") is None and "result" in res:
            return {"ok": True, "pda": pda, "seed": seed.decode(),
                    "units": val.get("unitsConsumed"), "epoch_day": epoch_day}
        last_err = {"pda": pda, "seed": seed.decode(), "err": val.get("err"),
                    "logs": (val.get("logs") or [])[-3:]}
    return {"ok": False, "epoch_day": epoch_day, **(last_err or {})}


def verify_update(creds, fixture_id=None, ts=None, message_id=None) -> dict:
    bundle = fetch_bundle(creds, fixture_id, ts, message_id)
    args = build_args(bundle)
    verdict = simulate(args, payer_pubkey="")
    verdict["message_id"] = args["odds_snapshot"]["message_id"]
    return verdict


def fetch_fixture_bundle(creds, fid: int, ts: int) -> dict:
    with httpx.Client(timeout=30) as client:
        url = f"{ORIGIN}/api/fixtures/validation?fixtureId={fid}&timestamp={ts}"
        r = client.get(url, headers=creds.headers())
        if r.status_code == 401:
            creds.renew_jwt()
            r = client.get(url, headers=creds.headers())
        r.raise_for_status()
        return r.json()


def verify_fixture(creds, fid: int, ts: int) -> dict:
    """G6 engine #2: validate_fixture — the officially-exampled devnet path."""
    b = fetch_fixture_bundle(creds, fid, ts)
    s, summ = b["snapshot"], b["summary"]
    args = {
        "snapshot": {
            "ts": int(s["Ts"]), "start_time": int(s["StartTime"]),
            "competition": s["Competition"], "competition_id": int(s["CompetitionId"]),
            "fixture_group_id": int(s["FixtureGroupId"]),
            "participant1_id": int(s["Participant1Id"]), "participant1": s["Participant1"],
            "participant2_id": int(s["Participant2Id"]), "participant2": s["Participant2"],
            "fixture_id": int(s["FixtureId"]), "participant1_is_home": bool(s["Participant1IsHome"]),
        },
        "summary": {
            "fixture_id": int(summ["fixtureId"]), "competition_id": int(summ["competitionId"]),
            "competition": summ["competition"],
            "update_stats": {
                "update_count": int(summ["updateStats"]["updateCount"]),
                "min_timestamp": int(summ["updateStats"]["minTimestamp"]),
                "max_timestamp": int(summ["updateStats"]["maxTimestamp"]),
            },
            "update_sub_tree_root": _bytes32(summ["updateSubTreeRoot"]),
        },
        "sub_tree_proof": _proof(b["subTreeProof"]),
        "main_tree_proof": _proof(b["mainTreeProof"]),
    }
    data = encode_instruction("validate_fixture", args)
    epoch_day = int(s["Ts"]) // 86_400_000
    aligned = (epoch_day // 10) * 10
    v = _simulate_ix(data, b"ten_daily_fixtures_roots", aligned)
    v["epoch_day"] = epoch_day
    v["kind"] = "fixture"
    return v


def _simulate_ix(data: bytes, seed: bytes, day: int, rpc_post=None) -> dict:
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    pda = _pda(seed, day)
    ix = Instruction(Pubkey.from_string(PROGRAM_ID), data,
                     [AccountMeta(Pubkey.from_string(pda), False, False)])
    cb = Instruction(Pubkey.from_string("ComputeBudget111111111111111111111111111111"),
                     bytes([2]) + struct.pack("<I", 1_400_000), [])
    bh = Hash.default()
    msg = Message.new_with_blockhash([cb, ix], _payer_pubkey(), bh)
    tx = Transaction.new_unsigned(msg)
    body = {"jsonrpc": "2.0", "id": 1, "method": "simulateTransaction",
            "params": [base64.b64encode(bytes(tx)).decode(),
                       {"encoding": "base64", "sigVerify": False,
                        "replaceRecentBlockhash": True, "commitment": "confirmed"}]}
    post = rpc_post or (lambda b: httpx.post(RPC, json=b, timeout=30).json())
    res = post(body)
    val = (res.get("result") or {}).get("value") or {}
    if val.get("err") is None and "result" in res:
        return {"ok": True, "pda": pda, "seed": seed.decode(), "units": val.get("unitsConsumed")}
    return {"ok": False, "pda": pda, "seed": seed.decode(), "err": val.get("err"),
            "logs": (val.get("logs") or [])[-4:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=int)
    ap.add_argument("--ts", type=int, required=True, help="update Ts in ms")
    ap.add_argument("--message-id")
    ap.add_argument("--kind", choices=["odds", "fixture"], default="odds")
    a = ap.parse_args()
    creds = get_credentials()
    if a.kind == "fixture":
        v = verify_fixture(creds, a.fixture, a.ts)
    else:
        v = verify_update(creds, a.fixture, a.ts, a.message_id)
    print(json.dumps(v, indent=1))
    print("G6 VERDICT:", "PASS — packet cryptographically anchored on-chain ✅" if v["ok"]
          else "FAIL — proof did not validate against the on-chain root ❌")


if __name__ == "__main__":
    main()
