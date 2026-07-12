"""Data management API endpoints."""
import asyncio
import csv
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.utils import PROJECT_ROOT, DATA_DIR, load_universe
from server.concept_index import (
    list_all_concepts, get_stocks_by_concept, get_concepts_for_stock, init_tables
)

router = APIRouter()

# Ensure concept index tables exist on first use
try:
    init_tables()
except Exception:
    pass

logger = logging.getLogger(__name__)

# AGSJJ stock list path for name lookups
_AGSJJ_STOCK_LIST = Path("D:/agsjj/竞价不复权/股票列表.csv")
_STOCK_NAME_MAP: dict[str, str] | None = None


def _load_stock_name_map() -> dict[str, str]:
    """Load AGSJJ stock list as {symbol: name} mapping. Cached in memory."""
    global _STOCK_NAME_MAP
    if _STOCK_NAME_MAP is not None:
        return _STOCK_NAME_MAP
    _STOCK_NAME_MAP = {}
    if _AGSJJ_STOCK_LIST.exists():
        try:
            with open(_AGSJJ_STOCK_LIST, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get("symbol", "").strip()
                    name = row.get("name", "").strip()
                    if sym and name:
                        _STOCK_NAME_MAP[sym] = name
        except Exception:
            logger.warning("Failed to load AGSJJ stock list", exc_info=True)
    return _STOCK_NAME_MAP


# 指数名称映射（parquet 文件无名称字段）
_INDEX_NAME_MAP = {
    "sh000001": "上证指数", "sh000016": "上证50", "sh000300": "沪深300",
    "sh000688": "科创50", "sh000852": "中证1000", "sh000905": "中证500",
    "sz399001": "深证成指", "sz399006": "创业板指", "sz399300": "沪深300(深)",
    "sh000010": "上证180",
}


@router.get("/stocks")
async def list_stocks(concept: str = Query(None, description="按概念/行业筛选举例: 人工智能, 新能源")):
    """List available stocks and their data status from all data sources.

    If 'concept' is provided, only stocks belonging to that concept are returned.
    """
    universe = load_universe()
    name_map = _load_stock_name_map()

    # Concept filter
    concept_symbols = None
    if concept:
        try:
            concept_stocks = get_stocks_by_concept(concept)
            concept_symbols = {c["symbol"] for c in concept_stocks}
        except Exception:
            concept_symbols = set()

    # Single-pass: build {symbol: {name, sizes: {period: bytes}, mtimes: {period: ts}}}
    info = {}  # symbol -> dict
    def _ensure(sym):
        if sym not in info:
            name = name_map.get(sym) or _INDEX_NAME_MAP.get(sym, "")
            info[sym] = {"name": name, "sizes": {}, "mtimes": {}}

    # Universe stocks (name priority)
    for s in universe:
        sym = s["symbol"]
        if concept_symbols is not None and sym not in concept_symbols:
            continue
        _ensure(sym)
        info[sym]["name"] = s["name"] or name_map.get(sym) or _INDEX_NAME_MAP.get(sym, "")

    # Enumerate all data dirs
    period_dirs = {}  # period_key -> Path
    daily_d = DATA_DIR / "daily"
    if daily_d.exists():
        period_dirs["daily"] = daily_d
    minute_d = DATA_DIR / "minute"
    if minute_d.exists():
        for pd in sorted(minute_d.iterdir()):
            if pd.is_dir():
                period_dirs[pd.name] = pd
    for p in ["weekly", "monthly"]:
        pd = DATA_DIR / p
        if pd.exists():
            period_dirs[p] = pd

    PERIOD_KEYS = ["1min", "5min", "15min", "30min", "60min", "daily", "weekly", "monthly"]

    # One scan per period dir — only stat each file once
    for pk, dir_path in period_dirs.items():
        for parq in dir_path.glob("*.parquet"):
            sym = parq.stem
            if concept_symbols is not None and sym not in concept_symbols:
                continue
            _ensure(sym)
            info[sym]["sizes"][pk] = parq.stat().st_size
            info[sym]["mtimes"][pk] = parq.stat().st_mtime

    # Build sorted output list
    stocks = []
    for symbol in sorted(info):
        d = info[symbol]
        entry = {"symbol": symbol, "name": d["name"]}
        # 拼音首字母搜索（如 szzs=上证指数, payh=平安银行）
        try:
            from pypinyin import lazy_pinyin, Style
            entry["pinyin"] = "".join(lazy_pinyin(d["name"], style=Style.FIRST_LETTER))
        except Exception:
            entry["pinyin"] = ""
        total_size = 0
        latest_ts = 0
        for pk in PERIOD_KEYS:
            sz = d["sizes"].get(pk, 0)
            ts = d["mtimes"].get(pk, 0)
            entry[f"has_{pk}"] = sz > 0
            entry[f"{pk}_bars"] = sz
            total_size += sz
            if ts > latest_ts:
                latest_ts = ts
        entry["total_size"] = total_size
        entry["last_updated"] = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d") if latest_ts > 0 else ""
        stocks.append(entry)

    return {"stocks": stocks, "count": len(stocks), "period_keys": PERIOD_KEYS}


@router.get("/stocks/{symbol}/kline")
async def get_kline(
    symbol: str,
    period: str = Query("daily", description="daily, 30min, 60min, etc."),
    start: str = None,
    end: str = None,
    limit: int = Query(500, description="Max bars to return"),
):
    """Get K-line data with computed indicators for a symbol. Uses DuckDB."""
    try:
        from server.db import get_kline as db_get_kline
        records = db_get_kline(symbol, period, start, end, limit)
    except Exception:
        logger.info("DuckDB query failed for %s, falling back to direct parquet read", symbol)
        # Fallback: direct parquet read
        import re as _re
        if not _re.fullmatch(r'\d{6}', symbol):
            raise HTTPException(status_code=400, detail="Invalid symbol")
        if period == "daily":
            fpath = DATA_DIR / "daily" / f"{symbol}.parquet"
        else:
            fpath = DATA_DIR / "minute" / period / f"{symbol}.parquet"
        if not fpath.exists():
            raise HTTPException(status_code=404, detail=f"No data for {symbol} ({period})")
        df = pd.read_parquet(fpath)
        # Handle both DatetimeIndex and column-based formats
        if not isinstance(df.index, pd.DatetimeIndex):
            date_col = "date" if period == "daily" else "datetime"
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col])
                df = df.set_index(date_col)
            else:
                raise HTTPException(status_code=500, detail=f"Unrecognized format for {symbol}")
        df = df.sort_index()
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        if limit and len(df) > limit:
            df = df.tail(limit)
        import math
        records = []
        for ts, row in df.iterrows():
            r = {}
            for k, v in row.to_dict().items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    r[k] = None
                else:
                    r[k] = v
            r["date"] = str(ts)[:19]
            records.append(r)

    # Compute additional indicators via Rust core if available
    try:
        import sancai_core
        ohlcv_data = {
            "open": [r.get("open") for r in records],
            "high": [r.get("high") for r in records],
            "low": [r.get("low") for r in records],
            "close": [r.get("close") for r in records],
            "volume": [r.get("volume") for r in records],
        }
        result_str = sancai_core.analyze_trends(json.dumps(ohlcv_data), period)
        indicators = json.loads(result_str)
        for i, rec in enumerate(records):
            for p_str, ma_vals in indicators.get("mas", {}).items():
                if i < len(ma_vals) and ma_vals[i] is not None:
                    rec[f"ma_{p_str}"] = round(ma_vals[i], 4)
            if i < len(indicators.get("kdj", {}).get("k", [])):
                k = indicators["kdj"]["k"][i]
                rec["kdj_k"] = round(k, 2) if k is not None else None
                d = indicators["kdj"]["d"][i]
                rec["kdj_d"] = round(d, 2) if d is not None else None
                j = indicators["kdj"]["j"][i]
                rec["kdj_j"] = round(j, 2) if j is not None else None
    except Exception:
        logger.warning("Rust core indicator computation failed for %s", symbol)
        pass  # Rust core not available — DuckDB MAs already computed

    return {
        "symbol": symbol,
        "period": period,
        "count": len(records),
        "data": records,
    }



def _symbol_prefix(symbol: str) -> str:
    """Convert symbol to akshare tick format: 000001→sz000001, 600519→sh600519."""
    if symbol.startswith(('6', '9')):
        return 'sh' + symbol
    return 'sz' + symbol


@router.get("/stocks/{symbol}/depth")
async def get_depth(symbol: str):
    """Get 5-level bid/ask depth for a symbol using akshare."""
    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(ak.stock_bid_ask_em, symbol=symbol),
            timeout=10,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="获取盘口超时")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取盘口失败: {e}")

    # Parse item/value structure
    raw = {}
    for _, row in df.iterrows():
        raw[row['item']] = row['value']

    # Extract 5-level depth
    def level(n, side):
        price = raw.get(f'{side}_{n}')
        vol = raw.get(f'{side}_{n}_vol')
        if price is None or vol is None:
            return None
        return {'price': float(price), 'volume': int(float(vol))}

    sells = [level(i, 'sell') for i in range(5, 0, -1)]
    buys = [level(i, 'buy') for i in range(1, 6)]
    sells = [s for s in sells if s]
    buys = [b for b in buys if b]

    spread = None
    spread_pct = None
    if sells and buys:
        spread = float(sells[-1]['price'] - buys[0]['price'])
        if buys[0]['price'] > 0:
            spread_pct = round(spread / buys[0]['price'] * 100, 3)

    return {
        'symbol': symbol,
        'sells': sells,  # 5→1, nearest last
        'buys': buys,    # 1→5, nearest first
        'spread': spread,
        'spread_pct': spread_pct,
        'prev_close': raw.get('昨收'),
        'limit_up': raw.get('涨停'),
        'limit_down': raw.get('跌停'),
        'total_volume': raw.get('总量'),
        'total_amount': raw.get('金额'),
    }


@router.get("/stocks/{symbol}/ticks")
async def get_ticks(symbol: str, limit: int = None):
    """Get tick-by-tick transactions with active buy/sell classification."""
    full_symbol = _symbol_prefix(symbol)

    try:
        df = await asyncio.wait_for(
            asyncio.to_thread(ak.stock_zh_a_tick_tx_js, symbol=full_symbol),
            timeout=10,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="获取逐笔成交超时")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取逐笔成交失败: {e}")

    if df.empty:
        return {
            'symbol': symbol,
            'summary': None,
            'recent_ticks': [],
            'big_trades': [],
            'note': '暂无逐笔成交数据',
        }

    # Column mapping (Chinese to English)
    col_map = {c: c for c in df.columns}
    time_col = df.columns[0]
    price_col = df.columns[1]
    change_col = df.columns[2]
    vol_col = df.columns[3]
    amount_col = df.columns[4]
    nature_col = df.columns[5]

    n = limit or min(100, len(df))
    recent = df.tail(n)

    # Classification
    buy_mask = recent[nature_col] == '买盘'
    sell_mask = recent[nature_col] == '卖盘'

    buy_cnt = int(buy_mask.sum())
    sell_cnt = int(sell_mask.sum())
    buy_vol = float(recent.loc[buy_mask, vol_col].sum()) if buy_cnt > 0 else 0.0
    sell_vol = float(recent.loc[sell_mask, vol_col].sum()) if sell_cnt > 0 else 0.0
    buy_amt = float(recent.loc[buy_mask, amount_col].sum()) if buy_cnt > 0 else 0.0
    sell_amt = float(recent.loc[sell_mask, amount_col].sum()) if sell_cnt > 0 else 0.0

    total_amt = buy_amt + sell_amt
    buy_pct = round(buy_amt / total_amt * 100, 1) if total_amt > 0 else 0

    # Big trades (>= 100 lots = 10000 shares)
    big = df[df[vol_col] >= 100].tail(30)
    big_trades = []
    for _, row in big.iterrows():
        big_trades.append({
            'time': str(row[time_col]),
            'price': float(row[price_col]),
            'volume': int(row[vol_col]),
            'amount': float(row[amount_col]),
            'type': str(row[nature_col]),
        })

    # Recent ticks
    recent_ticks = []
    for _, row in recent.iterrows():
        recent_ticks.append({
            'time': str(row[time_col]),
            'price': float(row[price_col]),
            'change': float(row[change_col]) if row[change_col] else 0,
            'volume': int(row[vol_col]),
            'amount': float(row[amount_col]),
            'type': str(row[nature_col]),
        })

    return {
        'symbol': symbol,
        'summary': {
            'buy_count': buy_cnt,
            'buy_volume': int(buy_vol),
            'buy_amount': round(buy_amt, 2),
            'sell_count': sell_cnt,
            'sell_volume': int(sell_vol),
            'sell_amount': round(sell_amt, 2),
            'net_flow': round(buy_amt - sell_amt, 2),
            'buy_pct': buy_pct,
            'total_count': len(recent),
        },
        'recent_ticks': recent_ticks,
        'big_trades': big_trades,
    }




# ---- Data download endpoint (background tail-update) ----

_download_status: dict = {
    "running": False, "period": "", "progress": 0, "total": 0,
    "updated": 0, "bars_added": 0, "errors": 0, "last_time": None, "last_result": None,
}
_download_lock = threading.Lock()


class DownloadRequest(BaseModel):
    period: str = Field("daily", description="数据周期: daily, 60min, 30min, 15min, 5min, 1min")
    max_symbols: int = Field(0, description="单次最多更新支数，0=全部")


@router.post("/download")
async def trigger_download(req: DownloadRequest):
    """触发后台数据下载（日线/分钟线尾缺口填补）"""
    global _download_status
    with _download_lock:
        if _download_status["running"]:
            return {"status": "busy", "message": "已有下载任务在运行中", "progress": _download_status}
        _download_status = {"running": True, "period": req.period, "progress": 0,
                           "total": 0, "updated": 0, "bars_added": 0, "errors": 0,
                           "last_time": None, "last_result": None}

    asyncio.create_task(_run_download(req.period, req.max_symbols))
    return {"status": "started", "period": req.period, "task_id": "download",
            "message": f"开始下载{req.period}数据"}


async def _run_download(period: str, max_symbols: int):
    global _download_status
    try:
        from datetime import datetime
        # Run in subprocess to avoid DuckDB lock conflicts with the running server
        import subprocess, sys
        script_dir = Path(__file__).parent.parent.parent / "scripts"
        cli_path = script_dir / "_run_data_update.py"

        cmd = [sys.executable, str(cli_path), period, str(max_symbols)]
        logger.info("Download subprocess start: %s", cmd)

        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=7200,
                cwd=str(script_dir.parent)
            )
        except subprocess.TimeoutExpired:
            logger.error("Download subprocess timed out after 2h")
            with _download_lock:
                _download_status.update({
                    "running": False, "last_result": "timeout",
                    "last_time": datetime.now().isoformat(),
                })
            return

        # Parse the single JSON line on stdout
        result = {}
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

        if proc.returncode != 0 or "error" in result:
            err_msg = result.get("error", proc.stderr.strip()[:500])
            logger.error("Download subprocess failed (rc=%d): %s", proc.returncode, err_msg)
            with _download_lock:
                _download_status.update({
                    "running": False, "errors": _download_status.get("errors", 0) + 1,
                    "last_result": err_msg, "last_time": datetime.now().isoformat(),
                })
            return

        updated = result.get("symbols_updated", 0)
        bars = result.get("bars_added", 0)
        errors = result.get("errors", 0)
        checked = result.get("symbols_checked", 0)
        logger.info("Download completed: updated=%d bars=%d errors=%d checked=%d",
                    updated, bars, errors, checked)

        # Refresh DuckDB views in the main process
        try:
            from server.db import refresh_views
            refresh_views()
        except Exception:
            pass

        with _download_lock:
            _download_status.update({
                "running": False, "updated": updated, "bars_added": bars,
                "errors": errors, "total": checked,
                "last_time": datetime.now().isoformat(), "last_result": "ok",
            })
    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
        with _download_lock:
            _download_status.update({"running": False, "last_result": str(e),
                                     "errors": _download_status.get("errors", 0) + 1})


@router.get("/download/status")
async def download_status():
    """获取数据下载任务进度"""
    return _download_status



# ---- Archive file import endpoint ----
import shutil

from fastapi import File, UploadFile

# Import status tracking
_import_status: dict = {"running": False, "last_result": None, "last_time": None,
                        "progress": 0, "total": 0, "current": 0,
                        "cancel_requested": False,
                        "current_file": "",
                        "processed_files": []}
_import_lock = threading.Lock()


@router.post("/import/zip")
async def import_zip(file: UploadFile = File(...), symbols: str = "", timeout_minutes: int = 30):
    """Upload a ZIP/RAR/7Z file containing CSV/Parquet data, auto-classify and merge into store.

    Processing runs in background with automatic timeout protection.
    Check status via GET /api/data/import/status.

    Parameters:
    - timeout_minutes: Max total import time (default 30 min). Import is force-cancelled after timeout.
    """
    global _import_status
    from scripts.sync_netdisk import process_file, get_meta_conn, ensure_meta_source_column

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required")

    suf = file.filename.lower()
    if not (suf.endswith(".zip") or suf.endswith(".rar") or suf.endswith(".7z")):
        raise HTTPException(status_code=400, detail="Only .zip, .rar, and .7z files are accepted")

    timeout_minutes = max(1, min(timeout_minutes, 120))  # clamp 1-120 min

    STAGING_DIR = PROJECT_ROOT / "data" / "staging"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = STAGING_DIR / f"archive_upload_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    symbol_filter = None
    if symbols.strip():
        symbol_filter = {s.strip() for s in symbols.split(",")}

    # Save uploaded file
    zip_path = run_dir / file.filename
    content = await file.read()
    with open(zip_path, "wb") as f:
        f.write(content)

    with _import_lock:
        if _import_status["running"]:
            return {"status": "busy", "message": "An import is already in progress"}
        _import_status["running"] = True
        _import_status["last_result"] = None
        _import_status["progress"] = 0
        _import_status["total"] = 0
        _import_status["current"] = 0
        _import_status["cancel_requested"] = False
        _import_status["current_file"] = ""
        _import_status["processed_files"] = []
        _import_status["started_at"] = datetime.now().isoformat()
        _import_status["timeout_minutes"] = timeout_minutes

    _import_stop_event = threading.Event()

    def _run():
        global _import_status
        start_time = datetime.now()
        timeout_seconds = timeout_minutes * 60
        cancel_cb = lambda: (_import_status.get("cancel_requested", False) or
                             _import_stop_event.is_set() or
                             (datetime.now() - start_time).total_seconds() > timeout_seconds)
        file_cb = lambda name: _import_status.update({"current_file": name})
        try:
            conn = get_meta_conn()
            ensure_meta_source_column(conn)
            try:
                results = process_file(zip_path, conn, "zip_upload", symbol_filter,
                                       progress_cb=lambda cur, tot: _import_status.update(
                                           {"progress": round(cur / max(tot, 1) * 100, 1),
                                            "current": cur, "total": tot}),
                                       cancel_cb=cancel_cb,
                                       file_cb=file_cb)
                # Push results to processed_files for UI display
                for r in results:
                    _import_status["processed_files"].append({
                        "name": r.get("file", r.get("symbol", "")),
                        "status": r.get("status", "?"),
                        "symbol": r.get("symbol", ""),
                        "period": r.get("period", ""),
                    })
            finally:
                conn.close()

            elapsed = (datetime.now() - start_time).total_seconds()
            imported = sum(1 for r in results if r.get("status") == "ok")
            skipped = sum(1 for r in results if r.get("status") == "skip")
            errs = sum(1 for r in results if r.get("status") not in ("ok", "skip", "cancelled"))
            timed_out = elapsed > timeout_seconds

            if _import_status.get("cancel_requested"):
                _import_status["last_result"] = {
                    "status": "cancelled",
                    "message": f"Import cancelled by user. Imported {imported}, skipped {skipped}, errors {errs}",
                    "filename": file.filename,
                    "size_kb": len(content) // 1024,
                    "imported": imported,
                    "skipped": skipped,
                    "errors": errs,
                    "elapsed_seconds": round(elapsed),
                    "details": [{k: v for k, v in r.items() if k != "status"}
                               for r in results if r.get("status") not in ("ok",)],
                }
            elif timed_out:
                _import_status["last_result"] = {
                    "status": "timeout",
                    "message": f"Import timed out after {timeout_minutes} min. Imported {imported}, skipped {skipped}, errors {errs}",
                    "filename": file.filename,
                    "size_kb": len(content) // 1024,
                    "imported": imported,
                    "skipped": skipped,
                    "errors": errs,
                    "elapsed_seconds": round(elapsed),
                    "details": [{k: v for k, v in r.items() if k != "status"}
                               for r in results if r.get("status") not in ("ok",)],
                }
            else:
                _import_status["last_result"] = {
                    "status": "ok",
                    "filename": file.filename,
                    "size_kb": len(content) // 1024,
                    "imported": imported,
                    "skipped": skipped,
                    "errors": errs,
                    "elapsed_seconds": round(elapsed),
                    "details": [{k: v for k, v in r.items() if k != "status"}
                               for r in results if r.get("status") != "ok"],
                }
            _import_status["last_time"] = datetime.now().isoformat()
        except Exception as e:
            _import_status["last_result"] = {"status": "error", "message": str(e)}
            _import_status["last_time"] = datetime.now().isoformat()
            logger.exception("Import thread failed with exception")
        finally:
            _import_status["running"] = False
            _import_status["progress"] = 100
            shutil.rmtree(run_dir, ignore_errors=True)

    thread = threading.Thread(target=_run, daemon=True, name=f"import-{ts}")
    thread.start()
    logger.info(f"Import started: {file.filename} ({len(content)//1024} KB), timeout={timeout_minutes}min, thread={thread.name}")
    return {"status": "started", "message": f"Import of {file.filename} running in background ({len(content)//1024} KB)",
            "timeout_minutes": timeout_minutes,
            "check_url": "/api/data/import/status",
            "cancel_url": "/api/data/import/cancel"}


@router.get("/import/status")
async def import_status():
    """Get current import job status."""
    import copy
    status = copy.copy(_import_status)
    # Add computed fields
    if status.get("started_at"):
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(status["started_at"])).total_seconds()
            status["elapsed_seconds"] = round(elapsed)
            if status.get("timeout_minutes"):
                status["timeout_remaining_seconds"] = max(0, status["timeout_minutes"] * 60 - round(elapsed))
        except Exception:
            pass
    return status





# ═══════════════════════════════════════════════════════════════════════════
# Real-time quote endpoints — 多数据源 (mootdx/腾讯/akshare)
# ═══════════════════════════════════════════════════════════════════════════

from server.utils.response import ok as _ok, err as _err


def _normalize_tencent_key(symbol: str) -> str:
    """腾讯行情返回的 key 不带 sh/sz/bj 前缀, 统一剥掉以便查找."""
    if len(symbol) == 8 and symbol.startswith(("sh", "sz", "bj")):
        return symbol[2:]
    return symbol


def _lookup_tencent_result(result: dict, symbol: str) -> dict | None:
    """在 tencent_quote 返回结果中查找 symbol, 兼容前缀有无两种情况."""
    # 优先精确匹配
    if symbol in result:
        return result[symbol]
    # 尝试去前缀匹配 (sh000001 → 000001)
    clean = _normalize_tencent_key(symbol)
    if clean in result:
        return result[clean]
    return None


def get_realtime_quote(symbol: str) -> dict:
    """获取单只股票实时行情 (腾讯财经)."""
    from data_sources.tencent_quotes import tencent_quote
    return tencent_quote([symbol])


def get_batch_quotes(codes: list[str], with_valuation: bool = True) -> dict:
    """批量获取实时行情 (腾讯财经)."""
    from data_sources.tencent_quotes import tencent_quote
    return tencent_quote(codes)


@router.get("/quote/{symbol}")
async def get_quote(symbol: str):
    """实时行情 — mootdx 优先，腾讯财经兜底.

    包含价格/PE/PB/市值/换手率/涨跌停价.
    """
    try:
        from data_sources.tencent_quotes import tencent_quote
        result = await asyncio.to_thread(get_realtime_quote, symbol)
        q = _lookup_tencent_result(result, symbol)
        if q:
            return _ok(q, source="tencent")
        return _err(f"无法获取 {symbol} 行情", source="tencent")
    except Exception as e:
        return _err(f"行情获取失败: {e}", source="tencent")


@router.get("/quotes")
async def get_quotes_batch(
    symbols: str = Query(..., description="逗号分隔的股票代码，如 000001,600519,000858"),
):
    """批量实时行情 — 腾讯财经 API (含估值).

    返回 PE/PB/市值/换手率/涨跌幅等.
    """
    try:
        codes = [s.strip() for s in symbols.split(",") if s.strip()]
        if not codes:
            return _err("请提供至少一个股票代码", source="tencent")
        if len(codes) > 50:
            return _err("最多支持 50 只股票同时查询", source="tencent")

        from data_sources.tencent_quotes import tencent_quote
        result = await asyncio.to_thread(get_batch_quotes, codes, with_valuation=True)
        return _ok({
            "quotes": result,
            "count": len(result),
            "symbols": codes,
        }, source="tencent")
    except Exception as e:
        return _err(f"批量行情获取失败: {e}", source="tencent")


@router.get("/valuation/{symbol}")
async def get_stock_valuation(symbol: str):
    """个股估值快照 — PE(TTM)/PE(静)/PB/市值/涨跌停价 (腾讯财经)."""
    try:
        from data_sources.tencent_quotes import tencent_quote
        result = await asyncio.to_thread(tencent_quote, [symbol])
        q = _lookup_tencent_result(result, symbol)
        if q:
            return _ok({
                "symbol": symbol, "name": q["name"], "price": q["price"],
                "pe_ttm": q["pe_ttm"], "pe_static": q.get("pe_static"),
                "pb": q["pb"], "mcap_yi": q["mcap_yi"],
                "float_mcap_yi": q["float_mcap_yi"],
                "turnover_pct": q["turnover_pct"], "change_pct": q["change_pct"],
                "amplitude_pct": q.get("amplitude_pct"),
                "limit_up": q["limit_up"], "limit_down": q["limit_down"],
            }, source="tencent")
        return _err(f"无法获取 {symbol} 估值数据", source="tencent")
    except Exception as e:
        return _err(f"估值获取失败: {e}", source="tencent")


# ── Concept index endpoints ────────────────────────────────────────────────

@router.get("/concepts")
async def list_concepts(type: str = Query(None, description="筛选类型: industry, concept, region")):
    """列出所有概念/行业/地域，含股票数量。"""
    try:
        concepts = await asyncio.to_thread(list_all_concepts, type or None)
        types = {}
        for c in concepts:
            t = c["type"]
            if t not in types:
                types[t] = []
            types[t].append(c)
        return {"concepts": concepts, "count": len(concepts), "by_type": types}
    except Exception as e:
        return _err(f"概念列表获取失败: {e}")


@router.get("/concepts/{concept}/stocks")
async def list_stocks_by_concept(concept: str):
    """获取指定概念下的所有股票。"""
    try:
        stocks = await asyncio.to_thread(get_stocks_by_concept, concept)
        return {"concept": concept, "stocks": stocks, "count": len(stocks)}
    except Exception as e:
        return _err(f"概念股票查询失败: {e}")


@router.get("/stocks/{symbol}/concepts")
async def get_stock_concepts(symbol: str):
    """获取某只股票的所有概念标签。"""
    try:
        concepts = await asyncio.to_thread(get_concepts_for_stock, symbol)
        return {"symbol": symbol, "concepts": concepts, "count": len(concepts)}
    except Exception as e:
        return _err(f"概念查询失败: {e}")


@router.get("/health")
async def data_health(period: str = Query("daily", description="数据周期")):
    """数据健康检查：新鲜度 + 文件计数（避免 DuckDB 锁竞争）."""
    from server.utils import DATA_DIR
    from datetime import datetime, timedelta
    import os

    try:
        period_dir = DATA_DIR / period if period != "daily" else DATA_DIR / "daily"
        if not period_dir.exists():
            return {"status": "error", "freshness": None, "schema": None, "message": f"目录不存在: {period_dir}"}

        files = list(period_dir.glob("*.parquet"))
        total = len(files)
        stale_symbols = 0
        latest_date = ""
        now = datetime.now()

        for f in files:
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            if mtime < now - timedelta(days=3):
                stale_symbols += 1
            if not latest_date or mtime.strftime("%Y-%m-%d") > latest_date:
                latest_date = mtime.strftime("%Y-%m-%d")

        status = "warning" if stale_symbols > 0 else "ok"
        return {
            "status": status,
            "freshness": {
                "total": total,
                "stale_symbols": stale_symbols,
                "latest_date": latest_date,
                "period": period,
            },
            "schema": {"violations": 0},
        }
    except Exception as e:
        logger.warning(f"Data health check failed: {e}")
        return {
            "status": "warning",
            "freshness": {"total": 0, "stale_symbols": 0, "latest_date": "", "period": period},
            "schema": {"violations": 0},
        }


@router.delete("/stocks/{symbol}")
async def delete_stock(symbol: str):
    """删除单只股票的所有数据文件（parquet + scan_history.db 记录）."""
    import re as _re
    if not _re.fullmatch(r'\d{6}', symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    deleted_files = []
    try:
        # Delete daily parquet
        daily_f = (daily_path / f"{symbol}.parquet").resolve()
        if not str(daily_f).startswith(str(daily_path.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
        if daily_f.exists():
            daily_f.unlink()
            deleted_files.append("daily")

        # Delete minute data
        for sub in ["1min", "5min", "15min", "30min", "60min"]:
            mf = (minute_path / sub / f"{symbol}.parquet").resolve()
            if not str(mf).startswith(str(minute_path.resolve())):
                continue
            if mf.exists():
                mf.unlink()
                deleted_files.append(sub)

        # Clean scan_history.db
        try:
            from server.scan_history_db import get_db
            db = get_db()
            db.execute("DELETE FROM scan_stocks WHERE symbol=?", (symbol,))
            db.execute("DELETE FROM scan_summary WHERE matched_count <= 0")
            db.commit()
        except Exception:
            pass

        logger.info(f"Deleted stock {symbol}: files={deleted_files}")
        return _ok({
            "symbol": symbol,
            "deleted": deleted_files,
            "message": f"已删除 {symbol} 的 {len(deleted_files)} 个数据文件",
        })
    except Exception as e:
        return _err(str(e))



# Path constants for deletion
daily_path = Path(__file__).parent.parent.parent / "data" / "raw" / "daily"
minute_path = Path(__file__).parent.parent.parent / "data" / "raw" / "minute"
