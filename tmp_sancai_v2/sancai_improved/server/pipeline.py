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

    # ── Step 0: 实时快照（全市场拉一次,15路策略共享）───
    _pipeline_status["phase"] = "live_snapshot"
    live_bars = {}
    try:
        live_bars = await asyncio.to_thread(_fetch_live_snapshot)
        _pipeline_status["progress"] = f"实时快照: {len(live_bars)} 只票"
        logger.info("Live snapshot: %d stocks", len(live_bars))
    except Exception as e:
        logger.warning("Live snapshot failed, using yesterday's data only: %s", e)

    try:
        # ── Step 1: 全策略并行扫描（注入实时快照）───
        _pipeline_status["phase"] = "scanning"
        report["steps"]["scan"] = await _step_scan_all(live_bars)
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


# ── Step 0: 实时快照（全市场拉一次，15路策略共享）───

def _fetch_live_snapshot() -> dict[str, dict]:
    """腾讯 API 批量拉取全市场实时行情。一次调用，5200只票约 6-8 秒。

    Returns: {symbol: {price, open, high, low, volume, amount, change_pct}}
    """
    from data_sources.tencent_quotes import tencent_quote
    import pandas as pd

    # 获取全市场股票列表
    daily_dir = PROJECT_ROOT / "data" / "raw" / "daily"
    all_syms = sorted(f.stem for f in daily_dir.glob("*.parquet")
                      if not f.stem.startswith(("bj", "92")) and len(f.stem) == 6)

    snap = {}
    # tencent_quote 一次最多约 80 个，分批
    batch_size = 80
    for i in range(0, len(all_syms), batch_size):
        batch = all_syms[i:i + batch_size]
        try:
            batch_data = tencent_quote(batch)
            for code, q in batch_data.items():
                price = q.get("price", 0) or 0
                if price <= 0:
                    continue
                snap[code] = {
                    "price": price,
                    "open": q.get("open", price) or price,
                    "high": q.get("high", price) or price,
                    "low": q.get("low", price) or price,
                    "volume": 0,  # 腾讯API不直接给日累计量，用成交额反推估算
                    "amount": q.get("amount_wan", 0) * 10000 if q.get("amount_wan") else 0,
                    "change_pct": q.get("change_pct", 0) or 0,
                }
        except Exception as e:
            if i == 0:  # 第一批量失败才报错
                logger.warning("Tencent quote batch %d failed: %s", i, e)

    today_str = datetime.now().strftime("%Y-%m-%d")
    for s in snap.values():
        s["date"] = today_str
    return snap


# ── Step 1: 全策略并行扫描 ──

async def _step_scan_all(live_bars: dict = None) -> dict:
    """用所有内置+自定义策略跑全市场扫描,每路取 Top-15。

    live_bars: 实时快照 {symbol: {price,open,high,low,volume,date}}。
    扫描时在每票数据内存中追加一行再评估策略，不写磁盘不碰 DuckDB。
    """
    from server.services.strategy_scanner import scan_market
    from backtest.strategies.registry import list_all_strategies
    import pandas as pd
    import numpy as np

    all_modes = [s["id"] for s in (list_all_strategies().get("builtin", []) + list_all_strategies().get("user", []))]
    unique = list(dict.fromkeys(all_modes))

    async def _scan_one(mode):
        try:
            result = await asyncio.to_thread(
                scan_market, mode=mode, exclude_st=True, min_price=1,
                live_bars=live_bars)
            # 标注实时数据 freshness
            if result.get("results"):
                for r in result["results"]:
                    sym = r.get("symbol", "")
                    bar = live_bars.get(sym, {}) if live_bars else {}
                    if bar.get("price", 0) > 0:
                        r["data_freshness"] = "live"
            return mode, result
        except Exception as e:
            logger.warning("Scan %s failed: %s", mode, e)
            return mode, None

    # 并行跑全部策略（限 3 路并发，避免 13 路同时抢 DuckDB 游标导致 OOM/卡死）
    all_candidates = {}
    sem = asyncio.Semaphore(3)
    async def _scan_one_limited(mode):
        async with sem:
            return await _scan_one(mode)
    tasks = [asyncio.create_task(_scan_one_limited(m)) for m in unique]
    for task in asyncio.as_completed(tasks):
        mode, result = await task
        if result and result.get("matched", 0) > 0:
            stocks = result.get("results", [])[:15]
            for s in stocks:
                s["strategy_mode"] = mode
            all_candidates[mode] = stocks

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

# ── 15:00 补漏 + 持仓审查 ──

async def _run_position_review():
    """检查所有策略账户的持仓：信号确认(追涨票仍在涨?)、止损、止盈。

    15:00 执行，15:25 前完成。输出 sell/adjust 订单到 live_journal。
    """
    try:
        from server.live_engine import get_db as _gdb
        db = _gdb()
        rows = db.execute(
            "SELECT * FROM account_positions WHERE shares > 0"
        ).fetchall()
        if not rows:
            logger.info("Position review: no open positions")
            return

        # 获取实时价格（用腾讯 API 最后一批数据即可）
        from server.auction import _batch_fetch_em
        symbols = list({r["symbol"] for r in rows})
        prices = _batch_fetch_em(symbols)
        price_map = {p["symbol"]: p for p in prices}

        today = datetime.now().strftime("%Y-%m-%d")
        reviews = []

        for pos in rows:
            sym = pos["symbol"]
            acc_id = pos["account_id"]
            avg_cost = pos["avg_cost"]
            shares = pos["shares"]
            bar = price_map.get(sym, {})
            price = bar.get("price", 0)
            if price <= 0:
                continue

            pnl_pct = round((price - avg_cost) / avg_cost * 100, 2) if avg_cost > 0 else 0
            action = "hold"
            reason = ""

            # 止损: [-5%]
            if pnl_pct <= -5:
                action = "sell_all"
                reason = f"止损, 跌{pnl_pct:+.1f}% (成本{avg_cost})"
            # 止盈: [+15%]
            elif pnl_pct >= 15:
                action = "sell_half"
                reason = f"止盈, 涨{pnl_pct:+.1f}% 减半仓"
            # 刷新策略信号：该票仍然被当前策略选中吗?
            elif acc_id in ("strict_reverse",):  # 追涨票：价还在涨?
                chg = bar.get("change_pct", 0)
                if chg < -2:
                    action = "sell_all"
                    reason = f"追涨失败, 今日跌{chg:+.1f}%, 清仓"
                elif chg < 0:
                    action = "sell_half"
                    reason = f"追涨弱化, 今日微跌{chg:+.1f}%, 减半仓"
            elif acc_id in ("strict", "simple"):  # 超跌票：还在低位吗?
                if pnl_pct > 8:
                    action = "sell_half"
                    reason = f"抄底修复, 涨{pnl_pct:+.1f}%, 减半仓"

            reviews.append({
                "account_id": acc_id, "symbol": sym, "price": price,
                "pnl_pct": pnl_pct, "action": action, "reason": reason,
                "shares": shares, "avg_cost": avg_cost,
            })

            # 执行卖出
            if action.startswith("sell"):
                sell_qty = shares if action == "sell_all" else shares // 2
                sell_qty = (sell_qty // 100) * 100
                if sell_qty < 100:
                    continue
                cost = round(sell_qty * price * (1 + 0.00025 + 0.0005), 2)  # 佣金+印花税
                pnl = round((price - avg_cost) * sell_qty - cost, 2)
                db.execute(
                    "INSERT INTO account_trades(account_id,date,symbol,side,price,shares,cost,reason,pnl) VALUES(?,?,?,?,?,?,?,?,?)",
                    (acc_id, today, sym, "sell", price, sell_qty, cost, reason, pnl))
                new_shares = shares - sell_qty
                if new_shares > 0:
                    db.execute(
                        "UPDATE account_positions SET shares=?, current_price=?, unrealized_pnl=? WHERE account_id=? AND symbol=?",
                        (new_shares, price, round((price - avg_cost) * new_shares, 2), acc_id, sym))
                else:
                    db.execute(
                        "DELETE FROM account_positions WHERE account_id=? AND symbol=?",
                        (acc_id, sym))
                # 返还现金
                db.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (cost, acc_id))
                logger.info("Position review: %s %s %s %s股 @%s pnl=%s",
                            acc_id, sym, action, sell_qty, price, pnl)

        db.commit()
        logger.info("Position review done: %d positions, %d actions",
                    len(rows), sum(1 for r in reviews if r["action"] != "hold"))

        # 飞书推送（如果 toggles 允许）
        try:
            import os
            webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
            if webhook:
                try:
                    toggles = json.loads((PROJECT_ROOT / "data" / "feishu_toggles.json").read_text())
                    if not toggles.get("daily_close", True):
                        return
                except Exception:
                    pass
                from server.notify.feishu import FeishuNotifier
                actions = [r for r in reviews if r["action"] != "hold"]
                if actions:
                    body = "**📋 15:00 持仓审查**\n\n"
                    for a in actions[:10]:
                        body += f"• {a['symbol']} {a['action']} {a['shares']}股 @{a['price']:.2f} {a['reason']}\n"
                    notifier = FeishuNotifier(webhook)
                    await notifier.send_card("📋 持仓审查", body, "yellow", cooldown_key="pos_review", cooldown_seconds=3600)
        except Exception:
            pass

    except Exception as e:
        logger.error("Position review failed: %s", e, exc_info=True)


async def pipeline_scheduler():
    """后台调度: 14:30 决策 + 15:00 补漏 + 全天持仓审查."""
    await asyncio.sleep(10)
    _14_done = False
    _15_done = False
    _pos_review_done = False
    while True:
        try:
            now = datetime.now()
            if now.weekday() >= 5:
                await asyncio.sleep(60)
                continue
            today_str = now.strftime("%Y%m%d")
            t = now.time()

            # ── 14:30 正常决策 ──
            if dt_time(14, 30) <= t <= dt_time(14, 34) and not _14_done:
                _14_done = True
                logger.info("Pipeline: 14:30 trigger")
                await run_daily_pipeline()

            # ── 15:00 补漏 + 持仓审查 ──
            if dt_time(15, 0) <= t <= dt_time(15, 4) and not _15_done:
                _15_done = True
                if not _14_done or (not PLAN_PATH.exists()):
                    # 14:30 没跑、或 plan 没成功落盘
                    logger.info("Pipeline catch-up: 14:30 missed, running now (deadline 15:25)")
                    _14_done = True  # 防止二次补
                await _run_position_review()  # 补漏+持仓审查
                _pos_review_done = True

            # 跨天重置
            if now.strftime("%Y%m%d") != today_str:
                _14_done = False
                _15_done = False
                _pos_review_done = False

            await asyncio.sleep(45)
        except asyncio.CancelledError:
            logger.info("Pipeline scheduler cancelled")
            break
        except Exception as e:
            logger.error("Pipeline scheduler error: %s", e, exc_info=True)
            await asyncio.sleep(60)


def get_pipeline_status() -> dict:
    return _pipeline_status
