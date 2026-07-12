"""三才量化 — 实时监控引擎（精简版）.

后台异步轮询，仅在交易时段检测策略买卖信号并通过飞书推送.

设计原则:
  - 只在正常交易时间运行 (9:30-11:30, 13:00-15:00)
  - 只提醒策略触发的 BUY/SELL 信号（不刷屏）
  - 纯 parquet + sancai_core 数据管线，不依赖 akshare（避免 py_mini_racer V8 崩溃）
  - 盘中定时报告（盘前/竞价/淘汰/午盘/盘后）继续保留
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from server.utils import DATA_DIR

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Trading time helpers
# ═══════════════════════════════════════════════════════════════════

def _is_trading_time() -> bool:
    """A股连续竞价交易时段 (9:30-11:30, 13:00-15:00)."""
    now = datetime.now()
    t = now.time()
    if now.weekday() >= 5:
        return False
    if dtime(9, 30) <= t <= dtime(11, 30):
        return True
    if dtime(13, 0) <= t <= dtime(15, 0):
        return True
    return False


def _is_trading_day(dt: datetime = None) -> bool:
    """判断是否为A股交易日 (周一到周五, 非节假日)."""
    if dt is None:
        dt = datetime.now()
    return dt.weekday() < 5  # Simplified: Mon-Fri. Holiday calendar is in fill_gaps.py


def _is_auction_window() -> bool:
    """集合竞价窗口 (9:20-9:28)."""
    now = datetime.now()
    t = now.time()
    if now.weekday() >= 5:
        return False
    return dtime(9, 20) <= t <= dtime(9, 28)


def _trading_session() -> str:
    """当前交易时段标签."""
    t = datetime.now().time()
    if dtime(9, 30) <= t <= dtime(11, 30):
        return "上午盘"
    if dtime(13, 0) <= t <= dtime(15, 0):
        return "下午盘"
    if _is_auction_window():
        return "集合竞价"
    return "非交易时段"


# ═══════════════════════════════════════════════════════════════════
# Data loading — parquet only, no akshare dependency
# ═══════════════════════════════════════════════════════════════════

def _load_daily_kline(symbol: str, tail: int = 250) -> Optional[pd.DataFrame]:
    """从本地 parquet 加载日线数据，不依赖 akshare."""
    fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
    if not fpath.exists():
        return None
    try:
        df = pd.read_parquet(fpath)
        if len(df) > tail:
            df = df.tail(tail)
        return df.reset_index(drop=True)
    except Exception:
        return None


def _try_get_live_price(symbol: str) -> Optional[dict]:
    """尝试获取腾讯实时行情（纯 HTTP，不绑 akshare）."""
    try:
        from data_sources.tencent_quotes import tencent_quote
        rt_data = tencent_quote([symbol])
        # tencent_quote 返回的 key 不带前缀 (如 "000001")，
        # 但 symbol 可能带前缀 (如 "sh000001")，需要统一
        lookup = symbol
        if len(symbol) == 8 and symbol.startswith(("sh", "sz", "bj")):
            lookup = symbol[2:]
        q = rt_data.get(lookup) or rt_data.get(symbol)
        if q and q.get("price", 0) > 0:
            return {
                "price": q["price"],
                "open": q["open"],
                "high": q["high"],
                "low": q["low"],
                "name": q.get("name", ""),
            }
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Global state
# ═══════════════════════════════════════════════════════════════════

_monitor_task: Optional[asyncio.Task] = None
_monitor_running = False
_monitor_config: dict = {}
_monitor_last_check: dict[str, float] = {}


def _load_config() -> dict:
    """加载监控配置."""
    try:
        from server.utils.config import get_config
        cfg = get_config()
        return cfg.get("monitor", {})
    except Exception:
        return {}


def get_monitor_status() -> dict:
    """获取监控运行状态."""
    return {
        "running": _monitor_running,
        "config": {
            "enabled": _monitor_config.get("enabled", False),
            "poll_interval_minutes": _monitor_config.get("poll_interval_minutes", 5),
            "feishu_configured": bool(
                os.environ.get("FEISHU_WEBHOOK_URL", "")
                or (
                    _monitor_config.get("feishu_webhook_url", "")
                    and not _monitor_config.get("feishu_webhook_url", "").startswith("${")
                )
            ),
        },
        "last_check": _monitor_last_check,
    }


def _load_holdings() -> list[dict]:
    """加载持仓数据 — 从共享工具模块."""
    from server.utils.holdings_utils import _load_holdings as _load
    return _load()


# ═══════════════════════════════════════════════════════════════════
# Core: strategy signal detection (delegates to strategy_scanner)
# ═══════════════════════════════════════════════════════════════════


def _check_strategy_signal(
    symbol: str,
    name: str,
    strategy_mode: str,
    df: pd.DataFrame,
    school_config: dict = None,
) -> Optional[dict]:
    """检测单只股票在指定策略下是否有买/卖信号.

    直接复用 strategy_scanner.check_buy_signal — 全三才系统统一策略逻辑.
    返回 None 表示无信号；返回 dict 表示有信号.
    """
    try:
        from server.services.strategy_scanner import check_buy_signal

        result = check_buy_signal(df, strategy_mode, school_config)
        return result  # already in canonical format: {price, reason, confidence, direction, buy_type, signal, conditions_met}
    except Exception as e:
        logger.debug(f"Strategy check error for {symbol}/{strategy_mode}: {e}", exc_info=True)
        return None


# ═══════════════════════════════════════════════════════════════════
# 统一价格快照 — 所有飞书提醒的数据入口
# ═══════════════════════════════════════════════════════════════════

def _get_price_snapshot(symbol: str) -> dict:
    """获取一只股票的最新价格快照, 优先级: 腾讯实时 > parquet日线.

    返回 dict:
      price:        最新价 (float)
      open/high/low: 当日开/高/低 (实时有则来自腾讯, 否则来自parquet昨日)
      name:         股票名称
      source:       "腾讯实时" | "parquet日线"
      freshness:    "实时" | "昨收(YYYY-MM-DD)" | "无数据"
      date_str:     数据日期 (YYYY-MM-DD)
      is_live:      True/False
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1) 尝试腾讯实时行情
    live = _try_get_live_price(symbol)
    if live and live.get("price", 0) > 0:
        return {
            "price": live["price"],
            "open": live.get("open", live["price"]),
            "high": live.get("high", live["price"]),
            "low": live.get("low", live["price"]),
            "name": live.get("name", ""),
            "source": "腾讯实时",
            "freshness": "实时",
            "date_str": today_str,
            "is_live": True,
        }

    # 2) 回退 parquet 日线 (前一交易日收盘)
    df = _load_daily_kline(symbol, tail=5)
    if df is not None and len(df) > 0 and "close" in df.columns:
        closes = df["close"].values
        price = float(closes[-1])
        last_date = str(df.iloc[-1].get("date", ""))[:10] if "date" in df.columns else "未知"

        # 判断数据新鲜度
        if last_date == today_str:
            freshness = "今日收盘" if not _is_trading_time() else "今日Parquet"
            source = "parquet日线(今日)"
        else:
            freshness = f"昨收({last_date})"
            source = f"parquet日线({last_date})"

        return {
            "price": price,
            "open": float(df["open"].values[-1]) if "open" in df.columns else price,
            "high": float(df["high"].values[-1]) if "high" in df.columns else price,
            "low": float(df["low"].values[-1]) if "low" in df.columns else price,
            "name": "",
            "source": source,
            "freshness": freshness,
            "date_str": last_date,
            "is_live": False,
        }

    return {"price": 0, "open": 0, "high": 0, "low": 0, "name": "",
            "source": "无数据", "freshness": "无数据", "date_str": "", "is_live": False}


def _freshness_note(snap: dict) -> str:
    """根据快照生成数据新鲜度说明."""
    if snap["is_live"]:
        return f"🟢 数据源: {snap['source']} — 与实盘一致"
    elif snap["price"] > 0:
        stale_days = 0
        if snap["date_str"]:
            try:
                d = datetime.strptime(snap["date_str"], "%Y-%m-%d")
                stale_days = (datetime.now() - d).days
            except Exception:
                pass
        if stale_days == 0:
            return f"🟡 数据源: {snap['source']} — 今日数据尚未更新到本地"
        elif stale_days == 1:
            return f"🟡 数据源: {snap['source']} — 非交易时段, 此为上一交易日收盘数据"
        else:
            return f"🔴 数据源: {snap['source']} — 数据滞后{stale_days}天, 请手动更新数据"
    return "🔴 无可用数据"

def _build_signal_card(symbol: str, name: str, signal: dict, hold_qty: int = 0,
                       cost_price: float = 0, snap: dict = None,
                       signal_date: str = "") -> tuple[str, str, str]:
    """构建飞书卡片内容. 返回 (title, body, color).

    snap 来自 _get_price_snapshot, 确保所有价格标注数据源.
    signal_date 是触发信号的K线日期.
    """
    direction = signal["direction"]
    strategy_name = signal.get("signal", "unknown")
    price = signal["price"]
    reason = signal.get("reason", "")
    confidence = signal.get("confidence", 0.8)
    session = _trading_session()

    if direction == "SELL":
        color = "red"
        emoji = "🔴"
        action = "卖出信号"
    else:
        color = "green"
        emoji = "🟢"
        action = "买入信号"

    display_name = name if name and name != symbol else ""
    # 如果快照有实时名称, 优先使用
    if snap and snap.get("name") and not display_name:
        display_name = snap["name"]
    name_part = f"{display_name} " if display_name else ""

    title = f"📊 {name_part}{symbol} · {emoji} {action}"

    # ── 信号价格行 (标注K线日期) ──
    signal_date_note = f" (@{signal_date})" if signal_date else ""
    body_lines = [
        f"**策略**: {strategy_name}",
        f"**信号**: {emoji} {direction}",
        f"**信号价**: {price:.2f}{signal_date_note}",
        f"**原因**: {reason}",
        f"**置信度**: {confidence:.0%}",
    ]

    # ── 实时价格对比 ──
    if snap and snap.get("is_live"):
        lp = snap["price"]
        diff = lp - price
        diff_pct = (diff / price * 100) if price > 0 else 0
        arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
        body_lines.append(f"**🟢 实时价**: {lp:.2f} ({arrow}{diff_pct:+.2f}% vs 信号价)")
    elif snap and snap["price"] > 0:
        body_lines.append(f"**{snap['freshness']}**: {snap['price']:.2f}")
        body_lines.append(f"⚠️ 无实时行情, 以上为 {snap['date_str']} 收盘数据")

    # ── 数据新鲜度说明 ──
    if snap:
        body_lines.append(_freshness_note(snap))

    if hold_qty > 0 and cost_price > 0:
        pnl = (price - cost_price) / cost_price * 100
        body_lines.append(f"**持仓**: {hold_qty}股 @{cost_price:.2f} (盈亏{pnl:+.1f}%)")

    body = "\n".join(f"- {line}" for line in body_lines)
    body += f"\n\n---\n> 三才量化 · 策略信号触发型提醒 | {session} | 仅作参考，风险自担"

    return title, body, color


# ═══════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════

async def _monitor_loop():
    """后台监控主循环."""
    global _monitor_running, _monitor_config, _monitor_last_check

    logger.info("Monitor loop started (strategy-signal-only mode)")
    _monitor_running = True

    # 配置文件路径
    config_path = Path(__file__).parent.parent / "config" / "defaults.yaml"

    while _monitor_running:
        try:
            # 热加载配置
            _monitor_config = _load_config()
            if not _monitor_config.get("enabled", False):
                await asyncio.sleep(30)
                continue

            webhook_url = _monitor_config.get("feishu_webhook_url", "")
            # Resolve ${ENV_VAR} placeholders / prefer env var over config
            if not webhook_url or (webhook_url.startswith("${") and webhook_url.endswith("}")):
                webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
            if not webhook_url:
                logger.warning("Monitor enabled but feishu_webhook_url not configured")
                await asyncio.sleep(60)
                continue

            from .notify.feishu import FeishuNotifier
            notifier = FeishuNotifier(webhook_url)

            poll_minutes = _monitor_config.get("poll_interval_minutes", 5)
            holdings = _load_holdings()
            now_dt = datetime.now()

            # ═══════════════════════════════════════════════════════════
            # 1. 盘中策略信号（仅交易时段）
            # ═══════════════════════════════════════════════════════════
            if _is_trading_time():
                now = time.time()

                for h in holdings:
                    symbol = h.get("symbol", "")
                    name = h.get("name", symbol)
                    strategy_mode = h.get("strategy", "")
                    if not symbol:
                        continue

                    # 没有绑定策略的持仓跳过信号检测
                    if not strategy_mode:
                        continue

                    # 轮询间隔
                    last_check = _monitor_last_check.get(symbol, 0)
                    if now - last_check < poll_minutes * 60:
                        continue
                    _monitor_last_check[symbol] = now

                    # 获取实时价格快照
                    snap = _get_price_snapshot(symbol)

                    # 加载 K 线
                    df = _load_daily_kline(symbol, tail=250)
                    if df is None or len(df) < 60:
                        continue

                    # 记录最后一条K线的日期
                    signal_date = str(df.iloc[-1].get("date", ""))[:10] if "date" in df.columns else ""

                    # 追加实时行情作为最新bar (用于策略检测)
                    today_str = now_dt.strftime("%Y-%m-%d")
                    if signal_date != today_str and snap.get("is_live"):
                        new_row = {
                            "date": today_str,
                            "open": snap["open"],
                            "high": snap["high"],
                            "low": snap["low"],
                            "close": snap["price"],
                            "volume": 0,
                        }
                        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                        signal_date = today_str  # 信号日期更新为今天

                    # 检测策略信号
                    signal = await asyncio.to_thread(
                        _check_strategy_signal, symbol, name or snap.get("name", symbol), strategy_mode, df
                    )

                    if signal is None:
                        continue  # 无信号，不发消息

                    # 构建并发送飞书卡片
                    title, body, color = _build_signal_card(
                        symbol, name or snap.get("name", symbol), signal,
                        hold_qty=h.get("quantity", 0),
                        cost_price=h.get("cost_price", 0),
                        snap=snap,
                        signal_date=signal_date,
                    )

                    # 去重key: symbol + 策略 + 信号方向 + 日期
                    cool_key = f"{symbol}_{strategy_mode}_{signal['direction']}_{now_dt.strftime('%Y%m%d_%H')}"
                    await notifier.send_card(title, body, color,
                                            cooldown_key=cool_key,
                                            cooldown_seconds=7200)  # 2小时冷却

                    # 事件引擎发布
                    try:
                        from server.events import event_engine, EventType
                        event_engine.emit(EventType.SIGNAL, {
                            "symbol": symbol, "name": name,
                            "signal": signal,
                            "timestamp": datetime.now().isoformat(),
                        }, source="monitor")
                    except ImportError:
                        pass

            # ═══════════════════════════════════════════════════════════
            # 2. 每日 9:00 盘中做T计划
            # ═══════════════════════════════════════════════════════════
            if now_dt.hour == 9 and 0 <= now_dt.minute < poll_minutes and \
                    _monitor_config.get("alert_rules", {}).get("daily_plan_push", True):
                last_key = "__daily_t0_plan__"
                today = now_dt.strftime("%Y%m%d")
                if _monitor_last_check.get(last_key) != today:
                    _monitor_last_check[last_key] = today
                    try:
                        hds = _load_holdings()
                        if hds:
                            t0_lines = []
                            # 全局数据新鲜度说明 (一次)
                            global_freshness = ""

                            for h in hds:
                                sym = h["symbol"]; nm = h.get("name", sym)
                                qty = h["quantity"]; cost = h["cost_price"]

                                # ── 用统一快照获取最新价格 ──
                                snap = _get_price_snapshot(sym)
                                price = snap["price"]
                                if price <= 0:
                                    continue

                                # 支撑/压力位始终来自 parquet 历史数据(20日高低点)
                                df = _load_daily_kline(sym, tail=60)
                                if df is None or len(df) < 20:
                                    continue
                                closes = df["close"].values
                                support = float(min(closes[-20:]))
                                resistance = float(max(closes[-20:]))

                                pnl_pct = round((price - cost) / cost * 100, 2) if cost > 0 else 0
                                t0_entry = round(support * 1.005, 2)
                                t0_exit = round(price * 1.02, 2)
                                t0_stop = round(support * 0.995, 2)

                                # 做T建议
                                if pnl_pct > 5:
                                    t0_advice = "🟢 浮盈中，可做正T（低吸高抛）"
                                elif pnl_pct < -3:
                                    t0_advice = "🔴 浮亏中，做反T降成本（高抛低吸）或持有等待"
                                elif price < resistance * 0.95:
                                    t0_advice = "🔵 距压力位较远，适合正T"
                                else:
                                    t0_advice = "🟡 接近压力位，谨慎做T"

                                # 现价标注数据源
                                if snap["is_live"]:
                                    price_label = f"🟢 实时价: {price:.2f}"
                                else:
                                    price_label = f"🟡 {snap['freshness']}: {price:.2f}"

                                if not global_freshness and not snap["is_live"]:
                                    global_freshness = _freshness_note(snap)

                                t0_lines.append(
                                    f"**{nm}({sym})** | 持仓{qty}股 @{cost:.2f}\n"
                                    f"- {price_label} 盈亏: {pnl_pct:+.2f}%\n"
                                    f"- 支撑(20日最低): {support:.2f} 压力(20日最高): {resistance:.2f} [历史数据]\n"
                                    f"- T0买入: {t0_entry} T0卖出: {t0_exit} 止损: {t0_stop}\n"
                                    f"- {t0_advice}"
                                )
                            if t0_lines:
                                body = f"**今日盘中做T计划**\n\n{global_freshness}\n\n" if global_freshness else "**今日盘中做T计划**\n\n"
                                body += "\n\n".join(t0_lines[:8])
                                body += "\n\n---\n> T0买入/卖出/止损基于历史支撑位计算 | 请以盘中实时价为准 | 做T仓位不超过总仓20%"
                                await notifier.send_card("📈 盘中做T计划", body, "blue",
                                                        cooldown_key=last_key, cooldown_seconds=3600)
                    except Exception as e:
                        logger.warning(f"T0 plan push failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 3. 盘前报告 9:00
            # ═══════════════════════════════════════════════════════════
            if now_dt.hour == 9 and 0 <= now_dt.minute < poll_minutes:
                last_key = "__pre_market_report__"
                today = now_dt.strftime("%Y%m%d")
                if _monitor_last_check.get(last_key) != today:
                    _monitor_last_check[last_key] = today
                    try:
                        idx_map = {"sh000001": "上证指数", "sz399001": "深证成指",
                                   "sz399006": "创业板指", "sh000688": "科创50"}
                        idx_lines = []
                        data_dates = set()
                        for code, nm in idx_map.items():
                            snap = _get_price_snapshot(code)
                            price = snap["price"]
                            if price <= 0:
                                continue
                            data_dates.add(snap["date_str"])
                            # 前一交易日对比
                            df = _load_daily_kline(code, tail=10)
                            if df is not None and len(df) >= 5 and "close" in df.columns:
                                c = df["close"].values
                                chg = round((float(c[-1]) - float(c[-2])) / float(c[-2]) * 100, 2) if c[-2] > 0 else 0
                                chg5d = round((float(c[-1]) - float(c[-5])) / float(c[-5]) * 100, 2) if c[-5] > 0 else 0
                                trend = "📈" if chg > 0 else "📉"
                                freshness_tag = "🟢" if snap["is_live"] else "🟡"
                                idx_lines.append(f"{trend} {freshness_tag} {nm}: {price:.2f} ({chg:+.2f}%) | 5日: {chg5d:+.2f}%")

                        hds = _load_holdings()
                        pos_lines = []; total_val = 0; total_pnl = 0
                        for h in hds:
                            snap = _get_price_snapshot(h["symbol"])
                            price = snap["price"]
                            if price <= 0:
                                continue
                            data_dates.add(snap["date_str"])
                            mv = round(price * h["quantity"], 2) if price > 0 else 0
                            pnl = round(mv - h["cost_price"] * h["quantity"], 2) if mv > 0 else 0
                            pnl_pct = round(pnl / (h["cost_price"] * h["quantity"]) * 100, 2) if h["cost_price"] > 0 else 0
                            total_val += mv; total_pnl += pnl
                            freshness_tag = "🟢" if snap["is_live"] else "🟡"
                            pos_lines.append(f"{freshness_tag} {h.get('name',h['symbol'])}: {price:.2f} 盈亏{pnl_pct:+.1f}%")

                        # 数据日期
                        dates_str = " / ".join(sorted(data_dates)) if data_dates else "未知"
                        body = "**📊 指数概览**\n" + "\n".join(idx_lines) if idx_lines else "**📊 指数概览**\n(无数据)"
                        if pos_lines:
                            body += f"\n\n**💼 持仓 (市值{total_val/10000:.1f}万 | 盈亏{total_pnl:+.0f})**\n" + "\n".join(pos_lines)
                        body += f"\n\n---\n> 盘前报告 · 9:00 | 数据日期: {dates_str}"
                        body += "\n> 🟢=实时 🟡=昨日收盘 | 非交易时段无法获取实时行情"
                        await notifier.send_card("🌅 盘前报告", body, "blue",
                                                cooldown_key=last_key, cooldown_seconds=3600)

                        # ── 盘前消息快报 (隔夜消息, 16h) ──
                        digest_key = f"__pre_digest_{now_dt.strftime('%Y%m%d')}__"
                        if _monitor_last_check.get(digest_key) != now_dt.strftime("%Y%m%d"):
                            _monitor_last_check[digest_key] = now_dt.strftime("%Y%m%d")
                            try:
                                from server.services.news_digest import build_news_digest, build_feishu_digest_card
                                groups = build_news_digest(hours_back=16)
                                if groups.get("total", 0) > 0:
                                    content = build_feishu_digest_card(groups, "📰 盘前消息快报")
                                    await notifier.send_card("📰 盘前消息快报", content, "yellow",
                                                            cooldown_key=digest_key, cooldown_seconds=7200)
                            except Exception as e:
                                logger.warning(f"Pre-market digest failed: {e}")
                    except Exception as e:
                        logger.warning(f"Pre-market report failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 4. 盘后报告 15:30
            # ═══════════════════════════════════════════════════════════
            if now_dt.hour == 15 and 30 <= now_dt.minute < 30 + poll_minutes:
                last_key = "__post_market_report__"
                today = now_dt.strftime("%Y%m%d")
                if _monitor_last_check.get(last_key) != today:
                    _monitor_last_check[last_key] = today
                    try:
                        idx_map = {"sh000001": "上证", "sz399001": "深成", "sz399006": "创业板",
                                   "sh000688": "科创50", "sh000016": "上证50"}
                        idx_lines = []
                        data_dates = set()
                        for code, nm in idx_map.items():
                            snap = _get_price_snapshot(code)
                            price = snap["price"]
                            if price <= 0:
                                continue
                            data_dates.add(snap["date_str"])
                            df = _load_daily_kline(code, tail=5)
                            if df is not None and len(df) >= 2 and "close" in df.columns:
                                c = df["close"].values
                                chg = round((float(c[-1]) - float(c[-2])) / float(c[-2]) * 100, 2) if c[-2] > 0 else 0
                                freshness_tag = "🟢" if snap["is_live"] else "🟡"
                                idx_lines.append(f"{freshness_tag} {nm}: {price:.2f} ({chg:+.2f}%)")

                        hds = _load_holdings()
                        pos_lines = []; total_val = 0; total_pnl = 0
                        for h in hds:
                            snap = _get_price_snapshot(h["symbol"])
                            price = snap["price"]
                            if price <= 0:
                                continue
                            data_dates.add(snap["date_str"])
                            df = _load_daily_kline(h["symbol"], tail=5)
                            pre = 0.0
                            if df is not None and len(df) >= 2:
                                pre = float(df["close"].values[-2])
                            day_chg = round((price - pre) / pre * 100, 2) if pre > 0 else 0
                            mv = round(price * h["quantity"], 2)
                            pnl = round(mv - h["cost_price"] * h["quantity"], 2)
                            total_val += mv; total_pnl += pnl
                            tag = "🔴" if day_chg < -2 else "🟢" if day_chg > 2 else "🟡"
                            freshness_tag = "🟢" if snap["is_live"] else "🟡"
                            pos_lines.append(f"{tag}{freshness_tag} {h.get('name',h['symbol'])}: {price:.2f} ({day_chg:+.1f}%) | 持仓盈亏{pnl:+.0f}")

                        dates_str = " / ".join(sorted(data_dates)) if data_dates else "未知"
                        freshness_note = ""
                        if all(not _get_price_snapshot(code)["is_live"] for code in idx_map):
                            freshness_note = "\n⚠️ 未获取到实时行情, 以上价格为最近可用数据"

                        body = "**📊 今日收盘**\n" + "\n".join(idx_lines) if idx_lines else "**📊 今日收盘**\n(无数据)"
                        if pos_lines:
                            body += f"\n\n**💼 持仓复盘 (市值{total_val/10000:.1f}万 | 总盈亏{total_pnl:+.0f})**\n" + "\n".join(pos_lines)
                        body += f"\n\n---\n> 盘后报告 · 15:30 | 数据日期: {dates_str}{freshness_note}"
                        body += "\n> 🟢=实时 🟡=可能滞后 | 投资有风险, 操作需谨慎"
                        await notifier.send_card("🌇 盘后报告", body, "yellow",
                                                cooldown_key=last_key, cooldown_seconds=3600)

                        # ── 盘后全天消息汇总 ──
                        digest_key = f"__post_digest_{now_dt.strftime('%Y%m%d')}__"
                        if _monitor_last_check.get(digest_key) != now_dt.strftime("%Y%m%d"):
                            _monitor_last_check[digest_key] = now_dt.strftime("%Y%m%d")
                            try:
                                from server.services.news_digest import build_news_digest, build_feishu_digest_card
                                groups = build_news_digest(hours_back=10)
                                if groups.get("total", 0) > 0:
                                    content = build_feishu_digest_card(groups, "📰 全天消息汇总")
                                    await notifier.send_card("📰 全天消息汇总", content, "yellow",
                                                            cooldown_key=digest_key, cooldown_seconds=7200)
                            except Exception as e:
                                logger.warning(f"Post-market digest failed: {e}")
                    except Exception as e:
                        logger.warning(f"Post-market report failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 5. 集合竞价分析 (保留)
            # ═══════════════════════════════════════════════════════════
            if _is_auction_window() and \
                    _monitor_config.get("reports", {}).get("auction_review", True):
                auc_key_24 = "__auction_924__"
                auc_key_28 = "__auction_928__"
                today_str3 = now_dt.strftime("%Y%m%d")

                if 24 <= now_dt.minute < 26 and _monitor_last_check.get(auc_key_24) != today_str3:
                    _monitor_last_check[auc_key_24] = today_str3
                    try:
                        from server.auction import fetch_auction_data
                        snap = await fetch_auction_data(force=True)
                        _monitor_last_check["__auction_snap_24__"] = json.dumps(snap.get("stocks", [])[:100])
                        logger.info(f"Auction 9:24 snapshot: {snap.get('count', 0)} stocks")
                    except Exception as e:
                        logger.warning(f"Auction 9:24 failed: {e}")

                if 26 <= now_dt.minute < 29 and _monitor_last_check.get(auc_key_28) != today_str3:
                    _monitor_last_check[auc_key_28] = today_str3
                    try:
                        from server.auction import auction_report
                        report = await auction_report()
                        analysis = report.get("analysis", {})
                        if analysis.get("high_open") or analysis.get("volume_surge"):
                            body = "**集合竞价归因结果**\n\n"
                            if analysis.get("high_open"):
                                body += "**🟢 高开>2%**\n" + "\n".join(f"- {x}" for x in analysis["high_open"][:8]) + "\n"
                            if analysis.get("low_open"):
                                body += "\n**🔴 低开<-2%**\n" + "\n".join(f"- {x}" for x in analysis["low_open"][:5]) + "\n"
                            if analysis.get("volume_surge"):
                                body += "\n**🔥 竞价放量**\n" + "\n".join(f"- {x}" for x in analysis["volume_surge"][:5]) + "\n"
                            body += f"\n**📐 概念趋向**\n{analysis.get('concept_trend', '')}\n"
                            body += f"\n**📋 操作建议**\n{analysis.get('operation_advice', '')}"
                            body += "\n\n---\n> 竞价复盘 · 9:26 | 仅作参考"
                            await notifier.send_card("🔔 集合竞价复盘", body, "yellow",
                                                    cooldown_key=auc_key_28, cooldown_seconds=300)
                            _monitor_last_check["__auction_candidates__"] = json.dumps(report.get("candidates", []))
                    except Exception as e:
                        logger.warning(f"Auction report failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 6. 每周一 9:00 周计划
            # ═══════════════════════════════════════════════════════════
            if _monitor_config.get("alert_rules", {}).get("weekly_plan_push", True):
                if now_dt.weekday() == 0 and now_dt.hour == 9 and 0 <= now_dt.minute < poll_minutes:
                    last_week_key = "__weekly_plan__"
                    last_week = _monitor_last_check.get(last_week_key, "")
                    today_week = now_dt.strftime("%Y%W")
                    if last_week != today_week:
                        _monitor_last_check[last_week_key] = today_week
                        try:
                            plans = _monitor_config.get("rendao", {}).get("profiles", {})
                            if plans:
                                p = plans.get("balanced", plans[list(plans.keys())[0]])
                                weekly_msg = (
                                    f"**本周操作计划**\n"
                                    f"- 风格: {p.get('style', '中线波段')}\n"
                                    f"- 仓位: {p.get('total_position_pct', 70)}%\n"
                                    f"- 止盈: +{p.get('take_profit_pct', 15)}%\n"
                                    f"- 止损: -{p.get('stop_loss_pct', 5)}%"
                                )
                                await notifier.send_card("📅 本周操作计划", weekly_msg, "blue",
                                                        cooldown_key=last_week_key, cooldown_seconds=86400)
                        except Exception as e:
                            logger.warning(f"Weekly plan push failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 7. 数据健康检查 (每日9:00 + 15:30)
            # ═══════════════════════════════════════════════════════════
            _data_health_hours = {9, 15}
            if now_dt.hour in _data_health_hours and now_dt.minute < poll_minutes:
                health_key = f"__data_health_{now_dt.hour}__"
                today = now_dt.strftime("%Y%m%d")
                if _monitor_last_check.get(health_key) != today:
                    _monitor_last_check[health_key] = today
                    try:
                        daily_dir = DATA_DIR / "daily"
                        if daily_dir.exists():
                            all_files = list(daily_dir.glob("*.parquet"))
                            total = len(all_files)

                            # 采样最新日期
                            latest_dates = {}
                            stale_count = 0
                            sample_size = min(200, total)
                            import random
                            sample = random.sample(all_files, sample_size) if total > sample_size else all_files

                            for fp in sample:
                                try:
                                    df = pd.read_parquet(fp)
                                    if len(df) > 0 and "date" in df.columns:
                                        d = str(df.iloc[-1]["date"])[:10]
                                        latest_dates[d] = latest_dates.get(d, 0) + 1
                                except Exception:
                                    stale_count += 1

                            # 找出最常见的日期
                            most_common_date = max(latest_dates, key=latest_dates.get) if latest_dates else "未知"
                            freshness_pct = latest_dates.get(most_common_date, 0) / max(sample_size, 1) * 100

                            # 判断健康状态
                            today_str = now_dt.strftime("%Y-%m-%d")
                            if most_common_date == today_str:
                                health_status = "✅ 健康"
                                health_color = "green"
                                health_note = "今日数据已更新到位"
                            elif most_common_date == (now_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d") or \
                                 (now_dt.weekday() == 0 and most_common_date == (now_dt - pd.Timedelta(days=3)).strftime("%Y-%m-%d")):
                                # 昨天 (或周五→周一)
                                health_status = "🟡 待更新"
                                health_color = "yellow"
                                health_note = "数据为前一个交易日, 等待今日更新"
                            else:
                                days_behind = 0
                                try:
                                    d = pd.Timestamp(most_common_date)
                                    days_behind = (pd.Timestamp.now() - d).days
                                except Exception:
                                    pass
                                health_status = f"🔴 滞后{days_behind}天" if days_behind > 0 else "🔴 异常"
                                health_color = "red"
                                health_note = f"最新数据日期为 {most_common_date}, 请检查数据更新链路"

                            body = (f"**📊 数据健康检查**\n\n"
                                    f"- 数据文件总数: **{total}** 只\n"
                                    f"- 抽样: {sample_size}只 | 损坏: {stale_count}只\n"
                                    f"- 最新数据日期: **{most_common_date}** (覆盖{freshness_pct:.0f}%)\n"
                                    f"- 状态: **{health_status}**\n"
                                    f"- 说明: {health_note}\n\n"
                                    f"---\n> 数据健康检查 · {now_dt.hour}:00 | 零akshare依赖 | 仅检查不更新")

                            await notifier.send_card(f"📦 数据健康 · {health_status}", body, health_color,
                                                    cooldown_key=health_key, cooldown_seconds=7200)
                    except Exception as e:
                        logger.warning(f"Data health check failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 8. 收盘后全量数据更新 (15:30-16:00, 仅交易日)
            # 窗口扩大到 30 分钟确保 5500+ 股票全部更新完毕
            # ═══════════════════════════════════════════════════════════
            _update_window_minutes = 30  # 30 分钟足够全量更新
            if now_dt.hour == 15 and 30 <= now_dt.minute < 30 + _update_window_minutes and \
                    _is_trading_day(now_dt):
                update_key = f"__data_update_{now_dt.strftime('%Y%m%d')}__"
                if _monitor_last_check.get(update_key) != now_dt.strftime("%Y%m%d"):
                    _monitor_last_check[update_key] = now_dt.strftime("%Y%m%d")
                    try:
                        # ── 更新前检查 ──
                        from scripts.update_daily_tencent import check_all_freshness, update_all
                        before = check_all_freshness()
                        logger.info(f"Data update starting: {before['total']} symbols, "
                                   f"most_common={before.get('most_common_date')}, stale={before.get('stale_count')}")

                        # ── 执行全量更新 ──
                        symbol_count = before["total"]
                        await notifier.send_card(
                            "📊 数据更新开始",
                            f"**每日收盘数据更新**\n\n"
                            f"- 总股票数: **{symbol_count}** 只\n"
                            f"- 更新前最新日期: **{before.get('most_common_date', '?')}**\n"
                            f"- 过期股票: **{before.get('stale_count')}** 只\n"
                            f"- 数据源: 东方财富API (腾讯备用)\n\n"
                            f"> 全量 {symbol_count} 只预计耗时3-5分钟，完成后推送详情",
                            "blue", cooldown_key=f"{update_key}_start", cooldown_seconds=3600)

                        result = await asyncio.to_thread(
                            update_all, symbols=None, days=10,
                            max_workers=8
                        )

                        # ── 更新后检查 ──
                        after = check_all_freshness()

                        # ── 飞书推送结果 ──
                        status_color = "green" if result["errors"] == 0 else "yellow"
                        status_emoji = "✅" if result["errors"] == 0 else "⚠️"

                        elapsed_sec = result.get("elapsed", 0)
                        elapsed_str = f"{elapsed_sec:.0f}秒" if elapsed_sec < 60 else f"{elapsed_sec/60:.1f}分钟"

                        # 计算变化
                        before_common = before.get("most_common_date", "?")
                        after_common = after.get("most_common_date", "?")
                        before_stale = before.get("stale_count", 0)
                        after_stale = after.get("stale_count", 0)
                        stale_fixed = max(0, before_stale - after_stale)
                        date_improved = "" if before_common == after_common else f" ({before_common} → **{after_common}**)"

                        # 构建未更新到最新日期的股票明细（分组显示）
                        stale_symbols = after.get("stale_symbols", [])
                        stale_detail_lines: list[str] = []
                        if stale_symbols and after_common:
                            # 按日期分组
                            from collections import defaultdict
                            grouped: dict[str, list[dict]] = defaultdict(list)
                            for s in stale_symbols:
                                grouped[s["latest_date"]].append(s)
                            stale_detail_lines.append(f"**📋 未更新到主流日期 ({after_common}) 的股票**\n")
                            # 按日期降序排列
                            for d in sorted(grouped.keys(), reverse=True):
                                items = grouped[d]
                                stale_detail_lines.append(f"**{d}** — {len(items)} 只")
                                # 每行显示: 代码 名称
                                line_items = [f"`{s['symbol']}` {s['name']}" for s in items[:30]]
                                stale_detail_lines.append("  " + " · ".join(line_items))
                                if len(items) > 30:
                                    stale_detail_lines.append(f"  ... 还有 {len(items) - 30} 只")
                                stale_detail_lines.append("")
                        stale_detail = "\n".join(stale_detail_lines) if stale_detail_lines else "✅ 所有股票均已更新到最新日期"

                        body = (
                            f"**{status_emoji} 每日收盘数据更新完成**\n\n"
                            f"**📈 执行概况**\n"
                            f"- 数据更新耗时: **{elapsed_str}**\n"
                            f"- 总股票数: **{result['symbols_total']}** 只\n"
                            f"- 成功更新: **{result['symbols_updated']}** 只\n"
                            f"- 新增K线: **{result['bars_added']}** 条\n"
                            f"- 错误: {result['errors']} 只\n\n"
                            f"**📅 数据新鲜度**\n"
                            f"- 主流日期: {before_common} → **{after_common}**{date_improved}\n"
                            f"- 仍过期: {after_stale} 只（修复了 {stale_fixed} 只）\n\n"
                            f"{stale_detail}\n"
                            f"---\n> 三才量化 · 每日收盘自动更新 | 东方财富API | {now_dt.strftime('%Y-%m-%d %H:%M')}"
                        )
                        await notifier.send_card(
                            f"📊 数据更新 · {status_emoji}",
                            body, status_color,
                            cooldown_key=f"{update_key}_done", cooldown_seconds=7200)

                        logger.info(f"Daily data update: {result['symbols_updated']} updated, "
                                   f"{result['bars_added']} bars, {result['errors']} errors")
                    except Exception as e:
                        logger.error(f"Daily data update failed: {e}", exc_info=True)
                        await notifier.send_card(
                            "🔴 数据更新失败",
                            f"**每日收盘数据更新异常**\n\n"
                            f"错误: {str(e)[:200]}\n\n"
                            f"> 请检查网络连接和腾讯API可用性",
                            "red", cooldown_key=f"{update_key}_error", cooldown_seconds=3600)

            # ═══════════════════════════════════════════════════════════
            # 9. 后台新闻采集（交易日全时段, 每30分钟静默入库, 不推送）
            # ═══════════════════════════════════════════════════════════
            _news_collect_key = f"__news_collect_{now_dt.strftime('%Y%m%d_%H')}__"
            if _is_trading_day(now_dt) and now_dt.minute % 30 < poll_minutes and \
                    _monitor_last_check.get(_news_collect_key) != now_dt.strftime("%Y%m%d_%H"):
                _monitor_last_check[_news_collect_key] = now_dt.strftime("%Y%m%d_%H")
                try:
                    from server.services.news_digest import silent_collect_news
                    task = asyncio.create_task(silent_collect_news(limit=10, include_firecrawl=True))
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
                except Exception as e:
                    logger.debug(f"News collect skipped: {e}")

            # ═══════════════════════════════════════════════════════════
            # 10. 高频票均线破位告警 (交易日 9:30-15:00, 每30分钟)
            # ═══════════════════════════════════════════════════════════
            if _is_trading_time() and now_dt.minute % 30 < poll_minutes:
                ma_key = f"__ma_break_alert_{now_dt.strftime('%Y%m%d_%H')}__"
                if _monitor_last_check.get(ma_key) != now_dt.strftime("%Y%m%d_%H"):
                    _monitor_last_check[ma_key] = now_dt.strftime("%Y%m%d_%H")
                    try:
                        import numpy as np
                        import pandas as pd
                        from server.scan_history_db import get_high_frequency_stocks
                        hot_stocks = get_high_frequency_stocks(days=7, min_count=3)
                        if hot_stocks:
                            break_alerts = []
                            for s in hot_stocks[:20]:
                                sym = s["symbol"]
                                fpath = DATA_DIR / "daily" / f"{sym}.parquet"
                                if not fpath.exists(): continue
                                df = pd.read_parquet(fpath)
                                if len(df) < 10: continue
                                closes = df["close"].values
                                price = float(closes[-1])
                                ma5 = float(np.mean(closes[-5:]))
                                ma10 = float(np.mean(closes[-10:]))
                                prev = float(closes[-2]) if len(closes) >= 2 else price
                                alerts = []
                                if price < ma5 and prev >= ma5:
                                    alerts.append("🔻首次跌破MA5")
                                if price < ma10 and prev >= ma10:
                                    alerts.append("🔻首次跌破MA10")
                                if alerts:
                                    break_alerts.append({
                                        "symbol": sym, "name": s.get("name", sym),
                                        "price": round(price, 2),
                                        "ma5": round(ma5, 2), "ma10": round(ma10, 2),
                                        "alerts": alerts, "scan_count": s["total_count"],
                                    })
                            if break_alerts:
                                lines = []
                                for a in break_alerts[:10]:
                                    lines.append(
                                        f"**{a['name']}({a['symbol']})** 现价{a['price']} "
                                        f"MA5:{a['ma5']} MA10:{a['ma10']}\n"
                                        f"- {' '.join(a['alerts'])} | 近7天扫描{a['scan_count']}次"
                                    )
                                body = "**🔥 高频票均线破位告警**\n\n" + "\n\n".join(lines)
                                body += "\n\n---\n> 近7天扫描≥3次的重点关注票 | 仅报告首次跌破 | 30分钟检测一次"
                                await notifier.send_card(
                                    f"🔥 均线破位·{len(break_alerts)}只",
                                    body, "red",
                                    cooldown_key=ma_key, cooldown_seconds=1800)
                                logger.info(f"MA break alert pushed: {len(break_alerts)} stocks")
                    except Exception as e:
                        logger.warning(f"MA break alert failed: {e}")

            # ═══════════════════════════════════════════════════════════
            # 11. 新闻事件回访调度 (每周期检查到期的T+30m/T+2h/T+1d)
            # ═══════════════════════════════════════════════════════════
            _review_key = f"__checkpoint_review_{now_dt.strftime('%Y%m%d_%H%M')[:4]}__"
            if _monitor_last_check.get(_review_key) != now_dt.strftime("%Y%m%d_%H%M")[:4]:
                _monitor_last_check[_review_key] = now_dt.strftime("%Y%m%d_%H%M")[:4]
                try:
                    from server.services.news_review import run_checkpoint_review
                    cr_result = await run_checkpoint_review(max_per_cycle=5)
                    if cr_result.get("reviewed", 0) > 0:
                        logger.info(f"Checkpoint review: {cr_result['reviewed']} checked, {cr_result.get('concluded',0)} concluded")
                except Exception as e:
                    logger.debug(f"Checkpoint review skipped: {e}")

            # ═══════════════════════════════════════════════════════════
            # 轮询等待
            # ═══════════════════════════════════════════════════════════
            if _is_auction_window():
                await asyncio.sleep(60)  # 竞价加速
            else:
                await asyncio.sleep(poll_minutes * 60)

        except Exception as e:
            logger.error(f"Monitor loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    logger.info("Monitor loop stopped")


def start_monitor():
    """启动后台监控任务."""
    global _monitor_task, _monitor_running
    if _monitor_task and not _monitor_task.done():
        logger.info("Monitor already running")
        return
    _monitor_running = True
    _monitor_task = asyncio.create_task(_monitor_loop())
    logger.info("Monitor task created")


async def stop_monitor():
    """停止后台监控任务."""
    global _monitor_running, _monitor_task
    _monitor_running = False
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None
    logger.info("Monitor stopped")
