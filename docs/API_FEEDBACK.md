# TxLINE API Feedback (hands-on, devnet free tier)

## What we liked most
1. **The 5-minute bucket endpoints are a superpower.** We reconstructed 29
   complete matches (2.88M raw odds rows) via /api/odds/updates/{day}/{hour}/
   {interval} in minutes. Historical depth at this granularity, free, is
   something even paid sportsbook APIs rarely offer.
2. **Demargined StablePrice.** Pct summing to ~100 removes the vig-stripping
   step entirely and makes closed-form hedging math exact. Great design.
3. The normalized schema + `Pct` field meant zero odds-conversion code.
4. The runnable devnet examples repo (tx-on-chain) got us from zero to an
   activated token in under an hour, including on-chain subscribe.

## Friction we hit (in order of impact)
1. **/api/scores/historical/{fixtureId} returned 200 with an empty body on
   devnet** for every finished match inside the documented 2-week/6-hour
   window. We lost ~1 hour before discovering the bucket endpoints were the
   working path. Suggest: return 404 or an explanatory body, and cross-link
   the bucket endpoints from the historical docs.
2. **Default /api/fixtures/snapshot only returns upcoming fixtures** — that
   it needs `startEpochDay` for past days is only discoverable from example
   code, not the docs.
3. **`Pct` values are strings, `Prices` are ints (odds ×1000)** — both
   undocumented types we had to infer from live payloads.
4. SSE streams reject with **403 (not just 401) on JWT expiry**; the docs
   only mention 401. Our reconnect logic needed both.
5. A one-hour token/JWT lifecycle note in a prominent place would save every
   team the same debugging session.

Net: excellent data layer with rough edges in discoverability. The data
itself never let us down — every gap we hit was documentation, not feed.
