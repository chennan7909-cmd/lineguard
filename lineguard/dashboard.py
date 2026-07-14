"""LineGuard operator dashboard.   streamlit run lineguard/dashboard.py"""
import glob
import json
from pathlib import Path

import streamlit as st

DATA = Path("data")
st.set_page_config(page_title="LineGuard — Autonomous In-Play Risk Desk", layout="wide")
st.title("🛡️ LineGuard — Autonomous In-Play Risk Desk")
st.caption("TxLINE StablePrice (demargined) → Guard → Signal → Closed-form Hedge → Solana anchor")

col1, col2, col3, col4 = st.columns(4)
decisions = []
dpath = DATA / "decisions.jsonl"
if dpath.exists():
    decisions = [json.loads(l) for l in open(dpath, encoding="utf-8")]
opens = [d for d in decisions if d["action"] == "OPEN"]
locks = [d for d in decisions if d["action"] == "LOCK"]
rejects = [d for d in decisions if d["action"].startswith("REJECT")]
anchored = [d for d in decisions if (d.get("anchor") or {}).get("sig") not in (None, "DRY_RUN")]
col1.metric("Decisions", len(decisions))
col2.metric("Locked positions", len(locks),
            f"Σ locked P/L {sum(d.get('locked_pnl', 0) for d in locks):+.2f}")
col3.metric("Guard rejections", len(rejects))
col4.metric("Anchored on Solana", len(anchored))

st.subheader("Decision log")
for d in reversed(decisions[-60:]):
    a = d.get("anchor") or {}
    icon = {"OPEN": "🟢", "LOCK": "🔒", "PROPOSED": "📝", "SUBMITTED": "📤",
            "FILLED": "✅", "PARTIALLY_FILLED": "◑", "NO_FILL": "❌", "QUOTE_HALT": "⏸️",
            "RECONCILED": "⚖️", "HOLD": "✋"}.get(d["action"], "🛑")
    with st.expander(f"{icon} {d['action']}  ·  fixture {d.get('fixture')}  ·  {d.get('detail','')}"):
        st.json({k: v for k, v in d.items() if k != "anchor"})
        if a.get("sig") and a["sig"] != "DRY_RUN":
            st.markdown(f"**sha256** `{a['hash'][:32]}…`  ·  "
                        f"[view tx on Solana explorer](https://explorer.solana.com/tx/{a['sig']}?cluster=devnet)")
        else:
            st.markdown(f"**sha256** `{a.get('hash','')[:32]}…` · anchor: {a.get('mode','-')}")

st.subheader("Evidence: spikes are ephemeral (27-match backtest)")
cpath = DATA / "decay_curve.json"
if cpath.exists():
    c = json.loads(cpath.read_text())
    st.line_chart({"all signals": c["all"], "sharp (no score event)": c["sharp_only"]})
    st.caption(f"Avg lockable P/L per 100 stake, minutes after signal · n={c['n_signals']} signals. "
               "The desk locks at t≈0; waiting costs the curve.")
else:
    st.info("run `python -m lineguard.analysis` to generate the decay curve")
