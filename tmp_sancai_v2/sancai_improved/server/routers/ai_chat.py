"""
策略选股 — AI 对话路由 (支持 豆包 / DeepSeek / Claude)
提供给前端调用的 AI 接口，统一走后端转发，安全管理 API Key
"""
import ast
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_api_key() -> Optional[str]:
    """获取 API Key，优先级：env var > .env 文件 > config/defaults.yaml"""
    _load_dotenv()
    for env_var in ("ANTHROPIC_API_KEY", "SUMMARIZER_API_KEY", "DEEPSEEK_API_KEY"):
        key = os.environ.get(env_var)
        if key:
            return key
    try:
        from server.utils.config import get_config
        cfg = get_config()
        return cfg.get("tools", {}).get("summarizer", {}).get("api_key", "")
    except Exception:
        return None


def _get_deepseek_key() -> Optional[str]:
    """获取 DeepSeek API Key."""
    _load_dotenv()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    try:
        from server.utils.config import get_config
        cfg = get_config()
        return cfg.get("tools", {}).get("deepseek", {}).get("api_key", "")
    except Exception:
        return None


def _load_dotenv():
    """从项目根目录 .env 文件加载环境变量"""
    try:
        import dotenv
        project_root = Path(__file__).parent.parent.parent
        env_file = project_root / ".env"
        if env_file.exists():
            dotenv.load_dotenv(env_file, override=True)
            return True
    except Exception:
        pass
    return False


def _get_provider() -> str:
    """推断 provider: anthropic 或 deepseek"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    try:
        from server.utils.config import get_config
        cfg = get_config()
        return cfg.get("tools", {}).get("summarizer", {}).get("provider", "anthropic")
    except Exception:
        return "anthropic"


class ChatRequest(BaseModel):
    message: str
    system: Optional[str] = None
    max_tokens: int = 1000


# ── AI 调用层 ──

async def _call_deepseek(prompt: str, system: str = "", max_tokens: int = 2000, timeout: float = 90.0) -> str:
    """调用 DeepSeek API (独立函数，用于双AI对比)."""
    import httpx
    api_key = _get_deepseek_key()
    if not api_key:
        api_key = _get_api_key()  # fallback
    if not api_key:
        raise HTTPException(status_code=503,
            detail="DeepSeek API Key 未配置。请设置环境变量 DEEPSEEK_API_KEY。")

    try:
        from server.utils.config import get_config
        cfg = get_config()
        ds_cfg = cfg.get("tools", {}).get("deepseek", {})
    except Exception:
        ds_cfg = {}

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(
                ds_cfg.get("api_base", "https://api.deepseek.com") + "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": ds_cfg.get("model", "deepseek-chat"),
                    "max_tokens": max_tokens,
                    "messages": messages,
                },
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DeepSeek 请求失败: {e}")

    if resp.status_code == 200:
        data = resp.json()
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
    raise HTTPException(status_code=502,
        detail=f"DeepSeek API 错误 [{resp.status_code}]: {resp.text[:200]}")


async def _call_llm(prompt: str, system: str = "", max_tokens: int = 1000) -> tuple[str, str]:
    """调用 LLM API (支持 Anthropic / DeepSeek)，返回 (content, provider_name)"""
    api_key = _get_api_key()
    provider = _get_provider()

    if not api_key:
        key_hint = "ANTHROPIC_API_KEY" if provider == "anthropic" else "DEEPSEEK_API_KEY"
        raise HTTPException(status_code=503,
            detail=f"AI API Key 未配置。请在环境变量 {key_hint} 或 SUMMARIZER_API_KEY 中设置。")

    try:
        import httpx

        if provider == "deepseek":
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                resp = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "max_tokens": max_tokens,
                        "messages": messages,
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    try:
                        from server.routers.admin import record_ai_usage
                        usage = data.get("usage", {})
                        tokens = usage.get("total_tokens", max_tokens)
                        record_ai_usage("deepseek", "chat", tokens)
                    except Exception: pass
                    return choices[0].get("message", {}).get("content", ""), "deepseek"
            raise HTTPException(status_code=502,
                detail=f"DeepSeek API 错误 [{resp.status_code}]: {resp.text[:200]}")

        else:
            body = {
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                body["system"] = system

            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", [])
                if content:
                    try:
                        from server.routers.admin import record_ai_usage
                        record_ai_usage("claude", "chat", max_tokens)
                    except Exception: pass
                    return content[0].get("text", ""), "claude"
            raise HTTPException(status_code=502,
                detail=f"Claude API 错误 [{resp.status_code}]: {resp.text[:200]}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LLM API call failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI 请求失败: {str(e)}")


# ── 策略代码校验 ──

FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "shutil", "socket", "http", "urllib",
    "requests", "httpx", "ctypes", "multiprocessing", "threading",
    "signal", "pickle", "marshal", "code", "codeop",
    "builtins", "__builtins__", "importlib",
}


def _validate_strategy_code(code: str, name: str) -> dict:
    """Validate generated strategy code for syntax and safety."""
    errors = []
    warnings = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"valid": False, "errors": [f"Syntax error at line {e.lineno}: {e.msg}"], "warnings": []}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_module = alias.name.split(".")[0]
                if top_module in FORBIDDEN_IMPORTS:
                    errors.append(f"Forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_module = node.module.split(".")[0]
                if top_module in FORBIDDEN_IMPORTS:
                    errors.append(f"Forbidden import from: {node.module}")

    # 核心符号必须从 ..interface 导入（拦截 Freqtrade 的 `from strategy import IStrategy`）
    _CORE_SYMBOLS = {"IStrategy", "Signal", "SignalType", "BarContext"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if {a.name for a in node.names} & _CORE_SYMBOLS:
                mod = node.module or ""
                if not mod.endswith("interface"):
                    errors.append(
                        f"IStrategy/Signal 等应从本系统 ..interface 导入，"
                        f"当前 'from {mod} import ...' 不正确（疑似 Freqtrade 写法）。"
                        f"正确写法：from ..interface import IStrategy, Signal, SignalType, BarContext"
                    )

    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not class_defs:
        errors.append("No class definition found")
    else:
        strat_cls_names = []
        for cls in class_defs:
            for base in cls.bases:
                if isinstance(base, ast.Name) and base.id == "IStrategy":
                    strat_cls_names.append(cls.name)
        if not strat_cls_names:
            warnings.append("No class explicitly inheriting from IStrategy found")
        else:
            # populate_indicators 在 IStrategy 里有默认实现(返回 df 不变)，运行时可省略；
            # 只有 entry/exit 是 @abstractmethod 必须实现。校验与实际运行要求保持一致。
            required_methods = {"populate_entry_signals", "populate_exit_signals"}
            for cls in class_defs:
                if cls.name in strat_cls_names:
                    methods = {n.name: n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)}
                    missing = required_methods - set(methods)
                    if missing:
                        errors.append(f"Missing method(s): {', '.join(missing)}")
                    # 拦截 Freqtrade 签名：本系统 entry/exit 为 (self, ctx)，Freqtrade 为 (self, dataframe, metadata)
                    for mname in ("populate_entry_signals", "populate_exit_signals"):
                        m = methods.get(mname)
                        if m and len(m.args.args) > 2:
                            errors.append(
                                f"{mname} 签名应为 (self, ctx: BarContext)，"
                                f"不要带 dataframe/metadata 参数（Freqtrade 写法）"
                            )

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════

SANCAI_SYSTEM = """你是三才量化交易系统的 AI 助手，专精 A 股市场分析。"""

DD_SYSTEM = """你是 A 股深度尽调分析师。对每只股票从 5 个维度进行穿透式评估：
1. 价格辩证法（Price Dialectics）— 当前位置是低位价值吸纳还是高位估值溢价？用 PE/历史分位/产业周期交叉验证
2. BOM 供应链（Supply Chain）— 公司在产业链的位置，上游原材料/中游制造/下游需求各环节的议价能力和风险
3. 财务穿透（Financial Penetration）— 营收/利润/现金流质量，剔除一次性项目后真实增速
4. 红队证伪（Red Team Falsification）— 假设自己是空头，找出 3 个最可能击穿当前逻辑的风险
5. 动态熔断（Dynamic Circuit Breaker）— 给出合理估值区间 + 操作纪律（什么情况下必须止损/止盈）

输出格式：5 段式 Markdown，每段 3-5 句话，最后给出综合评分（0-15 分）和操作建议（关注/观望/回避）。"""


class DeepDDRequest(BaseModel):
    symbol: str
    name: str = ""


@router.post("/ai/didao_deep_dd")
async def didao_deep_dd(req: DeepDDRequest):
    """对单只股票进行 5 阶段深度尽调，返回 Markdown 报告."""
    import numpy as np
    import pandas as pd

    sym = req.symbol.strip()
    name = req.name.strip()
    if len(sym) != 6 or not sym.isdigit():
        raise HTTPException(status_code=400, detail="股票代码格式错误")

    # ── 1. 收集数据 ──
    from server.utils import DATA_DIR
    kline_path = DATA_DIR / "daily" / f"{sym}.parquet"
    if not kline_path.exists():
        raise HTTPException(status_code=404, detail=f"无 {sym} 的日线数据")

    df = pd.read_parquet(kline_path)
    if len(df) < 60:
        raise HTTPException(status_code=400, detail=f"{sym} 仅 {len(df)} 根 K 线，需 60+")

    closes = df["close"].values
    volumes = df["volume"].values if "volume" in df.columns else np.zeros(len(closes))
    latest_price = float(closes[-1])
    ma20 = float(np.mean(closes[-20:]))
    ma60 = float(np.mean(closes[-60:])) if len(closes) >= 60 else ma20
    pct_20d = round((closes[-1] / closes[-min(20, len(closes))] - 1) * 100, 1)
    pct_60d = round((closes[-1] / closes[-min(60, len(closes))] - 1) * 100, 1)
    max_90d = float(np.max(closes[-min(90, len(closes)):]))
    min_90d = float(np.min(closes[-min(90, len(closes)):]))
    from_high = round((latest_price / max_90d - 1) * 100, 1)
    from_low = round((latest_price / min_90d - 1) * 100, 1)
    vol_ratio = round(float(np.mean(volumes[-5:])) / max(float(np.mean(volumes[-60:])), 1), 2)

    # 概念
    concepts = []
    try:
        from server.concept_index import get_concepts_for_symbol
        concepts = get_concepts_for_symbol(sym)[:8]
    except Exception:
        pass

    # 估值
    pe_val, pb_val, mkt_cap = "", "", ""
    try:
        from data_sources.tencent_quotes import tencent_quote
        q = tencent_quote([sym])
        if q and sym in q:
            pe_val = str(q[sym].get("pe", ""))
            pb_val = str(q[sym].get("pb", ""))
            mkt_cap = str(q[sym].get("market_cap", ""))
    except Exception:
        pass

    if not name:
        try:
            import json
            meta = DATA_DIR.parent / "metadata.db"
            if meta.exists():
                import sqlite3
                conn = sqlite3.connect(str(meta))
                row = conn.execute("SELECT name FROM stocks WHERE code=?", (sym,)).fetchone()
                if row: name = row[0]
                conn.close()
        except Exception:
            pass

    # ── 2. 构建 prompt ──
    prompt = f"""## {sym} {name} 深度尽调

### 价格数据
- 最新价: {latest_price:.2f} | MA20: {ma20:.2f} | MA60: {ma60:.2f}
- 20日涨跌: {pct_20d}% | 60日涨跌: {pct_60d}%
- 90日最高: {max_90d:.2f} (距高点 {from_high}%) | 90日最低: {min_90d:.2f} (距低点 {from_low}%)
- 近5日均量/近60日均量: {vol_ratio}x
- PE: {pe_val} | PB: {pb_val} | 市值: {mkt_cap}
- 概念: {', '.join(concepts) if concepts else '未知'}

请按 5 阶段模式输出尽调报告。"""

    result, source = await _call_llm(prompt, DD_SYSTEM, max_tokens=2500)

    # ── 3. 保存 ──
    try:
        import json
        from datetime import datetime
        from server.utils import DATA_DIR
        dd_dir = DATA_DIR.parent / "data" / "deep_dd"
        dd_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "symbol": sym,
            "name": name,
            "price": latest_price,
            "generated_at": datetime.now().isoformat(),
            "source": source,
            "report": result,
        }
        out_path = dd_dir / f"dd_{sym}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        logger.info(f"DD saved: {out_path}")
    except Exception as e:
        logger.warning(f"DD save failed: {e}")

    return {"symbol": sym, "name": name, "report": result, "source": source}


# ── 策略管理 AI 助手 ──

STRATEGY_CHAT_SYSTEM = """你是三才量化系统的策略编写助手，精通本系统的 IStrategy 策略框架。

⚠️ 本系统不是 Freqtrade！绝对不要用 Freqtrade 的写法。必须严格遵循下面的接口：

【导入】固定这一行，不要用 `from strategy import`：
    from ..interface import IStrategy, Signal, SignalType, BarContext

【类】继承 IStrategy，必须有 name 类属性（决定策略ID为 user_<name>）：
    class MyStrategy(IStrategy):
        name = "my_strategy"

【三个方法（签名必须完全一致）】
1) def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
     只有 df 一个参数（没有 metadata！）。加指标列后 return df。此方法可省略。
2) def populate_entry_signals(self, ctx: BarContext) -> Optional[Signal]:
     参数是单根K线的 ctx（不是 dataframe，没有 metadata）。
     满足买入条件时 `return Signal(type=SignalType.BUY, reason="...", price=ctx.price)`，否则 return None。
3) def populate_exit_signals(self, ctx: BarContext) -> Optional[Signal]:
     满足卖出条件时 return Signal(type=SignalType.SELL, ...)，否则 return None。
   ❌ 不要返回 dataframe，不要设 df['enter_signal']=1 这种 Freqtrade 写法。

【ctx: BarContext 常用字段】
    ctx.price          当前收盘价(float)
    ctx.in_position    是否已持仓(bool)
    ctx.entry_price    持仓成本(float，未持仓为0)
    ctx.bars_held      持仓K线数(int)
    ctx.kdj_k/kdj_d/kdj_j   KDJ值
    ctx.mas            {周期:均线值} 如 ctx.mas.get(20)、ctx.mas.get(34)
    ctx.factor_values  {列名:值} populate_indicators 里加的列的当前值，如 ctx.factor_values.get("ma5")
    ctx.volume_ratio   量比
    ctx.close_arr      截至当前的收盘价数组(list)，可算自定义指标
    ctx.trend          "up"/"down"/"neutral"

⚡ 性能铁律（否则回测会超时卡死）：populate_entry_signals/populate_exit_signals 每根K线都会被调用一次。
    ✅ 指标计算放进 populate_indicators（df 上向量化算一次），信号方法里用 ctx.mas / ctx.factor_values 读当前值。
    ❌ 绝对不要在信号方法里写 `for i in range(len(ctx.close_arr))` 遍历整个历史重算指标——
       那样是 O(N²)，几百根K线就会让回测卡几十秒到几分钟。
    需要某指标当前值就在 populate_indicators 里 `df["x"]=...`，信号方法用 `ctx.factor_values.get("x")` 取。

【完整最小示例】
```python
from typing import Optional
import pandas as pd
from ..interface import IStrategy, Signal, SignalType, BarContext

class MaCrossStrategy(IStrategy):
    name = "ma_cross"

    def populate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ma20"] = df["close"].rolling(20).mean()
        return df

    def populate_entry_signals(self, ctx: BarContext) -> Optional[Signal]:
        if ctx.in_position:
            return None
        ma20 = ctx.mas.get(20, 0)
        if ma20 and ctx.price > ma20:
            return Signal(type=SignalType.BUY, reason="上穿MA20", price=ctx.price)
        return None

    def populate_exit_signals(self, ctx: BarContext) -> Optional[Signal]:
        if not ctx.in_position:
            return None
        if ctx.entry_price > 0 and ctx.price <= ctx.entry_price * 0.95:
            return Signal(type=SignalType.SELL, reason="止损-5%", price=ctx.price)
        return None
```

禁止使用 os / sys / subprocess / socket / 网络请求 等危险模块。

职责：解释/审阅用户策略、按需求改写。规则：
- 改写代码时用一个 ```python 代码块包裹**完整可运行**的代码（能整体替换原文件）；
- 只解释/答疑不改代码时，**不要**输出代码块；
- 回复用中文，先结论后细节。"""


def _extract_code_block(text: str) -> str:
    """从 LLM 回复中提取第一个 ```python 代码块 (无语言标记的 ``` 也支持)."""
    if not text:
        return ""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else ""


class StrategyChatRequest(BaseModel):
    message: str
    context: dict = {}


@router.post("/ai/chat")
async def strategy_chat(req: StrategyChatRequest):
    """策略管理页 AI 助手：解释/审阅/改写策略代码，返回 {reply, code?}."""
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    ctx = req.context or {}
    strategy = str(ctx.get("strategy") or "").strip()
    code = str(ctx.get("code") or "")

    prompt = f"""当前策略：{strategy or '(未命名)'}

当前代码：
```python
{code or '(空 — 用户尚未编写代码)'}
```

用户请求：{message}
"""

    reply, source = await _call_llm(prompt, STRATEGY_CHAT_SYSTEM, max_tokens=2500)

    # 提取代码块并完整校验（语法/接口/Freqtrade 拦截），把结果回传前端引导逐轮修正
    suggested = _extract_code_block(reply)
    validation = None
    if suggested:
        validation = _validate_strategy_code(suggested, strategy or "new_strategy")
        # 纯语法错才不回填(无法作为代码用)；接口/Freqtrade 错仍回填, 供用户看+让 AI 修正
        if any(e.startswith("Syntax error") for e in validation["errors"]):
            suggested = ""

    return {"reply": reply, "code": suggested, "validation": validation, "source": source}

