"""TxLINE recorder — the perishable-data lifeline.

Captures EVERYTHING to newline-delimited JSON so the rest of the product
(signal engine, hedge engine, replay mode, demo video) can be built after
the matches are over. Run it during every remaining live match.

    python -m lineguard.txline.recorder --out data/

Design points:
  * SSE (/api/odds/stream) is the primary channel; every event is written
    verbatim with a local receive timestamp (recv_ts) next to TxLINE's Ts,
    so freshness-gate logic can later be tested against real latencies.
  * A slow poller snapshots /api/fixtures/snapshot every 60s (game state,
    used later for score-event attribution and match-end detection).
  * Reconnect with exponential backoff + jitter; a reconnect marker line is
    written so replay knows where gaps are.
  * One JSONL file per UTC hour per channel; writes are line-atomic
    (single write() of a full line), safe to tail/copy mid-run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import threading
import time
from pathlib import Path

import httpx

from .auth import ORIGIN, Credentials, get_credentials


def _utc_hour_tag() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H")


class JsonlSink:
    def __init__(self, root: Path, channel: str):
        self.root = root
        self.channel = channel
        self.root.mkdir(parents=True, exist_ok=True)
        self._tag = None
        self._fh = None
        self._lock = threading.Lock()

    def write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            tag = _utc_hour_tag()
            if tag != self._tag:
                if self._fh:
                    self._fh.close()
                path = self.root / f"{self.channel}_{tag}.jsonl"
                self._fh = open(path, "a", encoding="utf-8")
                self._tag = tag
                print(f"[recorder] writing -> {path}")
            self._fh.write(line)
            self._fh.flush()


def _iter_sse_lines(resp: httpx.Response):
    """Minimal, tolerant SSE parser: yields dicts {event, data} per message."""
    event, data_lines = None, []
    for raw in resp.iter_lines():
        line = raw.decode() if isinstance(raw, bytes) else raw
        if line == "":
            if data_lines:
                yield {"event": event, "data": "\n".join(data_lines)}
            event, data_lines = None, []
        elif line.startswith(":"):
            continue  # comment / keepalive
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield {"event": event, "data": "\n".join(data_lines)}


def stream_channel(channel: str, sink: JsonlSink, creds: "Credentials", stop: threading.Event) -> None:
    """channel in {"odds", "scores"} -> /api/{channel}/stream. Renews JWT on 401."""
    backoff = 1.0
    while not stop.is_set():
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=120)) as client:
                with client.stream(
                    "GET",
                    f"{ORIGIN}/api/{channel}/stream",
                    headers={**creds.headers(), "Accept": "text/event-stream",
                             "Cache-Control": "no-cache"},
                ) as resp:
                    if resp.status_code in (401, 403):
                        creds.renew_jwt()
                        raise RuntimeError(f"{resp.status_code} -> guest JWT renewed, reconnecting")
                    resp.raise_for_status()
                    print(f"[recorder] SSE {channel} connected")
                    backoff = 1.0
                    for msg in _iter_sse_lines(resp):
                        recv_ts = time.time()
                        try:
                            payload = json.loads(msg["data"])
                        except (json.JSONDecodeError, TypeError):
                            payload = {"_raw": msg["data"]}
                        sink.write({"recv_ts": recv_ts, "event": msg["event"], "payload": payload})
                        if stop.is_set():
                            return
        except Exception as e:
            wait = backoff + random.random()
            sink.write({"recv_ts": time.time(), "event": "_reconnect",
                        "payload": {"error": str(e), "wait_s": round(wait, 1)}})
            print(f"[recorder] SSE {channel} dropped ({e}); retry in {wait:.1f}s")
            stop.wait(wait)
            backoff = min(backoff * 2, 60)


def poll_fixtures(sink: JsonlSink, creds: "Credentials", stop: threading.Event, every: float = 60.0) -> None:
    with httpx.Client(timeout=20) as client:
        while not stop.is_set():
            try:
                r = client.get(f"{ORIGIN}/api/fixtures/snapshot", headers=creds.headers())
                if r.status_code == 401:
                    creds.renew_jwt()
                    r = client.get(f"{ORIGIN}/api/fixtures/snapshot", headers=creds.headers())
                sink.write({"recv_ts": time.time(), "status": r.status_code,
                            "payload": r.json() if r.status_code == 200 else r.text[:500]})
            except Exception as e:
                sink.write({"recv_ts": time.time(), "status": -1, "payload": str(e)})
            stop.wait(every)


def main() -> None:
    ap = argparse.ArgumentParser(description="Record TxLINE odds SSE + fixture snapshots to JSONL.")
    ap.add_argument("--out", default="data", help="output directory (default: data/)")
    ap.add_argument("--fixtures-every", type=float, default=60.0)
    args = ap.parse_args()

    creds = get_credentials()
    print(f"[recorder] credentials source: {creds.source} | origin: {ORIGIN}")
    root = Path(args.out)
    odds_sink = JsonlSink(root, "odds")
    scores_sink = JsonlSink(root, "scores")
    fx_sink = JsonlSink(root, "fixtures")

    stop = threading.Event()
    threads = [
        threading.Thread(target=stream_channel, args=("odds", odds_sink, creds, stop), daemon=True),
        threading.Thread(target=stream_channel, args=("scores", scores_sink, creds, stop), daemon=True),
        threading.Thread(target=poll_fixtures, args=(fx_sink, creds, stop, args.fixtures_every), daemon=True),
    ]
    for t in threads:
        t.start()
    print("[recorder] running — Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(timeout=5)
        print("[recorder] stopped cleanly")


if __name__ == "__main__":
    main()
