"""Solana devnet anchoring — every LineGuard decision leaves an on-chain trace.

Each decision dict is canonically hashed (sha256 over sorted-key JSON) and
written as an SPL Memo transaction on devnet, signed by the agent's own
wallet. This makes "the agent decided X at time T" a checkable claim: the
memo carries the hash + compact fields, and the dashboard links the tx.

Env:
  SOLANA_WALLET   path to a devnet keypair json (default ~/lineguard-keys/wallet.json)
  SOLANA_RPC      default https://api.devnet.solana.com
  ANCHOR_MODE     "live" (default) or "dry" (build+sign but do not send)

Uses solders + raw JSON-RPC over httpx: no heavyweight SDK.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
RPC = os.environ.get("SOLANA_RPC", "https://api.devnet.solana.com")
WALLET = os.environ.get("SOLANA_WALLET", str(Path.home() / "lineguard-keys" / "wallet.json"))
MODE = os.environ.get("ANCHOR_MODE", "live")


def canonical_hash(decision: dict) -> str:
    blob = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class Anchor:
    """Lazily-initialised memo writer; degrades to disabled if wallet missing."""

    def __init__(self):
        self.enabled = False
        self.pubkey = None
        self._kp = None
        try:
            from solders.keypair import Keypair
            raw = json.loads(Path(WALLET).read_text())
            self._kp = Keypair.from_bytes(bytes(raw))
            self.pubkey = str(self._kp.pubkey())
            self.enabled = True
        except Exception as e:
            self.reason = f"anchoring disabled: {type(e).__name__}: {e}"

    def _recent_blockhash(self, client: httpx.Client) -> str:
        r = client.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                   "method": "getLatestBlockhash",
                                   "params": [{"commitment": "finalized"}]})
        r.raise_for_status()
        return r.json()["result"]["value"]["blockhash"]

    def anchor(self, decision: dict, timeout: float = 20.0) -> dict:
        """Returns {hash, sig|None, mode}. Never raises into the agent loop."""
        h = canonical_hash(decision)
        memo = json.dumps({"lg": 1, "h": h[:32], "a": decision.get("action"),
                           "f": decision.get("fixture"), "t": decision.get("ts")},
                          separators=(",", ":"))
        if not self.enabled:
            return {"hash": h, "sig": None, "mode": "disabled"}
        try:
            from solders.hash import Hash
            from solders.instruction import AccountMeta, Instruction
            from solders.message import Message
            from solders.pubkey import Pubkey
            from solders.transaction import Transaction

            ix = Instruction(
                Pubkey.from_string(MEMO_PROGRAM_ID),
                memo.encode(),
                [AccountMeta(self._kp.pubkey(), is_signer=True, is_writable=False)],
            )
            if MODE == "dry":   # fully offline: sign against a null blockhash
                bh = Hash.default()
                msg = Message.new_with_blockhash([ix], self._kp.pubkey(), bh)
                Transaction([self._kp], msg, bh)
                return {"hash": h, "sig": "DRY_RUN", "mode": "dry"}
            import base64
            with httpx.Client(timeout=timeout) as client:
                last = "?"
                for attempt in range(3):
                    bh = Hash.from_string(self._recent_blockhash(client))
                    msg = Message.new_with_blockhash([ix], self._kp.pubkey(), bh)
                    tx = Transaction([self._kp], msg, bh)
                    r = client.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                               "params": [base64.b64encode(bytes(tx)).decode(),
                                                          {"encoding": "base64",
                                                           "skipPreflight": attempt == 2}]})
                    body = r.json()
                    if "result" in body:
                        return {"hash": h, "sig": body["result"], "mode": "live"}
                    last = body.get("error", {}).get("message", "?")[:80]
                    if "Blockhash" not in last:
                        break
                return {"hash": h, "sig": None, "mode": f"rpc_error:{last}"}
        except Exception as e:
            return {"hash": h, "sig": None, "mode": f"error:{type(e).__name__}"}
