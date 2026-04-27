# 06 — Glossary

SMC / ICT terminology used throughout the project. When in doubt, use the
definitions here. If a term used in code or docs is not in this glossary,
add it.

---

**Accumulation**
First phase of the Power of 3 daily cycle. Price consolidates, building
liquidity above and below the range. Typically corresponds to the Asia
session in TJR's model.

**A+ / A / B (setup quality)**
Internal grading scale for detected setups. A+ has full confluence (clean
sweep, strong displacement MSS, FVG overlapping OTE, RR ≥ 4:1). A meets
all required criteria. B has one weakness but is still tradeable. Anything
worse is rejected and not notified.

**ATR**
Average True Range. Standard volatility measure. Used to filter FVGs by
size, swings by amplitude, etc.

**BSL / Buy-side Liquidity**
Pending buy-stop orders resting above swing highs (breakout buyers and
shorts' stop losses). Targeted by sweeps in bearish setups.

**BOS — Break of Structure**
Price breaking a previous swing high (bullish) or swing low (bearish) in
the direction of the existing trend. Indicates trend continuation.
Distinct from MSS, which is a break against the existing trend.

**Calibrated rule**
A deterministic detector whose parameters (lookback, multipliers,
tolerances) must be empirically tuned to match the operator's expert
visual reading. See `07_DETECTION_PHILOSOPHY.md`.

**CHoCH — Change of Character**
Synonym used by some SMC educators for what TJR / ICT call MSS. In this
project we use **MSS** consistently. CHoCH may appear in user discussion
but the code uses MSS.

**Daily Bias**
Directional expectation for the day. In this project: derived from H4 + H1
swing structure. Values: `bullish`, `bearish`, `no_trade`.

**Discount / Premium**
Halves of a price range. Below the 0.5 = discount (buying zone in bullish
context). Above = premium (selling zone in bearish context). OTE lives in
discount/premium depending on direction.

**Displacement**
A strong, impulsive price move with a large body. Often leaves an FVG
behind. Calibrated parameter in this project.

**Distribution**
Third phase of Power of 3. The directional move plays out after Asia
accumulation and London/NY manipulation.

**Equal Highs / Equal Lows (EQH / EQL)**
Multiple swing highs or lows clustered at nearly the same price. Strong
liquidity zones (many stops resting at the same level). Tolerance for
"equal" is configurable per instrument.

**FVG — Fair Value Gap**
A 3-candle pattern where the middle candle leaves a gap between the
high/low of candle 1 and the low/high of candle 3. Represents inefficient
price action that the market often returns to "fill."

**Heuristic rule**
A deterministic detector whose underlying rule choice is somewhat
arbitrary among reasonable alternatives (e.g., POI priority FVG > OB).
Documented as such; revisited based on data. See `07_DETECTION_PHILOSOPHY.md`.

**HH / HL / LH / LL**
Higher High / Higher Low / Lower High / Lower Low. The four building
blocks of swing-structure analysis. Bullish structure = HH + HL.
Bearish = LH + LL.

**HTF / LTF**
Higher Timeframe / Lower Timeframe. HTF in this project usually means
H4/H1 (sometimes D1). LTF means M15 / M5 / M1.

**IDM — Inducement**
A minor swing point inside a larger structure that retail traders are
likely to mistake for the main level. Smart money sweeps it before going
to the real liquidity. Out of scope for v1.

**Judgment layer**
Tasks that genuinely require contextual reasoning that a deterministic
rule cannot capture without significant loss of signal. Reserved for
either operator validation or LLM qualifier (Sprint 7+).

**Killzone**
Time windows of high-probability institutional activity. In this project:
London killzone = 09:00–12:00 Paris, NY killzone = 15:30–18:00 Paris.

**Liquidity / Liquidity Pool**
Areas of resting orders (stop losses + breakout orders). Found above swing
highs (BSL) and below swing lows (SSL). Smart money targets these to
fill large institutional orders.

**Liquidity Sweep**
Price moves into a liquidity pool, triggers the resting orders, and then
reverses. The reversal is the entry signal for SMC traders.

**Manipulation**
Second phase of Power of 3. The session that sweeps Asian liquidity
(typically London). Creates the false move that traps retail.

**MSS — Market Structure Shift**
A break of a recent swing high (in a downtrend) or swing low (in an
uptrend) by a candle body close, indicating a potential reversal. In this
project, the trigger event after a successful sweep.

**OB — Order Block**
The last opposite-colored candle before an impulsive move. Treated as a
zone where institutional orders were placed. Used as a fallback POI when
no clean FVG exists.

**OHLC**
Open / High / Low / Close. The four price points of a candle.

**OTE — Optimal Trade Entry**
The 0.62–0.79 retracement zone of an impulse leg (using Fibonacci). Best
entry quality in ICT methodology. In this project, used as a confluence
factor: setups where the FVG/OB overlaps OTE get bumped to A+.

**PDH / PDL**
Previous Day High / Previous Day Low. Major liquidity levels.

**POI — Point of Interest**
A zone where price might react. In this project: FVG (priority 1) or
Order Block (priority 2).

**Power of 3 (PO3)**
ICT's daily framework: Accumulation → Manipulation → Distribution.

**Pure logic**
A detector with an exact mathematical or logical definition and no tunable
parameters. The simplest category in `07_DETECTION_PHILOSOPHY.md`.

**Range**
A period of sideways price action between two levels. The Asia session
range is the canonical example in TJR's model.

**RR — Risk-Reward Ratio**
Ratio of potential profit to potential loss on a trade. Computed as
(TP distance) / (SL distance). Minimum 3:1 required for a setup to fire.

**Setup**
The complete entry candidate produced by the detection pipeline. Contains
direction, entry, SL, TP, RR, quality, and supporting context.

**SMC — Smart Money Concepts**
The broader trading methodology TJR's strategy belongs to. Heavily
inspired by ICT.

**SSL — Sell-side Liquidity**
Pending sell-stop orders resting below swing lows (breakout sellers and
longs' stop losses). Targeted by sweeps in bullish setups.

**Sweep**
See Liquidity Sweep.

**Swing High / Swing Low**
Local extremes in price. In this project, defined parametrically by a
**lookback** (number of candles each side that must be lower/higher) and
filtered by an **ATR-based amplitude threshold**. Calibrated per timeframe.

**TP / SL**
Take Profit / Stop Loss. Order types placed when entering a trade.
