# Superteam Earn submission copy

## Project name

LineGuard — Autonomous In-Play Risk Desk for TxLINE

## One-liner

LineGuard is an autonomous risk-control agent that validates, attributes,
verifies, acts or refuses, reconciles, and audits decisions driven by TxLINE
data. Across the canonical 29-match evaluation, it processes 2,876,085 raw odds
rows, 176,286 deduplicated 1X2 updates, and 328 signals. The immediate lock mean
is -1.56 per 100 stake, while the unhedged terminal mean is -34.92 per 100
stake. This is a risk-control result, not a profitability or guaranteed-return
claim.

## Description

LineGuard is a fully autonomous paper-trading risk desk for World Cup 1X2
markets. It ingests TxLINE StablePrice through live SSE and deterministic replay,
then gates decisions through 5+1 validation:

- G1 Freshness
- G2 Demargin consistency
- G3 Price consistency
- G4 Range sanity
- G5 Timestamp monotonicity
- G6 Merkle provenance spot-check against TxLINE's on-chain validation views

G1-G5 validate operational data usability. G6 performs a cryptographic Merkle
spot-check. G6 does not claim that every incoming packet is fully verified
on-chain; it performs a targeted Merkle provenance spot-check against TxLINE's
on-chain validation views.

The agent detects probability movement with score-event attribution, calculates
lockable P/L with a closed-form hedge, routes decisions through a seeded
simulated venue, reconciles fills and partial fills, refuses stale or invalid
inputs, and writes a decision-audit trail to Solana devnet. Solana is used for
provenance verification and decision-audit anchoring, not sportsbook hedge
execution.

The current automated test suite contains 64 automated tests passing. The public
repository documents deterministic replay so judges can reproduce the same Guard,
signal, hedge, reconciliation, refusal, and audit logic shown in the demo,
subject to the documented setup and data requirements.

## Application Access

### Deployed dashboard

https://lineguard-txline.streamlit.app

The deployed Streamlit dashboard exposes recorded decision history, lockable
P/L, Guard rejection counts, execution-lifecycle status, and links to
corresponding Solana devnet audit transactions. Recorded decisions remain
available after matches end. Live match activity is not guaranteed during
judging.

### Independent Solana devnet audit

Wallet:

`23U7XbEWcPsY4ZU4FiwHUnobGFLdGomjBQsnT9AoZUra`

Explorer:

https://explorer.solana.com/address/23U7XbEWcPsY4ZU4FiwHUnobGFLdGomjBQsnT9AoZUra?cluster=devnet

This is the independent audit-history entry point. It is not the betting venue
and does not represent sportsbook hedge execution.

### Public repository and deterministic replay

Repository:

https://github.com/chennan7909-cmd/lineguard

Example deterministic replay:

```bash
python -m lineguard.agent \
  --replay "data/hist_odds_18222446_*.jsonl" \
  --speed 1000 \
  --exec-mode simulated \
  --exec-seed 42 \
  --out data/judge_replay
```

The replay is deterministic and reproduces the same Guard, signal, hedge,
reconciliation, refusal, and audit logic shown in the demo, subject to the setup
and data requirements documented in the repository.

## TxLINE and G6 interfaces

Txoracle devnet program ID:
`6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J`.

LineGuard uses `POST /auth/guest/start`, on-chain `subscribe`,
`POST /api/token/activate`, fixture snapshots, odds and scores SSE streams,
historical odds and scores update buckets, validation bundles, and
`validate_odds` / `validate_fixture` through `simulateTransaction`.

The odds-validation route uses `ts`, while the fixture-validation route uses
`timestamp`. These parameter names are not interchangeable.

## Current scope and boundaries

Current scope:

- live TxLINE ingestion;
- deterministic autonomous policy logic;
- simulated venue execution;
- execution reconciliation;
- stale-input refusal;
- Solana devnet provenance verification;
- Solana devnet decision-audit anchoring;
- public Streamlit dashboard;
- deterministic local replay.

Boundaries:

- no live-money sportsbook execution;
- simulated venue fills are used in the demo;
- Solana is not the betting venue;
- the current canonical evaluation covers 29 football matches;
- future sport or venue integrations are not part of the current evaluation.
