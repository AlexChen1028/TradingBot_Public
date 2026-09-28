# -*- coding: utf-8 -*-
"""
fast_direction_watch.py — sub-minute reconciliation between a local position
record and the exchange's actual position, dependency-injected for testing.

## Background

This bot places conditional stop-loss/take-profit orders with
`closePosition=True` on Binance USDⓈ-M futures. Investigating a recurring
"phantom position" incident (full write-up: `docs/checks.md` in the private
repo) turned up a real exchange-side quirk: roughly half the time a
`STOP_MARKET`/`TAKE_PROFIT_MARKET` closePosition order fires on one symbol,
it over-fills — flattening the intended position and opening a same-sized
*reverse* position, with no stop-loss or take-profit attached to it.

The bot's main loop only scanned once per `SCAN_INTERVAL` (originally
15 minutes), so a reverse position sat completely unprotected for up to
that long. Measured detection latency before this fix: 2.6 / 9.5 / 12.5
minutes across three real incidents — each one a naked position sitting a
percent or two from liquidation.

## What this adds (and deliberately doesn't add)

No new order-placing logic. This only shortens *detection* latency for a
check that already existed: "does the local position direction still match
what the exchange reports?" `_sleep_and_watch` replaces a plain
`time.sleep(SCAN_INTERVAL)` between scans with a loop that wakes every
`FAST_WATCH_INTERVAL` seconds and calls `_fast_direction_watch`, which hands
off to the pre-existing mismatch handler (not included here) the moment it
finds — and *confirms* — a mismatch.

Design choices worth noting:

- **Confirm before acting.** A mismatch is checked once, then re-checked
  after `FAST_WATCH_CONFIRM` seconds; only a mismatch that survives both
  reads triggers a handoff. Checking 15x more often than before increases
  the chance of catching a position mid-transition (the exchange settling
  a fill); the second read filters that out instead of amplifying it into
  15x more false positives.
- **Scoped to what's cheap and necessary.** Only symbols with an existing
  local position record are queried, and only "majors" (the small,
  always-watched set) are in scope — an idle account makes zero extra API
  calls per tick.
- **Fully dependency-injected**, despite the codebase having no test
  framework: `ex_position`, `handler`, `sleep`, and `clock` are all
  parameters. `test_fast_direction_watch.py` in this repo exercises 11
  scenarios (confirmed mismatch, transient mismatch that self-resolves,
  a query that fails outright, exception isolation, etc.) with no network
  calls and no sleeping in real time.
"""
import time

FAST_WATCH_INTERVAL = 60   # seconds between watch ticks
FAST_WATCH_CONFIRM = 3     # seconds to wait before re-checking a suspected mismatch

# Example scope — in production this is the bot's small "always watch"
# set (majors only); altcoin positions are left to the regular scan.
WATCH_ALWAYS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']


def _fast_direction_watch(exchange, positions, ex_position, handler, sleep=None):
    """Check each locally-tracked major for a confirmed direction mismatch
    against the exchange, handing any off to `handler`.

    Returns the list of symbols handed off this tick (useful for tests).

    ex_position(exchange, symbol) -> (direction, amount) | None
        None means "query failed" — deliberately not treated as a mismatch.
    handler(exchange, symbol, positions, ex_direction, ex_amount, reason)
        The existing (pre-existing, not shown here) takeover routine: it
        never places an order blind — it re-derives realized P&L from
        exchange income history, discards the stale local record, adopts
        the exchange's actual position, and re-attaches stop-loss/take-profit.
    """
    sleep = sleep or time.sleep
    handled = []
    for symbol in WATCH_ALWAYS:
        pos = positions.get(symbol)
        if not pos:
            continue
        st = ex_position(exchange, symbol)
        if st is None:                       # query failed: don't guess
            continue
        ex_d, ex_amt = st
        if ex_amt <= 0 or ex_d == pos.get('direction'):
            continue                          # flat (scan will settle it) or already consistent
        sleep(FAST_WATCH_CONFIRM)
        st2 = ex_position(exchange, symbol)
        if st2 is None or tuple(st2) != tuple(st):
            continue                          # didn't reproduce: likely transient, leave to next scan
        coin = symbol.split('/')[0]
        print(f"  {coin}: local={pos.get('direction')} exchange={ex_d} qty={ex_amt} "
              f"(confirmed after {FAST_WATCH_CONFIRM}s) -> handing off")
        handler(exchange, symbol, positions, ex_d, ex_amt, 'direction_reconcile')
        handled.append(symbol)
    return handled


def _sleep_and_watch(exchange, positions, total, watch, sleep=None, clock=None):
    """Drop-in replacement for `time.sleep(total)` between scans: splits the
    wait into `FAST_WATCH_INTERVAL`-sized chunks and runs `watch` between
    them. Total elapsed time is unchanged; the final chunk doesn't watch,
    since the main scan runs immediately after anyway."""
    sleep = sleep or time.sleep
    clock = clock or time.time
    end = clock() + total
    while True:
        left = end - clock()
        if left <= 0:
            return
        sleep(min(FAST_WATCH_INTERVAL, left))
        if end - clock() <= 1:
            return
        try:
            watch(exchange, positions)
        except Exception as e:               # a bug here must never take down the main loop
            print(f"  fast direction watch error (main scan unaffected): {e}")
