"""百度股市通 — 概念板块归属 + 个股资金流向（分钟级 + 20日历史）.

零鉴权，无需 API Key。
"""

import logging

import requests

logger = logging.getLogger(__name__)

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


def baidu_concept_blocks(code: str) -> dict:
    """获取个股概念板块归属（行业 + 概念 + 地域三维分类）.

    Returns:
        {industry: [...], concept: [...], region: [...], concept_tags: [...]}
    """
    url = (
        f"https://finance.pae.baidu.com/api/getrelatedblock"
        f"?code={code}&market=ab"
        f"&typeCode=all&finClientType=pc"
    )
    result = {"industry": [], "concept": [], "region": [], "concept_tags": []}
    try:
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        logger.error(f"Baidu concept blocks API failed for {code}: {e}")
        return result
    except ValueError as e:
        logger.error(f"Baidu concept blocks JSON parse failed for {code}: {e}")
        return result
    if str(d.get("ResultCode", -1)) != "0":
        logger.warning(f"百度PAE错误 (concept blocks {code}): {d}")
        return result

    for block in d.get("Result", []):
        block_type = block.get("type", "")
        for item in block.get("list", []):
            entry = {
                "name": item.get("name", ""),
                "change_pct": item.get("increase", ""),
                "desc": item.get("desc", ""),
            }
            if "行业" in block_type:
                result["industry"].append(entry)
            elif "概念" in block_type:
                result["concept"].append(entry)
                result["concept_tags"].append(entry["name"])
            elif "地域" in block_type:
                result["region"].append(entry)
    return result


def baidu_fund_flow_realtime(code: str, date: str) -> list[dict]:
    """个股资金流向（分钟级）.

    Args:
        code: 6 位代码
        date: YYYYMMDD 紧凑格式
    Returns:
        [{time, mainForce, retail, super, large, price}, ...]
    """
    url = (
        f"https://finance.pae.baidu.com/vapi/v1/fundflow"
        f"?code={code}&market=ab&date={date}"
        f"&finClientType=pc"
    )
    try:
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        logger.error(f"Baidu fund flow realtime API failed for {code}: {e}")
        return []
    except ValueError as e:
        logger.error(f"Baidu fund flow realtime JSON parse failed for {code}: {e}")
        return []
    if str(d.get("ResultCode", -1)) != "0":
        return []

    raw = d.get("Result", {}).get("update_data", "")
    if not raw:
        return []

    rows = []
    for segment in raw.split(";"):
        parts = segment.split(",")
        if len(parts) >= 9:
            rows.append({
                "time": parts[0],
                "mainForce": float(parts[2]) if parts[2] else 0,
                "retail": float(parts[3]) if parts[3] else 0,
                "super": float(parts[4]) if parts[4] else 0,
                "large": float(parts[5]) if parts[5] else 0,
                "price": float(parts[8]) if parts[8] else 0,
            })
    return rows


def baidu_fund_flow_history(code: str, days: int = 20) -> list[dict]:
    """个股资金流向（日级，最近 N 交易日）.

    Returns:
        [{date, close, change_pct, superNetIn, largeNetIn, mediumNetIn, littleNetIn, mainIn}, ...]
    """
    url = (
        f"https://finance.pae.baidu.com/vapi/v1/fundsortlist"
        f"?code={code}&market=ab&pn=0&rn={days}"
        f"&finClientType=pc"
    )
    try:
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        logger.error(f"Baidu fund flow history API failed for {code}: {e}")
        return []
    except ValueError as e:
        logger.error(f"Baidu fund flow history JSON parse failed for {code}: {e}")
        return []
    if str(d.get("ResultCode", -1)) != "0":
        return []

    rows = []
    for item in d.get("Result", {}).get("list", []):
        rows.append({
            "date": item.get("showtime", ""),
            "close": item.get("closepx", ""),
            "change_pct": item.get("ratio", ""),
            "superNetIn": item.get("superNetIn", ""),
            "largeNetIn": item.get("largeNetIn", ""),
            "mediumNetIn": item.get("mediumNetIn", ""),
            "littleNetIn": item.get("littleNetIn", ""),
            "mainIn": item.get("extMainIn", ""),
        })
    return rows
