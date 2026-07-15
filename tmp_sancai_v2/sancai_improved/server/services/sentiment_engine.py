"""策略市场温度计 — 从 15 路策略的扫描结果推导市场状态 + 确定性信号。

核心理念：每路策略不仅是选股工具，更是市场传感器。
  追涨票变多 = 多头活跃
  BP1票变多 = 恐慌蔓延
  结果扩散到多板块 = 全局性情绪
  多路策略同时选中同一票 = 高确定性信号

输入：管线 Step 1 产出 {mode: [symbol_dict]}（策略→候选票列表）
输出：daily_sentiment JSON（温度/交叉信号/顺势建议）
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.parent
HISTORY_PATH = PROJECT_ROOT / "data" / "sentiment_history.jsonl"
REPORT_PATH = PROJECT_ROOT / "data" / "daily_sentiment.json"

# 策略分类
BULL_STRATEGIES = {"strict_reverse", "chan_theory", "ict", "price_action", "wyckoff",
                   "morphology", "gann", "wave_theory", "dow_theory", "schools"}
BEAR_STRATEGIES = {"strict", "simple"}

MODE_LABELS = {
    "strict": "BP1超跌", "strict_reverse": "追涨突破", "simple": "KDJ超卖",
    "schools": "多学派共识", "chan_theory": "缠论", "ict": "ICT",
    "price_action": "价格行为", "wyckoff": "威科夫", "morphology": "形态学",
    "gann": "江恩", "wave_theory": "波浪", "dow_theory": "道氏",
}


def compute_sentiment(scan_results: dict) -> dict:
    """计算市场温度+交叉信号+顺势建议。

    Args:
        scan_results: {mode: [{"symbol":..., "name":..., "confidence":..., ...}, ...]}
    Returns:
        完整 daily_sentiment dict
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # ── 1. 每策略的统计 ──
    strategy_stats = {}
    for mode, stocks in scan_results.items():
        if not stocks:
            continue
        sectors = set()
        for s in stocks:
            # 尝试从 news_event_db 或 concept_enricher 获取板块，停用则空
            pass
        strategy_stats[mode] = {
            "matched": len(stocks),
            "label": MODE_LABELS.get(mode, mode),
            "type": "bull" if mode in BULL_STRATEGIES else "bear" if mode in BEAR_STRATEGIES else "neutral",
            "sectors": [],
            "stocks": stocks[:10],
        }

    if not strategy_stats:
        return {"date": today_str, "error": "无扫描数据"}

    # ── 2. 温度计算 ──
    bull_count = sum(s["matched"] for m, s in strategy_stats.items()
                     if s["type"] == "bull")
    bear_count = sum(s["matched"] for m, s in strategy_stats.items()
                     if s["type"] == "bear")
    total = bull_count + bear_count
    score = round(bull_count / max(total, 1) * 100)

    # ── 3. 趋势检测（对比历史 7 日均值） ──
    history = _load_history()
    bull_trend, bear_trend = _detect_trend(history, bull_count, bear_count)

    # ── 4. 标签 ──
    if score >= 70:
        label = "偏多"
        summary = f"多头信号占优(bull={bull_count},bear={bear_count})。市场做多意愿强。"
    elif score >= 40:
        label = "震荡"
        summary = f"多空信号接近(bull={bull_count},bear={bear_count})。市场方向不明，等确认。"
    else:
        label = "偏空"
        summary = f"空头信号占优(bull={bull_count},bear={bear_count})。恐慌或在蔓延，减少操作或做超跌反弹。"

    # 补充趋势信息
    if bull_trend == "up" and bear_trend == "up":
        summary += " 多头和空头信号都在增多——市场分歧加大，短期波动加剧。"
    elif bull_trend == "up":
        summary += " 多头信号在增多，做多趋势在形成。"
    elif bear_trend == "up":
        summary += " 空头信号在增多，警惕风险。"

    # ── 5. 交叉信号检测 ──
    cross_signals = _detect_cross_signals(scan_results)

    # ── 6. 顺势建议 ──
    advice = _generate_advice(score, label, bull_trend, bear_trend, strategy_stats, cross_signals)

    # ── 7. 持久化 ──
    report = {
        "date": today_str,
        "timestamp": now.strftime("%H:%M:%S"),
        "strategy_stats": strategy_stats,
        "sentiment": {"score": score, "label": label, "summary": summary,
                      "bull_count": bull_count, "bear_count": bear_count,
                      "bull_trend": bull_trend, "bear_trend": bear_trend},
        "cross_signals": cross_signals[:10],
        "advice": advice,
    }
    try:
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": today_str, "bull_count": bull_count,
                                "bear_count": bear_count, "score": score,
                                "label": label}, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to persist sentiment: %s", e)

    return report


def _load_history(days: int = 30) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    records = []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    for line in HISTORY_PATH.read_text(encoding="utf-8").strip().split("\n"):
        try:
            r = json.loads(line)
            if r.get("date", "") >= cutoff:
                records.append(r)
        except Exception:
            pass
    return records


def _detect_trend(history: list, bull: int, bear: int) -> tuple[str, str]:
    if len(history) < 3:
        return "flat", "flat"
    avg_bull = sum(r.get("bull_count", 0) for r in history[-7:]) / max(len(history[-7:]), 1)
    avg_bear = sum(r.get("bear_count", 0) for r in history[-7:]) / max(len(history[-7:]), 1)
    bt = "up" if bull > avg_bull * 1.15 else "down" if bull < avg_bull * 0.85 else "flat"
    bet = "up" if bear > avg_bear * 1.15 else "down" if bear < avg_bear * 0.85 else "flat"
    return bt, bet


def _detect_cross_signals(scan_results: dict) -> list[dict]:
    """检测被多路策略同时选中的票（高确定性信号）。"""
    by_symbol = {}
    for mode, stocks in scan_results.items():
        for s in stocks:
            sym = s.get("symbol", "")
            if not sym:
                continue
            if sym not in by_symbol:
                by_symbol[sym] = {"symbol": sym, "name": s.get("name", sym),
                                  "strategies": [], "prices": [], "confidences": []}
            by_symbol[sym]["strategies"].append(mode)
            by_symbol[sym]["prices"].append(s.get("price", 0))

    cross = []
    for sym, info in by_symbol.items():
        if len(info["strategies"]) >= 2:
            bull_hits = [m for m in info["strategies"] if m in BULL_STRATEGIES]
            bear_hits = [m for m in info["strategies"] if m in BEAR_STRATEGIES]
            # 至少有一个多策略+一个多方向才算有意义的交叉
            if len(set(info["strategies"])) >= 2:
                conf = "高" if len(info["strategies"]) >= 3 else "中"
                info["confidence"] = conf
                info["strategies"] = info["strategies"][:5]
                cross.append(info)

    cross.sort(key=lambda x: len(x["strategies"]), reverse=True)
    return cross


def _generate_advice(score: int, label: str, bull_trend: str, bear_trend: str,
                     stats: dict, cross_signals: list) -> dict:
    priority = []
    avoid = []
    for mode in stats:
        if stats[mode]["type"] == "bull" and bull_trend == "up":
            priority.append(mode)
        elif stats[mode]["type"] == "bear" and bear_trend == "up":
            avoid.append(mode)

    position = "保守" if label == "偏空" else "积极" if label == "偏多" else "稳健"
    max_pct = 10 if position == "保守" else (25 if position == "积极" else 17)

    warnings = []
    if bull_trend == "up" and bear_trend == "up":
        warnings.append("多头和空头同时增多——分歧加大，单票仓位控制在15%以内")
    if label == "偏空":
        warnings.append("市场偏空，BP1/抄底策略暂不执行，等恐慌收敛再入场")

    return {
        "position": position,
        "max_single_pct": max_pct,
        "priority_strategies": priority[:4],
        "avoid_strategies": avoid[:3],
        "cross_signal_count": len(cross_signals),
        "risk_warnings": warnings,
    }
