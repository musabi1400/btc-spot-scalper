# BTC Spot Scalper

Automated Bitcoin scalping bot for **Binance Spot** (no leverage, maker-only orders).

## Quick Start

```bash
# 1. Clone & enter
cd btc-scalper

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — set ENCRYPTION_KEY (generate with: python3 -c "import secrets; print(secrets.token_hex(32))")

# 5. Run locally
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 6. Open dashboard
#    http://localhost:8000
```

## Ubuntu Production Deployment

```bash
# From the project directory:
sudo bash deploy/install.sh

# The script will:
#   • Install system deps (Python, Nginx)
#   • Create /opt/btc-scalper with venv
#   • Install systemd service + Nginx reverse proxy
#   • Start the service
```

### Manual Service Commands

```bash
sudo systemctl start btc-scalper
sudo systemctl stop btc-scalper
sudo systemctl restart btc-scalper
sudo systemctl status btc-scalper
journalctl -u btc-scalper -f          # live logs
```

## Architecture

```
btc-scalper/
├── config.py          # Central configuration (strategy params, risk limits, fees)
├── models.py          # SQLAlchemy ORM (trades, logs, settings, signals, daily_stats)
├── strategy.py        # StrategyEngine: OHLCV fetch, indicators, confluence evaluation
├── execution.py       # ExecutionEngine: ccxt.pro limit orders, fee tracking, fill management
├── main.py            # FastAPI app: REST API, WebSocket, RiskManager, BotLoop
├── static/
│   ├── index.html     # Dashboard (Tailwind CSS, dark theme)
│   ├── css/dashboard.css
│   └── js/dashboard.js
├── deploy/
│   ├── install.sh     # Ubuntu installation script
│   ├── btc-scalper.service   # Systemd unit
│   └── nginx-btc-scalper     # Nginx config
├── requirements.txt
├── .env.example
└── README.md
```

## Strategy Overview

| Component | Detail |
|---|---|
| **Symbol** | BTC/USDT Spot |
| **Timeframe** | 5m (execution) + 15m (context) |
| **Indicators** | VWAP, EMA 9/21/50, Volume SMA 20, RSI 14, Order Book Depth |
| **Entry** | 3/5 confluence conditions (C1 mandatory: bullish EMA trend) |
| **Orders** | LIMIT ONLY (Maker / Post-Only) — never Market |
| **Min Profit** | 0.5% gross (clears fees + net profit) |
| **Stop Loss** | 0.3%-0.5% below entry or recent swing low |
| **R:R** | Minimum 1:1.5 |
| **Trailing** | Break-even at 1R, then trail at 1R distance |
| **Circuit Breaker** | 3 daily losses → 24h halt |
| **Max Concurrent** | 1 trade |
| **Fees** | 0.075% maker (with BNB discount) |

## Risk Management Rules

1. **Max risk per trade:** 1% of total balance
2. **Max position size:** 30% of available USDT
3. **Daily loss limit:** 3 consecutive losing trades
4. **Cooldown:** 24 hours after circuit breaker
5. **No leverage:** Position size ≤ available capital

## Dashboard Features

- 🔀 Demo/Live mode toggle
- 🔑 Encrypted credential storage
- 📊 Real-time indicators + confluence checklist
- 🎯 Active position monitor
- 📈 Performance metrics (win rate, profit factor, net PnL)
- 📋 Trading journal table
- 📡 Live log feed
- ⛔ Emergency stop button (cancels all + liquidates)

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard UI |
| GET | `/api/status` | Bot state, balance, active trade |
| GET | `/api/settings` | Current settings (no secrets) |
| POST | `/api/settings/credentials` | Save API keys (encrypted) |
| POST | `/api/settings/mode` | Switch demo/live |
| POST | `/api/settings/autotrade` | Enable/disable bot |
| POST | `/api/emergency-stop` | Emergency liquidation |
| GET | `/api/trades` | Trade history |
| GET | `/api/performance` | Performance metrics |
| GET | `/api/logs` | Bot logs |
| GET | `/api/signals` | Strategy signal audit trail |
| WS | `/ws` | Real-time updates stream |

## Security Notes

- API credentials are **encrypted with AES-256 (Fernet)** before storing in the database.
- The `ENCRYPTION_KEY` must be set in `.env` — keep it secret.
- For production: always use HTTPS (Certbot/Let's Encrypt).
- The systemd service runs with security hardening (NoNewPrivileges, ProtectSystem, etc.).
- Binance API keys should have **Spot trading only** — no withdrawal permissions.

## Disclaimer

This software is for educational purposes. Cryptocurrency trading carries significant risk.
Always test in DEMO mode first. Never trade with money you cannot afford to lose.