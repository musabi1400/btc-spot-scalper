"""
main.py — FastAPI application entry point.
Refactored: imports from core/, strategy/, execution/, risk/, utils/ layers.
Only contains: AppState, BotLoop, API endpoints, WebSocket endpoint.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Engine as SqlaEngine
from sqlalchemy.orm import sessionmaker

from core.config import APP, RISK, STRATEGY, SYMBOL, TradingMode, ROUND_TRIP_FEE
from core.logging_setup import setup_logging, get_logger
from core.models import (
    Settings, Trade, BotLog, SignalHistory,
    build_engine, build_session_factory, db_session,
)
from core.exceptions import ScalperError, ExchangeAuthError
from strategy.engine import StrategyEngine, IndicatorSnapshot, ConfluenceResult
from execution.engine import ExecutionEngine, FillResult
from risk.manager import RiskManager
from utils.crypto import CredentialVault
from utils.websocket_hub import WebSocketHub

# Initialise logging
setup_logging(level=APP.log_level, log_dir=APP.log_dir,
             max_bytes=APP.log_max_bytes, backup_count=APP.log_backup_count)
logger = get_logger("main")


# ──────────────────────────────────────────────
#  Application State
# ──────────────────────────────────────────────

class AppState:
    """Holds all shared state: engines, risk manager, DB, WebSocket hub."""

    def __init__(self):
        self.engine: Optional[SqlaEngine] = None
        self.db_factory: Optional[sessionmaker] = None
        self.vault = CredentialVault(APP.encryption_key)
        self.strategy: Optional[StrategyEngine] = None
        self.execution: Optional[ExecutionEngine] = None
        self.risk: Optional[RiskManager] = None
        self.ws_hub = WebSocketHub()
        self.bot_task: Optional[asyncio.Task] = None
        self.bot_running: bool = False
        self.emergency_stop_flag: bool = False
        self.active_trade: Optional[Trade] = None
        self.latest_snapshot: Optional[dict] = None
        self.latest_confluence: Optional[dict] = None
        self.bot_state: str = "STOPPED"

    def get_settings(self) -> Settings:
        with db_session(self.db_factory) as session:
            settings = session.query(Settings).first()
            if not settings:
                settings = Settings(id=1)
                session.add(settings)
            return settings

    def log(self, level: str, message: str, context: Optional[dict] = None) -> None:
        with db_session(self.db_factory) as session:
            entry = BotLog(
                level=level, message=message,
                context=json.dumps(context) if context else None,
            )
            session.add(entry)
        log_level = getattr(__import__("logging"), level.upper(), __import__("logging").INFO)
        logger.log(log_level, message)


state = AppState()


# ──────────────────────────────────────────────
#  Bot Main Loop
# ──────────────────────────────────────────────

class BotLoop:
    """Orchestrates strategy evaluation, order execution, and trade management."""

    def __init__(self, app_state: AppState):
        self.s = app_state
        self._tick_interval = APP.strategy_tick_sec

    async def run(self) -> None:
        self.s.bot_running = True
        self.s.bot_state = "SEARCHING"
        self.s.log("INFO", "Bot loop started")
        try:
            while self.s.bot_running and not self.s.emergency_stop_flag:
                await self._tick()
                await asyncio.sleep(self._tick_interval)
        except asyncio.CancelledError:
            logger.info("Bot loop cancelled")
        except Exception as e:
            logger.error("Bot loop crashed: %s", e, exc_info=True)
            self.s.log("ERROR", f"Bot loop crashed: {e}")
        finally:
            self.s.bot_running = False
            self.s.bot_state = "STOPPED"
            self.s.log("INFO", "Bot loop stopped")

    async def _tick(self) -> None:
        can_trade, reason = self.s.risk.can_trade()
        if not can_trade:
            if self.s.bot_state != "HALTED":
                self.s.log("WARN", f"Trading halted: {reason}")
            self.s.bot_state = "HALTED"
            await self._broadcast_status()
            return

        if self.s.active_trade and self.s.active_trade.status in ("IN_TRADE", "FILLED_BUY"):
            await self._manage_active_trade()
            return

        self.s.bot_state = "SEARCHING"
        snapshot = await self.s.strategy.get_snapshot()
        if not snapshot:
            return

        confluence = self.s.strategy.evaluate_confluence(snapshot)
        self.s.latest_snapshot = snapshot.to_dict()
        self.s.latest_confluence = confluence.to_dict()
        self._record_signal(snapshot, confluence)
        await self._broadcast_status()

        if confluence.should_enter:
            await self._execute_entry(snapshot, confluence)

    async def _execute_entry(self, snap: IndicatorSnapshot, confluence: ConfluenceResult) -> None:
        self.s.log("TRADE", f"Entry signal — {confluence.reason}")
        balance = await self.s.execution.get_balance()
        available = balance["usdt_free"]
        if available < 10:
            self.s.log("WARN", f"Insufficient USDT: {available}")
            return

        swing_low = None
        if hasattr(self.s.strategy, '_ohlcv_5m') and not self.s.strategy._ohlcv_5m.empty:
            swing_low = float(self.s.strategy._ohlcv_5m["low"].tail(10).min())

        sl_tp = self.s.risk.calculate_sl_tp(snap.price, swing_low)
        sizing = self.s.risk.calculate_position_size(available, snap.price, sl_tp["sl_price"])

        self.s.log("INFO",
            f"Plan: ${sizing['size_usdt']}, {sizing['quantity_btc']} BTC, "
            f"SL={sl_tp['sl_price']} TP={sl_tp['tp_price']} R:R=1:{sl_tp['rr_ratio']}")

        ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
        fill = await self.s.execution.place_limit_buy(
            price=ticker.get("bid", snap.price),
            quantity=sizing["quantity_btc"],
            timeout_sec=RISK.order_fill_timeout_sec,
        )
        if fill is None:
            self.s.log("WARN", "Buy not filled — skipping")
            return

        trade = Trade(
            symbol=SYMBOL, order_id_buy=fill.order_id, side="BUY", status="IN_TRADE",
            entry_time=fill.timestamp, entry_price=fill.avg_fill_price,
            quantity_btc=fill.filled_qty, position_size_usdt=fill.filled_qty * fill.avg_fill_price,
            stop_loss_price=sl_tp["sl_price"], take_profit_price=sl_tp["tp_price"],
            sl_pct=sl_tp["sl_pct"], tp_pct=sl_tp["tp_pct"],
            fee_buy_usdt=fill.fee_paid, fees_total_usdt=fill.fee_paid,
            confluence_score=confluence.score,
            conditions_met=json.dumps(list(confluence.conditions.keys())),
            entry_5m_close=snap.price,
        )
        with db_session(self.s.db_factory) as session:
            session.add(trade)
            session.flush()
            session.refresh(trade)
            self.s.active_trade = trade

        self.s.bot_state = "IN_TRADE"
        self.s.log("TRADE", f"BUY FILLED: {fill.filled_qty} @ ${fill.avg_fill_price}")
        await self._broadcast_status()

    async def _manage_active_trade(self) -> None:
        trade = self.s.active_trade
        if not trade:
            return
        ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
        price = ticker.get("last", 0)
        if price <= 0:
            return

        entry = trade.entry_price
        sl = trade.stop_loss_price
        tp = trade.take_profit_price
        trail_sl = trade.trailing_sl_price or sl

        be = entry * (1 + ROUND_TRIP_FEE)
        new_sl = self.s.risk.update_trailing_stop(entry, price, trail_sl, be, original_sl_price=sl)
        if new_sl and new_sl > trail_sl:
            trail_sl = new_sl
            with db_session(self.s.db_factory) as session:
                db_t = session.get(Trade, trade.id)
                if db_t:
                    db_t.trailing_sl_price = new_sl
            trade.trailing_sl_price = new_sl
            self.s.log("INFO", f"Trailing SL -> ${new_sl}")

        should_exit = False
        reason = ""
        if price <= trail_sl:
            should_exit = True
            reason = "stop_loss" if trail_sl == sl else "trailing_stop"
        elif price >= tp:
            should_exit = True
            reason = "take_profit"

        if should_exit:
            await self._execute_exit(price, reason)
        else:
            await self._broadcast_status()

    async def _execute_exit(self, exit_price: float, reason: str) -> None:
        trade = self.s.active_trade
        if not trade:
            return
        self.s.log("TRADE", f"Exit: {reason} @ ${exit_price}")

        if reason == "take_profit":
            fill = await self.s.execution.place_limit_sell(
                price=trade.take_profit_price, quantity=trade.quantity_btc, timeout_sec=120)
        else:
            ticker = await self.s.execution.exchange.fetch_ticker(SYMBOL)
            fill = await self.s.execution.place_limit_sell(
                price=ticker.get("bid", exit_price) * 0.9995,
                quantity=trade.quantity_btc, timeout_sec=30)

        if fill is None:
            self.s.log("ERROR", "Exit failed — emergency sell")
            fill = await self.s.execution.emergency_sell_all()
            if fill is None:
                self.s.log("ERROR", "CRITICAL: Cannot exit — manual intervention!")
                return

        gross = (fill.avg_fill_price - trade.entry_price) * fill.filled_qty
        fees = trade.fee_buy_usdt + fill.fee_paid
        net = gross - fees
        ret = (net / trade.position_size_usdt) * 100 if trade.position_size_usdt > 0 else 0

        with db_session(self.s.db_factory) as session:
            db_t = session.get(Trade, trade.id)
            if db_t:
                db_t.order_id_sell = fill.order_id
                db_t.exit_time = fill.timestamp
                db_t.exit_price = fill.avg_fill_price
                db_t.exit_reason = reason
                db_t.fee_sell_usdt = fill.fee_paid
                db_t.fees_total_usdt = fees
                db_t.gross_pnl_usdt = gross
                db_t.net_pnl_usdt = net
                db_t.return_pct = ret
                db_t.status = "CLOSED"

        self.s.risk.record_trade_result(net)
        self.s.log("TRADE",
            f"CLOSED ({'PROFIT' if net > 0 else 'LOSS'}): "
            f"${trade.entry_price} -> ${fill.avg_fill_price} | "
            f"Net: ${net:.4f} ({ret:.3f}%) | {reason}")

        self.s.active_trade = None
        self.s.bot_state = "SEARCHING"
        await self._broadcast_status()

    async def _execute_emergency_exit(self) -> None:
        self.s.log("ERROR", "EMERGENCY STOP — liquidating")
        await self.s.execution.cancel_all_open_orders()
        if self.s.active_trade:
            fill = await self.s.execution.emergency_sell_all()
            if fill:
                gross = (fill.avg_fill_price - self.s.active_trade.entry_price) * fill.filled_qty
                fees = self.s.active_trade.fee_buy_usdt + fill.fee_paid
                net = gross - fees
                ret = (net / self.s.active_trade.position_size_usdt) * 100 if self.s.active_trade.position_size_usdt > 0 else 0
                with db_session(self.s.db_factory) as session:
                    db_t = session.get(Trade, self.s.active_trade.id)
                    if db_t:
                        db_t.exit_time = fill.timestamp
                        db_t.exit_price = fill.avg_fill_price
                        db_t.exit_reason = "emergency"
                        db_t.fee_sell_usdt = fill.fee_paid
                        db_t.fees_total_usdt = fees
                        db_t.gross_pnl_usdt = gross
                        db_t.net_pnl_usdt = net
                        db_t.return_pct = ret
                        db_t.status = "CLOSED"
                self.s.risk.record_trade_result(net)
                self.s.log("ERROR", f"Emergency done — Net: ${net:.4f}")
        self.s.active_trade = None
        self.s.bot_state = "STOPPED"
        await self._broadcast_status()

    def _record_signal(self, snap: IndicatorSnapshot, conf: ConfluenceResult) -> None:
        with db_session(self.s.db_factory) as session:
            session.add(SignalHistory(
                price=snap.price, confluence_score=conf.score,
                conditions_met=json.dumps(list(conf.conditions.keys())),
                conditions_detail=json.dumps(conf.details),
                action="ENTER" if conf.should_enter else "EVAL",
            ))

    async def _broadcast_status(self) -> None:
        balance = await self.s.execution.get_balance() if self.s.execution else None
        await self.s.ws_hub.broadcast({
            "type": "status",
            "bot_state": self.s.bot_state,
            "bot_running": self.s.bot_running,
            "balance": balance,
            "active_trade": self.s.active_trade.to_dict() if self.s.active_trade else None,
            "latest_snapshot": self.s.latest_snapshot,
            "latest_confluence": self.s.latest_confluence,
            "risk_status": self.s.risk.get_status() if self.s.risk else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


# ──────────────────────────────────────────────
#  Pydantic API Models
# ──────────────────────────────────────────────

class CredentialsUpdate(BaseModel):
    api_key: str
    api_secret: str

class ModeUpdate(BaseModel):
    mode: str

class AutoTradeToggle(BaseModel):
    enabled: bool

class EmergencyStopRequest(BaseModel):
    confirm: bool


# ──────────────────────────────────────────────
#  Engine init helper
# ──────────────────────────────────────────────

async def _init_engines(api_key: str, api_secret: str, mode: TradingMode) -> bool:
    try:
        if state.strategy:
            await state.strategy.stop()
        if state.execution:
            await state.execution.stop()
        state.strategy = StrategyEngine(api_key, api_secret, mode)
        state.execution = ExecutionEngine(api_key, api_secret, mode)
        await state.strategy.start()
        await state.execution.start()
        return True
    except Exception as e:
        logger.error("Engine init failed: %s", e, exc_info=True)
        state.log("ERROR", f"Engine init failed: {e}")
        return False


# ──────────────────────────────────────────────
#  FastAPI App + Lifespan
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting BTC Scalper...")
    state.engine = build_engine(APP.dsn)
    state.db_factory = build_session_factory(state.engine)
    state.risk = RiskManager(state.db_factory)

    settings = state.get_settings()
    if settings.api_key_encrypted:
        ak = state.vault.decrypt(settings.api_key_encrypted)
        as_ = state.vault.decrypt(settings.api_secret_encrypted)
        await _init_engines(ak, as_, TradingMode(settings.mode))

    state.log("INFO", "Application started")
    yield
    logger.info("Shutting down...")
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass
    if state.strategy:
        await state.strategy.stop()
    if state.execution:
        await state.execution.stop()
    state.log("INFO", "Application stopped")


app = FastAPI(
    title="BTC Spot Scalper",
    description="Automated Bitcoin scalping — Spot, no leverage, maker-only",
    version="2.0.0",
    lifespan=lifespan,
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ──────────────────────────────────────────────
#  REST API
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    p = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Dashboard not found</h1>", 404)


@app.get("/api/status")
async def get_status():
    balance = await state.execution.get_balance() if state.execution else None
    return {
        "bot_state": state.bot_state, "bot_running": state.bot_running,
        "mode": state.get_settings().mode, "auto_trade": state.get_settings().auto_trade,
        "balance": balance,
        "active_trade": state.active_trade.to_dict() if state.active_trade else None,
        "latest_snapshot": state.latest_snapshot,
        "latest_confluence": state.latest_confluence,
        "risk_status": state.risk.get_status() if state.risk else None,
        "ws_clients": state.ws_hub.client_count,
    }


@app.get("/api/settings")
async def get_settings_api():
    s = state.get_settings()
    return {
        "mode": s.mode, "auto_trade": s.auto_trade, "use_bnb_fee": s.use_bnb_fee,
        "has_api_key": bool(s.api_key_encrypted),
        "has_api_secret": bool(s.api_secret_encrypted),
    }


@app.post("/api/settings/credentials")
async def update_credentials(creds: CredentialsUpdate):
    enc_k = state.vault.encrypt(creds.api_key)
    enc_s = state.vault.encrypt(creds.api_secret)
    with db_session(state.db_factory) as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings(id=1)
            session.add(s)
        s.api_key_encrypted = enc_k
        s.api_secret_encrypted = enc_s
    mode = TradingMode(state.get_settings().mode)
    if await _init_engines(creds.api_key, creds.api_secret, mode):
        state.log("INFO", "Credentials updated — engines reconnected")
        return {"status": "ok", "message": "Credentials updated"}
    raise HTTPException(500, "Credentials saved but engine init failed")


@app.post("/api/settings/mode")
async def update_mode(mode_update: ModeUpdate):
    new_mode = mode_update.mode.lower()
    if new_mode not in ("demo", "live"):
        raise HTTPException(400, "Mode must be 'demo' or 'live'")
    with db_session(state.db_factory) as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings(id=1)
            session.add(s)
        s.mode = new_mode
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass
    s = state.get_settings()
    if s.api_key_encrypted:
        await _init_engines(
            state.vault.decrypt(s.api_key_encrypted),
            state.vault.decrypt(s.api_secret_encrypted),
            TradingMode(new_mode),
        )
    state.log("WARN", f"Mode changed to: {new_mode.upper()}")
    return {"status": "ok", "mode": new_mode}


@app.post("/api/settings/autotrade")
async def toggle_auto_trade(toggle: AutoTradeToggle):
    with db_session(state.db_factory) as session:
        s = session.query(Settings).first()
        if not s:
            s = Settings(id=1)
            session.add(s)
        s.auto_trade = toggle.enabled
    if toggle.enabled:
        if not state.strategy or not state.execution:
            raise HTTPException(400, "Engines not initialised — set credentials first")
        if not state.bot_running:
            state.bot_task = asyncio.create_task(BotLoop(state).run())
            state.log("INFO", "Auto-trading ENABLED")
    else:
        if state.bot_task:
            state.bot_task.cancel()
            try:
                await state.bot_task
            except asyncio.CancelledError:
                pass
            state.log("INFO", "Auto-trading DISABLED")
    return {"status": "ok", "auto_trade": toggle.enabled}


@app.post("/api/emergency-stop")
async def emergency_stop(req: EmergencyStopRequest):
    if not req.confirm:
        raise HTTPException(400, "Confirmation required")
    state.emergency_stop_flag = True
    if state.bot_task:
        state.bot_task.cancel()
        try:
            await state.bot_task
        except asyncio.CancelledError:
            pass
    await BotLoop(state)._execute_emergency_exit()
    state.emergency_stop_flag = False
    state.log("ERROR", "Emergency stop completed")
    return {"status": "ok", "message": "Emergency stop executed"}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    with db_session(state.db_factory) as session:
        trades = session.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
        return [t.to_dict() for t in trades]


@app.get("/api/performance")
async def get_performance():
    with db_session(state.db_factory) as session:
        trades = session.query(Trade).filter(Trade.status == "CLOSED").all()
        if not trades:
            return {"total_trades": 0, "win_rate": 0, "total_net_profit": 0,
                    "profit_factor": 0, "avg_win": 0, "avg_loss": 0, "total_fees": 0}
        wins = [t for t in trades if t.net_pnl_usdt > 0]
        losses = [t for t in trades if t.net_pnl_usdt < 0]
        gp = sum(t.net_pnl_usdt for t in wins)
        gl = abs(sum(t.net_pnl_usdt for t in losses))
        pf = gp / gl if gl > 0 else float("inf")
        return {
            "total_trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round((len(wins) / len(trades)) * 100, 2),
            "total_net_profit": round(sum(t.net_pnl_usdt for t in trades), 4),
            "profit_factor": round(pf, 2) if pf != float("inf") else 999,
            "avg_win": round(gp / len(wins), 4) if wins else 0,
            "avg_loss": round(gl / len(losses), 4) if losses else 0,
            "total_fees": round(sum(t.fees_total_usdt or 0 for t in trades), 4),
        }


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    with db_session(state.db_factory) as session:
        logs = session.query(BotLog).order_by(BotLog.timestamp.desc()).limit(limit).all()
        return [l.to_dict() for l in logs]

@app.get("/api/signals")
async def get_signals(limit: int = 50):
    """Get recent strategy signals (audit trail)."""
    with db_session(state.db_factory) as session:
        sigs = session.query(SignalHistory).order_by(SignalHistory.timestamp.desc()).limit(limit).all()
        return [s.to_dict() for s in sigs]


# ──────────────────────────────────────────────
#  Alerts API
# ──────────────────────────────────────────────

@app.get("/api/alerts")
async def get_alerts(limit: int = 20, unacknowledged: bool = False):
    """Get recent alerts."""
    try:
        from notifications.alerts import AlertManager
        mgr = AlertManager(state.db_factory)
        return mgr.get_recent_alerts(limit=limit, unacknowledged_only=unacknowledged)
    except Exception as e:
        logger.error("get_alerts: %s", e)
        return []


@app.post("/api/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int):
    """Acknowledge an alert."""
    try:
        from notifications.alerts import AlertManager
        mgr = AlertManager(state.db_factory)
        success = mgr.acknowledge_alert(alert_id)
        if success:
            return {"status": "ok", "id": alert_id}
        raise HTTPException(404, "Alert not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/alerts/acknowledge-all")
async def acknowledge_all_alerts():
    """Acknowledge all unacknowledged alerts."""
    try:
        from notifications.alerts import AlertManager
        mgr = AlertManager(state.db_factory)
        count = mgr.acknowledge_all()
        return {"status": "ok", "acknowledged": count}
    except Exception as e:
        raise HTTPException(500, str(e))


# ──────────────────────────────────────────────
#  Analytics API
# ──────────────────────────────────────────────

@app.get("/api/analytics/performance")
async def get_analytics_performance():
    """Get daily and monthly performance summaries."""
    try:
        from analytics.performance import PerformanceAnalyzer
        analyzer = PerformanceAnalyzer(state.db_factory)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = analyzer.daily_performance(today)
        monthly = analyzer.monthly_performance(
            datetime.now(timezone.utc).year,
            datetime.now(timezone.utc).month,
        )
        return {"daily": daily, "monthly": monthly}
    except Exception as e:
        logger.error("analytics/performance: %s", e)
        return {"daily": {}, "monthly": {}, "error": str(e)}


@app.get("/api/analytics/equity-curve")
async def get_analytics_equity_curve():
    """Get equity curve data."""
    try:
        from analytics.performance import PerformanceAnalyzer
        analyzer = PerformanceAnalyzer(state.db_factory)
        from datetime import timedelta
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        curve = analyzer.equity_curve(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return {"curve": curve}
    except Exception as e:
        logger.error("analytics/equity-curve: %s", e)
        return {"curve": [], "error": str(e)}


@app.get("/api/analytics/distribution")
async def get_analytics_distribution():
    """Get trade distribution by hour, day, exit reason, score."""
    try:
        from analytics.performance import PerformanceAnalyzer
        analyzer = PerformanceAnalyzer(state.db_factory)
        with db_session(state.db_factory) as session:
            from core.models import Trade
            trades = session.query(Trade).filter(Trade.status == "CLOSED").all()
            dist = analyzer.trade_distribution([t.to_dict() for t in trades])
        return dist
    except Exception as e:
        logger.error("analytics/distribution: %s", e)
        return {"error": str(e)}


@app.get("/api/analytics/signal-score")
async def get_analytics_signal_score():
    """Get current signal score with weighted scoring."""
    try:
        from analytics.scoring import SignalScorer
        scorer = SignalScorer()
        if state.latest_snapshot and state.latest_confluence:
            score = scorer.score_signal(state.latest_snapshot, state.latest_confluence)
            regime = scorer.get_market_regime(state.latest_snapshot)
            return {**score, "market_regime": regime}
        return {"total_score": 0, "recommendation": "skip", "market_regime": "unknown"}
    except Exception as e:
        logger.error("analytics/signal-score: %s", e)
        return {"total_score": 0, "recommendation": "skip", "error": str(e)}


# ──────────────────────────────────────────────
#  Monitoring / Health API
# ──────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """System health check endpoint."""
    try:
        from monitoring.health import HealthChecker
        checker = HealthChecker(state.db_factory)
        health = checker.run_all(
            strategy=state.strategy,
            execution=state.execution,
            risk_manager=state.risk,
        )
        status_code = 200 if health.status == "healthy" else 503
        from fastapi import Response
        return Response(
            content=json.dumps(health.to_dict()),
            media_type="application/json",
            status_code=status_code,
        )
    except Exception as e:
        logger.error("Health check failed: %s", e)
        from fastapi import Response
        return Response(
            content=json.dumps({"status": "unhealthy", "error": str(e)}),
            media_type="application/json",
            status_code=503,
        )

@app.get("/api/monitoring/health")
async def get_monitoring_health():
    """Get detailed health status (alias for /health with more info)."""
    try:
        from monitoring.health import HealthChecker
        checker = HealthChecker(state.db_factory)
        return checker.run_all(
            strategy=state.strategy,
            execution=state.execution,
            risk_manager=state.risk,
        ).to_dict()
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.get("/api/monitoring/watchdog")
async def get_watchdog_status():
    """Get watchdog status."""
    try:
        if hasattr(state, 'watchdog') and state.watchdog:
            return state.watchdog.get_status()
        return {"running": False, "message": "Watchdog not initialised"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/monitoring/resources")
async def get_resource_usage():
    """Get current resource usage summary."""
    try:
        from monitoring.resource_monitor import ResourceMonitor
        monitor = getattr(state, 'resource_monitor', None)
        if not monitor:
            monitor = ResourceMonitor()
            state.resource_monitor = monitor
        monitor.sample()
        return monitor.get_summary()
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────
#  WebSocket
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await state.ws_hub.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()})
            elif data == "status":
                balance = await state.execution.get_balance() if state.execution else None
                await ws.send_json({
                    "type": "status", "bot_state": state.bot_state,
                    "bot_running": state.bot_running, "balance": balance,
                    "active_trade": state.active_trade.to_dict() if state.active_trade else None,
                    "latest_snapshot": state.latest_snapshot,
                    "latest_confluence": state.latest_confluence,
                    "risk_status": state.risk.get_status() if state.risk else None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
    except WebSocketDisconnect:
        state.ws_hub.disconnect(ws)
    except Exception as e:
        logger.error("WS error: %s", e)
        state.ws_hub.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=APP.host, port=APP.port,
                log_level=APP.log_level.lower(), ws_ping_interval=20, ws_ping_timeout=60)