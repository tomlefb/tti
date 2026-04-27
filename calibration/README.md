# Calibration

Empirical calibration of rule-based detectors against the operator's eye.
See `docs/07_DETECTION_PHILOSOPHY.md` for the full protocol.

## Layout

```
calibration/
├── README.md              # this file
├── reference_charts/      # operator-marked reference data (committed)
│   └── <sprint>_<symbol>_<scenario>/
│       ├── ohlc.parquet           # the raw candles for the period
│       ├── annotations.json       # operator's manual marks (swings,
│       │                          # liquidity levels, sweeps, MSS, FVGs)
│       └── notes.md               # what makes this scenario useful
└── runs/                  # output of calibration runs (gitignored)
    └── <date>_<detector>/
        ├── params.json            # the threshold values tested
        ├── results.json           # per-chart agreement metrics
        ├── diffs/                 # rendered comparison images
        └── report.md              # summary + recommended params
```

## How to run a calibration session

1. Pick the detector to calibrate (e.g. `swings` for Sprint 1).
2. Make sure at least 5 reference charts exist in `reference_charts/`
   covering varied conditions for that detector.
3. Run the detector across the reference charts with several parameter
   combinations.
4. Compare detector output to operator annotations; record agreement
   per-chart and aggregate.
5. Write a report under `runs/<date>_<detector>/report.md` with the
   recommended parameter values.
6. The operator (NOT Claude Code) decides whether to commit those values
   to `config/settings.py.example`.

## Hard rules

- Calibration runs are gitignored — only reports / reference charts are
  committed.
- Never tune on the same data used to validate. Hold out at least 30%.
- A detector is only "calibrated" once it ships with a checked-in report.
