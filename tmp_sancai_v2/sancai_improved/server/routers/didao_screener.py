"""策略选股筛选器 (Strategy Screener).

板块多空排列 + TOP-N 选股 + 热点概念 + 思维导图 + 估值 + 概念板块.
"""
import asyncio
import json as _json
import logging
import threading
import time
import uuid
import numpy as np
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query

from server.utils import DATA_DIR
from server.utils.cache import ttl_cache
from server.utils.response import ok as _ok, err as _err

logger = logging.getLogger(__name__)

router = APIRouter()

_cached = ttl_cache.get
_cache_set = ttl_cache.set


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# Strategy scan — 策略驱动的全市场买点扫描（后台任务模式，避免超时）
# ═══════════════════════════════════════════════════════════════════════════

# 后台扫描任务内存存储
_SCAN_TASKS: dict[str, dict] = {}
_SCAN_TASK_TTL = 600
_scan_lock = threading.Lock()


def _get_task_cancelled(task_id: str) -> bool:
    """线程安全地读取任务取消标志."""
    with _scan_lock:
        return _SCAN_TASKS.get(task_id, {}).get("cancelled", False)


def _scan_bg(task_id: str, mode: str, max_workers: int, tail_bars: int,
             timeout_single: int, timeout_schools: int,
             buy_type_filter: str, min_price: float, exclude_st: bool):
    """后台线程执行全市场扫描，结果写入 _SCAN_TASKS."""
    try:
        from server.services.strategy_scanner import scan_market, enrich_results_with_live_data
        result = scan_market(
            mode=mode, max_workers=max_workers, tail_bars=tail_bars,
            timeout_schools=timeout_single, timeout_ensemble=timeout_schools,
            buy_type_filter=buy_type_filter, min_price=min_price, exclude_st=exclude_st,
            cancel_check=lambda: _get_task_cancelled(task_id),
        )
        # Enrich with live turnover data from Tencent
        if result.get("results"):
            enrich_results_with_live_data(result["results"])
        with _scan_lock:
            if _SCAN_TASKS.get(task_id, {}).get("cancelled"):
                _SCAN_TASKS[task_id]["status"] = "cancelled"
                _SCAN_TASKS[task_id]["result"] = {"results": [], "scanned": 0, "matched": 0, "cancelled": True}
                return

            _SCAN_TASKS[task_id]["result"] = result
            _SCAN_TASKS[task_id]["status"] = "done" if not result.get("error") else "error"
            _SCAN_TASKS[task_id]["error"] = result.get("error", "")

        # 缓存结果（仅正常完成时）
        cache_key = f"strategy_scan_{mode}_{buy_type_filter}_{min_price}_{exclude_st}"
        _cache_set(cache_key, result)

        # 异步持久化
        try:
            from server.scan_history_db import save_scan_result
            save_scan_result(mode, result.get("scanned", 0), result.get("matched", 0),
                             result.get("elapsed_ms", 0), result.get("results", []))
        except Exception:
            pass
    except Exception as e:
        with _scan_lock:
            if task_id in _SCAN_TASKS:
                _SCAN_TASKS[task_id]["status"] = "error"
                _SCAN_TASKS[task_id]["error"] = str(e)


@router.get("/didao/screener/strategy-scan")
async def strategy_scan(
    mode: str = Query("strict"),
    min_confidence: float = Query(0, ge=0, le=1),
    buy_type: str = Query(""),
    min_price: float = Query(2.0, ge=0),
    exclude_st: bool = Query(True),
):
    """策略驱动的全市场买点扫描（后台任务模式）.

    立即返回 task_id，前端轮询 /strategy-scan/status?task_id=xxx 获取结果。
    扫描耗时约 2 分钟，后台执行不阻塞 HTTP 响应。
    """
    from server.utils.config import get_config

    # 清理过期后台任务
    now = time.time()
    with _scan_lock:
        for tid in list(_SCAN_TASKS.keys()):
            if now - _SCAN_TASKS[tid].get("created_at", 0) > _SCAN_TASK_TTL:
                del _SCAN_TASKS[tid]

    # 优先返回缓存
    cache_key = f"strategy_scan_{mode}_{buy_type}_{min_price}_{exclude_st}"
    cached = _cached(cache_key, ttl=300)
    if cached:
        r = dict(cached)
        if min_confidence > 0:
            r["results"] = [x for x in r.get("results", []) if x.get("confidence", 0) >= min_confidence]
            r["matched"] = len(r["results"])
        return _ok({**r, "task_id": "cached", "from_cache": True})

    cfg = get_config()
    scan_cfg = cfg.get("screener", {}).get("strategy_scan", {})
    max_workers = scan_cfg.get("max_workers", 4)
    tail_bars = scan_cfg.get("tail_bars", 300)
    timeout_single = scan_cfg.get("timeout_single", 60)
    timeout_schools = scan_cfg.get("timeout_schools", 120)

    task_id = str(uuid.uuid4())[:8]
    with _scan_lock:
        _SCAN_TASKS[task_id] = {
            "task_id": task_id, "status": "running", "mode": mode,
            "result": None, "error": "", "created_at": time.time(),
        }
    t = threading.Thread(target=_scan_bg, args=(
        task_id, mode, max_workers, tail_bars, timeout_single, timeout_schools,
        buy_type, min_price, exclude_st,
    ), daemon=True)
    t.start()
    logger.info(f"Scan task started: {task_id} mode={mode}")
    return _ok({"task_id": task_id, "status": "running", "mode": mode})


@router.get("/didao/screener/strategy-scan/status")
async def strategy_scan_status(task_id: str = Query(...)):
    """轮询扫描任务状态. 前端每 2 秒轮询直到 status='done'/'error'."""
    with _scan_lock:
        t = _SCAN_TASKS.get(task_id)
        if not t:
            return _ok({"status": "not_found", "task_id": task_id,
                         "result": None, "hint": "任务已过期或不存在，请重新扫描"})
        return _ok({
            "status": t["status"], "task_id": task_id, "mode": t.get("mode", ""),
            "result": t.get("result") if t["status"] != "running" else None,
            "error": t.get("error", ""),
        })


# ═══════════════════════════════════════════════════════════════
# 历史扫描记录查询
# ═══════════════════════════════════════════════════════════════

@router.get("/didao/screener/scan-history")
async def list_scan_history(days: int = Query(60, ge=1, le=365)):
    """列出最近N天有扫描记录的日期，包含每个日期下的策略模式和匹配数."""
    try:
        from server.scan_history_db import list_scan_dates
        history = await asyncio.to_thread(list_scan_dates, days)
        return _ok({"history": history, "total_days": len(history)})
    except Exception as e:
        logger.error(f"List scan history failed: {e}")
        return _err(str(e))


@router.get("/didao/screener/scan-history/{date}/{mode}")
async def load_scan_history(date: str, mode: str):
    """加载某天某模式的扫描结果明细."""
    try:
        from server.scan_history_db import get_scan_results
        results = await asyncio.to_thread(get_scan_results, date, mode)
        return _ok({
            "date": date, "mode": mode,
            "matched": len(results), "results": results,
        })
    except Exception as e:
        logger.error(f"Load scan history failed: {e}")
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# 深度尽调历史记录查询
# ═══════════════════════════════════════════════════════════════

_DEEP_DD_DIR = Path(__file__).parent.parent.parent / "data" / "deep_dd"


@router.get("/didao/deep-dd/history")
async def list_deep_dd_history():
    """列出所有深度尽调结果文件."""
    try:
        if not _DEEP_DD_DIR.exists():
            return _ok({"files": []})
        files = []
        # 扫描两种格式: deep_dd_*.json (批量) + dd_*.json (个股)
        all_dd_files = sorted(
            list(_DEEP_DD_DIR.glob("deep_dd_*.json")) + list(_DEEP_DD_DIR.glob("dd_*.json")),
            reverse=True,
        )
        for f in all_dd_files:
            stat = f.stat()
            name = f.name
            import re
            # 批量格式: deep_dd_{mode}_{N}stocks_{YYYYMMDD_HHMMSS}.json
            m = re.match(r"deep_dd_(.+?)_(\d+)stocks_(\d{8})_\d{6}\.json$", name)
            if m:
                files.append({
                    "filename": name,
                    "mode": m.group(1),
                    "stocks": int(m.group(2)),
                    "size_kb": round(stat.st_size / 1024, 1),
                    "date_str": m.group(3),
                })
                continue
            # 个股格式: dd_{symbol}_{YYYYMMDD_HHMMSS}.json
            m2 = re.match(r"dd_(\d{6})_(\d{8})_\d{6}\.json$", name)
            if m2:
                files.append({
                    "filename": name,
                    "mode": "individual",
                    "stocks": 1,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "date_str": m2.group(2),
                    "symbol": m2.group(1),
                })
        return _ok({"files": files, "total": len(files)})
    except Exception as e:
        return _err(str(e))


@router.get("/didao/deep-dd/load/{filename}")
async def load_deep_dd_file(filename: str):
    """加载指定的深度尽调结果文件."""
    import json as _json
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    fpath = (_DEEP_DD_DIR / filename).resolve()
    if not str(fpath).startswith(str(_DEEP_DD_DIR.resolve())):
        raise HTTPException(403, "Access denied")
    if not fpath.exists():
        return _err("文件不存在")
    try:
        data = _json.loads(fpath.read_text(encoding="utf-8"))
        return _ok(data)
    except Exception as e:
        return _err(str(e))


# ═══════════════════════════════════════════════════════════════
# 个股深度尽调查询 (扫描 data/deep_dd/*.json)
# ═══════════════════════════════════════════════════════════════

def _scan_all_dd_files(days: int = None) -> list[dict]:
    """扫描所有深度尽调JSON文件（批量deep_dd_*.json + 个股dd_*.json），返回 [{filename, date, mode, stocks: [{symbol,name}]}, ...].

    Args:
        days: 可选，仅统计最近 N 天内的尽调文件。None 表示全部。
    """
    if not _DEEP_DD_DIR.exists():
        return []

    # 计算截止日期
    cutoff = None
    if days:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    files = []
    all_dd_files = sorted(
        list(_DEEP_DD_DIR.glob("deep_dd_*.json")) + list(_DEEP_DD_DIR.glob("dd_*.json")),
        reverse=True,
    )
    for f in all_dd_files:
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
            name = f.name
            # 提取文件日期用于过滤
            file_date = ""
            if name.startswith("deep_dd_"):
                file_date = data.get("date", "")
            else:
                file_date = (data.get("generated_at", "") or "")[:10]

            # 按天数过滤
            if cutoff and file_date and file_date < cutoff:
                continue

            # 批量格式
            if name.startswith("deep_dd_"):
                per_stock = data.get("per_stock", [])
                files.append({
                    "filename": name,
                    "date": data.get("date", ""),
                    "mode": data.get("mode", ""),
                    "total": data.get("total", len(per_stock)),
                    "stocks": [{ "symbol": s.get("symbol",""), "name": s.get("name","") } for s in per_stock],
                })
            else:
                # 个股格式 dd_{symbol}_{timestamp}.json
                files.append({
                    "filename": name,
                    "date": data.get("generated_at", "")[:10],
                    "mode": "individual",
                    "total": 1,
                    "stocks": [{ "symbol": data.get("symbol",""), "name": data.get("name","") }],
                })
        except Exception:
            pass
    return files


@router.get("/didao/deep-dd/stock/{symbol}")
async def get_stock_deep_dd(
    symbol: str,
    days: int = Query(30, ge=1, le=90),
):
    """查询某只股票在最近N天深度尽调中的历史记录."""
    all_files = await asyncio.to_thread(_scan_all_dd_files, days)
    results = []
    for ff in all_files:
        for s in ff["stocks"]:
            if s["symbol"] == symbol:
                results.append({
                    "date": ff["date"],
                    "mode": ff["mode"],
                    "filename": ff["filename"],
                })
                break
    return _ok({
        "symbol": symbol,
        "dd_count": len(results),
        "days": days,
        "history": results,
    })


@router.get("/didao/deep-dd/counts")
async def deep_dd_counts(
    symbols: str = Query("", description="逗号分隔的股票代码"),
    days: int = Query(30, ge=1, le=90),
):
    """批量查询多只股票的尽调次数（默认最近30天）."""
    all_files = await asyncio.to_thread(_scan_all_dd_files, days)
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    count_map = {}
    for ff in all_files:
        for s in ff["stocks"]:
            sym = s["symbol"]
            if not sym_list or sym in sym_list:
                count_map[sym] = count_map.get(sym, 0) + 1
    return _ok({"counts": count_map, "days": days})


@router.get("/didao/screener/high-frequency-v2")
async def get_high_frequency_v2(
    days: int = Query(10, ge=3, le=30),
    min_count: int = Query(3, ge=2, le=10),
):
    """获取高频出现股票 + MA5/10/21/55破位日期检测."""
    try:
        from server.scan_history_db import get_high_frequency_stocks
        stocks = await asyncio.to_thread(get_high_frequency_stocks, days, min_count)
        import pandas as pd, numpy as np

        # Build per-stock MA break info
        ma_info = {}
        daily_dir = DATA_DIR / "daily"
        for s in stocks:
            sym = s["symbol"]
            fpath = daily_dir / f"{sym}.parquet"
            if not fpath.exists():
                continue
            try:
                df = pd.read_parquet(fpath)
                if len(df) < 60 or "close" not in df.columns:
                    continue
                closes = df["close"].values
                dates = df.index if isinstance(df.index, pd.DatetimeIndex) else (df["date"].values if "date" in df.columns else None)
                price = float(closes[-1])
                # Compute MAs
                ma5_arr = np.array([np.mean(closes[max(0,i-4):i+1]) for i in range(len(closes))])
                ma10_arr = np.array([np.mean(closes[max(0,i-9):i+1]) for i in range(len(closes))])
                ma21_arr = np.array([np.mean(closes[max(0,i-20):i+1]) for i in range(len(closes))])
                ma55_arr = np.array([np.mean(closes[max(0,i-54):i+1]) for i in range(len(closes))])
                # Find break dates: price crosses below MA within the MA's own period window
                # MA5 looks back 5 days, MA10 looks back 10 days, etc.
                break_dates = {}
                ma_specs = [("ma5", ma5_arr, 5), ("ma10", ma10_arr, 10), ("ma21", ma21_arr, 21), ("ma55", ma55_arr, 55)]
                for ma_name, ma_arr, lookback in ma_specs:
                    max_look = min(lookback, len(closes) - 1)
                    for i in range(1, max_look + 1):
                        idx = -i
                        prev_idx = -i - 1
                        if prev_idx < -len(closes): break
                        if closes[prev_idx] >= ma_arr[prev_idx] and closes[idx] < ma_arr[idx]:
                            if dates is not None:
                                dt = dates[idx]
                                try:
                                    break_dates[ma_name] = str(pd.Timestamp(dt).date())
                                except: pass
                            else:
                                break_dates[ma_name] = f"T-{i}"
                            break
                # Current state: below or above each MA
                below = {}
                for ma_name, ma_val in [("ma5", float(np.mean(closes[-5:]))), ("ma10", float(np.mean(closes[-10:]))),
                                         ("ma21", float(np.mean(closes[-21:]))), ("ma55", float(np.mean(closes[-55:])))]:
                    below[ma_name] = price < ma_val if ma_val > 0 else False
                # pct_change: (today_close - yesterday_close) / yesterday_close * 100
                pct_change = 0.0
                if len(closes) >= 2 and closes[-2] > 0:
                    pct_change = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
                ma_info[sym] = {
                    "price": round(price, 2),
                    "pct_change": pct_change,
                    "ma5": round(float(np.mean(closes[-5:])), 2) if len(closes) >= 5 else 0,
                    "ma10": round(float(np.mean(closes[-10:])), 2) if len(closes) >= 10 else 0,
                    "ma21": round(float(np.mean(closes[-21:])), 2) if len(closes) >= 21 else 0,
                    "ma55": round(float(np.mean(closes[-55:])), 2) if len(closes) >= 55 else 0,
                    "break_dates": break_dates,  # {ma5: "2026-06-20", ma10: null, ...}
                    "below": below,  # {ma5: true, ma10: false, ...}
                }
            except: pass

        return _ok({
            "high_frequency": stocks,
            "ma_detail": ma_info,
            "days": days, "min_count": min_count,
        })
    except Exception as e:
        logger.error(f"High freq v2 failed: {e}")
        return _err(str(e))


@router.get("/didao/screener/scan-frequency")
async def get_scan_frequency(
    symbols: str = Query(..., description="逗号分隔的股票代码"),
    days: int = Query(10, ge=3, le=30),
):
    """批量查询股票在最近N天扫描中的出现频次和最近日期."""
    try:
        from server.scan_history_db import get_symbols_frequency
        syms = [s.strip() for s in symbols.split(",") if s.strip()]
        if not syms:
            return _err("请提供至少一个股票代码")
        if len(syms) > 500:
            syms = syms[:500]
        freq = await asyncio.to_thread(get_symbols_frequency, syms, days)
        return _ok({"frequencies": freq, "days": days, "queried": len(syms)})
    except Exception as e:
        logger.error(f"Scan frequency failed: {e}")
        return _err(str(e))


@router.get("/didao/valuation/{symbol}")
async def get_stock_valuation_didao(symbol: str):
    """个股估值分析 — PE/PB/市值 (东方财富 + 腾讯财经双源)."""
    try:
        from data_sources.tencent_quotes import tencent_quote

        result = {}
        # 腾讯财经实时估值
        try:
            q = tencent_quote([symbol])
            if symbol in q:
                result['tencent'] = q[symbol]
        except Exception:
            result['tencent'] = None

        # 东方财富全市场估值
        try:
            from data_sources.em_valuation import get_stock_valuation
            em = get_stock_valuation(symbol)
            if em:
                result['em'] = {
                    'pe_ttm': em.get('pe_ttm'), 'pb': em.get('pb'),
                    'total_mcap_yi': em.get('total_mcap_yi'),
                    'float_mcap_yi': em.get('float_mcap_yi'),
                    'roe': em.get('roe'),
                }
        except Exception:
            result['em'] = None

        return _ok({'symbol': symbol, 'valuation': result})
    except Exception as e:
        return _err(f'估值获取失败: {e}')


# ═══════════════════════════════════════════════════════════════════════════
# 集合竞价解读看板
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/didao/screener/auction-board")
async def get_auction_board():
    """集合竞价解读看板：昨日扫描策略 × 今日竞价数据 = 解读。

    对比昨日各策略扫描出的标的在今天竞价中的表现（高开/低开/量能），
    给出每条策略信号的竞价确认/否认解读。
    """
    from datetime import datetime, timedelta
    from server.auction import fetch_auction_data, _batch_fetch_em
    from server.scan_history_db import get_db as _gdb, get_scan_results

    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. 获取昨日所有扫描模式
    db = _gdb()
    try:
        modes = db.execute(
            "SELECT DISTINCT mode FROM scan_summary WHERE date=? ORDER BY mode",
            (yesterday,)
        ).fetchall()
    except Exception:
        modes = []

    MODE_LABELS = {
        "strict": "三才BP1(超跌)", "strict_reverse": "追涨突破",
        "simple": "KDJ超卖", "schools": "多学派共识",
        "chan_theory": "缠论", "ict": "ICT", "price_action": "价格行为",
        "wyckoff": "威科夫", "morphology": "形态学",
        "gann": "江恩", "wave_theory": "波浪", "dow_theory": "道氏",
    }

    boards = []
    all_symbols = set()

    for (mode,) in modes:
        results = get_scan_results(yesterday, mode)
        if not results:
            continue
        for r in results:
            all_symbols.add(r["symbol"])
        boards.append({
            "mode": mode,
            "label": MODE_LABELS.get(mode, mode),
            "date": yesterday,
            "count": len(results),
            "stocks": results[:8],  # 每条策略展示 Top 8
        })

    # 2. 拉取今日竞价数据（只拉取这些标的）
    stocks_map = {}
    if all_symbols:
        fetched = _batch_fetch_em(list(all_symbols)[:50])
        stocks_map = {s["symbol"]: s for s in fetched}

    # 3. 逐策略解读: 昨日信号 vs 今日竞价
    for board in boards:
        interpretations = []
        confirmed = 0
        denied = 0
        for s in board["stocks"]:
            sym = s["symbol"]
            auc = stocks_map.get(sym)
            if not auc:
                continue
            gap = auc["gap_pct"]
            vol_r = auc["vol_ratio"]
            scan_confidence = s.get("confidence", 0)

            # 解读逻辑
            if board["mode"] == "strict_reverse":  # 追涨突破: 期望高开确认
                if gap > 1 and vol_r > 1.2:
                    verdict = "confirmed"
                    note = f"高开{gap:+.1f}% 放量{vol_r:.1f}x — 竞价确认追涨, 可关注"
                elif gap > 0:
                    verdict = "neutral"
                    note = f"小幅高开{gap:+.1f}% — 竞价中性, 待盘中确认"
                else:
                    verdict = "denied"
                    note = f"低开{gap:+.1f}% — 竞价否认追涨信号, 放弃"
            elif board["mode"] in ("strict", "simple"):  # BP1/KDJ超卖(抄底): 低开后反弹才好
                if -3 < gap < 0:
                    verdict = "confirmed"
                    note = f"小幅低开{gap:+.1f}% — 超跌标的小幅低开, 可抄底"
                elif gap > 1:
                    verdict = "denied"
                    note = f"高开{gap:+.1f}% — 超跌票高开不抄底, 等回落"
                else:
                    verdict = "neutral"
                    note = f"开{gap:+.1f}% — 竞价中性, 观察盘中"
            else:  # 各流派通用
                if gap > 2 and vol_r > 1.5:
                    verdict = "confirmed"
                    note = f"强势高开{gap:+.1f}% 放量{vol_r:.1f}x — 竞价确认"
                elif gap < -2:
                    verdict = "denied"
                    note = f"大幅低开{gap:+.1f}% — 竞价否认, 放弃"
                else:
                    verdict = "neutral"
                    note = f"正常竞价{gap:+.1f}% — 关注盘中走势"

            if verdict == "confirmed": confirmed += 1
            elif verdict == "denied": denied += 1

            interpretations.append({
                "symbol": sym, "name": auc.get("name", s.get("name", "")),
                "gap_pct": gap, "vol_ratio": vol_r,
                "price": auc["price"], "prev_close": auc["prev_close"],
                "change_pct": auc["change_pct"],
                "volume": auc["volume"],
                "verdict": verdict, "note": note,
                "scan_confidence": scan_confidence,
            })

        board["interpretations"] = interpretations
        board["confirmed"] = confirmed
        board["denied"] = denied
        board["data_quality"] = f"{len(interpretations)}/{len(board['stocks'])} 只有竞价数据"

    # 4. 自动保存竞价数据到 live_engine（供模型训练使用）
    try:
        from server.live_engine import get_db as _ldb
        ldb = _ldb()
        for board in boards:
            mode = board["mode"]
            for x in board.get("interpretations", []):
                ldb.execute(
                    "INSERT OR REPLACE INTO auction_confirm(account_id,date,symbol,auction_price,gap_pct,vol_ratio,verdict,note) VALUES(?,?,?,?,?,?,?,?)",
                    (mode, today_str, x["symbol"], x["price"], round(x["gap_pct"], 2),
                     round(x["vol_ratio"], 2), x["verdict"], x.get("note", "")))
        ldb.commit()
    except Exception:
        pass

    return _ok({
        "date": today_str,
        "scan_date": yesterday,
        "is_auction_window": datetime.now().hour < 9 or (datetime.now().hour == 9 and datetime.now().minute < 30),
        "boards": boards,
        "data_source": "eastmoney_push2",
    })
