"""
analytics/scoring.py
====================
SignalScorer — dynamic weighted scoring model that replaces the fixed
confluence checklist with a tunable, per-condition weighted score.

Each of the five confluence conditions (C1–C5) carries a configurable
weight that sums to 1.0.  The raw condition is binary (met / not met), so
the **weighted score** is ``Σ(weight_i × met_i) × 100`` — a value from 0
to 100.  C1 (bullish trend) is *mandatory*: if C1 fails the score is
forced to 0 regardless of the other conditions.

Recommendation thresholds:
    * ``>= 70``  → ``"enter"``
    * ``50–69``   → ``"wait"``
    * ``< 50``    → ``"skip"``

The scorer can also **learn** optimal weights from historical trades
(see :meth:`adjust_weights`) and classify the current market regime
(see :meth:`get_market_regime`).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("analytics.scoring")


# ──────────────────────────────────────────────
#  Default weights & thresholds
# ──────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "c1_bullish_trend": 0.30,
    "c2_vwap_position": 0.20,
    "c3_volume_spike": 0.20,
    "c4_rsi_zone": 0.15,
    "c5_bid_wall": 0.15,
}

# Short aliases used in result dicts.
_CONDITION_LABELS: dict[str, str] = {
    "c1_bullish_trend": "C1",
    "c2_vwap_position": "C2",
    "c3_volume_spike": "C3",
    "c4_rsi_zone": "C4",
    "c5_bid_wall": "C5",
}

ENTER_THRESHOLD: float = 70.0
WAIT_THRESHOLD: float = 50.0

# Market-regime detection thresholds.
_TREND_EMA_GAP_PCT: float = 0.001        # 0.1% gap between ema21 and ema50
_RSI_EXTREME_LOW: float = 35.0
_RSI_EXTREME_HIGH: float = 65.0
_VOLATILE_VOLUME_RATIO: float = 2.5


class SignalScorer:
    """Dynamic weighted signal scorer.

    Parameters
    ----------
    weights
        Optional override for the default condition weights.  Keys must
        match the confluence condition names (``c1_bullish_trend`` …).
    enter_threshold
        Score >= this → ``"enter"`` (default 70).
    wait_threshold
        Score >= this (and < enter) → ``"wait"`` (default 50).
    """

    def __init__(
        self,
        weights: Optional[dict[str, float]] = None,
        enter_threshold: float = ENTER_THRESHOLD,
        wait_threshold: float = WAIT_THRESHOLD,
    ) -> None:
        self.weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        if weights:
            # Merge — only accept known keys, renormalise to sum 1.0.
            for k, v in weights.items():
                if k in self.weights:
                    self.weights[k] = float(v)
            self._normalise_weights()
        self.enter_threshold = float(enter_threshold)
        self.wait_threshold = float(wait_threshold)

    # ──────────────────────────────────────────────
    #  Private helpers
    # ──────────────────────────────────────────────

    def _normalise_weights(self) -> None:
        """Scale weights so they sum to exactly 1.0."""
        total = sum(self.weights.values())
        if total <= 0:
            # fall back to defaults
            self.weights = dict(DEFAULT_WEIGHTS)
            return
        for k in self.weights:
            self.weights[k] = self.weights[k] / total

    @staticmethod
    def _condition_value(conditions: dict[str, bool], key: str) -> bool:
        """Return ``bool(condition)`` safely (missing → False)."""
        return bool(conditions.get(key, False))

    # ──────────────────────────────────────────────
    #  Public API
    # ──────────────────────────────────────────────

    def score_signal(self, snapshot: dict, confluence: dict) -> dict[str, Any]:
        """Score a signal from an indicator snapshot + confluence result.

        Parameters
        ----------
        snapshot
            ``IndicatorSnapshot.to_dict()`` output (or a plain dict with the
            same keys).
        confluence
            ``ConfluenceResult.to_dict()`` output, containing at least
            ``conditions`` (dict[str, bool]) and ``score`` (int).

        Returns
        -------
        dict
            Keys: ``total_score`` (0–100), ``weighted_score`` (0–100),
            ``individual_scores`` (dict of per-condition weighted scores),
            ``recommendation`` ("enter" / "wait" / "skip"),
            ``confidence`` (0–1), ``c1_mandatory_passed`` (bool).
        """
        conditions: dict[str, bool] = confluence.get("conditions", {}) if confluence else {}
        raw_score: int = int(confluence.get("score", 0)) if confluence else 0

        # Per-condition weighted scores (0 or weight*100).
        individual_scores: dict[str, float] = {}
        weighted_total = 0.0
        for key, weight in self.weights.items():
            met = self._condition_value(conditions, key)
            contrib = weight * 100.0 if met else 0.0
            individual_scores[_CONDITION_LABELS.get(key, key)] = round(contrib, 2)
            weighted_total += contrib

        c1_passed = self._condition_value(conditions, "c1_bullish_trend")

        # C1 is mandatory.
        if not c1_passed:
            weighted_total = 0.0
            # zero out individual scores too for clarity
            individual_scores = {k: 0.0 for k in individual_scores}

        weighted_total = round(weighted_total, 2)

        # Recommendation.
        if not c1_passed:
            recommendation = "skip"
        elif weighted_total >= self.enter_threshold:
            recommendation = "enter"
        elif weighted_total >= self.wait_threshold:
            recommendation = "wait"
        else:
            recommendation = "skip"

        # Confidence: how far above the wait threshold (or enter threshold).
        if recommendation == "enter":
            confidence = min(1.0, (weighted_total - self.enter_threshold) / (100.0 - self.enter_threshold + 1e-9))
        elif recommendation == "wait":
            confidence = 0.5 + 0.5 * (
                (weighted_total - self.wait_threshold) / (self.enter_threshold - self.wait_threshold + 1e-9)
            )
            confidence = max(0.0, min(0.99, confidence))
        else:
            confidence = max(0.0, min(0.49, weighted_total / self.wait_threshold)) if self.wait_threshold > 0 else 0.0

        return {
            "total_score": raw_score,
            "weighted_score": weighted_total,
            "individual_scores": individual_scores,
            "recommendation": recommendation,
            "confidence": round(confidence, 4),
            "c1_mandatory_passed": c1_passed,
            "weights": dict(self.weights),
        }

    def adjust_weights(self, historical_trades: list[dict]) -> dict[str, float]:
        """Learn optimal weights from historical trade outcomes.

        The idea: conditions that were met in *winning* trades should get
        more weight; conditions that were met in *losing* trades should get
        less.  We compute, per condition, ``wins_with / total_with`` and
        scale the default weights by that success ratio, then renormalise.

        Parameters
        ----------
        historical_trades
            List of trade dicts.  Each dict should contain:
            ``conditions_met`` (list[str] of met condition keys, or a dict
            of condition→bool) and ``net_pnl_usdt`` (float).

        Returns
        -------
        dict[str, float]
            The new (normalised) weights, also stored on ``self.weights``.
        """
        if not historical_trades:
            return dict(self.weights)

        # Accumulators.
        wins_with: dict[str, int] = {k: 0 for k in self.weights}
        total_with: dict[str, int] = {k: 0 for k in self.weights}

        for trade in historical_trades:
            pnl = float(trade.get("net_pnl_usdt", 0.0))
            is_win = pnl > 0
            met = trade.get("conditions_met", [])
            if isinstance(met, dict):
                met_keys = [k for k, v in met.items() if v]
            else:
                met_keys = list(met)
            for key in self.weights:
                if key in met_keys:
                    total_with[key] += 1
                    if is_win:
                        wins_with[key] += 1

        # Compute per-condition success ratio and scale default weights.
        new_weights: dict[str, float] = {}
        for key, base_weight in self.weights.items():
            n = total_with[key]
            if n == 0:
                # never observed → keep default weight
                ratio = 1.0
            else:
                ratio = wins_with[key] / n
            # Clamp ratio to [0.25, 2.0] so a single bad observation doesn't
            # zero out an important condition.
            ratio = max(0.25, min(2.0, ratio))
            new_weights[key] = base_weight * ratio

        self.weights = new_weights
        self._normalise_weights()
        logger.info("Adjusted signal weights: %s", self.weights)
        return dict(self.weights)

    def get_market_regime(self, snapshot: dict) -> str:
        """Classify the current market regime.

        Parameters
        ----------
        snapshot
            Indicator snapshot dict (must contain at least ``ema21``,
            ``ema50``, ``ema9``, ``vwap``, ``rsi``, ``volume_ratio``,
            ``price``).

        Returns
        -------
        str
            One of ``"trending_bull"``, ``"trending_bear"``, ``"ranging"``,
            ``"volatile"``.
        """
        price = float(snapshot.get("price", 0.0))
        ema9 = float(snapshot.get("ema9", 0.0))
        ema21 = float(snapshot.get("ema21", 0.0))
        ema50 = float(snapshot.get("ema50", 0.0))
        rsi = float(snapshot.get("rsi", 50.0))
        volume_ratio = float(snapshot.get("volume_ratio", 1.0))

        # Volatile regime takes precedence if volume is extremely high.
        if volume_ratio >= _VOLATILE_VOLUME_RATIO:
            return "volatile"

        # Trending regimes require a meaningful EMA gap.
        if ema50 > 0:
            ema_gap_pct = abs(ema21 - ema50) / ema50
        else:
            ema_gap_pct = 0.0

        trending = ema_gap_pct >= _TREND_EMA_GAP_PCT

        if trending:
            if price > ema21 and ema21 > ema50:
                return "trending_bull"
            if price < ema21 and ema21 < ema50:
                return "trending_bear"

        # Extreme RSI → still volatile-ish.
        if rsi >= _RSI_EXTREME_HIGH or rsi <= _RSI_EXTREME_LOW:
            return "volatile"

        # Price hovering around EMAs / VWAP → ranging.
        return "ranging"