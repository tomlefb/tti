# 01 — TJR Trading Strategy (Formalized)

This document is the **single source of truth** for what counts as a valid
setup.

> **Read `07_DETECTION_PHILOSOPHY.md` alongside this doc.** This file
> describes *what* must be detected. The philosophy doc describes *how to
> think about detecting it* — in particular, which detectors are pure
> logic, which require empirical calibration against the operator's eye,
> which are heuristic choices, and which belong to a judgment layer.

If you (developer or Claude Code) believe a rule needs to change, update
this doc first, then update the code.

---

## 1. Strategy overview

The TJR strategy is an SMC/ICT-derived approach built around the **Power of 3**
daily cycle: Accumulation → Manipulation → Distribution.

Daily cycle interpretation:

- **Asia session = Accumulation**: market consolidates, forming a range that
  defines two liquidity pools (Asian High = buy-side liquidity, Asian Low =
  sell-side liquidity).
- **London session = Manipulation**: price sweeps one of the Asian pools,
  trapping retail traders.
- **NY session = Distribution**: directional move plays out after the sweep.

The trader looks for: a **liquidity sweep** of a key level → a **market
structure shift (MSS)** on a lower timeframe → an entry on the resulting
**FVG** or **Order Block** → target the opposing liquidity pool with a
minimum 3:1 RR.

---

## 2. Sessions (Paris time)

| Session | Paris time | EST time | Purpose |
|---|---|---|---|
| Asia | 02:00 – 06:00 | 20:00 – 00:00 (prev day) | Range formation. **Do not trade.** |
| London killzone | 09:00 – 12:00 | 03:00 – 06:00 | Trade window |
| NY killzone | 15:30 – 18:00 | 09:30 – 12:00 | Trade window |

Notes:

- "Killzone" here means the time during which we are looking for entries.
- Outside London/NY killzones: **no notifications fire**, even if all setup
  conditions are met.
- DST handling: implementation must convert correctly across DST boundaries.
  US DST and EU DST do not align — use `zoneinfo` and let it handle the math.

**Detection category**: pure logic (see `07_DETECTION_PHILOSOPHY.md`).

---

## 3. Daily Bias (mandatory pre-filter)

**No setup is valid without a clear daily bias aligned with it.** This is the
filter that the operator was historically skipping, and it is the most
important determinant of whether a setup is taken.

### Method: swing-structure on H4 + H1

Bias is determined by analyzing the swing structure on **H4 and H1
simultaneously**. They must agree.

**Detection category**: calibrated rule. The naive 3-bar fractal produces
too much noise; the implementation must use a **lookback parameter** and an
**ATR-based amplitude filter**, both calibrated against the operator's
visual reading on a reference set of charts. See
`07_DETECTION_PHILOSOPHY.md` section "Calibration protocol".

### Swing point definition (parameterized)

A **swing high** is a candle whose `high` is strictly greater than the `high`
of the `N` candles immediately before AND the `N` candles immediately after
it (N = `SWING_LOOKBACK`, default 2).

A **swing low** is the symmetric definition.

A swing point is therefore confirmed only **after `N` more candles have
closed** following the pivot.

### Significant-swing filter

After raw swings are detected, filter:

- A swing only counts as **significant** if its amplitude vs the previous
  significant swing of opposite type is `>= MIN_SWING_AMPLITUDE_ATR_MULT × ATR(14)`.
- Default `MIN_SWING_AMPLITUDE_ATR_MULT = 0.5`. To be calibrated.

### Bias rules

Look at the last `BIAS_SWING_COUNT` (default 4) significant swing points on
H4 and H1 separately:

- **Bullish bias on a timeframe**: sequence of Higher Highs (HH) and Higher
  Lows (HL). Specifically: each new significant swing high > previous
  significant swing high, AND each new significant swing low > previous
  significant swing low.
- **Bearish bias on a timeframe**: sequence of Lower Highs (LH) and Lower
  Lows (LL).
- **Neutral / no bias**: anything else (mixed structure, broken pattern,
  insufficient data).

### Recent structure break — heuristic

If the most recent significant swing **breaks** the prior pattern (e.g.,
4 HH/HL then a sudden LL), the bias is **neutral** for the day. This is a
heuristic choice; alternatives (e.g., "trust the most recent swing as the
new bias") may perform differently — to be revisited based on data.

**Detection category**: heuristic rule. Document the choice; revisit after
seeing performance.

### Final daily bias

- `bullish` if H4 = bullish AND H1 = bullish
- `bearish` if H4 = bearish AND H1 = bearish
- `no_trade` otherwise

`no_trade` means the system fires zero notifications for that pair that day.

### Bias refresh

Bias is recomputed at the start of each killzone (09:00 Paris and 15:30 Paris).
It is **not** recomputed mid-killzone — once the killzone starts, the bias
is locked.

---

## 4. Liquidity points to mark (HTF)

Before each killzone, the system marks the following levels for each
instrument:

1. **Asian High** and **Asian Low** (high/low of the Asia session 02:00–06:00 Paris)
2. **Previous Day High (PDH)** and **Previous Day Low (PDL)** (D1 candle close)
3. **Swing H1** highs and lows (last 5 confirmed significant swings on H1)
4. **Swing H4** highs and lows (last 5 confirmed significant swings on H4)
5. **Equal Highs / Equal Lows**: clusters of swing points within
   `INSTRUMENT_CONFIG[symbol].equal_hl_tolerance`. These are high-priority
   liquidity zones.

**Detection categories**:

- Asian range, PDH/PDL: pure logic.
- Swing levels: calibrated rule (depends on the swing detector).
- Equal H/L: calibrated rule (tolerance per instrument needs calibration).

These levels are the candidates that price may sweep.

---

## 5. The setup (entry workflow)

### Step 1 — Liquidity sweep detection

A **sweep** occurs when:

1. During an active killzone (London or NY)
2. On M5 (or M1 for refinement, but M5 is the primary)
3. Price's **wick** crosses one of the marked liquidity levels (in the
   direction opposite to the daily bias — i.e. for a bullish bias, we want a
   sweep of a low; for a bearish bias, we want a sweep of a high)
4. The candle's **close** returns back across the level (within the same
   candle, OR within the next 1–2 candles maximum)
5. The wick exceeds the level by at least the configured **sweep buffer** for
   the instrument (to filter noise; values defined in `config/settings.py`)

**Detection category**: calibrated rule. Both the sweep buffer per
instrument AND the "1–2 candles" return window are heuristics that require
empirical validation. Default values:

- XAUUSD: 1.0 USD
- NAS100: 5 points
- EURUSD: 5 pips
- GBPUSD: 5 pips
- Return window: 2 candles after the wick

If multiple sweeps occur in close succession on the same instrument, the
**most recent qualifying sweep** is the trigger. Heuristic; alternatives
(prioritize the largest sweep, or the one of the most significant level)
may be tested later.

Sweep alignment with bias:

- Bullish bias → only sweeps of **lows** trigger candidate setups
- Bearish bias → only sweeps of **highs** trigger candidate setups

### Step 2 — MSS confirmation (M5)

After a valid sweep, the system watches M5 for a Market Structure Shift:

- For a **bullish setup** (sweep of a low, bullish bias):
  - Identify the **most recent significant swing high** on M5 that formed
    during the pullback before/around the sweep.
  - MSS confirmed when an M5 candle **closes** (body close, not just wick)
    above that swing high.
- For a **bearish setup**: symmetric.

The MSS move should be **impulsive** (displacement). Formalized as:

- The MSS-confirming candle (or one of the 1-3 candles forming the move)
  must have a **body** at least `MSS_DISPLACEMENT_MULTIPLIER` × the average
  body of the previous `MSS_DISPLACEMENT_LOOKBACK` M5 candles.
  Defaults: 1.5 and 20 respectively.

**Detection category**: calibrated rule. The displacement multiplier is the
key parameter to tune.

### Step 3 — POI identification (entry zone)

Once MSS is confirmed, identify the entry POI in priority order:

1. **FVG** created by the displacement move:
   - Standard 3-candle FVG: gap between candle 1's high and candle 3's low
     (bullish) or candle 1's low and candle 3's high (bearish).
   - FVG must be `>= FVG_MIN_SIZE_ATR_MULTIPLIER × ATR(FVG_ATR_PERIOD)` on M5.
     Defaults: 0.3 and 14.
2. **Order Block**:
   - Last opposite-colored candle before the displacement move.
   - Used as fallback if no qualifying FVG exists.

**Detection categories**:

- FVG geometric detection: pure logic.
- FVG size filter: calibrated rule.
- Order Block: pure logic (once "displacement" is defined).
- POI priority (FVG > OB): heuristic.

Optional confluence (does NOT make or break the setup, but logged for
later analysis):

- **OTE (Optimal Trade Entry) zone**: 0.62 – 0.79 Fib retracement of the
  displacement leg. Setups where the FVG/OB overlaps the OTE zone are
  flagged as **A+ quality**.

### Step 4 — Entry, SL, TP

**Entry**: limit order at the proximal edge of the FVG (or OB).

- For a bullish setup: limit buy at the upper edge of the FVG.
- For a bearish setup: limit sell at the lower edge of the FVG.

**Stop loss**:

- Just beyond the sweep extreme (the wick that swept the liquidity).
- Plus `INSTRUMENT_CONFIG[symbol].sl_buffer`.

**Take profit**:

- The **opposing liquidity pool** (e.g., if we swept Asian Low, target Asian
  High; if we swept PDL, target PDH; etc.).
- The system picks the **nearest opposing liquidity** that yields a RR ≥ 3:1.
- If no opposing liquidity yields RR ≥ 3:1, the setup is **rejected** (no
  notification).

**Detection category**: pure logic (arithmetic).

### Step 5 — Setup quality grading

Each notification carries a quality label:

- **A+**: All of: clean sweep, MSS with strong displacement, FVG + OB overlap,
  OTE confluence, RR ≥ `A_PLUS_RR_THRESHOLD` (default 4.0).
- **A**: Clean sweep, MSS, FVG present, RR ≥ `MIN_RR` (default 3.0).
- **B**: All required conditions met but with one weakness (e.g. small FVG,
  weak displacement, RR exactly 3:1).

Below B → rejected, no notification.

**Detection category**: heuristic. The grading rules are an opinionated
empilement of booleans; alternatives (numeric scoring, or LLM-based
qualification in Sprint 7) are valid.

---

## 6. Hard invalidation filters

A setup is **rejected outright** (regardless of all other conditions) if:

- Daily bias is `no_trade`.
- Outside London or NY killzone.
- High-impact news within ±30 minutes (NFP, FOMC, CPI, ECB, BoE rate decisions).
  News calendar to be integrated; for Sprint 1–4, news filter may be a
  manual on/off switch in settings.
- Already 2 setups taken on this pair today.
- Already 2 SL hit today across all pairs (daily stop).
- Funded Next daily loss limit reached (see `05_TRADING_RULES.md`).

**Detection category**: pure logic (comparisons).

---

## 7. Per-instrument notes

- **XAUUSD**: very volatile, sweeps often very clean. Buffer larger to
  account for spread spikes.
- **NAS100**: best traded during NY killzone. London moves can be choppy.
  Watch for cash open volatility at 15:30 Paris.
- **EURUSD**: best during London killzone. NY can be slow unless USD news.
- **GBPUSD**: most volatile in London. Strong sweeps in 09:00–10:30 Paris
  window. Wider stops needed.

---

## 8. What this doc deliberately does NOT cover

The following are **out of scope** for v1 and should not be implemented
unless explicitly added to a later sprint:

- Multi-timeframe FVG mitigation (HTF FVG as draw on liquidity)
- Order Block volume filtering
- Inducement (IDM) detection
- Power of 3 candle pattern on the daily timeframe
- Inversed FVG (iFVG) entries
- Breaker blocks
- Premium/Discount arrays beyond simple OTE
- Market regime detection (trending vs ranging vs dead)
- Cross-asset confluence (e.g., DXY direction influencing EUR/GBP setups)

These are valid concepts but adding them now would explode complexity
before we have a baseline. Add only after measuring v1's performance.
Several of these are good candidates for a **judgment-layer LLM qualifier**
(Sprint 7) rather than additional rules.
