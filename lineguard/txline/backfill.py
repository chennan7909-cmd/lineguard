"""Backfill v3.1 — bucket-based historical replay, with retries + resume.

Per-fixture /scores/historical/{id} returned empty on devnet, but TxLINE
exposes 5-minute time-bucket endpoints (confirmed in official examples/docs):

    /api/odds/updates/{epochDay}/{hourOfDay}/{interval}
    /api/scores/updates/{epochDay}/{hourOfDay}/{interval}

epochDay = floor(unix_ms / 86400000), hourOfDay = UTC hour, interval = minute//5.

Strategy: discover fixtures per epoch-day, take each fixture's StartTime,
scan buckets from (start - pre) to (start + post), fetch both channels once
per bucket, split rows by FixtureId into per-fixture JSONL files.

    python -m lineguard.txline.backfill --out data/
    python -m lineguard.txline.backfill --days 14 --pre-h 2 --post-h 3
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import httpx

from .auth import ORIGIN, get_credentials
from .recorder import JsonlSink

WORLD_CUP_COMPETITION_ID = 72
MS_PER_DAY = 86_400_000
MS_PER_BUCKET = 300_000  # 5 minutes


def _get(client, creds, url, retries: int = 3):
    last_exc = None
    for attempt in range(retries):
        try:
            r = client.get(url, headers=creds.headers())
            if r.status_code in (401, 403):
                creds.renew_jwt()
                r = client.get(url, headers=creds.headers())
            return r
        except httpx.HTTPError as e:
            last_exc = e
            wait = 2 ** attempt
            print(f"[net] {type(e).__name__} on {url.rsplit('/api/',1)[-1]}, retry {attempt+1}/{retries} in {wait}s")
            time.sleep(wait)
    raise last_exc


def _json_or_none(r):
    if not r.content or not r.content.strip():
        return None
    try:
        return r.json()
    except json.JSONDecodeError:
        return None


def _fixture_id(fx):
    for k in ("FixtureId", "fixtureId", "id", "Id"):
        if k in fx:
            return fx[k]
    return None


def _label(fx):
    return f"{fx.get('Participant1','?')} vs {fx.get('Participant2','?')}"


def discover_fixtures(client, creds, competition, days, sink):
    today = int(time.time() // 86400)
    found = {}
    for day in range(today - days, today + 1):
        url = f"{ORIGIN}/api/fixtures/snapshot?competitionId={competition}&startEpochDay={day}"
        data = _json_or_none(_get(client, creds, url))
        rows = data if isinstance(data, list) else (data or {}).get("fixtures", []) if isinstance(data, dict) else []
        for fx in rows:
            fid = _fixture_id(fx)
            if fid and fid not in found:
                found[fid] = fx
                sink.write({"recv_ts": time.time(), "epoch_day": day, "payload": fx})
        time.sleep(0.1)
    print(f"[discover] {len(found)} unique fixture(s) across {days + 1} day(s)")
    return found


def bucket_of(ms: int) -> tuple[int, int, int]:
    day = ms // MS_PER_DAY
    rem = ms % MS_PER_DAY
    hour = rem // 3_600_000
    interval = (rem % 3_600_000) // 60_000 // 5
    return (day, hour, interval)


def buckets_for_window(start_ms: int, end_ms: int):
    t = (start_ms // MS_PER_BUCKET) * MS_PER_BUCKET
    while t <= end_ms:
        yield bucket_of(t)
        t += MS_PER_BUCKET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--competition", type=int, default=WORLD_CUP_COMPETITION_ID)
    ap.add_argument("--pre-h", type=float, default=2.0, help="hours before kickoff to include")
    ap.add_argument("--post-h", type=float, default=3.0, help="hours after kickoff to include")
    args = ap.parse_args()

    creds = get_credentials()
    out = Path(args.out)
    now_ms = int(time.time() * 1000)

    with httpx.Client(timeout=30) as client:
        fx_sink = JsonlSink(out, "fixtures_discovered")
        fixtures = discover_fixtures(client, creds, args.competition, args.days, fx_sink)

        # Build bucket -> interested fixture ids map (only fixtures already started)
        bucket_map: dict[tuple, set] = defaultdict(set)
        labels = {}
        for fid, fx in fixtures.items():
            start = fx.get("StartTime")
            labels[fid] = _label(fx)
            if not isinstance(start, (int, float)) or start > now_ms:
                continue  # not started yet
            lo = int(start - args.pre_h * 3_600_000)
            hi = int(min(start + args.post_h * 3_600_000, now_ms))
            for b in buckets_for_window(lo, hi):
                bucket_map[b].add(fid)

        progress_path = out / ".backfill_done.json"
        done: set = set()
        if progress_path.exists():
            done = {tuple(b) for b in json.loads(progress_path.read_text())}
            print(f"[backfill] resume: {len(done)} bucket(s) already done, skipping them")
        todo = [b for b in sorted(bucket_map) if b not in done]
        print(f"[backfill] {len(todo)} bucket(s) to fetch this run (x2 channels), {len(bucket_map)} total")
        sinks: dict[str, JsonlSink] = {}
        counts = defaultdict(int)
        empty_buckets = 0
        failed_buckets = 0

        for i, (day, hour, interval) in enumerate(todo):
            ok = True
            for channel in ("odds", "scores"):
                url = f"{ORIGIN}/api/{channel}/updates/{day}/{hour}/{interval}"
                try:
                    r = _get(client, creds, url)
                except httpx.HTTPError as e:
                    print(f"[backfill] {channel} {day}/{hour}/{interval} FAILED after retries: {type(e).__name__} — skipping")
                    ok = False
                    failed_buckets += 1
                    continue
                rows = _json_or_none(r)
                if r.status_code != 200:
                    print(f"[backfill] {channel} {day}/{hour}/{interval} -> HTTP {r.status_code}")
                    ok = False
                    continue
                if not rows:
                    empty_buckets += 1
                    continue
                rows = rows if isinstance(rows, list) else [rows]
                for row in rows:
                    fid = row.get("FixtureId") or row.get("fixtureId")
                    if fid is None:
                        continue
                    key = f"hist_{channel}_{fid}"
                    if key not in sinks:
                        sinks[key] = JsonlSink(out, key)
                    sinks[key].write({"recv_ts": time.time(), "bucket": [day, hour, interval], "payload": row})
                    counts[(channel, fid)] += 1
            if ok:
                done.add((day, hour, interval))
            if i % 25 == 0:
                progress_path.write_text(json.dumps(sorted(done)))
                print(f"[backfill] progress {i}/{len(todo)} buckets…")
            time.sleep(0.08)
        progress_path.write_text(json.dumps(sorted(done)))

        print(f"\n[backfill] DONE. empty channel-buckets: {empty_buckets}, failed buckets: {failed_buckets}")
        for (channel, fid), n in sorted(counts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            print(f"  {fid} ({labels.get(fid, '?')}): {n} {channel} rows")
        if not counts:
            print("  (no rows at all — devnet may not retain historical buckets; live recording tomorrow is the fallback)")


if __name__ == "__main__":
    main()
