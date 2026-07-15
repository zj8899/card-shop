"""新闻摘要生成器 — 三级分类 + 飞书汇总卡片.

核心流程:
  1. build_news_digest() — 从 DB 读取事件 → 按三级分类 → 返回分组 dict
  2. build_feishu_digest_card() — 将分组 dict 转为飞书卡片 Markdown

三级分类:
  🏛️ official  — source_level=official_l1 或 event_type 以'政策'开头
  📊 market    — source_level=authority_media_l2（非 official）
  🌐 sentiment — 其余（包括 market_rumor_l3）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── 三级分类映射 ──

OFFICIAL_EVENT_TYPES = frozenset({
    "政策-监管", "政策-利好", "制裁-关税",
})

MARKET_EVENT_TYPES = frozenset({
    "业绩-预增", "业绩-预亏", "减持", "增持", "回购", "分红",
    "重组", "中标-签约", "新股-IPO", "大宗交易", "限售解禁",
    "停复牌", "龙虎榜",
})

# 额外关键词命中 official（source_level 不一定是 official_l1 但内容涉及官方）
OFFICIAL_TEXT_KW = ["证监会", "财政部", "央行", "国务院", "发改委", "统计局",
                    "银保监会", "交易所", "上交所", "深交所", "国家统计局", "工信部"]

CATEGORY_META = {
    "official":  ("🏛️ 官媒/政策", "red"),
    "market":    ("📊 市场/公司", "blue"),
    "sentiment": ("🌐 舆情/社会", "grey"),
}


def _classify_news_category(event: dict) -> str:
    """基于已有字段映射到三级分类, 不修改 DB schema."""
    source_level = event.get("source_level", "")
    event_type = event.get("event_type", "")
    title = (event.get("title") or "").lower()
    content = (event.get("content_text") or "").lower()

    # 1) official_l1 → official
    if source_level == "official_l1":
        return "official"

    # 2) 政策类 event_type → official
    if event_type in OFFICIAL_EVENT_TYPES:
        return "official"

    # 3) 标题/正文含官方关键词 → official
    combined = title + " " + content
    if any(kw in combined for kw in OFFICIAL_TEXT_KW):
        return "official"

    # 4) authority_media_l2 且非官方 → market
    if source_level == "authority_media_l2":
        if event_type in MARKET_EVENT_TYPES or event_type == "未知":
            return "market"
        return "market"

    # 5) 市场类 event_type → market
    if event_type in MARKET_EVENT_TYPES:
        return "market"

    # 6) 其余 → sentiment (market_rumor_l3, 社交来源, 未知)
    return "sentiment"


# ── 摘要构建 ──

def build_news_digest(hours_back: int = 24) -> dict:
    """从 news_events 库读取最近 N 小时的事件，按三级分类分组.

    Returns:
        {
            "official":  [event_dict, ...],
            "market":    [event_dict, ...],
            "sentiment": [event_dict, ...],
            "total": N,
        }
    """
    try:
        from server import news_event_db as nedb
    except Exception as e:
        logger.warning(f"news_event_db unavailable: {e}")
        return {"official": [], "market": [], "sentiment": [], "total": 0}

    try:
        events = nedb.list_recent(limit=200)
    except Exception as e:
        logger.warning(f"list_recent failed: {e}")
        return {"official": [], "market": [], "sentiment": [], "total": 0}

    cutoff = datetime.now() - timedelta(hours=hours_back)
    groups = {"official": [], "market": [], "sentiment": []}

    for e in events:
        # 解析时间
        pub = e.get("published_at", "")
        try:
            pub_dt = datetime.fromisoformat(pub) if pub else datetime.now()
        except (ValueError, TypeError):
            pub_dt = datetime.now()
        if pub_dt < cutoff:
            continue

        cat = _classify_news_category(e)
        groups[cat].append(e)

    groups["total"] = sum(len(v) for v in groups.values())
    return groups


# ── 飞书卡片构建 ──

def build_feishu_digest_card(groups: dict, title: str = "📰 消息汇总") -> str:
    """将三级分类分组构建为飞书卡片 Markdown 正文.

    Returns:
        str — 飞书卡片 elements[0] 的 markdown content
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"**{title}**",
        f"",
    ]

    total = 0
    seen_titles = set()
    for cat_key in ("official", "market", "sentiment"):
        events = groups.get(cat_key, [])
        if not events:
            continue

        cat_label, _ = CATEGORY_META[cat_key]
        filtered = []
        for e in events:
            t = (e.get("title") or "")[:30].strip()
            if t and t in seen_titles:
                continue  # 标题前30字符重复 → 跳过
            if t:
                seen_titles.add(t)
            filtered.append(e)
        lines.append(f"**{cat_label}** ({len(filtered)}条)")
        total += len(filtered)

        for e in filtered[:8]:  # 每组最多8条(去重后)
            e_title = (e.get("title") or "无标题")[:60]
            source = e.get("source_label") or e.get("source_name", "")
            credibility = e.get("credibility", 50) or 50

            # 影响标的
            try:
                syms = json.loads(e.get("stock_symbols_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                syms = []
            sym_str = "、".join(syms[:5]) if syms else "—"

            # 概念/板块
            try:
                concepts = json.loads(e.get("concepts_json", "[]"))
            except (json.JSONDecodeError, TypeError):
                concepts = []
            concept_str = " · ".join(concepts[:3]) if concepts else ""

            # 构建单条摘要
            sentiment = e.get("sentiment_label", "")
            tag = {"乐观":"🟢","偏乐观":"🟢","贪婪":"🔥","中性":"➖",
                   "偏悲观":"🟡","悲观":"🔴","恐慌":"🚨"}.get(sentiment, "")
            cred_str = f" {credibility:.0f}%" if credibility else ""

            extra_parts = []
            if sym_str != "—":
                extra_parts.append(sym_str)
            if concept_str:
                extra_parts.append(concept_str)
            extra = " | ".join(extra_parts)

            lines.append(
                f"- {tag} {e_title} — {source}{cred_str}"
                + (f"  \n  ↳ {extra}" if extra else "")
            )

        lines.append("")

    if total == 0:
        lines.append("> 暂无新消息")

    lines.append(f"---\n> 三才量化 · 消息智能汇总 | 按三级分类: 官媒/政策 · 市场/公司 · 舆情/社会")
    return "\n".join(lines)


# ── 后台采集（silent — 只入库不推送） ──

async def silent_collect_news(limit: int = 10, include_firecrawl: bool = True) -> dict:
    """静默采集新闻入库（不推送），供 monitor 盘中定时调用.

    直接复用 server.routers.news 的 file_news_events 逻辑.
    """
    result = {"filed": 0, "skipped": 0, "sources": []}
    try:
        from server import news_event_db as nedb
        from research.engines.news_analyzer import analyze_news
        from server.services.news_analyzer import classify_source
        from research.engines.news_capital_align import align_news_capital

        # ── 源 1: WSCN ──
        from data_sources.wscn_news import get_a_stock_news
        items = get_a_stock_news(limit=limit * 2)
        result["sources"].append("WSCN")

        # ── 源 2: Firecrawl ──
        fc_items = []
        if include_firecrawl:
            try:
                from data_sources.firecrawl_news import fetch_news, is_enabled
                if is_enabled():
                    fc_items = fetch_news(max_sources=3)
                    result["sources"].append("Firecrawl")
            except Exception as e:
                logger.debug(f"Firecrawl skipped: {e}")

        # 合并
        all_items = list(items or [])
        for fc in fc_items:
            all_items.append({
                "id": "fc_" + str(abs(hash(fc.get("title", ""))) % 10**9),
                "title": fc.get("title", ""),
                "content_text": fc.get("content_text", ""),
                "display_time": datetime.now().isoformat(),
                "symbols": fc.get("symbols", []),
                "tags": fc.get("tags", []),
                "_source_override": fc.get("source", "Firecrawl"),
                "_source_level_override": "authority_media_l2",
                "_source_label_override": "权威媒体二级",
            })

        if not all_items:
            return result

        a_share_kw = ["A股", "上证", "深证", "创业板", "科创板", "涨停", "跌停",
                       "板块", "概念", "个股", "券商", "IPO", "回购", "分红", "业绩",
                       "政策", "证监会", "央行", "增持", "减持", "重组"]

        for item in all_items:
            title = (item.get("title") or "").strip()
            content = (item.get("content_text") or "").strip()
            if not title and content:
                title = content.split("\n")[0][:80]
            if not title:
                result["skipped"] += 1
                continue

            combined = (title + " " + content).lower()
            if not any(kw.lower() in combined for kw in a_share_kw):
                result["skipped"] += 1
                continue

            news_id = str(item.get("id", abs(hash(title)) % 10**9))
            raw_time = item.get("display_time", "")
            try:
                ts = float(raw_time)
                published_at = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                published_at = str(raw_time)[:19] if raw_time else datetime.now().isoformat()

            symbols = []
            for s in item.get("symbols", []) or []:
                if isinstance(s, dict):
                    s = s.get("symbol", "")
                if isinstance(s, str) and len(s) >= 6:
                    import re
                    m = re.search(r"(\d{6})", s)
                    if m:
                        symbols.append(m.group(1))

            source_override = item.get("_source_override", "")
            source_name = source_override or "华尔街见闻"
            raw = {"id": news_id, "title": title, "content": content,
                   "source": source_name, "created_at": published_at,
                   "symbols": symbols, "tags": item.get("tags", [])[:10]}
            structured = analyze_news(raw)

            if item.get("_source_level_override") and item.get("_source_label_override"):
                level = item["_source_level_override"]
                label = item["_source_label_override"]
                rule = f"Firecrawl抓取: {source_name}"
            else:
                level, label, rule = classify_source(title, content)

            cap_type, cap_score_val, cap_phase = "", 0.0, ""
            try:
                al = align_news_capital(structured)
                if al:
                    cap_type = al.alignment_type
                    cap_score_val = al.alignment_strength
            except Exception:
                pass

            baseline = []
            try:
                import pandas as pd
                from server.utils import DATA_DIR
                for sym in structured.stock_symbols[:5]:
                    fp = DATA_DIR / "daily" / f"{sym}.parquet"
                    if fp.exists():
                        df = pd.read_parquet(fp)
                        price = float(df["close"].iloc[-1])
                        baseline.append({"symbol": sym, "name": sym, "price": round(price, 2)})
            except Exception:
                pass

            try:
                nedb.create_event(
                    news_id=news_id, title=title, content_text=content,
                    published_at=published_at,
                    event_type=structured.event_type,
                    event_subtype=structured.event_subtype,
                    event_impact=structured.event_impact,
                    sentiment_label=structured.sentiment_label,
                    sentiment_score=structured.sentiment_score,
                    entities=[{"name": e.name, "type": e.type, "symbol": e.symbol}
                              for e in structured.entities],
                    sectors=structured.sectors, concepts=structured.concepts,
                    stock_symbols=structured.stock_symbols,
                    propagation=structured.propagation_chain,
                    summary_5w1h=structured.summary_5w1h,
                    source_name=source_name, source_level=level,
                    source_label=label, source_rule=rule,
                    credibility=structured.credibility_score,
                    cap_alignment_type=cap_type,
                    cap_alignment_score=cap_score_val,
                    cap_phase=cap_phase,
                    cap_score=cap_score_val * 100 if cap_score_val else 50,
                    baseline_prices=baseline,
                )
                result["filed"] += 1
            except Exception:
                result["skipped"] += 1

    except Exception as e:
        logger.warning(f"silent_collect_news failed: {e}")

    if result["filed"] > 0 or result["skipped"] > 0:
        logger.info(f"Silent news collect: {result['filed']} filed, {result['skipped']} skipped, sources={result['sources']}")
    return result
