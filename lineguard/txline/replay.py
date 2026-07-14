"""Replay recorded JSONL as if it were the live SSE feed.

    python -m lineguard.txline.replay data/odds_20260714T19.jsonl --speed 10

Yields the same {recv_ts, event, payload} dicts the recorder wrote, paced by
recv_ts deltas divided by --speed (speed=0 -> as fast as possible). This is
what makes the whole product demoable after the World Cup ends.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
from typing import Iterator


def replay(paths: list[Path], speed: float = 10.0) -> Iterator[dict]:
    prev = None
    for path in sorted(paths):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if speed > 0 and prev is not None:
                    delta = max(0.0, (rec.get("recv_ts", prev) - prev) / speed)
                    time.sleep(min(delta, 5.0))  # cap long gaps
                prev = rec.get("recv_ts", prev)
                yield rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--speed", type=float, default=10.0, help="time compression factor; 0 = no pacing")
    args = ap.parse_args()
    n = 0
    for rec in replay(args.files, args.speed):
        n += 1
        sys.stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[replay] {n} records", file=sys.stderr)


if __name__ == "__main__":
    main()
