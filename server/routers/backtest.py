"""Backtest API endpoints."""
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

# In-memory task store (replace with Redis/db for production)
_backtest_tasks: dict[str, dict] = {}


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=10)
    period: str = Field("daily", pattern="^(daily|30min|60min|15min|5min|1min)$")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = Field(1_000_000, ge=10000)
    max_positions: int = Field(5, ge=1, le=20)
    risk_per_trade: float = Field(0.02, ge=0.005, le=0.1)
    mode: str = Field("simple", pattern="^(simple|schools|strict|chan_theory|ict|price_action|wyckoff|morphology|gann|wave_theory|dow_theory)$")
    school_config: Optional[dict] = Field(None, description="Fine-tuning flags for individual school modes")


class BacktestResult(BaseModel):
    task_id: str
    status: str
    metrics: Optional[dict] = None
    trades: Optional[list] = None
    equity_curve: Optional[list] = None
    signals: Optional[list] = None
    error: Optional[str] = None


@router.post("/run")
async def run_backtest(req: BacktestRequest):
    """Start a backtest (runs async, returns task_id)."""
    task_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    _backtest_tasks[task_id] = {
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "request": req.model_dump(),
    }

    # Run in background
    asyncio.create_task(_execute_backtest(task_id, req))

    return {"task_id": task_id, "status": "running"}


async def _execute_backtest(task_id: str, req: BacktestRequest):
    """Execute backtest in background thread."""
    try:
        from backtest.engine import SancaiBacktestEngine

        config = {
            "initial_capital": req.initial_capital,
            "max_positions": req.max_positions,
            "risk_per_trade": req.risk_per_trade,
        }

        engine = SancaiBacktestEngine(config)
        result = await asyncio.to_thread(
            engine.run,
            symbols=req.symbols,
            period=req.period,
            start_date=req.start_date,
            end_date=req.end_date,
            mode=req.mode,
            school_config=req.school_config,
        )

        _backtest_tasks[task_id].update({
            "status": "completed",
            "completed_at": datetime.now().isoformat(),
            "result": result,
        })
    except Exception as e:
        _backtest_tasks[task_id].update({
            "status": "failed",
            "error": str(e),
        })


@router.get("/{task_id}/status")
async def get_backtest_status(task_id: str):
    """Poll backtest progress."""
    task = _backtest_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    resp = {
        "task_id": task_id,
        "status": task["status"],
        "started_at": task.get("started_at"),
    }
    if task["status"] == "completed":
        resp["metrics"] = task.get("result", {}).get("metrics", {})
        resp["trade_count"] = len(task.get("result", {}).get("trades", []))
        resp["signal_count"] = len(task.get("result", {}).get("signals", []))
    elif task["status"] == "failed":
        resp["error"] = task.get("error")
    return resp


@router.get("/{task_id}/result")
async def get_backtest_result(task_id: str):
    """Get full backtest result."""
    task = _backtest_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Task is {task['status']}")

    result = task["result"]
    return {
        "task_id": task_id,
        "status": "completed",
        "metrics": result.get("metrics", {}),
        "trades": result.get("trades", []),
        "equity_curve": result.get("equity_curve", []),
        "signals": result.get("signals", []),
    }
