"""
Sancai A-Share Trading System - FastAPI Backend
Serves REST API + WebSocket + Static Dashboard
"""
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# Early bootstrap: proxy bypass + path setup + system logger
# ═══════════════════════════════════════════════════════════════

# 绕过 Windows 系统代理，直连国内数据源
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Initialize system logger BEFORE any other imports
from server.syslog import init_syslog, get_logger, Phase
init_syslog()
logger = get_logger(__name__)

# ── Now import the rest ──
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Lazy-import routers to survive individual import failures
import importlib

_ROUTER_IMPORTS = {
    "data":              "server.routers.data",
    "backtest":          "server.routers.backtest",
    "signal":            "server.routers.signal",
    "sancai":            "server.routers.sancai",
    "sancai_layers":     "server.routers.sancai_layers",
    "rendao_quiz":       "server.routers.rendao_quiz",
    "rendao_plan":       "server.routers.rendao_plan",
    "didao_screener":    "server.routers.didao_screener",
    "didao_mindmap":     "server.routers.didao_mindmap",
    "tiandao_sentiment": "server.routers.tiandao_sentiment",
    "tiandao_media":     "server.routers.tiandao_media",
    "vbt":              "server.routers.vbt",
    "ai_chat":          "server.routers.ai_chat",
    "card_shop":        "server.routers.card_shop",
}

_loaded_routers = {}
_failed_routers = []

for _name, _mod_path in _ROUTER_IMPORTS.items():
    try:
        _mod = importlib.import_module(_mod_path)
        _loaded_routers[_name] = _mod
        logger.debug("Router loaded: %s", _name)
    except Exception as _e:
        _failed_routers.append((_name, str(_e)))
        logger.error("Router FAILED to load: %s — %s", _name, _e, exc_info=True)

if _failed_routers:
    logger.warning(
        "%d/%d routers failed to load: %s",
        len(_failed_routers), len(_ROUTER_IMPORTS),
        ", ".join(n for n, _ in _failed_routers),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: event engine + data check + DB init. Shutdown: cleanup."""
    # ── Phase 1: Event Engine ──
    with Phase("event_engine"):
        try:
            from server.events import event_engine
            event_engine.start()
            logger.info("Event engine started")
        except Exception as e:
            logger.error("Event engine FAILED to start: %s", e, exc_info=True)

    # ── Phase 2: DuckDB + Parquet Views ──
    with Phase("duckdb_views"):
        try:
            from server.db import get_db
            get_db()
            logger.info("DuckDB initialized with parquet views")
        except Exception as e:
            logger.error("DuckDB init FAILED: %s", e, exc_info=True)

    # ── Phase 3: Data Freshness Check ──
    with Phase("data_freshness"):
        try:
            from scripts.data_update import check_freshness, run_tail_update
            from server.utils.config import get_config
            cfg = get_config()
            data_cfg = cfg.get("data_updates", {})
            auto_fill = data_cfg.get("auto_fill_on_startup", False)
            max_symbols = data_cfg.get("max_symbols_per_run", 500)
            delay = data_cfg.get("rate_limit_delay", 0.3)

            freshness = check_freshness("daily")
            if freshness["stale_symbols"] > 0:
                logger.warning(
                    "Data freshness: %d/%d symbols stale (latest: %s, %d days behind)",
                    freshness["stale_symbols"], freshness["total_symbols"],
                    freshness["latest_date"], freshness["staleness_days"],
                )
                if auto_fill:
                    logger.info("Auto-filling tail gaps...")
                    result = run_tail_update(period="daily", max_symbols=max_symbols, delay=delay)
                    if not result.get("dry_run", True):
                        logger.info(
                            "Auto-fill complete: %d updated, %d bars added, %d errors",
                            result.get("symbols_updated", 0),
                            result.get("bars_added", 0),
                            result.get("errors", 0),
                        )
                else:
                    logger.info("Auto-fill disabled in config — skipping")
            else:
                logger.info("Data freshness: all %d symbols up to date", freshness["total_symbols"])
        except Exception as e:
            logger.error("Data freshness check FAILED: %s", e, exc_info=True)

    # ── Phase 4: Review DB ──
    with Phase("review_db"):
        try:
            from server.review_db import get_review_db
            get_review_db()
            logger.info("Review database initialized")
        except Exception as e:
            logger.error("Review DB init FAILED: %s", e, exc_info=True)

    # ── Phase 5: Concept Index ──
    with Phase("concept_index"):
        try:
            from server.concept_index import init_tables as concept_init
            concept_init()
            logger.info("Concept index tables ready")
        except Exception as e:
            logger.error("Concept index init FAILED: %s", e, exc_info=True)

    # ── Phase 6: mootdx (best-effort) ──
    with Phase("mootdx_init"):
        try:
            from data_sources.provider import init_mootdx
            mootdx_ok = init_mootdx()
            if mootdx_ok:
                logger.info("mootdx TCP data source enabled")
            else:
                logger.info("mootdx unavailable — will use fallback sources")
        except Exception as e:
            logger.info("mootdx init skipped: %s", e)

    # ── Phase 7: Concept index build (best-effort, non-blocking) ──
    with Phase("concept_index_build"):
        try:
            import threading
            def _build_ci():
                import sys as _sys
                _orig_argv = _sys.argv[:]
                _sys.argv = ["build_concept_index"]
                try:
                    from scripts.build_concept_index import main as build_ci
                    build_ci()
                    logger.info("Concept index build complete")
                except BaseException as e:
                    logger.info("Concept index build skipped: %s: %s", type(e).__name__, e)
                finally:
                    _sys.argv = _orig_argv
            # Run in background — don't block server startup
            threading.Thread(target=_build_ci, daemon=True, name="concept_index_build").start()
        except Exception as e:
            logger.info("Concept index build skipped: %s", e)

    # ── Startup Summary ──
    from server.syslog import log_startup_summary
    log_startup_summary()

    yield  # ═══════════ Server running ═══════════

    # ── Shutdown ──
    logger.info("Server shutting down...")
    with Phase("shutdown"):
        try:
            from server.review_db import close_review_db
            close_review_db()
            logger.info("Review DB closed")
        except Exception as e:
            logger.warning("Review DB close failed: %s", e)
        try:
            from server.events import event_engine
            event_engine.stop()
            logger.info("Event engine stopped")
        except Exception as e:
            logger.warning("Event engine stop failed: %s", e)
    logger.info("════════════ Server shutdown complete ════════════")


app = FastAPI(
    title="三才量化",
    description="Sancai Quantitative Trading System",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API key auth (no-op when SANCAI_API_KEY env is unset)
from .auth import APIKeyMiddleware
app.add_middleware(APIKeyMiddleware)

# API Routers — only register successfully loaded routers
_ROUTER_REGISTRATIONS = [
    ("data",              "/api/data",      ["Data"]),
    ("backtest",          "/api/backtest",  ["Backtest"]),
    ("signal",            "/api/signal",    ["Signal"]),
    ("sancai",            "/api/sancai",    ["Sancai"]),
    ("sancai_layers",     "/api/sancai",    ["Sancai Layers"]),
    ("rendao_quiz",       "/api",           ["Rendao Quiz"]),
    ("rendao_plan",       "/api",           ["Rendao Plan"]),
    ("didao_screener",    "/api",           ["Didao Screener"]),
    ("didao_mindmap",     "/api",           ["Didao Mindmap"]),
    ("tiandao_sentiment", "/api",           ["Tiandao Sentiment"]),
    ("tiandao_media",     "/api",           ["Tiandao Media"]),
    ("vbt",              "/api/vbt",        ["VectorBT"]),
    ("ai_chat",          "/api",            ["AI Chat"]),
    ("card_shop",        "/api",            ["Card Shop"]),
]

for _name, _prefix, _tags in _ROUTER_REGISTRATIONS:
    if _name in _loaded_routers:
        app.include_router(_loaded_routers[_name].router, prefix=_prefix, tags=_tags)
    else:
        logger.warning("Skipping router '%s' — failed to load", _name)

# Static files (dashboard HTML/JS/CSS)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """Serve main dashboard."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"status": "ok", "message": "三才量化 API Server", "docs": "/docs"}


@app.get("/shop")
async def shop_page():
    """卡密商城"""
    shop_path = static_dir / "shop.html"
    if shop_path.exists():
        return FileResponse(str(shop_path))
    return {"status": "error", "message": "shop.html not found"}


@app.get("/api/health")
async def health():
    """Health check."""
    try:
        import sancai_core
        rust_ok = True
    except ImportError:
        rust_ok = False

    # Include syslog phase stats in health response for diagnostics
    from server.syslog import get_phase_stats, is_initialized as syslog_ok
    phase_stats = get_phase_stats()

    return {
        "status": "ok",
        "rust_core": rust_ok,
        "syslog_initialized": syslog_ok(),
        "startup_phases": phase_stats,
        "version": "0.2.0",
    }


@app.get("/api/logs")
async def view_logs(tail: int = 100, level: str = None):
    """View recent system logs via API (for debugging from dashboard).

    Args:
        tail: Number of recent log lines (max 500)
        level: Filter by level (DEBUG, INFO, WARNING, ERROR)
    """
    tail = min(max(tail, 10), 500)

    from server.syslog import LOG_DIR
    from datetime import date

    today = date.today().isoformat()
    log_path = LOG_DIR / f"sancai_{today}.jsonl"

    if not log_path.exists():
        return {"status": "ok", "entries": [], "note": f"No log file for today ({today})"}

    entries = []
    total_lines = 0
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        total_lines = len(lines)
        for line in lines[-tail:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if level and entry.get("level") != level.upper():
                    continue
                entries.append(entry)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "date": today,
        "total_lines": total_lines,
        "returned": len(entries),
        "entries": entries,
    }
