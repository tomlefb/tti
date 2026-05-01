"""Broker calibration scripts (FundedNext MT5).

Empirical extraction of execution conditions: historical trade
slippage, live spread distributions, and instrument specs. Outputs
feed the realistic execution model in the backtest engine.

Not part of the production runtime — these scripts are run manually
on the Windows host where MT5 is connected.
"""
