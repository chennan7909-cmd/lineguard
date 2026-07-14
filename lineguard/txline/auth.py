"""TxLINE authentication — verified against official docs (2026-07-13).

Confirmed flow (docs: txline-docs.txodds.com, quickstart + worldcup pages):

  1. POST {origin}/auth/guest/start          -> {"token": <guest JWT>}
  2. On-chain: Txoracle `subscribe(SERVICE_LEVEL_ID=1, DURATION_WEEKS)`
     Free WC tier: no TxL needed, wallet only needs SOL for fees.
     -> txSig
  3. Sign message  f"{txSig}:{','.join(leagues)}:{jwt}"  (empty leagues =>
     f"{txSig}::{jwt}") with the SAME wallet's ed25519 key, nacl detached,
     base64-encode.
  4. POST {origin}/api/token/activate {txSig, walletSignature, leagues}
     with header Authorization: Bearer <jwt>   -> API token
  5. ALL data requests carry BOTH headers:
       Authorization: Bearer <jwt>       (renew via guest/start on 401)
       X-Api-Token:   <api token>

Networks (must match end-to-end):
  devnet : https://txline-dev.txodds.com  program 6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J
  mainnet: https://txline.txodds.com      program 9ExbZjAapQww1vfcisDmrngPinHTEfpjYRWMunJgcKaA
  Free service levels: mainnet 1 (60s delay) or 12 (real-time);
  devnet 1 (samplingIntervalSec=0 per current pricing matrix).

Fast path for tonight: run the OFFICIAL script once to subscribe+activate —
  git clone https://github.com/txodds/tx-on-chain && yarn install
  TOKEN_MINT_ADDRESS=4Zao8ocPhmMgq7PdsYWyxvqySMGx7xb9cMftPMkEokRG \
  ANCHOR_PROVIDER_URL=https://api.devnet.solana.com \
  ANCHOR_WALLET=./_keys/wallet.json \
  yarn ts-node examples/devnet/scripts/subscription_free_tier.ts
then export TXLINE_API_TOKEN=<printed token> and run this package's tools.
Signing wallet MUST be the wallet that sent the subscribe tx.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

NETWORKS = {
    "devnet": {
        "origin": "https://txline-dev.txodds.com",
        "program_id": "6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J",
        "rpc": "https://api.devnet.solana.com",
    },
    "mainnet": {
        "origin": "https://txline.txodds.com",
        "program_id": "9ExbZjAapQww1vfcisDmrngPinHTEfpjYRWMunJgcKaA",
        "rpc": "https://api.mainnet-beta.solana.com",
    },
}
NETWORK = os.environ.get("TXLINE_NETWORK", "devnet")
ORIGIN = os.environ.get("TXLINE_ORIGIN", NETWORKS[NETWORK]["origin"])
CACHE = Path(os.environ.get("TXLINE_TOKEN_CACHE", ".txline_token.json"))


@dataclass
class Credentials:
    """Holds both credentials; auto-renews the guest JWT on 401."""

    api_token: str
    jwt: str = ""
    source: str = "env"
    _client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=15), repr=False)

    def ensure_jwt(self) -> str:
        if not self.jwt:
            self.renew_jwt()
        return self.jwt

    def renew_jwt(self) -> str:
        r = self._client.post(f"{ORIGIN}/auth/guest/start")
        r.raise_for_status()
        self.jwt = r.json()["token"]
        return self.jwt

    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.ensure_jwt()}",
            "X-Api-Token": self.api_token,
        }


def sign_activation_message(tx_sig: str, jwt: str, secret_key: bytes, leagues: list[int] | None = None) -> str:
    """ed25519 detached signature over f"{txSig}:{leagues}:{jwt}", base64.

    secret_key: 64-byte solana keypair secret (as in id.json). Requires pynacl.
    """
    import nacl.signing  # type: ignore

    leagues = leagues or []
    msg = f"{tx_sig}:{','.join(map(str, leagues))}:{jwt}".encode()
    signing_key = nacl.signing.SigningKey(bytes(secret_key)[:32])
    return base64.b64encode(signing_key.sign(msg).signature).decode()


def activate(tx_sig: str, secret_key: bytes, leagues: list[int] | None = None) -> str:
    """Activate an API token from a confirmed subscribe txSig (python path)."""
    leagues = leagues or []
    with httpx.Client(timeout=20) as client:
        jwt = client.post(f"{ORIGIN}/auth/guest/start").json()["token"]
        sig = sign_activation_message(tx_sig, jwt, secret_key, leagues)
        r = client.post(
            f"{ORIGIN}/api/token/activate",
            json={"txSig": tx_sig, "walletSignature": sig, "leagues": leagues},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        r.raise_for_status()
        body = r.json()
        token = body.get("token") if isinstance(body, dict) else body
        CACHE.write_text(json.dumps({"api_token": token, "ts": time.time()}))
        return token


def get_credentials() -> Credentials:
    tok = os.environ.get("TXLINE_API_TOKEN")
    if tok:
        return Credentials(tok, source="env")
    if CACHE.exists():
        blob = json.loads(CACHE.read_text())
        return Credentials(blob["api_token"], source="cache")
    raise SystemExit(
        "No API token. Either run the official subscription script "
        "(see module docstring) and export TXLINE_API_TOKEN, or call "
        "lineguard.txline.auth.activate(txSig, secret_key)."
    )


def _probe() -> int:
    print(f"[probe] network={NETWORK} origin={ORIGIN}")
    with httpx.Client(timeout=15) as client:
        try:
            jwt = client.post(f"{ORIGIN}/auth/guest/start").json()["token"]
            print(f"[probe] guest JWT OK ({jwt[:14]}…)")
        except Exception as e:
            print(f"[probe] guest/start FAILED: {e}")
            return 1
        tok = os.environ.get("TXLINE_API_TOKEN")
        if not tok and CACHE.exists():
            tok = json.loads(CACHE.read_text()).get("api_token")
        if not tok:
            print("[probe] no API token yet — run official subscription_free_tier.ts (see docstring)")
            return 2
        r = client.get(
            f"{ORIGIN}/api/fixtures/snapshot",
            headers={"Authorization": f"Bearer {jwt}", "X-Api-Token": tok},
        )
        print(f"[probe] fixtures/snapshot -> {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"[probe] TOKEN VALID. fixtures visible: {len(data) if isinstance(data, list) else 'obj'}")
            print("[probe] ready to record: python -m lineguard.txline.recorder")
            return 0
        print(f"[probe] body: {r.text[:300]}")
        return 3


if __name__ == "__main__":
    sys.exit(_probe())
