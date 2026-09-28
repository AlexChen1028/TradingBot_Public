# Crypto Futures Trading Bot (Showcase)

> 繁體中文版： [README.zh-TW.md](README.zh-TW.md)

A 24/7 crypto-futures trading bot that combines a stacked technical-signal
scanner with a pipeline that turns YouTube market commentary into
machine-readable risk parameters, running on Binance USDⓈ-M perpetuals.
This is a **curated excerpt** of a larger, privately-run system; two
representative pieces are included here rather than the full production
file (~4,700 lines with five months of incident-response history) — see
[Note](#note).

## What this shows

- A multi-signal, stacked-filter entry system (below) instead of one indicator
- An unusual data pipeline: RSS detection → transcript extraction (native
  captions, falling back to local Whisper for caption-disabled channels) →
  structured market-call registry, feeding real risk parameters
- Dependency-injected, unit-tested risk-engineering code in a codebase that
  otherwise has no test framework — because the one piece that reacts to a
  real exchange-side failure mode is exactly the piece worth pinning down
- Three concrete incident → fix → regression-test stories, not just a
  finished system with the debugging history erased
- A self-critical writeup of the project's own backtest methodology,
  including the seven reasons an early version of it couldn't be trusted

## Architecture

```
 Binance (OHLCV, funding rate)      YouTube (RSS feed, 3 channels)
              │                                  │
              ▼                                  ▼
    ┌────────────────────┐                ┌────────────────────────┐
    │   signal scanner   │                │      kol_fetch.py      │
    │  (stacked filters) │                │ native captions, or    │
    └─────────┬──────────┘                │ Whisper fallback       │
              │                           └───────────┬────────────┘
              │                                       │ transcripts
              ▼                                       ▼
    ┌──────────────────┐               ┌────────────────────────┐
    │   entry / exit   │◀──────────── │  risk parameters       │
    │   (positions)    │  support/     │  (support/resistance,  │
    └─────────┬────────┘  resistance   │   blacklist, bias)     │
              │              zones     └────────────────────────┘
              ▼
    ┌──────────────────┐
    │ fast_direction_  │   60s reconciliation between scans
    │  watch.py        │   (see the incident story below)
    └──────────────────┘
```

### 1. Entry is gated by stacked filters, not a single signal

A scan produces a signal count (volume spike, compression, breakout
proximity, funding-rate surge) and requires several to agree before an entry
is even considered. On top of that sits a stack of independent vetoes: RSI
extremes, trend agreement, a directional bias switch, proximity to a known
support/resistance band, a coin blacklist, and an hourly macro-bias vote.
None of these alone is sophisticated; the discipline is in never letting a
single one authorize a trade by itself, and in auditing each veto's
hit rate on a schedule so a filter that quietly stopped mattering (or
started blocking 100% of one direction) doesn't sit there unnoticed.

### 2. Risk parameters come from a text pipeline, not just backtests (`kol_fetch.py`)

Included here as a real, working example. Three YouTube channels publish
near-daily crypto market analysis; `kol_fetch.py` polls their RSS feeds,
pulls a transcript for every new video — native captions where available,
falling back to a local Whisper pass for caption-disabled channels — and
hands the raw text off for structured extraction into a versioned registry
of price levels and their stated rationale. A few things worth noting in
the code itself:

- **Channel IDs are pinned, not resolved by scraping a page each run** —
  an earlier version re-scraped `youtube.com/@handle` every call and
  occasionally got rate-limited into a false "no channel found," which
  silently looked identical to "no new videos."
- **A video with genuinely no speech (a promo clip, a music short) is
  retired immediately**, distinct from a transient transcription failure
  that gets retried for up to `RETIRE_HOURS`. Before this distinction
  existed, a no-speech clip would get re-downloaded and re-run through the
  Whisper model on every cycle indefinitely.
- **Every retirement is logged with its title**, not just its video ID —
  so a judgment call like "this was correctly skipped" stays checkable
  after the fact instead of being an unrecoverable one-line decision.

### 3. An incident, and the fix, and the test for the fix (`fast_direction_watch.py`)

This is the piece I'd point to first. The bot uses `closePosition=True`
conditional stop-loss/take-profit orders. Investigating a pattern of
unexplained reverse positions turned up a real exchange-side quirk: on one
symbol, roughly half of these conditional orders over-filled on trigger —
flattening the intended position and opening a same-sized position in the
*opposite* direction, with no stop-loss attached. The bot's main loop only
scanned once every 15 minutes, so a naked, wrong-direction position could
sit unprotected for most of that window; three real incidents measured
2.6, 9.5, and 12.5 minutes of exposure before detection.

The fix adds no new order-placing logic — it only shortens how often one
existing check runs, from once per scan to once every 60 seconds, and
requires a mismatch to reproduce on a second read 3 seconds later before
acting (so checking 15x more often doesn't turn a small false-positive
rate into a much bigger one). `test_fast_direction_watch.py` pins down 16
scenarios — confirmed mismatch, transient mismatch that self-resolves, a
failed exchange query, an exception inside the watcher not being allowed
to escape and take down the main loop — with no network calls and no real
sleeping, in a codebase that otherwise has no test suite.

```bash
python test_fast_direction_watch.py
#  16/16 scenarios passed
```

### 4. A second incident, this one caused by looking in the wrong place (`check_algo_protection.py`)

For a long time this project's documentation stated, as established fact,
that the exchange's demo environment couldn't see or cancel conditional
(stop-loss/take-profit) orders. Investigating a third occurrence of the
reverse-position bug above turned up the actual explanation: Binance had
moved conditional orders to a separate "Algo" order service. The bot's
order-status checks were calling the legacy endpoints, which correctly
return nothing for an Algo order — not because the order is missing, but
because they're the wrong endpoint. Querying with the right parameter
found the same orders immediately, correct status and trigger price
included. What had been treated as a demo-environment defect for months
was a wrong-endpoint artifact in the bot's own code.

`check_algo_protection.py` is the read-only audit written in response:
it checks every open position has a live, correctly-priced stop order on
the exchange side, and separately tracks two exchange-side failure modes
across recent history — orders that over-filled into a reverse position
(see above), and orders that were triggered but rejected outright, leaving
a position briefly unprotected either way. It deliberately does **not**
change the order-placement code path itself; the project's own established
pattern is that touching order logic in the middle of an incident is how
incidents get bigger, not smaller. A fix is gated behind an explicit,
pre-written trigger condition instead (see the script's docstring) —
measure first, then earn the right to act on what was measured.

```bash
python check_algo_protection.py --selftest
#  20/20 scenarios passed
```

### 5. Not claiming false confidence (`stale_gate_report.py`)

A smaller story, but a real one. The full audit suite reports on trading
gates that go stale — support/resistance levels no longer backed by any
current market call, directional biases that quietly started blocking
100% of one side. An early version of the reporting layer printed a green
checkmark on every line unconditionally. One of the audit's data sources
only exists in the production container, not on a dev machine — so on a
dev machine, "this wasn't checked" and "this was checked and is fine"
rendered as the exact same green checkmark. For a while, an entire audit
path was silently not running, and nothing on screen said so.

The fix is the three-state distinction in this file: **issues** (exit 1),
**couldn't verify** (exit 2 — a distinct state, not a fallback to "ok"),
and **actually healthy** (exit 0, the only state `--quiet` is allowed to
silence). It's a small function, but it's the one place a "totally clean"
report and a "we have no idea" report are guaranteed not to look the same.

```bash
python stale_gate_report.py
#  8/8 scenarios passed
```

## Backtesting: what it can and can't tell you

[`BACKTESTING.md`](BACKTESTING.md) is an adapted excerpt of the private
repo's internal writeup on why an early exploratory backtest script
*could not* validate the live strategy — seven concrete methodology
problems (survivorship bias in the coin pool, no leverage in the fee
math, look-ahead-free but economically meaningless exit logic, and more),
plus what an actually-valid backtest of this system would require. It's
included because the reasoning is more useful than a return number would
have been — which is also why no return number is quoted, here or there.

## Risk framework (structure, not current live parameters)

| Mechanism | Approach |
|---|---|
| Position sizing | Scales with signal-strength count, within a fixed per-trade margin cap |
| Stop-loss / take-profit | Exchange-side conditional orders (`closePosition=True`), reconciled against live exchange state every scan |
| Reconciliation | Local position record vs. exchange truth checked every 60s (majors) / every scan (all coins) — see above |
| Correlation control | Coordinated moves across correlated assets are capped rather than treated as independent opportunities |
| Auditability | Every gate that can block a trade is tallied on a schedule — a filter that silently stopped firing, or started blocking one direction entirely, surfaces automatically instead of being discovered by accident |

Specific numeric thresholds (leverage, exact stop-loss percentages, the
live support/resistance levels the KOL pipeline currently has registered)
are intentionally not reproduced here — they change frequently and aren't
really the engineering content; the mechanisms that compute and audit them
are.

## Tech stack

`Python` · `ccxt` (exchange integration) · `feedparser` (RSS) ·
`youtube_transcript_api` + local Whisper fallback (transcript extraction) ·
dependency-injected unit tests (no framework — plain functions and asserts)

## Note

This repository is a portfolio excerpt shared for academic application
purposes (graduate school admissions). The full system — the ~4,700-line
main scanning/trading loop, live order execution, the full risk-parameter
registry, VPS deployment, and a Telegram monitoring/alerting pipeline —
runs in a private repository and is not included here, both because most
of it is specific operational history rather than illustrative code, and
because the exact live strategy parameters aren't something I'm looking to
publish. No API keys, account data, or live trading records are part of
this excerpt; `check_algo_protection.py`'s default risk parameters are
representative values, not the currently-live configuration.

This project is for educational/research purposes and is not financial advice.
