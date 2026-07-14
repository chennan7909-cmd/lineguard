# Superteam Earn 提交表单文案(复制粘贴用)

## Project name
LineGuard — Autonomous In-Play Risk Desk for TxLINE

## One-liner
Everyone detects sharp moves; LineGuard acts on them — closed-form hedges
executed the second a spike appears, gated by data-integrity checks,
anchored on Solana. Evidence: spikes decay -8/100 within 60s (27-match,
1.4M-update backtest on real TxLINE data).

## Description (short)
LineGuard is a fully autonomous paper-trading risk desk for World Cup 1X2
markets. It ingests TxLINE StablePrice via SSE, validates every packet
through a 5-check integrity guard (stale/corrupt data cannot trigger
decisions — the refusal itself is logged and anchored), detects sharp moves
with score-event attribution, and manages positions with an exact closed-form
dutching engine: F_lock = S(a·q_i − 1). Every decision is sha256-hashed and
anchored on Solana devnet by the agent's own wallet, producing an audit trail
that survives independently of our servers (see the wallet's Memo history on
explorer). Deterministic logic, no LLM in the decision path, 18 unit tests,
replay mode so judges can reproduce everything after the tournament ends.

## Links
- Demo video: <录完填>
- Repo: https://github.com/chennan7909-cmd/lineguard
- Live dashboard: <Streamlit Cloud 部署后填>
- On-chain audit trail: https://explorer.solana.com/address/23U7XbEWcPsY4ZU4FiwHUnobGFLdGomjBQsnT9AoZUra?cluster=devnet

## 部署 Streamlit Cloud(20分钟)
1. 确认 repo 已推到 GitHub,且包含 data/decay_curve.json、两场演示数据、
   requirements.txt
2. 在本地生成一份演示用 decisions.jsonl 并提交进 repo:
   python -m lineguard.agent --replay "data/hist_odds_18202701_*.jsonl" \
     "data/hist_scores_18202701_*.jsonl" --speed 0
   git add -f data/decisions.jsonl && git commit -m "demo decisions" && git push
3. share.streamlit.io → New app → 选仓库 → Main file: lineguard/dashboard.py
   → Deploy,拿到公开 URL 填入表单
