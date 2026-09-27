# Trading Bot

## Installation

Create a fresh virtual environment for your operating system, then install
only the application's direct dependencies. The new name preserves the old
`.venv` directory until you have verified the replacement:

```bash
python -m venv .venv-kraken
```

Activate it, then install the dependencies:

```bash
# Linux/macOS
source .venv-kraken/bin/activate
# Windows PowerShell: .venv-kraken\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `config.env.example` to `config.env`, then set new Telegram credentials.
The bot uses Kraken's public market data and does not need exchange API keys.

## Run

```bash
python check_startup.py
python main.py
```

`main.py` is paper trading only: it simulates limit-order fills locally and
never sends trading orders to Kraken. Kraken's public OHLC endpoint returns at
most 720 candles per request, which can limit backtests on shorter intervals.

`bot/main.py` is a separate legacy Binance implementation that can submit real
orders. It has not been migrated; do not run it as part of this setup.