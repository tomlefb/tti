# TJR Trading System

Automated SMC/ICT setup detector based on TJR Trades' methodology. Sends
Telegram notifications with annotated chart screenshots when a valid setup
is found on XAUUSD, NDX100, EURUSD, or GBPUSD during London/NY killzones.

**The system never trades. It only notifies. The human executes manually.**

## Quick start

### Prerequisites

- A Windows machine with MetaTrader 5 installed and connected to a broker account.
- Python 3.11+ on the Windows machine.
- A Telegram account.

### Setup (Windows host)

```powershell
# 1. Clone the repo
git clone <your-repo-url>
cd tjr-trading-system

# 2. Create venv and install deps
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
copy config\settings.py.example config\settings.py
copy config\secrets.py.example config\secrets.py
notepad config\secrets.py    # fill in Telegram token + chat ID + MT5 creds

# 4. Make sure MT5 terminal is open and logged in to your account

# 5. Run a connectivity test (Sprint 0 deliverable)
python scripts\test_mt5.py
python scripts\test_telegram.py
```

### Setup (Mac dev machine)

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd tjr-trading-system

# 2. Create venv with Python 3.11 and install Mac deps
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements-mac.txt

# 3. Note: MT5 and Telegram smoke tests can only be run on the
#    Windows host. The Mac is for dev/test/lint only.
```

### Project layout

```
tjr-trading-system/
├── CLAUDE.md             # auto-loaded context for Claude Code
├── README.md             # this file
├── requirements.txt
├── docs/                 # all reference documentation
├── config/               # settings + secrets templates
└── src/                  # source code (filled in across sprints)
```

## Documentation

Start with `docs/00_PROJECT_CONTEXT.md` for the why, then
`docs/07_DETECTION_PHILOSOPHY.md` for *how* we decide what gets coded vs
deferred to LLM/human judgment, then `docs/01_STRATEGY_TJR.md` for the
trading logic that drives everything.

## Status

Sprint 0 — scaffolding. See `docs/03_ROADMAP.md` for progress.

## License

Private project. Not for redistribution.
