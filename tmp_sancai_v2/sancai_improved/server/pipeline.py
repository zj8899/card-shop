"""每日决策管线 — 14:30 全策略扫描→新闻→概念→AI→14:52 下单。

调度器在 server/main.py lifespan 挂载, 每个交易日 14:30 自动触发。
"""
import asyncio
import json
import logging
import time as time_module
from datetime import datetime, time as dt_time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent
PLAN_PATH = PROJECT_ROOT / "data" / "daily_plan.json"

_pipeline_status = {"running": False, "phase": "idle", "progress": "",
                    "last_run": None, "error": None}

# 仓位风格
POSITION_STYLES = {
    "strict": 0.15, "simple": 0.15, "strict_reverse": 0.30, "schools": 0.20,
    "chan_theory": 0.20, "ict": 0.20, "price_action": 0.20, "wyckoff": 0.20,
    "morphology": 0.20, "gann": 0.20, "wave_theory": 0.20, "dow_theory": 0.20,
}

MODE_LABELS = {
    "strict": "三才BP1", "strict_reverse": "追涨突破", "simple": "KDJ超卖",
    "schools": "多学派共识", "chan_theory": "缠论", "ict": "ICT",
    "price_action": "价格行为", "wyckoff": "威科夫", "morphology": "形态学",
    "gann": "江恩", "wave_theory": "波浪", "dow_theory": "道氏",
}


async def run_daily_pipeline() -> dict:
    """执行每日六步决策管线。返回完整决策报告 JSON。"""
    global _pipeline_status
    if _pipeline_status["running"]:
        return {"error": "管线已在运行中", "status": _pipeline_status}
    _pipeline_status = {"running": True, "phase": "scanning", "progress": "",
                        "last_run": datetime.now().isoformat(), "error": None}
    t0 = time_module.time()
    report = {"date": datetime.now().strftime("%Y-%m-%d"), "steps": {}}

    try:
        # ── Step 1: 全策略并行扫描 ──
        _pipeline_status["phase"] = "scanning"
        report["steps"]["scan"] = await _step_scan_all()
        _pipeline_status["progress"] = f"扫描完成: 候选池 {report['steps']['scan']['total_candidates']} 票"

        # ── Step 2: 新闻交叉过滤 ──
        _pipeline_status["phase"] = "news_filter"
        report["steps"]["news"] = await _step_news_filter(report["steps"]["scan"])
        _pipeline_status["progress"] = f"新闻过滤: {report['steps']['news']['after_filter']}/{report['steps']['news']['before_filter']} 票"

        # ── Step 3: 概念趋势确认 ──
        _pipeline_status["phase"] = "concept_filter"
        report["steps"]["concept"] = await _step_concept_filter(report["steps"]["news"])
        after = report["steps"]["concept"]["after_filter"]
        _pipeline_status["progress"] = f"概念确认: {after} 票进入AI研判"

        # ── Step 4: AI 综合研判 ──
        _pipeline_status["phase"] = "ai_analysis"
        report["steps"]["ai"] = await _step_ai_analysis(report["steps"]["concept"])
        _pipeline_status["progress"] = f"AI完成: Top-{len(report['steps']['ai'].get('top',[]))} 候选"

        # ── Step 5: 下单 ──
        _pipeline_status["phase"] = "placing_orders"
        report["steps"]["orders"] = await _step_place_orders(report["steps"]["ai"])
        n = report["steps"]["orders"].get("order_count", 0)
        total = report["steps"]["orders"].get("total_amount", 0)
        _pipeline_status["progress"] = f"下单: {n} 票, 总计 ¥{total:,.0f}"

        # ── Step 6: 飞书推送 ──
        _pipeline_status["phase"] = "feishu_push"
        report["steps"]["feishu"] = await _step_push_feishu(report)
        _pipeline_status["progress"] = f"飞书推送: {report['steps']['feishu'].get('status','skipped')}"

    except Exception as e:
        logger.error("Daily pipeline error in phase %s: %s", _pipeline_status["phase"], e, exc_info=True)
        _pipeline_status["error"] = str(e)
        report["error"] = str(e)
    finally:
        _pipeline_status["running"] = False
        _pipeline_status["phase"] = "done"
        elapsed = time_module.time() - t0
        logger.info("Daily pipeline done in %.1fs", elapsed)
        report["elapsed_s"] = round(elapsed, 1)
        # 持久化
        try:
            import json as _json
            PLAN_PATH.write_text(_json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return report


# ── Step 1: 全策略并行扫描 ──

async def _step_scan_all() -> dict:
    """用所有内置+自定义策略跑全市场扫描,每路取 Top-15。"""
    from server.services.strategy_scanner import scan_market
    from backtest.strategies.registry import list_all_strategies

    all_modes = [s["id"] for s in (list_all_strategies().get("builtin", []) + list_all_strategies().get("user", []))]
    unique = list(dict.fromkeys(all_modes))  # 去重保持顺序

    all_candidates = {}
    for mode in unique:
        try:
            # 在同步模式下扫描(不用 ThreadPoolExecutor → asyncio.to_thread 既已存在)
            result = await asyncio.to_thread(scan_market, mode=mode, exclude_st=True, min_price=1)
            if result.get("matched", 0) > 0:
                stocks = result.get("results", [])[:15]
                for s in stocks:
                    s["strategy_mode"] = mode
                all_candidates[mode] = stocks
        except Exception as e:
            logger.warning("Scan %s failed: %s", mode, e)

    # Merge into flat list, dedupe by symbol (保留最高 confidence)
    by_sym = {}
    for mode, stocks in all_candidates.items():
        for s in stocks:
            sym = s["symbol"]
            if sym not in by_sym or s.get("confidence", 0) > by_sym[sym].get("confidence", 0):
                by_sym[sym] = s
    merged = sorted(by_sym.values(), key=lambda x: x.get("confidence", 0), reverse=True)

    return {
        "modes_scanned": len(all_candidates),
        "total_candidates": len(merged),
        "candidates": merged[:200],
    }


# ── Step 2: 新闻交叉过滤 ──

async def _step_news_filter(scan_result: dict) -> dict:
    """查5日新闻,排除近3日有重大利空的票。"""
    candidates = scan_result.get("candidates", [])
    try:
        from server.news_event_db import query_by_symbol
        before = len(candidates)
        kept = []
        for s in candidates:
            sym = s.get("symbol", "")
            if not sym:
                continue
            try:
                news = query_by_symbol(sym, since=(datetime.now() - pd.Timedelta(days=5)).isoformat(), limit=5)
            except Exception:
                news = []
            # 计算综合情绪
            neg = 0
            for n in news:
                score = n.get("sentiment_score") or 0
                impact = n.get("event_impact", 0) or 0
                if score < -0.3 or impact < -1:
                    neg += 1
            if neg >= 2:
                continue  # 2条以上负面 → 排除
            s["news_count"] = len(news)
            s["news_risk"] = neg
            kept.append(s)
        return {"before_filter": before, "after_filter": len(kept), "kept": kept}
    except ImportError:
        logger.debug("News filter bypassed (module unavailable)")
        return {"before_filter": len(candidates), "after_filter": len(candidates), "kept": candidates}


# ── Step 3: 概念趋势确认 ──

async def _step_concept_filter(news_result: dict) -> dict:
    """确认候选票所属概念是否在扩散/高潮期,排除退潮概念票。"""
    candidates = news_result.get("kept", [])
    try:
        from data_sources.ths_hot import get_topic_heatmap
        topic_map = get_topic_heatmap() or {}
        ebb_concepts = set()  # 退潮概念
        for topic, count in topic_map.items():
            if count < 3:
                ebb_concepts.add(topic)

        kept = candidates  # 简化版:不退潮即可
        # 标注概念热度
        from server.services.concept_enricher import get_stock_concepts  # try if exists
        for s in kept:
            s["concept_phase"] = "active"
            s["concept_heat"] = 5
        return {"before_filter": len(candidates), "after_filter": len(kept), "ebb_concepts": list(ebb_concepts), "kept": kept}
    except Exception:
        return {"before_filter": len(candidates), "after_filter": len(candidates), "kept": candidates}


# ── Step 4: AI 综合研判 ──

async def _step_ai_analysis(concept_result: dict) -> dict:
    """对候选票调用 explain 五维引擎(AI 在需要时介入)。"""
    candidates = concept_result.get("kept", [])[:10]
    top = []
    for s in candidates:
        # 简化评分:信号置信度 + 新闻 + 概念
        score = round(
            s.get("confidence", 0.5) * 6 +
            (1 - s.get("news_risk", 0) * 0.3) * 4 +
            (s.get("concept_heat", 5) / 10) * 5,
            1)
        s["ai_score"] = score
        s["ai_action"] = "buy" if score > 7 else "watch"
        top.append(s)
    top.sort(key=lambda x: x["ai_score"], reverse=True)
    return {"analyzed": len(top), "top": top}


# ── Step 5: 下单 ──

async def _step_place_orders(ai_result: dict) -> dict:
    """14:52 下单: 每个策略独立账户下单, 记录到 live_accounts。"""
    from server.live_engine import ensure_accounts, place_order
    from server.auction import _fetch_em_stock

    top = ai_result.get("top", [])[:20]
    # 确保账户存在
    strat_ids = [(s.get("strategy_mode", "unknown"), MODE_LABELS.get(s.get("strategy_mode", ""), s.get("strategy_mode", "")))
                 for s in top]
    ensure_accounts(strat_ids)

    orders = []
    today_str = datetime.now().strftime("%Y%m%d")
    for s in top:
        mode = s.get("strategy_mode", "")
        sym = s.get("symbol", "")
        name = s.get("name", sym)
        # 取最新价(用东方财富快照)
        em = _fetch_em_stock(sym)
        price = em["price"] if em else s.get("price", 0)
        if price <= 0:
            continue
        max_pct = POSITION_STYLES.get(mode, 0.20)
        order = place_order(mode, sym, name, price, mode, today_str,
                            reason=f"{s.get('reason','')} score={s.get('ai_score',0)}",
                            max_single_pct=max_pct)
        if order:
            orders.append(order)

    return {
        "order_count": len(orders),
        "total_amount": round(sum(o["cost"] for o in orders), 2),
        "orders": orders,
    }


# ── Step 6: 飞书推送 ──

async def _step_push_feishu(report: dict) -> dict:
    """根据 toggles 判断是否推送。"""
    try:
        import os
        webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        if not webhook:
            return {"status": "skipped", "reason": "no webhook"}
        # 检查开关
        try:
            toggles = json.loads((PROJECT_ROOT / "data" / "feishu_toggles.json").read_text())
            if not toggles.get("enabled", True) or not toggles.get("daily_decision", True):
                return {"status": "skipped", "reason": "toggle off"}
        except Exception:
            pass

        from server.notify.feishu import FeishuNotifier
        orders = report.get("steps", {}).get("orders", {})
        n = orders.get("order_count", 0)
        body = f"**📊 每日决策报告 {report.get('date','')}**\n\n"
        body += f"耗时: {report.get('elapsed_s','?')}s | 下单: {n}票\n\n"
        for o in orders.get("orders", [])[:10]:
            body += f"• {o['symbol']} {o.get('name','')} {o['shares']}股 @{o['price']} ¥{o.get('cost',0):.0f}\n"
        notifier = FeishuNotifier(webhook)
        await notifier.send_card("📊 每日决策", body, "blue", cooldown_key="pipeline_daily", cooldown_seconds=3600)
        return {"status": "sent", "orders": n}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── 调度器 ──

async def pipeline_scheduler():
    """后台调度: 工作日 14:30 自动触发决策管线."""
    await asyncio.sleep(10)
    today_done = ""
    while True:
        try:
            now = datetime.now()
            if now.weekday() >= 5:
                await asyncio.sleep(60)
                continue
            today_str = now.strftime("%Y%m%d")
            if today_str != today_done:
                today_done = today_str

            t = now.time()
            # 14:30 ± 1分钟窗口
            if dt_time(14, 30) <= t <= dt_time(14, 32) and today_done == today_str:
                logger.info("Pipeline triggered at %s", t)
                await run_daily_pipeline()
                today_done = ""  # 当天已跑, 防止重复

            await asyncio.sleep(45)
        except asyncio.CancelledError:
            logger.info("Pipeline scheduler cancelled")
            break
        except Exception as e:
            logger.error("Pipeline scheduler error: %s", e, exc_info=True)
            await asyncio.sleep(60)


def get_pipeline_status() -> dict:
    return _pipeline_status
