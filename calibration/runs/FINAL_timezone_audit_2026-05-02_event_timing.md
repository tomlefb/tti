# Event-timing spot check

The lag scan (`lag_scan.md`) already establishes the offset
empirically and to high resolution; this section is a sanity check
on a single named market event.

## Caveat on the first attempted event

NFP release on 2026-04-03 was the original test target, but
**2026-04-03 was Good Friday**: CME index futures and the OTC gold
market were closed for the bulk of the day, Dukascopy returned an
empty window, Databento had ~5 sparse bars, and only the broker CFD
quote (MT5) showed continuous activity at low volatility. Useless
for alignment work.

This is the right place to remember that sampling around macro
events for cross-source verification needs a market-calendar check
first; an "event" with one source closed proves nothing.

## What the lag-scan already proves

The empirical offset detected on multiple full-month windows is:

- `+180 min` during EU DST (Apr → Oct, broker = EEST = UTC+3)
- `+120 min` during EET (Nov → Mar, broker = UTC+2)

The shift of exactly `−60 min` across the EU DST boundary (last Sunday
of October) is the conclusive signature: this is a regular wallclock
behaviour of a server running on Athens / Cyprus time, not random
data corruption nor a per-instrument quirk.

A pinpoint event-time check would only confirm what the 12 single-month
cells of `lag_scan.md` already establish at correlation 0.95+ each.
The investigation skipped re-attempting the event check after the
holiday miss.
