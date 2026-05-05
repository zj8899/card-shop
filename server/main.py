"""
Sancai A-Share Trading System - FastAPI Backend
Serves REST API + WebSocket + Static Dashboard
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routers import data, backtest, signal, sancai, sancai_layers

app = FastAPI(
    title="三才回测实盘 A 股交易系统",
    description="Sancai A-Share Backtest & Live Trading System",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Routers
app.include_router(data.router, prefix="/api/data", tags=["Data"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"])
app.include_router(signal.router, prefix="/api/signal", tags=["Signal"])
app.include_router(sancai.router, prefix="/api/sancai", tags=["Sancai"])
app.include_router(sancai_layers.router, prefix="/api/sancai", tags=["Sancai Layers"])

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
    return {"status": "ok", "message": "三才交易系统 API Server", "docs": "/docs"}


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
        "version": "0.1.0",
    }
