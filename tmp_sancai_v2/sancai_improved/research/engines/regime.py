"""Market Regime Engine (引擎①) — automatic market state classification.

Classifies the current market into one of 10 states, outputs:
  - label: market regime name
  - confidence: classification confidence (0-1)
  - risk_level: 1 (low) to 5 (high)
  - suggested_position_pct: recommended position allocation
  - recommended_strategies: list of strategy mode names
  - explanation: human-readable rationale

States and their characteristics:

  BULLISH:
    主升浪    — MA bull alignment + volume expansion + new highs + streak premium > 3%
    趋势行情  — MA bull alignment + moderate volume + trend_strength > 0.6
    NEUTRAL:
    修复行情  — oversold bounce + sentiment recovery + volume contraction
    高位震荡  — new high then contraction + sentiment divergence + RSI > 70
    板块轮动  — rotation_speed > 0.7 + no persistent mainline
    机构行情  — northbound inflow + large-cap leading + low vol
    游资行情  — small-cap leading + high turnover + rapid theme rotation

  BEARISH:
    情绪退潮  — break_rate > 30% + profit_effect < 0.3 + leader breakdown
    冰点行情  — limit_up < 20 + volume floor + fear index > 0.8
    一致高潮  — limit_up > 100 + streak premium > 5% + greed > 0.8  (danger)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Regime definitions ──

REGIME_DEFINITIONS = {
    "主升浪": {
        "risk_level": 3,
        "position_pct": 0.85,
        "strategies": ["strict_reverse", "ml", "schools"],
        "description": "多头排列+放量新高+连板溢价高",
    },
    "趋势行情": {
        "risk_level": 2,
        "position_pct": 0.70,
        "strategies": ["strict", "ml", "chan_theory", "schools"],
        "description": "多头排列+温和放量+趋势明确",
    },
    "修复行情": {
        "risk_level": 3,
        "position_pct": 0.40,
        "strategies": ["simple", "strict", "chan_theory"],
        "description": "超跌反弹+情绪回暖+缩量止跌",
    },
    "高位震荡": {
        "risk_level": 3,
        "position_pct": 0.30,
        "strategies": ["wyckoff", "ict", "price_action"],
        "description": "新高后缩量+分歧加大+超买",
    },
    "板块轮动": {
        "risk_level": 3,
        "position_pct": 0.40,
        "strategies": ["schools", "ict", "price_action"],
        "description": "轮动速度高+无持续主线",
    },
    "机构行情": {
        "risk_level": 2,
        "position_pct": 0.60,
        "strategies": ["dow_theory", "chan_theory", "wave_theory"],
        "description": "北向持续流入+大市值领涨+低波动",
    },
    "游资行情": {
        "risk_level": 4,
        "position_pct": 0.40,
        "strategies": ["strict_reverse", "ict", "price_action"],
        "description": "小市值领涨+高换手+题材轮动快",
    },
    "情绪退潮": {
        "risk_level": 4,
        "position_pct": 0.10,
        "strategies": [],
        "description": "炸板率高+赚钱效应弱+龙头断板",
    },
    "冰点行情": {
        "risk_level": 5,
        "position_pct": 0.05,
        "strategies": [],
        "description": "涨停极少+成交地量+极度恐慌",
    },
    "一致高潮": {
        "risk_level": 4,
        "position_pct": 0.20,
        "strategies": ["gann", "wyckoff"],
        "description": "涨停潮+过度贪婪+高连板溢价",
    },
}


@dataclass
class RegimeResult:
    """Output from market regime detection."""
    label: str
    confidence: float
    risk_level: int               # 1–5
    suggested_position_pct: float  # 0.0–1.0
    recommended_strategies: list[str]
    explanation: str
    all_scores: dict[str, float] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════════════════════════════════════
# Market Regime Classifier
# ═══════════════════════════════════════════════════════════════════════════════

class MarketRegimeClassifier:
    """Rule-based + ML hybrid market state classifier.

    Priority: rule-based (interpretable) with ML fallback when factor data
    is insufficient for rule confidence.

    Input: a dict of current factor values (from the 22-factor system).
    Output: RegimeResult.
    """

    def __init__(self, ml_model_path: str | None = None):
        self._ml_model = None
        self._ml_fitted = False

    # ── Public API ──

    def classify(self, factors: dict[str, float]) -> RegimeResult:
        """Classify market regime from current factor values.

        Args:
            factors: dict mapping factor_name → current value.
                     Required keys vary by rule; missing keys → default to 0.

        Returns:
            RegimeResult with label, confidence, risk_level, strategies, explanation.
        """
        scores = self._score_all_regimes(factors)
        best_label = max(scores, key=scores.get)
        best_score = scores[best_label]

        # Fallback to ML if confidence too low
        if best_score < 0.3 and self._ml_fitted:
            ml_label, ml_conf = self._predict_ml(factors)
            if ml_conf > best_score:
                best_label = ml_label
                best_score = ml_conf

        definition = REGIME_DEFINITIONS.get(best_label, REGIME_DEFINITIONS["修复行情"])
        return RegimeResult(
            label=best_label,
            confidence=round(best_score, 4),
            risk_level=definition["risk_level"],
            suggested_position_pct=definition["position_pct"],
            recommended_strategies=definition["strategies"],
            explanation=self._build_explanation(best_label, factors, best_score),
            all_scores=scores,
        )

    def classify_from_df(self, df: pd.DataFrame) -> RegimeResult:
        """Classify from a DataFrame containing factor columns.

        Uses the last row's factor values.
        """
        factors = self._extract_factors_from_df(df)
        return self.classify(factors)

    def history(self, df: pd.DataFrame) -> list[dict]:
        """Classify every bar in a DataFrame, returning a regime time series."""
        results = []
        for i in range(len(df)):
            row = df.iloc[i]
            factors = {
                col: float(row[col]) if col in df.columns and not pd.isna(row[col]) else 0.0
                for col in [
                    "trend_strength", "vol_ratio_20", "rsi_14", "atr_14",
                    "ma_dev_34", "kdj_9_3",
                    "profit_effect_index", "limit_up_ratio", "limit_down_ratio",
                    "break_board_rate", "streak_momentum", "sector_rotation_speed",
                    "northbound_flow", "order_imbalance", "liquidity_density",
                ]
            }
            result = self.classify(factors)
            date_str = str(df.index[i])[:10] if hasattr(df, 'index') else str(i)
            results.append({
                "date": date_str,
                "label": result.label,
                "confidence": result.confidence,
                "risk_level": result.risk_level,
            })
        return results

    # ── Rule-based scoring ──

    def _score_all_regimes(self, f: dict[str, float]) -> dict[str, float]:
        """Score each regime with a rule-based formula. Returns {label: score 0-1}."""
        # Detect whether sentiment data is available
        has_sentiment = (
            abs(self._g(f, "profit_effect_index")) > 0.01 or
            abs(self._g(f, "limit_up_ratio")) > 0.01 or
            abs(self._g(f, "limit_down_ratio")) > 0.01 or
            abs(self._g(f, "break_board_rate")) > 0.01
        )

        scores = {
            "主升浪": self._score_main_uptrend(f),
            "趋势行情": self._score_trend(f),
            "修复行情": self._score_recovery(f),
            "高位震荡": self._score_high_consolidation(f),
            "板块轮动": self._score_rotation(f),
            "机构行情": self._score_institutional(f),
            "游资行情": self._score_retail(f),
            "情绪退潮": self._score_ebb(f) if has_sentiment else 0.0,
            "冰点行情": self._score_ice_point(f) if has_sentiment else 0.0,
            "一致高潮": self._score_euphoria(f) if has_sentiment else 0.0,
        }

        # Without sentiment data, boost technical-based scores
        if not has_sentiment:
            for key in ["趋势行情", "修复行情", "高位震荡"]:
                scores[key] = min(scores[key] * 1.3, 1.0)

        return scores

    def _g(self, f: dict, key: str, default: float = 0.0) -> float:
        """Safe factor getter with default."""
        return float(f.get(key, default) or default)

    def _score_main_uptrend(self, f) -> float:
        ts = self._g(f, "trend_strength")
        vr = self._g(f, "vol_ratio_20")
        md = self._g(f, "ma_dev_34")
        streak = self._g(f, "streak_momentum")
        score = 0.0
        if ts > 0.6: score += 0.30
        elif ts > 0.3: score += 0.15
        if vr > 1.3: score += 0.25
        elif vr > 1.0: score += 0.12
        if md > 0.05: score += 0.25
        if streak > 0.5: score += 0.20
        return score

    def _score_trend(self, f) -> float:
        ts = self._g(f, "trend_strength")
        md = self._g(f, "ma_dev_34")
        score = 0.0
        if ts > 0.4: score += 0.40
        elif ts > 0.2: score += 0.20
        if md > 0: score += 0.30
        rsi = self._g(f, "rsi_14")
        if 50 < rsi < 70: score += 0.30
        return min(score, 1.0)

    def _score_recovery(self, f) -> float:
        kdj = self._g(f, "kdj_9_3")
        md = self._g(f, "ma_dev_34")
        pei = self._g(f, "profit_effect_index")
        score = 0.0
        if kdj < -1.0: score += 0.35  # oversold z-score
        if md < -0.03: score += 0.30  # below MA
        if 0.2 < pei < 0.5: score += 0.35
        return score

    def _score_high_consolidation(self, f) -> float:
        rsi = self._g(f, "rsi_14")
        atr = self._g(f, "atr_14")
        vr = self._g(f, "vol_ratio_20")
        score = 0.0
        if rsi > 0.8: score += 0.35
        if atr > 0.03: score += 0.25
        if vr < 0.8: score += 0.40
        return score

    def _score_rotation(self, f) -> float:
        rot = self._g(f, "sector_rotation_speed")
        ts = self._g(f, "trend_strength")
        score = 0.0
        if rot > 0.6: score += 0.50
        elif rot > 0.4: score += 0.25
        if abs(ts) < 0.3: score += 0.50
        return score

    def _score_institutional(self, f) -> float:
        nf = self._g(f, "northbound_flow")
        rsi = self._g(f, "rsi_14")
        atr = self._g(f, "atr_14")
        score = 0.0
        if nf > 0.5: score += 0.40
        elif nf > 0: score += 0.20
        if 40 <= abs(rsi) <= 60: score += 0.30
        if atr < 0.02: score += 0.30
        return score

    def _score_retail(self, f) -> float:
        vr = self._g(f, "vol_ratio_20")
        rot = self._g(f, "sector_rotation_speed")
        ts = self._g(f, "trend_strength")
        score = 0.0
        if vr > 1.5: score += 0.30
        if rot > 0.5: score += 0.30
        if abs(ts) < 0.4: score += 0.20
        if vr > 1.0: score += 0.20
        return score

    def _score_ebb(self, f) -> float:
        bbr = self._g(f, "break_board_rate")
        pei = self._g(f, "profit_effect_index")
        ldr = self._g(f, "limit_down_ratio")
        score = 0.0
        if bbr < 0.5: score += 0.35  # high break rate = low value
        if pei < 0.3: score += 0.35
        if ldr > 0.5: score += 0.30
        return score

    def _score_ice_point(self, f) -> float:
        lur = self._g(f, "limit_up_ratio")
        ldr = self._g(f, "limit_down_ratio")
        pei = self._g(f, "profit_effect_index")
        score = 0.0
        if lur < -1.0: score += 0.40
        if ldr > 1.0: score += 0.30
        if pei < 0.2: score += 0.30
        return score

    def _score_euphoria(self, f) -> float:
        lur = self._g(f, "limit_up_ratio")
        streak = self._g(f, "streak_momentum")
        score = 0.0
        if lur > 1.5: score += 0.50
        elif lur > 0.8: score += 0.25
        if streak > 0.7: score += 0.50
        elif streak > 0.4: score += 0.25
        return score

    # ── Explanation builder ──

    def _build_explanation(self, label: str, f: dict, confidence: float) -> str:
        """Generate a human-readable explanation for the classification."""
        definition = REGIME_DEFINITIONS.get(label, {})
        desc = definition.get("description", "未知状态")
        risk = definition.get("risk_level", 3)
        pos = definition.get("position_pct", 0.5)
        strats = definition.get("strategies", [])

        risk_word = {1: "极低", 2: "较低", 3: "中等", 4: "较高", 5: "极高"}.get(risk, "中等")
        strat_names = {
            "strict_reverse": "追涨突破", "strict": "三才BP1", "simple": "KDJ超卖",
            "ml": "ML预测", "schools": "多流派共识", "chan_theory": "缠论",
            "ict": "ICT", "price_action": "价格行为", "wyckoff": "威科夫",
            "dow_theory": "道氏理论", "wave_theory": "波浪理论", "gann": "江恩",
            "morphology": "形态学",
        }

        parts = [
            f"市场状态: {label}（置信度 {confidence:.0%}）",
            f"判定依据: {desc}",
            f"风险等级: {risk_word}（{risk}/5）",
            f"建议仓位: {pos:.0%}",
        ]
        if strats:
            names = [strat_names.get(s, s) for s in strats[:4]]
            parts.append(f"推荐策略: {', '.join(names)}")
        else:
            parts.append("推荐策略: 空仓观望")

        # Add key factor values
        key_factors = []
        ts = self._g(f, "trend_strength")
        if abs(ts) > 0.2:
            key_factors.append(f"趋势强度={ts:.2f}")
        pei = self._g(f, "profit_effect_index")
        if pei > 0:
            key_factors.append(f"赚钱效应={pei:.2f}")
        lur = self._g(f, "limit_up_ratio")
        if abs(lur) > 0.5:
            key_factors.append(f"涨停比Z={lur:.1f}")
        if key_factors:
            parts.append("关键指标: " + " | ".join(key_factors))

        return "。".join(parts) + "。"

    # ── ML fallback ──

    def _predict_ml(self, factors: dict[str, float]) -> tuple[str, float]:
        """ML-based regime prediction — requires a trained model file.

        Without a trained model, this returns a sentinel indicating ML is unavailable.
        The caller should fall back to _rule_based_classify().
        """
        # Check for trained model (LightGBM .txt or pickle)
        model_path = None
        try:
            from pathlib import Path
            candidates = [
                Path(__file__).parent.parent / "models" / "regime_model.txt",
                Path(__file__).parent.parent / "models" / "regime_model.pkl",
            ]
            for p in candidates:
                if p.exists():
                    model_path = p
                    break
        except Exception:
            pass

        if model_path is None:
            # No trained model available — return sentinel
            return ("__ml_unavailable__", 0.0)

        # TODO: load and run the trained model
        return ("__ml_unavailable__", 0.0)

    # ── Factor extraction from DataFrame ──

    def _extract_factors_from_df(self, df: pd.DataFrame) -> dict[str, float]:
        """Extract latest factor values from a DataFrame."""
        row = df.iloc[-1]
        factor_names = [
            "trend_strength", "vol_ratio_20", "rsi_14", "atr_14",
            "ma_dev_34", "kdj_9_3",
            "profit_effect_index", "limit_up_ratio", "limit_down_ratio",
            "break_board_rate", "streak_momentum", "sector_rotation_speed",
            "northbound_flow", "order_imbalance", "liquidity_density",
            "stop_loss_cluster", "turnover_volatility",
        ]
        return {
            name: float(row[name]) if name in df.columns and not pd.isna(row[name]) else 0.0
            for name in factor_names
        }


# ── Convenience function ──

def detect_regime(factors: dict[str, float]) -> RegimeResult:
    """Quick one-shot regime detection."""
    classifier = MarketRegimeClassifier()
    return classifier.classify(factors)
