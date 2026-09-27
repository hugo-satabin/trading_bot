# Trading Bot

## Installation

Create a fresh virtual environment for your operating system, then install
only the application's direct dependencies:

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

Set `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `KRAKEN_API_KEY`, and
`KRAKEN_API_SECRET` in the local `config.env`. The file is ignored by Git.
The Kraken API key needs permission to query funds and create/cancel orders;
do not grant withdrawal permission. Never paste secrets into source files or
chat messages.

## Run

```bash
python check_startup.py
python main.py
```

`check_startup.py` checks public markets and authenticated balance access; it
does not place a test order or prove that the key has trading permission.
`main.py` places real Kraken Spot orders using the EUR pairs in `config.env`.
`INITIAL_BALANCE` caps the bot's budget, which is also capped by Kraken's free
EUR balance. Use only funds you can afford to lose. Stop-loss and take-profit thresholds
are monitored by this process, not guaranteed exchange-side protective orders;
keep the process and network connection available. Unknown open Kraken orders
block startup and are never canceled automatically. Live account state is
stored separately in `trading_bot_live.db`, not in the old paper-trading DB.

Kraken's public OHLC endpoint returns at most 720 candles per request, which
can limit backtests on shorter intervals.
