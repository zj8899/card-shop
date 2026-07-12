"""系统配置管理 — 飞书/AI秘钥 + 推送开关 + AI用量统计."""
import asyncio
import json as _json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
USAGE_DB = PROJECT_ROOT / "data" / "ai_usage.db"


# ── Models ─────────────────────────────────────────────────────────────

class AIConfig(BaseModel):
    deepseek_key: str = ""
    doubao_key: str = ""
    doubao_model: str = ""
    deepseek_model: str = ""
    anthropic_key: str = ""

class FeishuConfig(BaseModel):
    enabled: bool = True
    webhook_url: str = ""
    poll_interval_minutes: int = 5
    trade_hours_only: bool = True

class FirecrawlConfig(BaseModel):
    enabled: bool = True
    api_key: str = ""

class AdminConfig(BaseModel):
    ai: AIConfig = AIConfig()
    feishu: FeishuConfig = FeishuConfig()
    firecrawl: FirecrawlConfig = FirecrawlConfig()


# ── Usage tracking (simple file-based counter, resets daily) ──────────

def _init_usage_db():
    db = sqlite3.connect(str(USAGE_DB))
    db.execute("CREATE TABLE IF NOT EXISTS usage (ts TEXT, provider TEXT, endpoint TEXT, tokens INTEGER)")
    db.commit()
    db.close()

def _record_usage(provider: str, endpoint: str = "", tokens: int = 0):
    try:
        _init_usage_db()
        db = sqlite3.connect(str(USAGE_DB))
        db.execute("INSERT INTO usage VALUES (datetime('now','localtime'), ?, ?, ?)",
                   (provider, endpoint, tokens))
        db.commit()
        db.close()
    except Exception:
        pass


# ── GET /api/admin/config ─────────────────────────────────────────────

@router.get("/api/admin/config")
async def get_config():
    """读取当前配置（脱敏，仅显示后4位）."""
    result = {"ai": {}, "feishu": {}, "env_keys": {}}

    # Read defaults.yaml
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        result["feishu"] = cfg.get("monitor", {})
        tools = cfg.get("tools", {})
        result["ai"] = {
            "summarizer": tools.get("summarizer", {}),
            "doubao": tools.get("doubao", {}),
            "tavily": tools.get("tavily", {}),
        }
        # 定时扫描配置
        result["scheduled_scans"] = tools.get("scheduled_scans", {}) or cfg.get("scheduled_scans", {})
        result["firecrawl"] = tools.get("firecrawl", {})
    except Exception as e:
        logger.warning(f"Config read failed: {e}")

    # Read .env keys (masked)
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            env_text = f.read()
        for key in ["DEEPSEEK_API_KEY", "DOUBAO_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "SANCAI_API_KEY"]:
            m = re.search(rf"{key}\s*=\s*(.+)", env_text)
            if m:
                val = m.group(1).strip()
                masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
                result["env_keys"][key] = masked
            else:
                result["env_keys"][key] = ""
    except Exception:
        result["env_keys"] = {}

    return {"status": "ok", "data": result}


# ── POST /api/admin/config ────────────────────────────────────────────

@router.post("/api/admin/config")
async def save_config(req: AdminConfig):
    """保存配置到 defaults.yaml 和 .env."""
    changes = []

    # 1) Update defaults.yaml (飞书 + AI 非秘钥字段)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        # Monitor section
        if "monitor" not in cfg:
            cfg["monitor"] = {}
        cfg["monitor"]["enabled"] = req.feishu.enabled
        cfg["monitor"]["feishu_webhook_url"] = req.feishu.webhook_url
        cfg["monitor"]["poll_interval_minutes"] = req.feishu.poll_interval_minutes

        # AI models (non-secret fields)
        if "tools" not in cfg:
            cfg["tools"] = {}
        if "doubao" not in cfg["tools"]:
            cfg["tools"]["doubao"] = {}
        if req.ai.doubao_model:
            cfg["tools"]["doubao"]["model"] = req.ai.doubao_model
        if "summarizer" not in cfg["tools"]:
            cfg["tools"]["summarizer"] = {}
        if req.ai.deepseek_model:
            cfg["tools"]["summarizer"]["model"] = req.ai.deepseek_model

        # Firecrawl
        if "firecrawl" not in cfg["tools"]:
            cfg["tools"]["firecrawl"] = {}
        if req.firecrawl.api_key:
            cfg["tools"]["firecrawl"]["api_key"] = req.firecrawl.api_key
        cfg["tools"]["firecrawl"]["enabled"] = req.firecrawl.enabled

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            original = f.read()
        import io
        buf = io.StringIO()
        yaml.safe_dump(cfg, buf, allow_unicode=True, default_flow_style=False, sort_keys=False)
        new_yaml = buf.getvalue()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(new_yaml)
        changes.append("defaults.yaml")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"配置文件写入失败: {e}")

    # 2) Update .env (secret keys only if non-empty)
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            env_text = f.read()
        env_map = {"DEEPSEEK_API_KEY": req.ai.deepseek_key,
                   "DOUBAO_API_KEY": req.ai.doubao_key,
                   "ANTHROPIC_API_KEY": req.ai.anthropic_key}
        for key, val in env_map.items():
            if val and not val.startswith("*"):  # only update if real value provided
                if re.search(rf"^{key}\s*=", env_text, re.MULTILINE):
                    env_text = re.sub(rf"^{key}\s*=.*$", lambda m: f"{key}={val}", env_text, flags=re.MULTILINE)
                else:
                    env_text += f"\n{key}={val}"
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(env_text)
        changes.append(".env")
    except Exception as e:
        logger.warning(f".env write failed: {e}")

    # Reload env vars
    for key in ["DEEPSEEK_API_KEY", "DOUBAO_API_KEY", "ANTHROPIC_API_KEY"]:
        if req.ai.dict().get(key.lower().replace("_api_key", "_key"), ""):
            pass  # env vars will take effect on next server restart

    return {"status": "ok", "message": f"已保存: {', '.join(changes)}", "restart_required": True}


class ToggleRequest(BaseModel):
    enabled: bool = True


@router.put("/api/admin/config/afternoon-scan")
async def toggle_afternoon_scan(req: ToggleRequest):
    """开启/关闭定时策略扫描总开关."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "tools" not in cfg:
            cfg["tools"] = {}
        if "scheduled_scans" not in cfg["tools"]:
            cfg["tools"]["scheduled_scans"] = {}
        cfg["tools"]["scheduled_scans"]["enabled"] = req.enabled
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        logger.info("scheduled_scans.enabled = %s", req.enabled)
        return {"status": "ok", "enabled": req.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SlotToggleRequest(BaseModel):
    slot_index: int
    enabled: bool = True


@router.put("/api/admin/config/scheduled-slot")
async def toggle_scheduled_slot(req: SlotToggleRequest):
    """开启/关闭单个定时扫描槽位."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        slots = cfg.get("tools", {}).get("scheduled_scans", {}).get("slots", [])
        if req.slot_index < 0 or req.slot_index >= len(slots):
            raise HTTPException(status_code=400, detail=f"slot_index {req.slot_index} out of range (0-{len(slots)-1})")
        slots[req.slot_index]["enabled"] = req.enabled
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        logger.info("scheduled_slot[%d].enabled = %s", req.slot_index, req.enabled)
        return {"status": "ok", "slot_index": req.slot_index, "enabled": req.enabled}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/admin/ai-usage ───────────────────────────────────────────

@router.get("/api/admin/ai-usage")
async def get_ai_usage(days: int = 30):
    """AI调用统计."""
    try:
        _init_usage_db()
        db = sqlite3.connect(str(USAGE_DB))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # Daily breakdown
        daily_rows = db.execute(
            "SELECT substr(ts,1,10) as d, provider, SUM(tokens) as t, COUNT(*) as c "
            "FROM usage WHERE ts >= ? GROUP BY d, provider ORDER BY d DESC",
            (cutoff,)
        ).fetchall()

        daily = {}
        for r in daily_rows:
            d = r[0]
            if d not in daily:
                daily[d] = {}
            daily[d][r[1]] = {"tokens": r[2] or 0, "calls": r[3]}

        # Totals
        totals = {}
        for r in db.execute(
            "SELECT provider, SUM(tokens), COUNT(*) FROM usage WHERE ts >= ? GROUP BY provider",
            (cutoff,)
        ).fetchall():
            totals[r[0]] = {"tokens": r[1] or 0, "calls": r[2]}

        db.close()
        return {"status": "ok", "data": {"daily": daily, "totals": totals, "days": days}}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── POST /api/admin/feishu-test ───────────────────────────────────────

@router.post("/api/admin/feishu-test")
async def test_feishu():
    """发送飞书测试消息."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        webhook = cfg.get("monitor", {}).get("feishu_webhook_url", "")
        if not webhook:
            raise HTTPException(status_code=400, detail="飞书 Webhook URL 未配置")

        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(webhook, json={
                "msg_type": "interactive",
                "card": {
                    "header": {"title": {"content": "🧪 飞书连接测试", "tag": "plain_text"}, "template": "blue"},
                    "elements": [
                        {"tag": "div", "text": {"content": "✅ 策略选股系统 — 飞书机器人连接成功！\n\n时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tag": "lark_md"}}
                    ]
                }
            })
        if resp.status_code == 200:
            d = resp.json()
            if d.get("code") == 0:
                return {"status": "ok", "message": "测试消息已发送，请查看飞书"}
            return {"status": "error", "message": f"飞书返回错误: {d.get('msg', '')}"}
        raise HTTPException(status_code=502, detail=f"飞书请求失败 [{resp.status_code}]: {resp.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Expose record_usage for other modules ─────────────────────────────
def record_ai_usage(provider: str, endpoint: str = "", tokens: int = 0):
    _record_usage(provider, endpoint, tokens)
