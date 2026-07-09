"""
tests/test_analytics.py
========================
Unit tests for the Phase-3 analytics layer:
    - SignalScorer         (scoring / regime detection / weight learning)
    - PerformanceAnalyzer  (in-memory SQLite + synthetic trades)
    - TradeRecorder        (create / retrieve analytics records)

No network access required.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from analytics.models import TradeAnalytics, init_analytics_tables
from analytics.recorder import TradeRecorder
from analytics.performance import PerformanceAnalyzer
from analytics.scoring import SignalScorer, DEFAULT_WEIGHTS
from core.models import Base, Trade, build_session_factory


# ──────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────

@pytest.fixture
def db_factory():
    """In-memory SQLite session factory with all tables created."""
    engine = create_engine("sqlite://", echo=False, future=True)
    # Core tables + analytics table on the same Base.metadata.
    init_analytics_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def engine():
    e = create_engine("sqlite://", echo=False, future=True)
    init_analytics_tables(e)
    return e


# ──────────────────────────────────────────────
#  Helper: build an IndicatorSnapshot-like dict
# ──────────────────────────────────────────────

def make_snapshot(**overrides) -> dict:
    snap = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price": 100_200.0,
        "ema9": 100_150.0,
        "ema21": 100_100.0,
        "ema50": 99_900.0,
        "vwap": 100_050.0,
        "current_volume": 25.0,
        "volume_sma20": 15.0,
        "volume_ratio": 1.7,
        "rsi": 52.0,
        "bid_wall_strength": 0.002,
        "bid_wall_within_range": True,
        "trend_15m": "bullish",
        "ema21_15m": 100_050.0,
        "ema50_15m": 99_950.0,
    }
    snap.update(overrides)
    return snap


def make_confluence(**overrides) -> dict:
    """ConfluenceResult.to_dict() shape with controllable conditions."""
    confluence = {
        "score": 5,
        "conditions": {
            "c1_bullish_trend": True,
            "c2_vwap_position": True,
            "c3_volume_spike": True,
            "c4_rsi_zone": True,
            "c5_bid_wall": True,
        },
        "details": {},
        "should_enter": True,
        "reason": "Entry: 5/5 conditions, C1 satisfied",
    }
    confluence.update(overrides)
    return confluence


def seed_trade(db_factory, **kwargs) -> Trade:
    """Insert a CLOSED trade row and return its dict."""
    defaults = dict(
        symbol="BTC/USDT",
        side="BUY",
        status="CLOSED",
        entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
        entry_price=100_000.0,
        quantity_btc=0.01,
        position_size_usdt=1_000.0,
        exit_time=datetime.now(timezone.utc),
        exit_price=100_500.0,
        exit_reason="take_profit",
        stop_loss_price=99_600.0,
        take_profit_price=100_600.0,
        sl_pct=0.4,
        tp_pct=0.6,
        fee_buy_usdt=0.075,
        fee_sell_usdt=0.075,
        gross_pnl_usdt=5.0,
        net_pnl_usdt=4.85,
        fees_total_usdt=0.15,
        return_pct=0.485,
        confluence_score=4,
        conditions_met=json.dumps(["c1_bullish_trend", "c2_vwap_position"]),
    )
    defaults.update(kwargs)
    from core.models import db_session
    with db_session(db_factory) as session:
        t = Trade(**defaults)
        session.add(t)
        session.flush()
        session.refresh(t)
        tid = t.id
    # re-fetch as dict
    with db_session(db_factory) as session:
        t = session.query(Trade).get(tid)
        return t.to_dict(), tid


# ──────────────────────────────────────────────
#  SignalScorer
# ──────────────────────────────────────────────

class TestSignalScorer:
    def test_all_conditions_met_scores_100(self):
        scorer = SignalScorer()
        result = scorer.score_signal(make_snapshot(), make_confluence())
        assert result["weighted_score"] == pytest.approx(100.0, abs=0.01)
        assert result["recommendation"] == "enter"
        assert result["c1_mandatory_passed"] is True
        assert 0.0 <= result["confidence"] <= 1.0

    def test_c1_fail_forces_zero(self):
        scorer = SignalScorer()
        confluence = make_confluence()
        confluence["conditions"]["c1_bullish_trend"] = False
        confluence["score"] = 4
        result = scorer.score_signal(make_snapshot(), confluence)
        assert result["weighted_score"] == 0.0
        assert result["recommendation"] == "skip"
        assert result["c1_mandatory_passed"] is False

    def test_enter_threshold(self):
        scorer = SignalScorer()
        # C1 + C2 + C3 = 0.30+0.20+0.20 = 0.70 → 70 → enter
        confluence = make_confluence()
        confluence["conditions"]["c4_rsi_zone"] = False
        confluence["conditions"]["c5_bid_wall"] = False
        confluence["score"] = 3
        result = scorer.score_signal(make_snapshot(), confluence)
        assert result["weighted_score"] == pytest.approx(70.0, abs=0.01)
        assert result["recommendation"] == "enter"

    def test_wait_zone(self):
        scorer = SignalScorer()
        # C1 + C3 = 0.30 + 0.20 = 0.50 → 50 → wait
        confluence = make_confluence()
        confluence["conditions"]["c2_vwap_position"] = False
        confluence["conditions"]["c4_rsi_zone"] = False
        confluence["conditions"]["c5_bid_wall"] = False
        confluence["score"] = 2
        result = scorer.score_signal(make_snapshot(), confluence)
        assert result["weighted_score"] == pytest.approx(50.0, abs=0.01)
        assert result["recommendation"] == "wait"

    def test_skip_zone(self):
        scorer = SignalScorer()
        # C1 only = 30 → skip
        confluence = make_confluence()
        for k in ("c2_vwap_position", "c3_volume_spike", "c4_rsi_zone", "c5_bid_wall"):
            confluence["conditions"][k] = False
        confluence["score"] = 1
        result = scorer.score_signal(make_snapshot(), confluence)
        assert result["weighted_score"] == pytest.approx(30.0, abs=0.01)
        assert result["recommendation"] == "skip"

    def test_custom_weights_normalise(self):
        # Provide weights that don't sum to 1 — should be normalised.
        scorer = SignalScorer(weights={
            "c1_bullish_trend": 3.0,
            "c2_vwap_position": 2.0,
            "c3_volume_spike": 2.0,
            "c4_rsi_zone": 1.5,
            "c5_bid_wall": 1.5,
        })
        total = sum(scorer.weights.values())
        assert total == pytest.approx(1.0, abs=1e-9)
        # All met → 100
        result = scorer.score_signal(make_snapshot(), make_confluence())
        assert result["weighted_score"] == pytest.approx(100.0, abs=0.01)

    def test_individual_scores_sum_to_weighted(self):
        scorer = SignalScorer()
        confluence = make_confluence()
        confluence["conditions"]["c5_bid_wall"] = False
        confluence["score"] = 4
        result = scorer.score_signal(make_snapshot(), confluence)
        ind_sum = sum(result["individual_scores"].values())
        assert ind_sum == pytest.approx(result["weighted_score"], abs=0.01)

    def test_adjust_weights_from_history(self):
        scorer = SignalScorer()
        original = dict(scorer.weights)
        # C3 was met in all winning trades, never in losers → ratio rises.
        history = [
            {"conditions_met": ["c1_bullish_trend", "c3_volume_spike"], "net_pnl_usdt": 10.0},
            {"conditions_met": ["c1_bullish_trend", "c3_volume_spike"], "net_pnl_usdt": 5.0},
            {"conditions_met": ["c1_bullish_trend"], "net_pnl_usdt": -8.0},
        ]
        new_weights = scorer.adjust_weights(history)
        # C3 weight should have increased relative to original.
        assert new_weights["c3_volume_spike"] > original["c3_volume_spike"]
        # Weights still sum to 1.
        assert sum(new_weights.values()) == pytest.approx(1.0, abs=1e-9)

    def test_adjust_weights_empty_history(self):
        scorer = SignalScorer()
        before = dict(scorer.weights)
        after = scorer.adjust_weights([])
        assert after == before

    def test_get_market_regime_bull(self):
        scorer = SignalScorer()
        snap = make_snapshot(price=101_000, ema21=100_500, ema50=99_900)
        assert scorer.get_market_regime(snap) == "trending_bull"

    def test_get_market_regime_bear(self):
        scorer = SignalScorer()
        snap = make_snapshot(price=99_000, ema21=99_500, ema50=100_100)
        assert scorer.get_market_regime(snap) == "trending_bear"

    def test_get_market_regime_volatile(self):
        scorer = SignalScorer()
        snap = make_snapshot(volume_ratio=3.0)
        assert scorer.get_market_regime(snap) == "volatile"

    def test_get_market_regime_ranging(self):
        scorer = SignalScorer()
        # Tight EMAs, normal volume, RSI mid.
        snap = make_snapshot(
            price=100_000, ema21=100_010, ema50=100_000,
            volume_ratio=1.0, rsi=50.0,
        )
        assert scorer.get_market_regime(snap) == "ranging"


# ──────────────────────────────────────────────
#  PerformanceAnalyzer
# ──────────────────────────────────────────────

class TestPerformanceAnalyzer:
    def test_daily_performance_empty(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = analyzer.daily_performance(today)
        assert result["trades"] == 0
        assert result["net_pnl"] == 0.0
        assert result["win_rate"] == 0.0
        assert result["date"] == today

    def test_daily_performance_with_trades(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        # Seed 3 trades today (2 wins, 1 loss).
        td, _ = seed_trade(db_factory, net_pnl_usdt=5.0)
        td, _ = seed_trade(db_factory, net_pnl_usdt=3.0)
        td, _ = seed_trade(db_factory, net_pnl_usdt=-2.0)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        result = analyzer.daily_performance(today)
        assert result["trades"] == 3
        assert result["wins"] == 2
        assert result["losses"] == 1
        assert result["net_pnl"] == pytest.approx(6.0, abs=0.01)
        assert result["win_rate"] == pytest.approx(2 / 3, abs=0.01)
        assert result["best_trade"] == pytest.approx(5.0, abs=0.01)
        assert result["worst_trade"] == pytest.approx(-2.0, abs=0.01)

    def test_monthly_performance(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        # Seed 2 wins this month.
        seed_trade(db_factory, net_pnl_usdt=4.0)
        seed_trade(db_factory, net_pnl_usdt=6.0)
        now = datetime.now(timezone.utc)
        result = analyzer.monthly_performance(now.year, now.month)
        assert result["trades"] == 2
        assert result["wins"] == 2
        assert result["net_pnl"] == pytest.approx(10.0, abs=0.01)
        assert result["year"] == now.year
        assert result["month"] == now.month

    def test_trade_distribution_by_hour(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        # Seed a trade at a specific hour.
        from core.models import db_session
        exit_dt = datetime(2025, 1, 15, 14, 30, tzinfo=timezone.utc)
        with db_session(db_factory) as session:
            t = Trade(
                symbol="BTC/USDT", side="BUY", status="CLOSED",
                entry_time=exit_dt - timedelta(hours=1),
                entry_price=100_000.0, exit_time=exit_dt,
                exit_price=100_300.0, exit_reason="take_profit",
                net_pnl_usdt=3.0, confluence_score=4,
                conditions_met=json.dumps(["c1_bullish_trend"]),
            )
            session.add(t)
        trades = analyzer.get_closed_trades("2025-01-15", "2025-01-15")
        dist = analyzer.trade_distribution(trades)
        assert dist["by_hour"][14] >= 1
        # Jan 15 2025 is a Wednesday (weekday 2)
        assert dist["by_day_of_week"][2] >= 1
        assert dist["by_confluence_score"][4] >= 1
        assert dist["by_exit_reason"]["take_profit"] >= 1

    def test_equity_curve(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        # Seed two trades on consecutive days.
        from core.models import db_session
        d1 = datetime(2025, 1, 10, 10, 0, tzinfo=timezone.utc)
        d2 = datetime(2025, 1, 11, 10, 0, tzinfo=timezone.utc)
        with db_session(db_factory) as session:
            session.add(Trade(
                symbol="BTC/USDT", side="BUY", status="CLOSED",
                entry_time=d1 - timedelta(hours=1), entry_price=100_000.0,
                exit_time=d1, exit_price=100_400.0, exit_reason="take_profit",
                net_pnl_usdt=4.0, confluence_score=4,
            ))
            session.add(Trade(
                symbol="BTC/USDT", side="BUY", status="CLOSED",
                entry_time=d2 - timedelta(hours=1), entry_price=100_400.0,
                exit_time=d2, exit_price=100_100.0, exit_reason="stop_loss",
                net_pnl_usdt=-3.0, confluence_score=3,
            ))
        curve = analyzer.equity_curve("2025-01-10", "2025-01-12")
        assert isinstance(curve, list)
        # 3 days inclusive → 3 points
        assert len(curve) == 3
        assert curve[0][1] == pytest.approx(4.0, abs=0.01)   # day 1 cumulative
        assert curve[1][1] == pytest.approx(1.0, abs=0.01)   # day 2 cumulative (4-3)
        assert curve[2][1] == pytest.approx(1.0, abs=0.01)   # day 3 no trades

    def test_streak_analysis(self, db_factory):
        analyzer = PerformanceAnalyzer(db_factory)
        trades = [
            {"net_pnl_usdt": 1.0},
            {"net_pnl_usdt": 2.0},
            {"net_pnl_usdt": -1.0},
            {"net_pnl_usdt": -1.0},
            {"net_pnl_usdt": -1.0},
            {"net_pnl_usdt": 3.0},
        ]
        result = analyzer.streak_analysis(trades)
        assert result["max_win_streak"] == 2
        assert result["max_loss_streak"] == 3
        assert result["current_streak"] == 1  # last trade was a win

    def test_streak_analysis_empty(self):
        analyzer = PerformanceAnalyzer.__new__(PerformanceAnalyzer)  # no DB needed
        result = analyzer.streak_analysis([])
        assert result == {"max_win_streak": 0, "max_loss_streak": 0, "current_streak": 0}


# ──────────────────────────────────────────────
#  TradeRecorder
# ──────────────────────────────────────────────

class TestTradeRecorder:
    def test_record_entry_and_retrieve(self, db_factory):
        # Need a parent trade row for the FK.
        from core.models import db_session
        with db_session(db_factory) as session:
            t = Trade(
                symbol="BTC/USDT", side="BUY", status="IN_TRADE",
                entry_price=100_000.0,
            )
            session.add(t)
            session.flush()
            session.refresh(t)
            trade_id = t.id

        recorder = TradeRecorder(db_factory)
        snap = make_snapshot()
        confluence = make_confluence()
        record = recorder.record_entry(
            trade_id=trade_id,
            snapshot=snap,
            confluence=confluence,
            execution_time_ms=120.5,
            slippage_pct=0.02,
        )
        assert record.trade_id == trade_id
        assert record.entry_price == pytest.approx(100_200.0, abs=0.01)
        assert record.confluence_score == 5
        assert record.execution_time_ms == pytest.approx(120.5, abs=0.01)

        # Retrieve via the helper.
        fetched = recorder.get_analytics(trade_id)
        assert fetched is not None
        assert fetched["trade_id"] == trade_id
        assert fetched["confluence_score"] == 5
        assert set(fetched["conditions_met"]) == set(confluence["conditions"].keys())
        # indicators_snapshot round-trips as JSON
        assert isinstance(fetched["indicators_snapshot"], dict)
        assert fetched["indicators_snapshot"]["price"] == pytest.approx(100_200.0, abs=0.01)

    def test_record_exit_updates_row(self, db_factory):
        from core.models import db_session
        with db_session(db_factory) as session:
            t = Trade(symbol="BTC/USDT", side="BUY", status="OPEN", entry_price=100_000.0)
            session.add(t)
            session.flush()
            session.refresh(t)
            trade_id = t.id

        recorder = TradeRecorder(db_factory)
        recorder.record_entry(
            trade_id=trade_id,
            snapshot=make_snapshot(),
            confluence=make_confluence(),
            execution_time_ms=100.0,
            slippage_pct=0.01,
        )
        recorder.record_exit(
            trade_id=trade_id,
            exit_reason="take_profit",
            execution_time_ms=80.0,
            slippage_pct=0.015,
            latency_ms=45.0,
            exit_price=100_400.0,
        )
        fetched = recorder.get_analytics(trade_id)
        assert fetched is not None
        assert fetched["exit_reason"] == "take_profit"
        assert fetched["exit_price"] == pytest.approx(100_400.0, abs=0.01)
        assert fetched["latency_ms"] == pytest.approx(45.0, abs=0.01)

    def test_record_error_appends(self, db_factory):
        from core.models import db_session
        with db_session(db_factory) as session:
            t = Trade(symbol="BTC/USDT", side="BUY", status="REJECTED", entry_price=100_000.0)
            session.add(t)
            session.flush()
            session.refresh(t)
            trade_id = t.id

        recorder = TradeRecorder(db_factory)
        recorder.record_entry(
            trade_id=trade_id,
            snapshot=make_snapshot(),
            confluence=make_confluence(),
            execution_time_ms=0.0,
            slippage_pct=0.0,
        )
        recorder.record_error(trade_id, "Order rejected: INSUFFICIENT_BALANCE", {"order_id": "x123"})
        recorder.record_error(trade_id, "Retry exhausted", {"attempt": 3})
        fetched = recorder.get_analytics(trade_id)
        assert fetched is not None
        assert len(fetched["errors"]) == 2
        assert "INSUFFICIENT_BALANCE" in fetched["errors"][0]["error"]
        assert fetched["errors"][1]["context"]["attempt"] == 3

    def test_record_error_without_trade_id(self, db_factory):
        recorder = TradeRecorder(db_factory)
        record = recorder.record_error(None, "Pre-trade error", {"stage": "warmup"})
        assert record.trade_id is None
        fetched = recorder.list_analytics(limit=5)
        assert any(r["trade_id"] is None for r in fetched)

    def test_list_analytics(self, db_factory):
        recorder = TradeRecorder(db_factory)
        # record a few entries without trade_id
        for i in range(3):
            recorder.record_entry(
                trade_id=None,
                snapshot=make_snapshot(price=100_000 + i),
                confluence=make_confluence(),
                execution_time_ms=float(i),
                slippage_pct=0.0,
            )
        rows = recorder.list_analytics(limit=10)
        assert len(rows) >= 3
        # Most recent first (id desc)
        assert rows[0]["entry_price"] is not None

    def test_init_analytics_tables_idempotent(self, engine):
        # Calling create_all twice should not error.
        init_analytics_tables(engine)
        init_analytics_tables(engine)
        # Verify the table exists by querying it.
        from sqlalchemy import inspect
        insp = inspect(engine)
        assert "trade_analytics" in insp.get_table_names()