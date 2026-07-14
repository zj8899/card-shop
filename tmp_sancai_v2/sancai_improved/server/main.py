"""
Strategy Stock Screener - FastAPI Backend
Serves REST API + Static Dashboard
"""
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional

# 加载 .env 文件（在最早期执行，确保后续所有模块看到的 os.environ 都是完整的）
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_path = Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        _load_dotenv(_env_path, override=False)  # override=False: env var 优先于 .env
except ImportError:
    pass  # python-dotenv 未安装 — 跳过，由用户自行设环境变量

# 绕过 Windows 系统代理，直连国内数据源
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 禁用 py_mini_racer V8 引擎 — Windows Python 3.13 上随机 FATAL crash
# 在 py_mini_racer 被 import 之前注入 mock, akshare 会自动回退
import sys as _sys
_noop_mod = type(_sys)("py_mini_racer")
class _MockMiniRacer:
    def __init__(self, *a, **kw): pass  # no-op: never load real V8
    def eval(self, *a, **kw): return None
    def execute(self, *a, **kw): return None
    def call(self, *a, **kw): return None
_noop_mod.MiniRacer = _MockMiniRacer
_noop_mod.__version__ = "0.0.0"
if "py_mini_racer" not in _sys.modules:
    _sys.modules["py_mini_racer"] = _noop_mod

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response

from .routers import (data, backtest,
                          didao_screener, holdings, admin,
                        ai_chat, user_strategies, research, review, strategy_lab, news, evolution)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: event engine + data check. Shutdown: cleanup."""
    try:
        from server.events import event_engine
        event_engine.start()
        logger.info("Event engine started")
    except Exception as e:
        logger.warning(f"Event engine init skipped: {e}")

    # 启动实时监控引擎（飞书提醒）
    try:
        from server.monitor import start_monitor, _load_config
        cfg = _load_config()
        if cfg.get("enabled", False):
            start_monitor()
            logger.info("Real-time monitor started (Feishu alerts active)")
        else:
            logger.info("Real-time monitor disabled (set monitor.enabled=true in config)")
    except Exception as e:
        logger.warning(f"Monitor init skipped: {e}")

    # ── 午后自动扫描调度器 ──
    _scheduled_scan_task: Optional[asyncio.Task] = None
    try:
        _scheduled_scan_task = asyncio.create_task(_scheduled_scan_scheduler())
        logger.info("Scheduled scan scheduler started (multi-slot)")
    except Exception as e:
        logger.warning(f"Afternoon scan scheduler init skipped: {e}")

    # Auto-fill tail gaps on startup — skip entirely, too slow
    # Data updates handled by monitor post-market window (15:30-16:00)
    logger.info("Startup data auto-fill skipped (handled by monitor)")

    yield
    # Shutdown
    if _scheduled_scan_task:
        _scheduled_scan_task.cancel()
        try:
            await _scheduled_scan_task
        except asyncio.CancelledError:
            pass
    try:
        from server.monitor import stop_monitor
        await stop_monitor()
    except Exception as e:
        logger.warning(f"Shutdown cleanup failed: {e}")
    try:
        from server.events import event_engine as _ee
        _ee.stop()
    except Exception as e:
        logger.warning(f"Shutdown cleanup failed: {e}")
    try:
        from server.db import close_db
        close_db()
        logger.info("DuckDB connections closed")
    except Exception as e:
        logger.warning(f"Shutdown cleanup failed: {e}")
    try:
        from server.scan_history_db import close_db as _close_scan
        _close_scan()
    except Exception as e:
        logger.warning(f"Shutdown cleanup failed: {e}")
    try:
        from server.news_event_db import close_db as _close_news
        _close_news()
    except Exception as e:
        logger.warning(f"Shutdown cleanup failed: {e}")


app = FastAPI(
    title="策略选股",
    description="Strategy Stock Screener — Strategy Scanning & Analysis",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — also prevent caching on dev static files
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 开发模式：禁止静态 JS 缓存 ──
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Remove ETag so browser always re-fetches JS/CSS (dev mode only)."""
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/js/") or path.endswith(".js") or path.endswith(".css"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            if "etag" in response.headers:
                del response.headers["etag"]
        return response

app.add_middleware(NoCacheStaticMiddleware)

# API key auth (no-op when SANCAI_API_KEY env is unset)
from .auth import APIKeyMiddleware
app.add_middleware(APIKeyMiddleware)

# API Routers
app.include_router(data.router, prefix="/api/data", tags=["Data"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"])
app.include_router(didao_screener.router, prefix="/api", tags=["Strategy Screener"])
app.include_router(ai_chat.router, prefix="/api", tags=["AI Chat"])
app.include_router(user_strategies.router, tags=["User Strategies"])
app.include_router(holdings.router, tags=["Holdings"])
app.include_router(admin.router, tags=["Admin"])
app.include_router(research.router, prefix="/api/research", tags=["Research"])
app.include_router(review.router, prefix="/api", tags=["Review"])
app.include_router(strategy_lab.router, prefix="/api", tags=["Strategy Lab"])
app.include_router(news.router, prefix="/api", tags=["News Events"])
app.include_router(evolution.router, prefix="/api", tags=["Evolution"])

# Static files (dashboard HTML/JS/CSS)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico")
async def favicon():
    """Empty favicon to suppress 404 noise."""
    return Response(content="", media_type="image/x-icon")


@app.get("/")
async def root():
    """Serve main dashboard."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "ok", "message": "策略选股 API Server", "docs": "/docs"}


@app.get("/ai-dashboard")
async def ai_dashboard():
    """Serve AI Cockpit (V4 AI Decision Center)."""
    dashboard_path = static_dir / "ai-dashboard.html"
    if dashboard_path.exists():
        return FileResponse(str(dashboard_path))
    return {"status": "error", "message": "AI dashboard not found"}


@app.get("/api/health")
async def health():
    """Health check."""
    try:
        import sancai_core
        rust_ok = True
    except ImportError:
        rust_ok = False
    return {
        "status": "ok",
        "rust_core": rust_ok,
        "version": "2.0.0",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 定时策略扫描调度器 (多时段)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_scheduled_scan_config() -> dict:
    """加载定时扫描配置."""
    try:
        import yaml
        cfg_path = Path(__file__).parent.parent / "config" / "defaults.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("tools", {}).get("scheduled_scans", {}) or cfg.get("scheduled_scans", {})
    except Exception:
        return {}


async def _scheduled_scan_scheduler():
    """后台调度器: 每个交易日按配置的时段自动执行策略扫描.

    config 格式:
      tools.scheduled_scans:
        enabled: true
        slots:
          - time: "14:45", mode: "strict_reverse", label: "...", enabled: true
          - time: "09:30", mode: "strict",        label: "...", enabled: false
    """
    await asyncio.sleep(5)  # 等初始化

    # 记录每个 slot 今天是否已执行过  { (HH,MM,mode): True }
    today_done: dict[tuple, bool] = {}
    today_str_last = ""

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # 跨天重置
            if today_str != today_str_last:
                today_done.clear()
                today_str_last = today_str

            # 仅周一至周五
            if now.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            # 加载配置
            ssc = _load_scheduled_scan_config()
            if not ssc.get("enabled", False):
                await asyncio.sleep(60)
                continue

            slots = ssc.get("slots", [])
            if not slots:
                await asyncio.sleep(60)
                continue

            now_t = now.strftime("%H:%M")
            now_h, now_m = now.hour, now.minute

            for slot in slots:
                if not slot.get("enabled", True):
                    continue

                time_str = slot.get("time", "")
                mode = slot.get("mode", "strict_reverse")
                label = slot.get("label", mode)

                # 解析时间
                try:
                    h, m = map(int, time_str.split(":"))
                except Exception:
                    continue

                slot_key = (h, m, mode)

                # 今天已跑过
                if today_done.get(slot_key):
                    continue

                # 时间窗口: 目标时间到目标时间+14分钟
                target_mins = h * 60 + m
                now_mins = now_h * 60 + now_m
                if now_mins < target_mins:
                    continue                                          # 还没到
                if now_mins > target_mins + 14:
                    today_done[slot_key] = True                       # 过期, 今天不跑了
                    continue

                # ── 执行 ──
                logger.info("Scheduled scan: [%s] %s TRIGGERED at %s", time_str, label, now_t)
                try:
                    await _run_scheduled_scan(mode, label, time_str)
                except Exception as e:
                    logger.error("Scheduled scan [%s] FAILED: %s", label, e, exc_info=True)
                today_done[slot_key] = True

            await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("Scheduled scan scheduler cancelled")
            break
        except Exception as e:
            logger.error("Scheduled scan scheduler error: %s", e, exc_info=True)
            await asyncio.sleep(30)


async def _run_scheduled_scan(mode: str, label: str, time_str: str = ""):
    """执行一次定时策略扫描 + 飞书推送.

    Heavy data loading + scanning runs in a thread pool to avoid blocking the
    asyncio event loop. Only the Feishu notification stays on the loop.
    """
    from server.notify.feishu import FeishuNotifier
    import time as time_module

    t0 = time_module.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_date = datetime.now().strftime("%Y-%m-%d")

    # ── Phase 1: data loading + scanning (run in thread to avoid blocking) ──
    def _scan_sync():
        from scripts.afternoon_scan import (
            fetch_live_market, normalize_spot, filter_candidates,
            load_klines_with_live_bar, parallel_scan,
            enrich_frequency, enrich_concepts, enrich_names, group_by_frequency,
            build_card_content, _load_config,
        )
        import numpy as _np
        import pandas as _pd

        spot_raw, data_source = fetch_live_market()
        if spot_raw is None:
            return {"error": "no_market_data", "label": label}

        spot_df = normalize_spot(spot_raw)
        candidates = filter_candidates(spot_df)

        spot_map = {}
        for _, row in candidates.iterrows():
            sym = str(row.get("symbol", ""))
            if not sym or len(sym) != 6:
                continue
            spot_map[sym] = {
                "name": str(row.get("name", sym)),
                "price": float(row["price"]) if not _pd.isna(row.get("price", _np.nan)) else 0,
                "today_open": float(row.get("open", 0)) if not _pd.isna(row.get("open", _np.nan)) else 0,
                "today_high": float(row.get("high", 0)) if not _pd.isna(row.get("high", _np.nan)) else 0,
                "today_low": float(row.get("low", 0)) if not _pd.isna(row.get("low", _np.nan)) else 0,
                "pct_change": float(row.get("pct_change", 0)) if not _pd.isna(row.get("pct_change", _np.nan)) else 0,
                "turnover_rate": float(row.get("turnover_rate", 0)) if not _pd.isna(row.get("turnover_rate", _np.nan)) else 0,
            }

        symbols = list(spot_map.keys())
        logger.info("Scheduled scan [%s]: %d candidates (source=%s)", label, len(symbols), data_source)

        kdata = load_klines_with_live_bar(symbols, spot_map)
        scan_results = parallel_scan(kdata, mode=mode)

        if not scan_results:
            return {"error": "no_matches", "label": label, "n_symbols": len(symbols),
                    "data_source": data_source, "spot_map": spot_map}

        scan_results = enrich_frequency(scan_results)
        scan_results = enrich_concepts(scan_results)
        scan_results = enrich_names(scan_results, spot_map)
        grouped = group_by_frequency(scan_results)

        cfg = _load_config() or {}
        return {"grouped": grouped, "n_symbols": len(symbols), "data_source": data_source,
                "spot_map": spot_map, "scan_results": scan_results,
                "ssc": cfg.get("tools", {}).get("scheduled_scans", {})
                       or cfg.get("scheduled_scans", {}),
                "cfg": cfg}

    scan_data = await asyncio.to_thread(_scan_sync)

    if "error" in scan_data:
        err = scan_data["error"]
        if err == "no_market_data":
            logger.error("Scheduled scan [%s]: no market data", label)
        elif err == "no_matches":
            logger.info("Scheduled scan [%s]: 0 matched", label)
        return

    grouped = scan_data["grouped"]
    n_symbols = scan_data["n_symbols"]
    data_source = scan_data["data_source"]
    spot_map = scan_data["spot_map"]
    scan_results = scan_data["scan_results"]
    ssc = scan_data["ssc"]
    cfg = scan_data["cfg"]

    # ── Phase 2: Feishu notification (async, stays on event loop) ──
    webhook = (ssc.get("feishu_webhook_url", "")
               or cfg.get("monitor", {}).get("feishu_webhook_url", ""))
    if not webhook:
        logger.error("Scheduled scan [%s]: no webhook URL", label)
        return

    # ── 跨进程/重启持久化去重 ──
    try:
        from server.scan_history_db import try_claim_scheduled_push
        if not try_claim_scheduled_push(now_date, time_str or "", mode):
            logger.info("Scheduled scan [%s]: 今日该时段已推送(重复进程/窗口内重启), 跳过推送", label)
            try:
                from server.scan_history_db import save_scan_result
                save_scan_result(mode, n_symbols, len(scan_results),
                                 int((time_module.time() - t0) * 1000), scan_results)
            except Exception:
                pass
            return
    except Exception as e:
        logger.warning("Scheduled scan [%s]: 去重占用异常, 继续推送: %s", label, e)

    from scripts.afternoon_scan import build_card_content
    notifier = FeishuNotifier(webhook)

    FREQ_GROUPS = {
        "1次·首次出现": (1, 1, "green"),
        "2次·趋势确认": (2, 2, "blue"),
        "3次·高频信号": (3, 3, "yellow"),
        "4次+·持续热点": (4, 99, "red"),
    }

    for flabel in [k for k in FREQ_GROUPS if grouped.get(k)]:
        stocks = grouped[flabel]
        content = build_card_content(flabel, stocks, now_str, data_source)
        color = FREQ_GROUPS[flabel][2]
        ok = await notifier.send_card(
            title=f"🧨 {label}·{flabel.split('·')[1]}({len(stocks)}只)",
            content=content, color=color,
            cooldown_key=f"scheduled_{mode}_{flabel}_{now_date}",
            cooldown_seconds=300,
        )
        logger.info("Scheduled scan [%s]: %s (%d只) sent=%s", label, flabel, len(stocks), ok)
        await asyncio.sleep(0.5)

    # 6. 持久化
    elapsed_ms = int((time_module.time() - t0) * 1000)
    try:
        from server.scan_history_db import save_scan_result
        save_scan_result(mode, n_symbols, len(scan_results), elapsed_ms, scan_results)
    except Exception as e:
        logger.warning("Scheduled scan [%s]: save failed: %s", label, e)

    total_t = time_module.time() - t0
    logger.info("Scheduled scan [%s]: DONE total=%.1fs matched=%d source=%s",
                label, total_t, len(scan_results), data_source)
