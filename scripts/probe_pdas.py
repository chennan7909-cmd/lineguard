"""Ask devnet which Txoracle root accounts actually exist.

    python scripts/probe_pdas.py [epoch_day]
"""
import json
import struct
import sys
import time

import httpx
from solders.pubkey import Pubkey

PROGRAM = Pubkey.from_string("6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J")
RPC = "https://api.devnet.solana.com"

day = int(sys.argv[1]) if len(sys.argv) > 1 else int(time.time() // 86400)
rows = []
for d in [day - 2, day - 1, day]:
    le = struct.pack("<H", d)
    for seed in [b"daily_batch_roots", b"daily_odds_roots", b"daily_odds_merkle_roots",
                 b"daily_scores_roots"]:
        pda, _ = Pubkey.find_program_address([seed, le], PROGRAM)
        rows.append((f"{seed.decode()}[{d}]", str(pda)))
aligned = (day // 10) * 10
pda, _ = Pubkey.find_program_address([b"ten_daily_fixtures_roots", struct.pack("<H", aligned)], PROGRAM)
rows.append((f"ten_daily_fixtures_roots[{aligned}]", str(pda)))

resp = httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                             "params": [[p for _, p in rows], {"encoding": "base64"}]},
                  timeout=30).json()
vals = resp["result"]["value"]
for (label, pda), v in zip(rows, vals):
    size = len(__import__("base64").b64decode(v["data"][0])) if v else 0
    print(f"{'EXISTS' if v else 'absent':>7}  {label:<38} {pda}  {size and f'{size}B' or ''}")
